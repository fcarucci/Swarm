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
from contextvars import ContextVar

from swarm import gitpin, paths, safefs, textsafe

SUBDIR = "enrolled"
FORMAT = 1
HARNESSES = ("claude", "codex")
KINDS = ("agent", "job", "owner")
RECORD_MAX = 96 * 1024
MAX_BOARD_KEY = 1024
MAX_NAME = 256            # job, agent_key, session_id
MAX_CWD = 4096
GOAL_MAX = 12000          # a drive job's goal text (the owner record keeps it for a recovering pass)
_FILE = re.compile(r"(agent|job|owner)-[0-9a-f]{64}\.json")   # always fullmatch


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
    prompt: str | None = None
    owner: dict | None = None     # owner records only: roots, remotes, credentials, requirements, recipe


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
    if rec.harness not in HARNESSES and not (rec.kind == "owner" and rec.harness is None):
        raise ValueError(f"enrolment: harness must be one of {HARNESSES}, not {rec.harness!r:.40}")
    _text(rec.session_id, "session_id", MAX_NAME, optional=True)
    _text(rec.cwd, "cwd", MAX_CWD)
    if (not os.path.isabs(rec.cwd) or os.path.normpath(rec.cwd) != rec.cwd
            or ".." in rec.cwd.replace(os.sep, "/").split("/")):
        raise ValueError(f"enrolment: cwd must be an absolute, normalised path, not {rec.cwd!r:.80}")
    if (not isinstance(rec.created_at, (int, float)) or isinstance(rec.created_at, bool)
            or not math.isfinite(rec.created_at)):
        raise ValueError("enrolment: created_at must be a number")
    if rec.prompt is not None and (not isinstance(rec.prompt, str) or len(rec.prompt) > 12000):
        raise ValueError("enrolment: prompt must be at most 12000 characters")
    if rec.owner is not None:
        if rec.kind != "owner":
            raise ValueError("enrolment: only an owner record carries owner details")
        _validate_owner(rec.owner)
    return rec


OWNER_FORMAT = 2   # the drive owner details' own format: 2 adds target_branch, config_dirs and the pinned-git checks
OWNER_KEYS = ("format", "roots", "remotes", "credentials", "requirements", "recipe", "review_role",
              "review_gates", "goal", "brief", "target_branch", "config_dirs")


class OwnerRecordInvalid(ValueError):
    """An owner record that exists but is not one this build reads (an older format, a missing or
    unknown field, a refused value): the job is held, never treated as having no record."""


def _validate_owner(o) -> None:
    """The owner details: work roots (absolute, normalised), pinned remotes and credential sources
    (destination -> text), pinned requirements (path -> blob id) and the recipe. Anything else is
    refused, so a planted key can never decide a launch."""
    if not isinstance(o, dict):
        raise ValueError("enrolment: owner details must be a map")
    if o.get("format") != OWNER_FORMAT:
        raise ValueError(f"enrolment: owner details format {o.get('format')!r:.20} is not {OWNER_FORMAT}")
    if set(o) != set(OWNER_KEYS):
        raise ValueError(f"enrolment: owner details must have exactly the keys {OWNER_KEYS}, "
                         f"not {sorted(set(o) ^ set(OWNER_KEYS))[:5]}")
    roots = o["roots"]
    if not isinstance(roots, list) or len(roots) > 64:
        raise ValueError("enrolment: owner roots must be a list of at most 64 paths")
    for r in roots:
        _text(r, "owner root", MAX_CWD)
        if not os.path.isabs(r) or os.path.normpath(r) != r or ".." in r.replace(os.sep, "/").split("/"):
            raise ValueError(f"enrolment: owner root must be an absolute, normalised path, not {r!r:.80}")
    for key in ("remotes", "credentials", "requirements"):
        m = o[key]
        if not isinstance(m, dict) or len(m) > 256:
            raise ValueError(f"enrolment: owner {key} must be a map of at most 256 entries")
        for k, v in m.items():
            _text(k, f"owner {key} key", MAX_CWD)
            _text(v, f"owner {key} value", 4096)
    try:   # a pinned URL is never an option, a transport helper or another transport; a pin is on the allowlist
        for url in o["remotes"].values():
            gitpin.check_url(url)
        for pin in o["credentials"].values():
            gitpin.check_pin(pin)
        gitpin.check_branch(o["target_branch"])
    except gitpin.GitPinError as exc:
        raise ValueError(f"enrolment: owner {exc}") from None
    _text(o["recipe"], "owner recipe", 64)
    for key in ("review_role", "review_gates"):   # null for a recipe without a review stage
        if o[key] is not None:
            _text(o[key], f"owner {key}", 64)
    _text(o["target_branch"], "owner target_branch", 255)   # pinned at activation, never read from config;
    # checked as a branch name above
    cdirs = o["config_dirs"]   # destination -> the GH_CONFIG_DIR of a gh credential helper (absolute)
    if not isinstance(cdirs, dict) or len(cdirs) > 256:
        raise ValueError("enrolment: owner config_dirs must be a map of at most 256 entries")
    for k, v in cdirs.items():
        _text(k, "owner config_dirs key", MAX_CWD)
        _text(v, "owner config_dirs value", MAX_CWD)
        if not os.path.isabs(v) or os.path.normpath(v) != v or v.startswith("-"):
            raise ValueError("enrolment: an owner config dir must be an absolute, normalised path")
    _text(o["goal"], "owner goal", GOAL_MAX)
    _text(o["brief"], "owner brief", MAX_CWD)
    if not os.path.isabs(o["brief"]):
        raise ValueError("enrolment: the owner brief must be an absolute path")


