"""Local enrolment records (planted or forged records must not decide a launch).

Board rows are written by agents (sandboxed or not) and, through the shared database role, by
another OS user, so nothing on the board can say which agents this host user runs, where, or with
which harness. The unsandboxed hook records that here instead, in the host-private directory
~/.local/share/swarm/host/enrolled (0700, reached through safefs; not a sandbox writable root):

- an agent record, at SubagentStart and on the first hook call after `swarm activate`, keyed by
  (board_key, agent_key): "this host user enrolled that agent of that job on that board, with this
  harness, session and working directory";
- a job record, when the hook sees a job activated in this session, keyed by (board_key, job): the
  local proof that the orchestrator's job was activated here, and when (the snapshot window).

The supervisor (candidates, workdir, harness), transcripts.owns and the snapshot sweep trust only
these records. Harness, session id and cwd are what the host's hook payload and the hook process
say, never a board row or a marker's contents; the writer validates every field and the reader
validates again (and that the record is for the key asked), so a malformed or planted record is
simply not found.

A record is JSON: {"v": 1, "kind": "agent"|"job", "board_key", "job", "agent_key" (null for a
job), "harness": "claude"|"codex", "session_id" (or null), "cwd" (absolute), "created_at" (epoch
seconds)}. File names are "agent-<sha256>.json" / "job-<sha256>.json" over the NUL-joined key.
board_key is autoinit.store_key(cfg)."""
from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
from swarm import compat
import re
import time
from dataclasses import asdict, dataclass

from swarm import paths, safefs, textsafe

SUBDIR = "enrolled"
FORMAT = 1
HARNESSES = ("claude", "codex")
KINDS = ("agent", "job")
RECORD_MAX = 16 * 1024
MAX_BOARD_KEY = 1024
MAX_NAME = 256            # job, agent_key, session_id
MAX_CWD = 4096
_FILE = re.compile(r"(agent|job)-[0-9a-f]{64}\.json")   # always fullmatch


@dataclass(frozen=True)
class Record:
    kind: str                 # "agent" or "job"
    board_key: str
    job: str
    agent_key: str | None     # None for a job record
    harness: str
    session_id: str | None
    cwd: str
    created_at: float


# --- names and validation ----------------------------------------------------------------------

def _digest(*parts: str) -> str:
    return hashlib.sha256("\0".join(parts).encode("utf-8", "surrogatepass")).hexdigest()


def record_name(board_key: str, agent_key: str) -> str:
    """The file name of the agent record for (board_key, agent_key)."""
    return f"agent-{_digest('agent', board_key, agent_key)}.json"


def job_record_name(board_key: str, job: str) -> str:
    """The file name of the job record for (board_key, job)."""
    return f"job-{_digest('job', board_key, job)}.json"


def _text(value, field: str, limit: int, optional: bool = False):
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value or len(value) > limit or textsafe.has_controls(value):
        raise ValueError(f"enrolment: {field} must be 1-{limit} characters without control "
                         f"characters, not {value!r:.80}")
    return value


def _validate(rec: Record) -> Record:
    if rec.kind not in KINDS:
        raise ValueError(f"enrolment: kind {rec.kind!r}")
    _text(rec.board_key, "board_key", MAX_BOARD_KEY)
    _text(rec.job, "job", MAX_NAME)
    if rec.kind == "agent":
        _text(rec.agent_key, "agent_key", MAX_NAME)
    elif rec.agent_key is not None:
        raise ValueError("enrolment: a job record has no agent_key")
    if rec.harness not in HARNESSES:
        raise ValueError(f"enrolment: harness must be one of {HARNESSES}, not {rec.harness!r:.40}")
    _text(rec.session_id, "session_id", MAX_NAME, optional=True)
    _text(rec.cwd, "cwd", MAX_CWD)
    if (not os.path.isabs(rec.cwd) or os.path.normpath(rec.cwd) != rec.cwd
            or ".." in rec.cwd.split("/")):
        raise ValueError(f"enrolment: cwd must be an absolute, normalised path, not {rec.cwd!r:.80}")
    if (not isinstance(rec.created_at, (int, float)) or isinstance(rec.created_at, bool)
            or not math.isfinite(rec.created_at)):
        raise ValueError("enrolment: created_at must be a number")
    return rec


def _parse(data: bytes | None) -> Record | None:
    if data is None:
        return None
    try:
        obj = json.loads(data)
        if not isinstance(obj, dict) or obj.get("v") != FORMAT:
            return None
        return _validate(Record(kind=obj["kind"], board_key=obj["board_key"], job=obj["job"],
                                agent_key=obj["agent_key"], harness=obj["harness"],
                                session_id=obj["session_id"], cwd=obj["cwd"],
                                created_at=obj["created_at"]))
    except (ValueError, KeyError, TypeError, ArithmeticError):   # ArithmeticError: 10**400
        return None


