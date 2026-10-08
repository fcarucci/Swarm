"""Owner side: the final transcript of an agent the supervisor closed, before its replacement's
brief is built (so the supervisor can access the transcripts when restarting lost
agents). Claude subagent: the file under its orchestrator's session (or, for an agent spawned
from an attached session, found by its unique agent id); a replacement: its own root session;
Codex: the rollout named by the thread id. Only this machine+user's agents (transcripts.owns)."""
from __future__ import annotations

import datetime as _dt
import re
from pathlib import Path
from swarm import compat

_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")   # an agent id / session id: no path or glob characters


def transcript_path(js, a, cfg: dict | None = None) -> Path | None:
    """Where a stuck-closed agent's transcript is. Claude sessions are found by UUID only, by
    exact name (hosts.claude.find_session_transcript: no glob, no path): a replacement's own
    session; a subagent's file under its parent session, taken from this host user's enrolment
    record (with `cfg`) first, then the job's session. Codex: the rollout named for the thread."""
    from swarm import hosts, transcripts
    from swarm.hosts import codex
    key = a.agent_key or ""
    if not _KEY.fullmatch(key):
        return None
    if (a.harness or "claude") == "codex":
        return codex.find_rollout(key)
    host = hosts.get("claude")
    if a.resume_of:
        return host.find_session_transcript(key)
    rec = transcripts.enrolled(a, cfg) if cfg else None
    for sid in dict.fromkeys(s for s in ((rec.session_id if rec else None), js.session_id) if s):
        main = host.find_session_transcript(sid)
        p = host.find_agent_transcript(main, key) if main else None
        if p is not None and p.is_file() and host.transcript_ok(p, sid, key):
            return p
    return None


def capture_final(board, cfg: dict, js, a, deadline: float | None = None) -> bool:
    """The final transcript of `a` (one of this host user's agents) now. A capture that fails
    (or runs out of time) is recorded as a pending final (note_pending: the sweeps and the
    supervisor pass retry it) and raised."""
    import time
    from swarm import transcripts
    from swarm.supervisor.settings import log
    if not transcripts.enabled(cfg) or not transcripts.owns(a, cfg):
        return False
    path = transcript_path(js, a, cfg)
    if path is None:
        log(f"transcript of {a.name} ({a.agent_key}) on {js.job} not found; not captured")
        return False
    start = time.monotonic()
    try:
        return transcripts.capture_subagent(board, cfg, js.job, a.agent_key, path, True, a.name,
                                            js.session_id, deadline, harness=a.harness or "claude",
                                            use_mtime=False)
    except Exception as exc:
        note_pending(cfg, js.job, a.agent_key, a.harness or "claude", exc,
                     None if deadline is None else deadline - start)
        raise


def retry_state_path() -> Path:
    from swarm.supervisor.settings import private_dir
    return private_dir() / "supervise-transcript-retries.json"


def _read_retries() -> dict:
    import json
    from swarm.supervisor.settings import read_private
    try:
        data = json.loads(read_private(retry_state_path().name) or b"{}")
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}


def _write_retries(state: dict) -> None:
    import json
    from swarm.supervisor.settings import PrivateDirError, log, write_private
    try:
        write_private(retry_state_path().name, json.dumps(state, sort_keys=True))
    except PrivateDirError as exc:
        log(f"can't record transcript retries: {exc}")
    except OSError as exc:
        log(f"can't record transcript retries in {retry_state_path()}: {exc.strerror or exc}")