def _parse_checked(data: bytes) -> Record:
    """The record, or ValueError saying why it is not one this build reads."""
    try:
        obj = json.loads(data)
        if not isinstance(obj, dict) or obj.get("v") != FORMAT:
            raise ValueError(f"record format {obj.get('v') if isinstance(obj, dict) else None!r:.20} is not {FORMAT}")
        return _validate(Record(kind=obj["kind"], board_key=obj["board_key"], job=obj["job"],
                                agent_key=obj["agent_key"], harness=obj["harness"],
                                session_id=obj["session_id"], cwd=obj["cwd"],
                                created_at=obj["created_at"], prompt=obj.get("prompt"),
                                owner=obj.get("owner")))
    except KeyError as exc:
        raise ValueError(f"missing field {exc}") from None
    except (TypeError, ArithmeticError) as exc:   # ArithmeticError: 10**400
        raise ValueError(f"bad value ({type(exc).__name__})") from None


def _parse(data: bytes | None) -> Record | None:
    if data is None:
        return None
    try:
        return _parse_checked(data)
    except ValueError:
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


def _store(rec: Record, name: str, *, durable: bool = False) -> Record:
    _validate(rec)
    body = json.dumps({"v": FORMAT, **asdict(rec)}, sort_keys=True)
    lock = coordinator_lock(rec.board_key, rec.job) if rec.kind in ("job", "owner") else contextlib.nullcontext()
    with lock, _dir(create=True) as d:
        safefs.write_atomic(d, name, body, durable=durable)
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
            lock = coordinator_lock('', '') if name.startswith(('job-', 'owner-')) else contextlib.nullcontext()
            with lock:
                safefs.unlink(d, name)
    except FileNotFoundError:
        pass


# --- agent records -----------------------------------------------------------------------------

def write(board_key: str, *, job: str, agent_key: str, harness: str, session_id: str | None,
          cwd: str, now: float | None = None, prompt: str | None = None) -> Record:
    """Record that this host user enrolled `agent_key` of `job` on board `board_key` (replacing
    any earlier record for the pair). ValueError for an invalid field (nothing written); OSError
    (safefs.UnsafePathError) when the host dir is unsafe."""
    rec = Record("agent", board_key, job, agent_key, harness, session_id, cwd,
                 time.time() if now is None else float(now), prompt)
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
                lock = coordinator_lock('', '') if name.startswith(('job-', 'owner-')) else contextlib.nullcontext()
                try:
                    with lock:
                        # Read under the writer lock: a replacement with a fresh creation
                        # time must not be deleted using an older retention snapshot.
                        rec = _parse(safefs.read(d, name, RECORD_MAX))
                        if rec is None and name.startswith("owner-"):
                            continue   # an unreadable owner proof holds its job ([RR1])
                        if rec is None or (now - rec.created_at > max_age and not (keep and keep(rec))):
                            safefs.unlink(d, name)
                            removed += 1
                except BlockingIOError:
                    continue   # a reassignment/writer owns the proof; retain until next sweep
    except FileNotFoundError:
        return 0
    return removed


