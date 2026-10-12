"""Control-record receipts (spec 4.5): the host's proof that a drive session's CLI command created a
given control record. Stored next to the enrolment records in ~/.local/share/swarm/host/enrolled
(0700, reached through safefs, same validation style as enrolment): the board is writable by
agents, so a record's existence on the board never says the host's driver caused it.

A receipt binds what the driver later relies on:
- the job, board key, incarnation (a random id per ledger, so a deleted and re-created job's
  receipts never validate) and generation;
- the action, attempt and seat the driver launched;
- the record's kind, id and revision (rev), so a rev-2 row is not a rev-1 receipt;
- content_sha: a digest of the record's decision fields (content_sha()). matches() recomputes it
  from the board row, so a row rewritten after the hook's receipt is not matched. Task 5 must use
  find() and then matches() before any decision;
- expires_at: a receipt is not valid after it.
args_sha is the hash of the submitted argv, kept for audit only: it is not a content binding
(stdin, @file bodies, cwd and env are not in it).

File "receipt-<sha256(board_key NUL job NUL kind NUL record_id)>.json": {"v": 1, ...fields}.
Writes are durable (a failed directory fsync raises)."""
from __future__ import annotations

import contextlib
import hashlib
import json
import math
import re
import unicodedata
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from swarm import enrolment, paths, safefs

FORMAT = 1
RECORD_MAX = 64 * 1024
KINDS = ("done", "review", "verdict", "goal_ready", "integrated", "seat_request", "post", "ask",
         "answer", "blocker_resolution", "person", "ack", "withdraw", "land", "grant", "control_request")
# The decision fields of each kind: the values whose digest is content_sha. A kind's row must
# carry every one of them. Task 5 and Task 6 write and read records with these names. A verdict binds
# the artifact it judges (an artifact verdict) or the candidate (a goal or ruling verdict): the other of
# the two is None, so a receipt for one artifact never authorizes a verdict for another.
DECISION = {
    "done": ("artifact", "sha", "group"),
    "review": ("group", "artifact", "outcome"),
    "verdict": ("verdict", "scope", "generation", "candidate", "req", "artifact"),
    "goal_ready": ("generation", "candidate"),
    "integrated": ("group", "sha", "into"),
    "seat_request": ("role", "model", "brief", "workdir"),
    "post": ("to", "body"),
    "ask": ("question", "until", "default"),
    "answer": ("ask_id", "answer"),
    "blocker_resolution": ("blocker_id", "resolution"),
    "person": ("action", "target"),
    "ack": ("request_id",),
    "withdraw": ("request_id",),
    "land": ("destinations",),
    # A grant is the hook's attestation, BEFORE a control command runs, of who submitted it (drivegate.
    # write_grants): record_id is args_sha(submitted argv), seat/action_id say who. Single use (take()).
    "grant": ("argv_sha",),
    # A Codex session's ledger-writing control command, posted as CONTROL-REQUEST <request> <args_sha> ...
    # (drive_control): record_id is the message id; the driver applies it after checking this receipt.
    "control_request": ("request", "args_sha"),
}
LAND_STATES = ("intent", "pushed", "verified")
_SHA = re.compile(r"[0-9a-f]{64}")
_HEX32 = re.compile(r"[0-9a-f]{32}")
_BAD_CATEGORIES = ("Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp")


@dataclass(frozen=True)
class Receipt:
    board_key: str
    job: str
    generation: int
    incarnation: str
    action_id: str
    attempt: int
    seat: str | None
    kind: str
    record_id: str
    rev: int
    content_sha: str
    args_sha: str
    expires_at: float          # epoch seconds; not valid at or after this time
    destinations: dict | None = None   # land receipts: url -> {"state": ..., "sha": ...}


@contextlib.contextmanager
def _dir(kind: str, create: bool):
    """A verified descriptor of the directory receipts live in: the host-private enrolled directory (safefs,
    0700), for every kind, grants included. It is never a Codex writable root: a sandboxed seat can neither
    write nor use up a receipt there (a Codex session's control command goes through a CONTROL-REQUEST)."""
    with enrolment._dir(create=create) as d:
        yield d