# --------------------------------------------------------------------------- pending finals
#
# An agent must not be able to stop its own final archive by making redaction run out
# of time (about 10 MB of adversarial output outlasts a hook's 4 s). A final capture of an owned
# agent that fails -- the SubagentStop hook, auto-close, a stuck close, the capture before a
# restart, a runner's finish, a sweep -- leaves the agent pending (its row missing or not final:
# the board's pending_final_transcripts) and records the failure here (note_pending). The retry
# state (retry_state_path, 0600 in the supervisor's private directory) holds, per
# "job<TAB>agent_key": {"harness", "at": last attempt (epoch s), "missing": the transcript
# wasn't found, "skip": not this host user's (looked at again after SKIP_RECHECK_SECONDS),
# "slow": a failure at a real budget (at least transcripts.SLOW_AFTER_SECONDS): left to the
# supervisor pass, "tries": failures at the full retry budget, "reason"}. It keeps only pending
# agents.
#
# The state is read-modify-written under RETRIES_LOCK so concurrent hooks and the pass never lose
# each other's updates. The lock file is 0200 and locked through a write-only descriptor
# (safefs.open_wlock), so a process that can only read it (a sandbox) can't hold it; and it is
# never waited on for long: another process of this user can still hold it, so it is tried for
# at most RETRIES_LOCK_SECONDS (after one timeout, not waited for at all for CONTENDED_BACKOFF).
# Without it the state is still read (atomically replaced, so a read is whole): the sweeps keep
# their order and leave slow entries alone; the supervisor pass (pass_writes) also writes it back
# (write_atomic), so pending finals still reach their capture-failed mark -- a lost update only
# perturbs an order or a count. Other writers without the lock write nothing. `swarm doctor`
# warns while the lock is held.
#
# - The sweeps (hooks, watch/tail, one-shot CLI commands; finalize_pending_owned and
#   transcripts.finalize_owned) try the pending agents that aren't slow, least recently tried
#   first, at most transcripts.FINALIZE_PER_SWEEP, within the sweep's deadline.
# - The supervisor pass (retry_slow_finals) retries the slow ones of both harnesses in one list,
#   least recently tried first, each with transcripts.FINAL_RETRY_SECONDS (60 s), only while the
#   pass's own budget has that much left and no kill switch is thrown. Throughput: one 60 s retry
#   per pass in practice (PASS_BUDGET_SECONDS 95 leaves room for one), so with the timer every
#   2 minutes a never-finishing agent gets its capture-failed row after ~3 passes, and N of them
#   after ~3N; a retry that succeeds quickly leaves room for the next one.
# - An attempt that fails with the full retry budget (or with no deadline at all) counts as a
#   try; after transcripts.FINAL_RETRY_MAX (3) tries the board marks the capture failed
#   (Board.mark_capture_failed): a stored redacted snapshot is kept and becomes final with the
#   reason; with none, a bodiless marker (transcripts.capture_failed_row). The reason names the
#   transcript file's size. Either way the agent is no longer pending.

RETRIES_LOCK = "supervise-transcript-retries.lock"
RETRIES_LOCK_SECONDS = 1.0     # the longest wait for the lock (cf. hooks.MARKER_LOCK_SECONDS)
CONTENDED_BACKOFF = 60.0       # after a timed-out wait, this process doesn't wait again for this long
SKIP_RECHECK_SECONDS = 3600.0  # a "not ours" entry is looked at again after this long
_contended = [float("-inf")]   # time.monotonic() of this process's last timed-out wait
_pass_writes = [False]          # this process is the supervisor pass: write the state even unlocked


class pass_writes:
    """The supervisor pass's scope: without the lock, the retry state is still written back."""

    def __enter__(self):
        self.before, _pass_writes[0] = _pass_writes[0], True
        return self

    def __exit__(self, *exc):
        _pass_writes[0] = self.before
        return False


def _entry_key(job: str, key: str) -> str:
    return f"{job}\t{key}"