def write_job_owner(board_key: str, *, job: str, harness: str, session_id: str | None, cwd: str, now=None,
                    roots: list | None = None, remotes: dict | None = None, credentials: dict | None = None,
                    requirements: dict | None = None, recipe: str | None = None, goal: str | None = None,
                    brief: str | None = None, review_role: str | None = None, review_gates: str | None = None,
                    target_branch: str | None = None, config_dirs: dict | None = None):
    """Creation proof, independent of attached coordinators; written only for new jobs. A drive job
    also records its work roots, pinned remotes, credential sources, pinned requirements and recipe
    ([RR1] step 1, [G2], [F10]): roots default to [cwd] and the rest to empty, so a plain activation
    writes the same record it always did plus those keys."""
    owner = None
    if any(v is not None for v in (roots, remotes, credentials, requirements, recipe, goal, brief,
                                   target_branch, config_dirs)):
        kind = recipe or "coding"
        owner = {"roots": [cwd] if roots is None else list(roots), "remotes": dict(remotes or {}),
                 "credentials": dict(credentials or {}), "requirements": dict(requirements or {}),
                 "recipe": kind, "goal": goal or "", "brief": brief or os.path.join(cwd, "LEAD.md"),
                 # the recipe's review stage (spec 3.6, [P1]): the coding recipe reviews with "reviewer";
                 # a recipe without a review stage says null
                 "review_role": review_role if review_role is not None else ("reviewer" if kind == "coding" else None),
                 "review_gates": review_gates if review_gates is not None else ("standard" if kind == "coding" else None),
                 "target_branch": target_branch or "main",   # [G3] the landing target, pinned at activation
                 "config_dirs": dict(config_dirs or {}),     # [G2a] GH_CONFIG_DIR per gh-helper destination
                 "format": OWNER_FORMAT}
    rec = Record("owner", board_key, job, None, harness, session_id, cwd,
                 time.time() if now is None else float(now), owner=owner)
    return _store(rec, f"owner-{_digest('owner', board_key, job)}.json")


def find_job_owner(board_key: str, job: str, *, strict: bool = False):
    """The owner record for (board_key, job), or None. Not `strict`: anything invalid is None (as for
    find). `strict` (drive activation and its recovery): None only when there is no record; a record that
    exists but is unsafe, of another format, or has missing, unknown or refused fields raises
    OwnerRecordInvalid ("owner record format/fields invalid ..."), so the job is held, not orphaned."""
    name = f"owner-{_digest('owner', board_key, job)}.json"
    if not strict:
        rec = _load(name)
        return rec if rec and rec.kind == "owner" and rec.board_key == board_key and rec.job == job else None
    try:
        with _dir(create=False) as d:
            if os.stat in os.supports_dir_fd:
                os.stat(name, dir_fd=d, follow_symlinks=False)   # FileNotFoundError: no record
            data = safefs.read(d, name, RECORD_MAX)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise OwnerRecordInvalid(f"owner record format/fields invalid (unreadable or unsafe: "
                                 f"{type(exc).__name__})") from None
    if data is None:
        if os.stat not in os.supports_dir_fd:
            return None
        raise OwnerRecordInvalid("owner record format/fields invalid (not a regular private file, or too large)")
    try:
        rec = _parse_checked(data)
    except ValueError as exc:
        raise OwnerRecordInvalid(f"owner record format/fields invalid ({exc})") from None
    if rec.kind != "owner" or rec.board_key != board_key or rec.job != job:
        raise OwnerRecordInvalid("owner record format/fields invalid (it is for another key)")
    return rec


def agent_sessions(board_key: str) -> dict:
    """session id -> agent key for the agent records of this board (every job) whose agent IS its own session
    (agent_key == session_id: a launch or other agent running as its own host session). An in-process subagent
    has its own agent id but carries its PARENT's session id: its record says nothing about the parent session,
    which may be a requester's main session, so it is left out (the hook still never grants a subagent: its
    payload has an agent_id). Read only: a missing directory is an empty map; an unsafe or unreadable entry
    is skipped, never followed."""
    out = {}
    try:
        with _dir(create=False) as d:
            for name in sorted(compat.listdir(d)):
                if not name.startswith("agent-") or not _FILE.fullmatch(name):
                    continue
                rec = _parse(safefs.read(d, name, RECORD_MAX))
                if (rec and rec.kind == "agent" and rec.board_key == board_key and rec.session_id
                        and rec.agent_key == rec.session_id):
                    out[rec.session_id] = rec.agent_key
    except FileNotFoundError:
        pass
    return out