def drop(board_key: str, job: str, kind: str, record_id: str) -> bool:
    """Remove the receipt of this record (no checks: the caller decided it is void). True when one was removed."""
    from swarm import compat
    try:
        with _dir(kind, create=False) as d:
            compat.unlink(safefs._entry(receipt_name(board_key, job, kind, record_id)), dir_fd=d)
        return True
    except OSError:
        return False


def args_sha(argv: list) -> str:
    """sha256 of the NUL-joined argv: an audit record of what was run. Not a content binding."""
    return hashlib.sha256("\0".join(argv).encode("utf-8", "surrogatepass")).hexdigest()


def content_sha(kind: str, decision: dict) -> str:
    """sha256 of the canonical JSON of the decision fields of `kind` taken from `decision` (a
    record's decision fields, or a board row that carries them). ValueError for an unknown kind,
    a missing decision field, or a value that is not finite JSON."""
    if kind not in DECISION:
        raise ValueError(f"receipt: no decision fields for kind {kind!r:.40}")
    if not isinstance(decision, dict):
        raise ValueError("receipt: decision must be a mapping")
    missing = [k for k in DECISION[kind] if k not in decision]
    if missing:
        raise ValueError(f"receipt: decision of {kind} lacks {missing}")
    body = {k: decision[k] for k in DECISION[kind]}
    try:
        text = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"receipt: decision of {kind} is not canonical JSON: {exc}") from exc
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def matches(r: Receipt, row) -> bool:
    """Whether the board row `row` is the record `r` was written for: same kind, id and revision,
    and the same decision fields (by content_sha). `row` is a mapping with "kind", "id", "rev"
    and the kind's decision fields."""
    if not isinstance(row, dict) or row.get("kind") != r.kind:
        return False
    rev = row.get("rev")
    if type(rev) is not int or rev != r.rev or str(row.get("id")) != r.record_id:
        return False
    try:
        return content_sha(r.kind, row) == r.content_sha
    except ValueError:
        return False


def receipt_name(board_key: str, job: str, kind: str, record_id: str) -> str:
    d = hashlib.sha256("\0".join((board_key, job, kind, record_id)).encode("utf-8", "surrogatepass"))
    return f"receipt-{d.hexdigest()}.json"


def _text(value, field: str, limit: int = 256, optional: bool = False):
    if value is None and optional:
        return None
    if (not isinstance(value, str) or not value or len(value) > limit
            or any(unicodedata.category(c) in _BAD_CATEGORIES for c in value)):
        raise ValueError(f"receipt: {field} must be 1-{limit} characters without control characters")
    return value


def _int(v, field: str, minimum: int = 0) -> None:
    if not isinstance(v, int) or isinstance(v, bool) or v < minimum:
        raise ValueError(f"receipt: {field} must be an integer >= {minimum}")


def _validate(r: Receipt) -> Receipt:
    _text(r.board_key, "board_key", enrolment.MAX_BOARD_KEY)
    _text(r.job, "job")
    _int(r.generation, "generation", 1)
    if not isinstance(r.incarnation, str) or not _HEX32.fullmatch(r.incarnation):
        raise ValueError("receipt: incarnation must be 32 hex digits")
    _text(r.action_id, "action_id", 1024)
    _int(r.attempt, "attempt")
    _text(r.seat, "seat", optional=True)
    if r.kind not in KINDS:
        raise ValueError(f"receipt: kind {r.kind!r:.40}")
    _text(r.record_id, "record_id")
    _int(r.rev, "rev", 1)
    for name, value in (("content_sha", r.content_sha), ("args_sha", r.args_sha)):
        if not isinstance(value, str) or not _SHA.fullmatch(value):
            raise ValueError(f"receipt: {name} must be 64 hex digits")
    if (isinstance(r.expires_at, bool) or not isinstance(r.expires_at, (int, float))
            or not math.isfinite(r.expires_at)):
        raise ValueError("receipt: expires_at must be a finite number of seconds")
    if r.destinations is not None:
        if r.kind != "land" or not isinstance(r.destinations, dict):
            raise ValueError("receipt: destinations belong to land receipts")
        for url, v in r.destinations.items():
            _text(url, "destination url", 2048)
            if (not isinstance(v, dict) or set(v) != {"state", "sha"} or v["state"] not in LAND_STATES
                    or not isinstance(v["sha"], str) or len(v["sha"]) > 128):
                raise ValueError("receipt: destination entries are {state, sha}")
    return r