def _num(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _try_lock(fd: int, wait: float) -> bool:
    """A non-blocking exclusive flock on `fd`, tried for up to `wait` seconds."""
    import time
    end = time.monotonic() + wait
    while True:
        try:
            compat.flock(fd, compat.LOCK_EX | compat.LOCK_NB)
            return True
        except BlockingIOError:
            if time.monotonic() >= end:
                return False
            time.sleep(0.02)


class _Retries:
    """The retry state, read and written back (when changed) under RETRIES_LOCK (bounded: see
    above). A private directory that can't be used, or a lock that isn't had in time, gives an
    empty state that is never written (logged); with create=False, a missing directory or state
    file too (quietly)."""

    def __init__(self, create: bool = True):
        self.create = create

    def __enter__(self) -> dict:
        import json
        import os
        import time
        from swarm import safefs
        from swarm.supervisor import privfs
        from swarm.supervisor.settings import PrivateDirError, log
        self.fd = self.lock = None
        self.state, self.before, self.locked = {}, None, False
        try:
            self.fd = privfs.open_dir(create=self.create)
            if not self.create and not privfs.exists(self.fd, retry_state_path().name):
                raise FileNotFoundError(retry_state_path().name)
            self.lock = safefs.open_wlock(self.fd, RETRIES_LOCK)
            backing_off = time.monotonic() - _contended[0] < CONTENDED_BACKOFF
            locked = _try_lock(self.lock, 0.0 if backing_off else RETRIES_LOCK_SECONDS)
            if not locked:
                if not backing_off:
                    log(f"transcript retry state: {RETRIES_LOCK} held by another process for "
                        f"{RETRIES_LOCK_SECONDS:g} s; carrying on without the lock"
                        + (" (the pass still writes the state)" if _pass_writes[0] else
                           " (the state is read, not written)"))
                _contended[0] = time.monotonic()
                os.close(self.lock)
                self.lock = None
            else:
                _contended[0] = float("-inf")
                self.locked = True
            raw = privfs.read(self.fd, retry_state_path().name)
        except (PrivateDirError, OSError) as exc:
            if self.create or not isinstance(exc, FileNotFoundError):
                log(f"can't use the transcript retry state: {getattr(exc, 'strerror', None) or exc}")
            self._close()
            return self.state
        try:
            data = json.loads(raw or b"{}")
        except ValueError:
            data = {}
        self.state = {k: v for k, v in data.items() if isinstance(v, dict)} if isinstance(data, dict) else {}
        if self.lock is not None or _pass_writes[0]:   # else read only: before stays None
            self.before = json.dumps(self.state, sort_keys=True)
        return self.state

    def __exit__(self, exc_type, exc, tb):
        import json
        from swarm.supervisor import privfs
        from swarm.supervisor.settings import log
        try:
            if exc_type is None and self.fd is not None and self.before is not None:
                text = json.dumps(self.state, sort_keys=True)
                if text != self.before:
                    try:
                        privfs.write_atomic(self.fd, retry_state_path().name, text)
                    except OSError as err:
                        log(f"can't record transcript retries in {retry_state_path()}: {err.strerror or err}")
        finally:
            self._close()
        return False

    def _close(self) -> None:
        import os
        for fd in (self.lock, self.fd):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
        self.fd = self.lock = None


def lock_contended() -> bool:
    """Whether another process holds the retry state's lock past RETRIES_LOCK_SECONDS now (for
    doctor). Opens nothing that doesn't exist."""
    import os
    from swarm import safefs
    from swarm.supervisor import privfs
    try:
        with privfs.dir_fd(create=False) as d:
            if not privfs.exists(d, RETRIES_LOCK):
                return False
            fd = safefs.open_wlock(d, RETRIES_LOCK)
            try:
                return not _try_lock(fd, RETRIES_LOCK_SECONDS)
            finally:
                os.close(fd)
    except Exception:
        return False


def _reason(exc: BaseException) -> str:
    from swarm import transcripts
    return "ran out of time" if isinstance(exc, transcripts.OutOfTime) else f"failed ({type(exc).__name__})"


def _count_failure(entry: dict, reason: str, budget: float | None) -> None:
    """One failed attempt with `budget` seconds (None: no deadline): a try when it had the full
    retry budget; slow (left to the supervisor pass) when it had a real one."""
    from swarm import transcripts
    entry["reason"] = reason
    if budget is None or budget >= transcripts.FINAL_RETRY_SECONDS * 0.99:
        entry["tries"] = int(_num(entry.get("tries"))) + 1
        entry["slow"] = True
    elif budget >= transcripts.SLOW_AFTER_SECONDS:
        entry["slow"] = True


def note_pending(cfg: dict, job: str, key: str, harness: str | None, exc: BaseException,
                 budget: float | None) -> None:
    """A final capture of this host user's agent `key` on `job` failed with `exc` after a
    `budget` of seconds (None: no deadline): record it (never raises)."""
    import time
    try:
        with _Retries() as state:
            entry = state.setdefault(_entry_key(job, key), {})
            entry.update(harness=harness or "claude", at=time.time())
            entry.pop("skip", None)
            entry.pop("missing", None)
            _count_failure(entry, _reason(exc), budget)
    except Exception:   # the capture's own failure is what gets reported
        pass


def mark_failed(board, job: str, key: str, name: str | None, session_id: str | None,
                harness: str | None, reason: str, role: str = "subagent") -> str:
    """Board.mark_capture_failed with a bodiless marker for when there is no snapshot."""
    from swarm import transcripts
    if not name:
        state = board.sync_state(key)
        name = state.name if state else key
    marker = transcripts.capture_failed_row(job, key, name, reason, transcripts._host(), session_id,
                                            harness, role)
    return board.mark_capture_failed(job, key, marker.failed, marker)


def _give_up(board, cfg: dict, job: str, key: str, name: str | None, session_id: str | None,
             harness: str, path, reason: str, tries: int) -> str:
    """FINAL_RETRY_MAX tries failed: mark the capture failed (a snapshot is kept)."""
    import os
    from swarm import transcripts
    try:
        size = transcripts.human_size(os.stat(path).st_size)
    except OSError:
        size = "?"
    got = mark_failed(board, job, key, name, session_id, harness,
                      f"{reason}, {tries} tries of {transcripts.FINAL_RETRY_SECONDS:g} s; transcript file {size}")
    transcripts.log(f"transcripts: final capture of {key} (job {job}) given up after {tries} tries "
                    f"({reason}; file {size}): {got}")
    return got


class Source:
    """One harness's pending finals for _finalize: `pending` [(job, agent_key)]; `locate(job,
    key)` -> None (not ours: skipped for SKIP_RECHECK_SECONDS) or (agent name or None, session
    id, transcript path or None: not found); `log_missing(job, key, name)`, called the first
    time a transcript isn't found."""

    def __init__(self, harness: str, pending: list, locate, log_missing=None):
        self.harness, self.pending, self.locate, self.log_missing = harness, pending, locate, log_missing


def finalize_pending(board, cfg: dict, harness: str, pending: list, locate,
                     deadline: float | None = None, retry: bool = False, log_missing=None) -> int:
    """_finalize over one harness's pending finals."""
    return _finalize(board, cfg, [Source(harness, pending, locate, log_missing)], deadline, retry)


def _finalize(board, cfg: dict, sources: list, deadline: float | None = None, retry: bool = False) -> int:
    """Try the final captures of the sources' pending agents (this host user's agents whose row
    is missing or not final), all in one list, least recently tried first, at most
    transcripts.FINALIZE_PER_SWEEP. Without `retry` (the sweeps): the ones not left to the
    supervisor pass, within `deadline`. With `retry` (the pass): only those (slow), each with
    FINAL_RETRY_SECONDS, while `deadline` leaves that much and no kill switch is thrown.
    Returns how many it stored."""
    import time
    from swarm import transcripts
    from swarm.supervisor.settings import off_reason
    wall = time.time()
    retries = _Retries(create=any(src.pending for src in sources))
    with retries as state:   # nothing pending: only prune
        for src in sources:
            live = {_entry_key(j, k) for j, k in src.pending}
            for k in [k for k, v in state.items() if k not in live and v.get("harness", "claude") == src.harness]:
                del state[k]   # no longer pending (stored, rotated out, or no longer ours to try)
        view = {k: dict(v) for k, v in state.items()}
    todo = []
    for src in sources:
        for job, key in src.pending:
            e = view.get(_entry_key(job, key), {})
            if e.get("skip") and wall - _num(e.get("at")) < SKIP_RECHECK_SECONDS:
                continue
            # without the lock the pass can't trust the state to hold every failure (a hook's
            # note may have been lost): it retries every pending final, not only the slow ones
            if bool(e.get("slow")) == retry or (retry and not retries.locked):
                todo.append((_num(e.get("at")), src, job, key))
    todo.sort(key=lambda t: t[0])   # stable: never-tried ones in the board's order (left_at)
    n = tried = 0
    noted: list = []   # (entry key, harness, time, not ours, transcript missing) not yet written

    def flush() -> None:
        """Write the noted tries to the retry state: one locked read and fsynced write for all."""
        if not noted:
            return
        with _Retries() as state:
            for nk, harness, at, skip, missing in noted:
                entry = state.setdefault(nk, {})
                entry["harness"], entry["at"] = harness, at
                if skip:
                    entry["skip"] = True
                else:
                    entry.pop("skip", None)
                    entry["missing"] = missing
        noted.clear()
    for _at, src, job, key in todo:
        if tried >= transcripts.FINALIZE_PER_SWEEP:
            break
        now = time.monotonic()
        if retry:
            if deadline is None or deadline - now < transcripts.FINAL_RETRY_SECONDS or off_reason(cfg):
                break
            cap = now + transcripts.FINAL_RETRY_SECONDS
        else:
            if deadline is not None and now > deadline:
                break
            cap = deadline
        k = _entry_key(job, key)
        found = src.locate(job, key)
        note = (k, src.harness, time.time(), found is None, found is not None and found[2] is None)
        if found is None or found[2] is None:
            noted.append(note)   # nothing is captured: written with the others, in one fsync
            if found is not None:
                tried += 1
                if src.log_missing is not None and not view.get(k, {}).get("missing"):
                    src.log_missing(job, key, found[0])
            continue
        noted.append(note)
        flush()                  # before a capture, which may be slow or killed
        tried += 1
        name, session_id, path = found
        try:
            n += bool(transcripts.capture_subagent(board, cfg, job, key, path, True, name, session_id, cap,
                                                   harness=src.harness, use_mtime=False))
        except Exception as exc:
            budget = None if cap is None else cap - now
            with _Retries() as state:
                entry = state.setdefault(k, {"harness": src.harness})
                _count_failure(entry, _reason(exc), budget)
                tries = int(_num(entry.get("tries")))
            transcripts.log(f"transcripts: final capture of {key} (job {job}) {_reason(exc)}"
                            + ("" if budget is None else f" in {budget:.1f} s")
                            + f" (try {tries} of {transcripts.FINAL_RETRY_MAX})")
            if tries >= transcripts.FINAL_RETRY_MAX:
                try:
                    _give_up(board, cfg, job, key, name, session_id, src.harness, path, _reason(exc), tries)
                except Exception as err:
                    transcripts.log(f"transcripts: capture-failed mark of {key} (job {job}) not stored: "
                                    f"{type(err).__name__}")
                    continue
                with _Retries() as state:
                    state.pop(k, None)
    flush()
    return n


def claude_source(board, cfg: dict) -> Source:
    """The pending finals of this machine+user's Claude agents -- every ended one (stopped,
    stuck-closed, left, its job closed) whose stored row is missing or not final: its stop
    capture failed or ran out of time, the capture at a close failed, or its transcript wasn't
    found yet. Pending rows that aren't this host user's (no local enrolment record:
    transcripts.owns) are noted and skipped. A transcript not found is logged once."""
    import getpass
    from swarm import transcripts
    from swarm.supervisor.settings import log
    since = board.now() - _dt.timedelta(days=float(transcripts.settings(cfg)["retention_days"]))
    pending = board.pending_final_transcripts(transcripts._host(), getpass.getuser(), "claude", since)
    rows: dict = {}   # job -> (job status, {agent_key: agent}), read once per job

    def locate(job, key):
        if job not in rows:
            rows[job] = (board.job_status(job), {a.agent_key: a for a in board.agents(job)})
        js, agents = rows[job]
        a = agents.get(key)
        if js is None or a is None or not transcripts.owns(a, cfg):
            return None
        return a.name, js.session_id, transcript_path(js, a, cfg)

    def log_missing(job, key, name):
        log(f"transcript of {name} ({key}) on {job} not found yet; retried at later sweeps")
    return Source("claude", pending, locate, log_missing)


def finalize_pending_owned(board, cfg: dict, deadline: float | None = None, retry: bool = False) -> int:
    """Owner side: the pending finals of this machine+user's Claude agents (claude_source), from
    the sweeps; Codex ones are transcripts.finalize_owned's. Returns how many it stored. One
    query when there is nothing to do."""
    from swarm import transcripts
    if not transcripts.enabled(cfg):
        return 0
    return _finalize(board, cfg, [claude_source(board, cfg)], deadline, retry)


finalize_stuck_owned = finalize_pending_owned   # its old name, from before it took every ended agent


def retry_slow_finals(board, cfg: dict, deadline: float) -> int:
    """The supervisor pass: retry the pending finals the sweeps left to it (slow), Claude and
    Codex in one list (least recently tried first, so neither harness starves the other), each
    with transcripts.FINAL_RETRY_SECONDS, within `deadline` (the pass's budget)."""
    from swarm import transcripts
    if not transcripts.enabled(cfg):
        return 0
    codex, finish = transcripts.codex_source(board, cfg)
    try:
        return _finalize(board, cfg, [claude_source(board, cfg), codex], deadline, retry=True)
    finally:
        finish()


def slow_pending() -> int:
    """How many pending finals are left to the supervisor pass (slow) now: read only, for doctor."""
    return sum(1 for v in _read_retries().values() if isinstance(v, dict) and v.get("slow") and not v.get("skip"))