# --- the directory -----------------------------------------------------------------------------

def enrolled_dir():
    """~/.local/share/swarm/host/enrolled (a Path; access goes through _dir)."""
    return paths.host_dir() / SUBDIR


@contextlib.contextmanager
def _dir(create: bool):
    """A verified descriptor of the enrolled dir: host_dir and it are real 0700 directories of
    this user (safefs). FileNotFoundError when missing and not `create`."""
    base = safefs.open_base(paths.host_dir(), create=create, strict_mode=0o700)
    try:
        fd = safefs.open_sub(base, SUBDIR, create=create, strict_mode=0o700)
    finally:
        os.close(base)
    try:
        yield fd
    finally:
        os.close(fd)


def _store(rec: Record, name: str) -> Record:
    _validate(rec)
    body = json.dumps({"v": FORMAT, **asdict(rec)}, sort_keys=True)
    with _dir(create=True) as d:
        safefs.write_atomic(d, name, body)
    return rec


def _load(name: str) -> Record | None:
    try:
        with _dir(create=False) as d:
            return _parse(safefs.read(d, name, RECORD_MAX))
    except OSError:   # missing, or unsafe: no record (fail closed)
        return None


def _delete(name: str) -> None:
    try:
        with _dir(create=False) as d:
            safefs.unlink(d, name)
    except FileNotFoundError:
        pass


# --- agent records -----------------------------------------------------------------------------

def write(board_key: str, *, job: str, agent_key: str, harness: str, session_id: str | None,
          cwd: str, now: float | None = None) -> Record:
    """Record that this host user enrolled `agent_key` of `job` on board `board_key` (replacing
    any earlier record for the pair). ValueError for an invalid field (nothing written); OSError
    (safefs.UnsafePathError) when the host dir is unsafe."""
    rec = Record("agent", board_key, job, agent_key, harness, session_id, cwd,
                 time.time() if now is None else float(now))
    return _store(rec, record_name(board_key, agent_key) if isinstance(board_key, str)
                  and isinstance(agent_key, str) else "invalid")


def find(board_key: str, agent_key: str) -> Record | None:
    """The agent record for (board_key, agent_key), or None: missing, unreadable, unsafe (a link,
    a FIFO, a loose or symlinked directory), invalid, or for another key. Creates nothing."""
    rec = _load(record_name(board_key, agent_key))
    if rec is None or rec.kind != "agent" or rec.board_key != board_key or rec.agent_key != agent_key:
        return None
    return rec


def owns(board_key: str, agent_key: str, job: str) -> bool:
    """Whether this host user enrolled `agent_key` on this board for `job` (the row's job must
    match the record's)."""
    rec = find(board_key, agent_key)
    return rec is not None and rec.job == job


def remove(board_key: str, agent_key: str) -> None:
    """Drop the agent record (its transcript is final); missing is fine."""
    _delete(record_name(board_key, agent_key))


# --- job records -------------------------------------------------------------------------------

def write_job(board_key: str, *, job: str, harness: str, session_id: str | None, cwd: str,
              now: float | None = None) -> Record:
    """Record that `job` was activated on board `board_key` by this host user's session (replacing
    any earlier record: created_at is the latest activation). Errors as for write()."""
    rec = Record("job", board_key, job, None, harness, session_id, cwd,
                 time.time() if now is None else float(now))
    return _store(rec, job_record_name(board_key, job) if isinstance(board_key, str)
                  and isinstance(job, str) else "invalid")


def find_job(board_key: str, job: str) -> Record | None:
    """The job record for (board_key, job), or None (as for find)."""
    rec = _load(job_record_name(board_key, job))
    if rec is None or rec.kind != "job" or rec.board_key != board_key or rec.job != job:
        return None
    return rec


def remove_job(board_key: str, job: str) -> None:
    _delete(job_record_name(board_key, job))


# --- retention ---------------------------------------------------------------------------------

def prune(max_age: float, *, now: float | None = None, keep=None) -> int:
    """Remove records created more than `max_age` seconds ago (unless `keep(record)` says the
    caller still needs it), and any entry named like a record that isn't a valid one (a planted
    link, FIFO, garbage: the entry is unlinked, never followed). Other names are left alone.
    Returns how many entries were removed; 0 when the directory doesn't exist (nothing is
    created)."""
    now = time.time() if now is None else now
    removed = 0
    try:
        with _dir(create=False) as d:
            for name in sorted(compat.listdir(d)):
                if not _FILE.fullmatch(name):
                    continue
                rec = _parse(safefs.read(d, name, RECORD_MAX))
                if rec is None or (now - rec.created_at > max_age and not (keep and keep(rec))):
                    safefs.unlink(d, name)
                    removed += 1
    except FileNotFoundError:
        return 0
    return removed