def coordinator_records(board_key: str, job: str) -> list[tuple[str, Record]]:
    """Read existing local coordinator proofs strictly; never create missing authority.

    Used by explicit coordinator reassignment. Worker proofs and capability payloads are separate.
    A corrupt or unsafe existing record refuses the operation instead of looking absent.
    """
    names = (job_record_name(board_key, job), f"owner-{_digest('owner', board_key, job)}.json")
    records = []
    try:
        with _dir(create=False) as d:
            for name in names:
                try:
                    if os.stat in os.supports_dir_fd:
                        os.stat(name, dir_fd=d, follow_symlinks=False)
                    data = safefs.read(d, name, RECORD_MAX)
                except FileNotFoundError:
                    continue
                if data is None:
                    raise ValueError('enrolment: coordinator record is unsafe or unreadable')
                rec = _parse_checked(data)
                expected_kind = 'owner' if name.startswith('owner-') else 'job'
                if rec.board_key != board_key or rec.job != job or rec.kind != expected_kind:
                    raise ValueError('enrolment: coordinator record is for another key')
                records.append((name, rec))
    except FileNotFoundError:
        pass
    return records


def reassign_coordinator_records(records: list[tuple[str, Record]], session_id: str) -> None:
    """Update only existing records' session ids; preserve harness, roots, credentials and timestamps."""
    from dataclasses import replace
    for name, rec in records:
        _store(replace(rec, session_id=session_id), name)


_COORDINATOR_LOCKS = ContextVar('swarm_coordinator_locks', default=frozenset())


@contextlib.contextmanager
def coordinator_lock(board_key: str, job: str):
    """Serialize local coordinator proof writers and session operations for this host user.
    Nonblocking acquisition avoids lock inversion with existing board-then-proof writers;
    a busy coordinator operation must be retried, never overwrite another operation.
    Reentrant within one context so existing writer helpers take the same lock safely."""
    name = 'coordinator.lock'
    held = _COORDINATOR_LOCKS.get()
    if name in held:
        yield
        return
    with safefs.dir_fd(paths.host_dir(), create=True, strict_mode=0o700) as d, safefs.locked(d, name, blocking=False):
        token = _COORDINATOR_LOCKS.set(held | {name})
        try:
            yield
        finally:
            _COORDINATOR_LOCKS.reset(token)


def coordinator_command(command):
    """Serialize owner activation/teardown with recovery and coordinator reassignment."""
    import functools
    import sys
    @functools.wraps(command)
    def run(cfg, args):
        from swarm.board.autoinit import store_key
        try:
            with coordinator_lock(store_key(cfg), args.job):
                return command(cfg, args)
        except BlockingIOError:
            print('swarm: another coordinator operation is in progress; retry', file=sys.stderr)
            return 1
    return run


def _routing_stop_name(board_key, job):
    return 'routing-stop-' + _digest(board_key, job) + '.json'


def disable_job_routing(board_key: str, job: str) -> None:
    """Preserve explicit local deactivation intent even if the database is unavailable."""
    rec = find_job(board_key, job)
    if rec is not None:
        _store(rec, _routing_stop_name(board_key, job), durable=True)


def job_routing_disabled(rec: Record) -> bool:
    name = _routing_stop_name(rec.board_key, rec.job)
    with _dir(create=False) as directory:
        if not safefs.exists(directory, name):
            return False
        try:
            data = safefs.read(directory, name, RECORD_MAX)
        except FileNotFoundError:
            return False
        if data is None:
            raise ValueError('routing stop proof is unsafe or unreadable')
        stopped = _parse_checked(data)
    if stopped.kind != 'job' or (stopped.board_key, stopped.job) != (rec.board_key, rec.job):
        raise ValueError('routing stop proof does not match the job')
    return stopped.created_at >= rec.created_at


def stop_job_routing(cfg: dict, job: str) -> None:
    from swarm.board.autoinit import store_key
    disable_job_routing(store_key(cfg), job)