def _parse(data: bytes | None) -> Receipt | None:
    if data is None:
        return None
    try:
        obj = json.loads(data)
        names = {f.name for f in fields(Receipt)}
        if not isinstance(obj, dict) or obj.get("v") != FORMAT or set(obj) - {"v"} != names:
            return None
        return _validate(Receipt(**{k: v for k, v in obj.items() if k != "v"}))
    except (ValueError, TypeError, ArithmeticError, RecursionError):
        return None


def write(board_key: str, r: Receipt) -> Path:
    """Store the receipt (replacing one for the same key). ValueError for an invalid field or a
    board_key other than r.board_key (nothing written); OSError when the enrolled directory is
    unsafe or the directory fsync fails."""
    _text(board_key, "board_key", enrolment.MAX_BOARD_KEY)
    _validate(r)
    if r.board_key != board_key:
        raise ValueError("receipt: board_key differs from the board the receipt is written for")
    name = receipt_name(board_key, r.job, r.kind, r.record_id)
    body = json.dumps({"v": FORMAT, **asdict(r)}, sort_keys=True, allow_nan=False)
    with _dir(r.kind, create=True) as d:
        safefs.write_atomic(d, name, body, durable=True)
    return enrolment.enrolled_dir() / name


def find(board_key: str, job: str, kind: str, record_id: str, *, rev: int, incarnation: str,
         generation: int, now: float) -> Receipt | None:
    """The receipt for this record at this revision of this incarnation and generation, if it is
    for this board and not expired at `now`; otherwise None (missing, unreadable, unsafe, invalid,
    or for another key, board, revision, incarnation or generation). The caller still checks
    the board row with matches()."""
    r = find_latest(board_key, job, kind, record_id, incarnation=incarnation, generation=generation, now=now)
    return r if r is not None and r.rev == rev else None


def find_latest(board_key: str, job: str, kind: str, record_id: str, *, incarnation: str,
                generation: int, now: float) -> Receipt | None:
    """find() for whatever revision the stored receipt carries (one file per record: a new revision's
    receipt replaces the old one). For a caller that checks the revision itself."""
    try:
        with _dir(kind, create=False) as d:
            r = _parse(safefs.read(d, receipt_name(board_key, job, kind, record_id), RECORD_MAX))
    except (OSError, TypeError):
        return None
    if r is None:
        return None
    if (r.board_key, r.job, r.kind, r.record_id, r.incarnation, r.generation) != (
            board_key, job, kind, record_id, incarnation, generation):
        return None
    if not r.expires_at > now:
        return None
    return r


GRANT_INCARNATION_NO_LEDGER = "0" * 32   # a grant for `drive repair` of a job whose ledger does not read


def take(board_key: str, job: str, kind: str, record_id: str, *, rev: int, incarnation: str,
         generation: int, now: float) -> Receipt | None:
    """find(), then remove the receipt: a single-use receipt (a grant) serves one command. Whoever loses the
    removal race, or cannot remove it (a sandbox that may read the enrolled directory but not write it),
    gets None."""
    r = find(board_key, job, kind, record_id, rev=rev, incarnation=incarnation, generation=generation, now=now)
    if r is None:
        return None
    from swarm import compat
    try:
        with _dir(kind, create=False) as d:
            compat.unlink(safefs._entry(receipt_name(board_key, job, kind, record_id)), dir_fd=d)
    except OSError:
        return None
    return r
