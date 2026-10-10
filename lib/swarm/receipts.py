"""Control-record receipts (spec 4.5): the host's proof that a drive session's CLI command created
a given control record, with the arguments it ran with bound by hash. Stored next to the
enrolment records in ~/.local/share/swarm/host/enrolled (0700, reached through safefs, same
validation style as enrolment): the board is writable by agents, so a record's existence on the
board never says the host's driver caused it.

File "receipt-<sha256(board_key NUL job NUL kind NUL record_id)>.json": {"v": 1, ...fields}."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from swarm import enrolment, paths, safefs, textsafe

FORMAT = 1
RECORD_MAX = 64 * 1024
KINDS = ("done", "review", "verdict", "goal_ready", "integrated", "seat_request", "post", "ask",
         "answer", "blocker_resolution", "person", "ack", "withdraw", "land")
LAND_STATES = ("intent", "pushed", "verified")
_SHA = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class Receipt:
    job: str
    generation: int
    action_id: str
    attempt: int
    seat: str | None
    kind: str
    record_id: str
    args_sha: str
    destinations: dict | None = None   # land receipts: url -> {"state": ..., "sha": ...}


def args_sha(argv: list) -> str:
    return hashlib.sha256("\0".join(argv).encode("utf-8", "surrogatepass")).hexdigest()


def receipt_name(board_key: str, job: str, kind: str, record_id: str) -> str:
    d = hashlib.sha256("\0".join((board_key, job, kind, record_id)).encode("utf-8", "surrogatepass"))
    return f"receipt-{d.hexdigest()}.json"


def _text(value, field: str, limit: int = 256, optional: bool = False):
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value or len(value) > limit or textsafe.has_controls(value):
        raise ValueError(f"receipt: {field} must be 1-{limit} characters without control characters")
    return value


def _int(v, field: str) -> None:
    if not isinstance(v, int) or isinstance(v, bool) or v < 0:
        raise ValueError(f"receipt: {field} must be a non-negative integer")


def _validate(r: Receipt) -> Receipt:
    _text(r.job, "job")
    _int(r.generation, "generation")
    _text(r.action_id, "action_id", 1024)
    _int(r.attempt, "attempt")
    _text(r.seat, "seat", optional=True)
    if r.kind not in KINDS:
        raise ValueError(f"receipt: kind {r.kind!r:.40}")
    _text(r.record_id, "record_id")
    if not isinstance(r.args_sha, str) or not _SHA.fullmatch(r.args_sha):
        raise ValueError("receipt: args_sha must be 64 hex digits")
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
    """Store the receipt (replacing one for the same key). ValueError for an invalid field
    (nothing written); OSError when the enrolled directory is unsafe."""
    _text(board_key, "board_key", enrolment.MAX_BOARD_KEY)
    _validate(r)
    name = receipt_name(board_key, r.job, r.kind, r.record_id)
    body = json.dumps({"v": FORMAT, **asdict(r)}, sort_keys=True)
    with enrolment._dir(create=True) as d:
        safefs.write_atomic(d, name, body)
    return enrolment.enrolled_dir() / name


def find(board_key: str, job: str, kind: str, record_id: str) -> Receipt | None:
    """The receipt, or None: missing, unreadable, unsafe, invalid or for another key."""
    try:
        with enrolment._dir(create=False) as d:
            r = _parse(safefs.read(d, receipt_name(board_key, job, kind, record_id), RECORD_MAX))
    except (OSError, TypeError):
        return None
    if r is None or (r.job, r.kind, r.record_id) != (job, kind, record_id):
        return None
    return r
