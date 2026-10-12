"""The session driver (spec 3.11 [T21]): one event-driven process per orchestrating session that owns an
unfinished drive job. It replaces every timer: nothing is installed (no systemd, launchd, Task Scheduler or
cron unit), and the driver never calls a model (seats are launched through the runner, never called).

Lifecycle. `ensure(cfg, session, host_pid)` (from `swarm activate` and from the session's SessionStart or first
hook) starts it when the session is the requester of an unfinished drive ledger of this board and no driver of
the session runs: a detached child (POSIX: its own session; Windows: DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP)
with the runner's sanitized environment; its parameters go over stdin, never argv. Exactly one per session: the
host-private lock `driver-<sha256(session)>.lock` in the supervisor's private directory (flock / LockFileEx). It
exits when the session's host process (the nearest claude/codex ancestor of whatever started it) is gone, when no
unfinished drive ledger of the session remains, or on the kill switch (`supervise.off`).

The loop. It runs one pass (`one_pass`: the driver steps of the session's jobs and the reaping of finished runs,
under the host pass lock), then sleeps until the first of: a board change (Postgres LISTEN/NOTIFY, or an OS
notification on the board's wake file: inotify, kqueue, ReadDirectoryChangesW: swarm.supervisor.wake), the exit of
the session's host process (swarm.supervisor.procwatch), or the next ledger deadline (`job_deadline`: hold
`not_before`, question `until`, ruling due + grace and + deadline, the close deadline, the wall clock, lease
holds, the stall wake, the stalled notice), with a bounded backoff for a launch still pending after a pass. There
is no periodic tick; only without any notification mechanism does it poll (wake.PollSource, 2 s doubling to
60 s), and doctor names that mode.

`swarm drive run [--job J]` is the same loop in the foreground with no session to watch."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import time

from swarm import compat
from swarm.supervisor import wake

log = logging.getLogger("swarm.supervisor.session_driver")

PREFIX = "driver-"
LOCK_TRIES = 5              # a doctor or starter probe holds the lock shared for a moment: try again first
LOCK_RETRY_SECONDS = 0.1
BUSY_MIN, BUSY_MAX = 1.0, 30.0          # the host pass lock is held by another pass: retry on this backoff
PENDING_MIN, PENDING_MAX = 2.0, 60.0    # a launch or a close still pending after a pass: attempt backoff
MIN_GAP = 0.25              # changes within this of the previous pass are one pass (review rr5)


def _sha(text: str) -> str:
    return hashlib.sha256(str(text).encode("utf-8", "surrogatepass")).hexdigest()


def lock_name(session: str) -> str:
    return f"{PREFIX}{_sha(session)}.lock"


def run_lock_name(board_key: str, job: str | None) -> str:
    return f"{PREFIX}run-{_sha(str(board_key) + chr(0) + (job or ''))}.lock"


def state_name(lock: str) -> str:
    return lock[:-len(".lock")] + ".json"


def _log(text: str) -> None:
    from swarm.supervisor.settings import log as sup_log
    sup_log(f"session driver: {text}")


# --- the per-session lock ----------------------------------------------------------------------------

def take_lock(name: str):
    """The exclusive lock `name` in the private directory: its descriptor, or None when another process
    holds it. A probe (held shared for an instant) is waited out for a few tries."""
    from swarm import safefs
    from swarm.supervisor import privfs
    with privfs.dir_fd() as d:
        fd = safefs.open_lock(d, name)
    for i in range(LOCK_TRIES):
        try:
            compat.flock(fd, compat.LOCK_EX | compat.LOCK_NB)
            return fd
        except OSError:
            if i + 1 < LOCK_TRIES:
                time.sleep(LOCK_RETRY_SECONDS)
    os.close(fd)
    return None


def held(name: str) -> bool:
    """Whether a driver holds the lock `name` (a shared, non-blocking probe, released at once). A lock
    file that is missing or unsafe to open is "not held"."""
    from swarm import safefs
    from swarm.supervisor import privfs
    try:
        with privfs.dir_fd(create=False) as d:
            if not safefs.exists(d, name):
                return False
            fd = safefs.open_lock(d, name)
    except Exception:
        return False
    try:
        compat.flock(fd, compat.LOCK_SH | compat.LOCK_NB)
    except OSError:
        return True
    finally:
        os.close(fd)                     # closing releases the probe's shared lock
    return False


def running(session: str) -> dict | None:
    """The live driver of `session`: its state file's content ({} when unreadable), or None."""
    name = lock_name(session)
    if not held(name):
        return None
    from swarm.supervisor.settings import read_private
    try:
        data = json.loads(read_private(state_name(name)) or b"{}")
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}


# --- which jobs ----------------------------------------------------------------------------------------

def _store_key(cfg: dict) -> str:
    from swarm.board.autoinit import store_key
    return store_key(cfg)


def unfinished(board_key: str) -> list:
    """The unfinished drive ledgers of this board. When the enumeration fails (a planted lock), the directory
    is read file by file instead (driver._scan_files), so one bad entry never hides the good ones."""
    from swarm.supervisor import ledger, settings
    if not os.path.lexists(settings.private_dir() / ledger.SUBDIR):
        return []
    try:
        return [x for x in ledger.unfinished(board_key) if isinstance(x, ledger.Ledger)]
    except Exception:
        from swarm.supervisor import driver
        try:
            leds, bad = driver._scan_files(board_key)
        except Exception:
            return None                  # cannot be read now: unknown, never "none left" (review rr6)
        return None if bad and not leds else leds


def session_jobs(board_key: str, session: str | None, job: str | None = None) -> list[str] | None:
    """The jobs a driver drives: the unfinished drive ledgers whose requester is `session` (every one when
    `session` is None: `swarm drive run`), only `job` when given. None when the ledgers cannot be read now."""
    found = unfinished(board_key)
    if found is None:
        return None
    return sorted(led.job for led in found
                  if (session is None or led.requester_session == session) and (job is None or led.job == job))


# --- deadlines -----------------------------------------------------------------------------------------

def _ts(text):
    if not text:
        return None
    try:
        t = dt.datetime.fromisoformat(str(text))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


def _minutes(value) -> float:
    from swarm.supervisor import driver_life
    return driver_life._minutes(value)


def ledger_deadlines(led, flat: dict, sup: dict | None = None) -> list:
    """The deadlines the ledger alone fixes (spec 2.2, 3.2, 3.6, [F5], [G4]). A halted job has none: a person's
    command (which touches the wake file) is what moves it."""
    if led.terminal is not None or led.halted is not None:
        return []
    out = []
    shift = dt.timedelta(seconds=float(led.halted_total_seconds or 0))
    ruling = dt.timedelta(minutes=_minutes(flat.get("ruling_deadline_minutes", 120)))
    due = _ts(led.ruling_due_at)
    if due is not None:
        out += [due + dt.timedelta(minutes=_minutes(flat.get("ruling_grace_minutes", 15))), due + ruling + shift]
    start = _ts(led.activated_at)
    if start is not None:
        wall = dt.timedelta(hours=float(flat.get("max_wall_hours", 24)))
        out += [start + wall, start + wall + ruling + shift]
    intent = led.terminal_intent
    if intent is not None and _ts(intent.get("at")) is not None:
        out.append(_ts(intent["at"]) + dt.timedelta(minutes=_minutes(flat.get("close_deadline_minutes", 60))))
    since = (led.watermarks or {}).get("stalled.since")
    if isinstance(since, (int, float)):
        period = 2 * 60 * float((sup or {}).get("timer_minutes", 5))
        out.append(dt.datetime.fromtimestamp(since + period + 1, dt.timezone.utc))
    return out


def snapshot_deadlines(snap, dcfg: dict, actions: list) -> list:
    """The deadlines the board fixes, through the pure model (no I/O): every hold's `not_before`, each seat's
    question `until`, the stall wake (last activity + stall_minutes while seats are live) and lease holds."""
    from swarm.supervisor import driver_model as dm
    out = [a.not_before for a in actions if a.kind == "hold" and a.not_before is not None]
    c = dm._Ctx(snap, dcfg)
    for name in c.seats:
        until = c.gated(name)
        if until is not None:
            out.append(until)
    last = c.last_activity()
    if c.live and last is not None:
        out.append(last + dt.timedelta(minutes=float(c.cfg["stall_minutes"])))
    for entry in (snap.integrated or {}).values():
        since = _ts((entry or {}).get("lease_held_since"))
        if since is not None and "lease_hold_minutes" in c.cfg:
            out.append(since + dt.timedelta(minutes=float(c.cfg["lease_hold_minutes"])) + dt.timedelta(seconds=1))
    return out


ACTING = ("launch", "stop_seat", "close", "record_unreachable", "rollover", "complete_land")


def job_deadline(board, cfg: dict, sup: dict, job: str, now: dt.datetime):
    """(earliest deadline or None, whether something is still to do now) of one job, read after a pass."""
    from swarm.supervisor import driver, driver_model as dm, driver_snapshot as ds, ledger
    led = ledger.load(_store_key(cfg), job)
    if led is None or led.terminal is not None:
        return None, False
    flat = driver._flat_cfg(cfg, led)
    out = ledger_deadlines(led, flat, sup)
    pending = led.terminal_intent is not None and led.halted is None
    if led.halted is None and led.terminal_intent is None:
        try:
            dcfg = ds.drive_cfg(led.board_key, job, flat)
            snap = ds.read(board, led, job, cfg=dcfg, now=now)
            actions = dm.next_actions(snap, dcfg)
            out += snapshot_deadlines(snap, dcfg, actions)
            pending = pending or any(a.kind in ACTING for a in actions)
        except Exception as exc:          # the pass reports it; the next deadline still holds
            log.info("deadline read of %s failed: %s", job, type(exc).__name__)
    return (min(out) if out else None), pending


# --- one pass ------------------------------------------------------------------------------------------

class PassResult:
    def __init__(self, status: str, deadline=None, pending: bool = False) -> None:
        self.status, self.deadline, self.pending = status, deadline, pending

    def __repr__(self) -> str:
        return f"PassResult({self.status!r}, {self.deadline!r}, pending={self.pending})"


def one_pass(cfg: dict, jobs: list, *, start_runner=None, config_path: str = "", say=None,
             scope_available=None) -> PassResult:
    """One pass over `jobs` under the host pass lock: the driver's steps for each job (the deadlines of every
    ledger first, as `driver.drive` does), the reaping of finished runs, then the next deadline. Status "ok",
    "busy" (another pass holds the lock), "unreachable", "off" (kill switch) or "refused"."""
    from swarm import paths
    from swarm.board import BoardUnavailable
    from swarm.supervisor import command, driver, lost, runner, settings as st
    say = say or (lambda line: _log(str(line)))
    try:
        sup = st.settings(cfg)
    except Exception as exc:
        _log(f"settings: {exc}")
        return PassResult("refused")
    if st.switched_off() is not None:
        return PassResult("off")
    try:
        st.ensure_private_dir(cfg, create=True)
    except Exception as exc:
        _log(f"private dir: {exc}")
        return PassResult("refused")
    refusal = command.exposure_refusal()
    if refusal:
        _log(refusal)
        return PassResult("refused")
    probe = scope_available or runner.scope_available
    scope: list = []

    def scope_ok() -> bool:
        if not scope:
            scope.append(bool(probe()))
        return scope[0]

    lock = command._take_pass_lock()
    if lock is None:
        return PassResult("busy")
    began = time.monotonic()
    try:
        state = st.load_state()
        try:
            with command._open(cfg, False) as board:
                with lost.pass_writes():
                    for job in jobs:
                        driver.run(board, cfg, sup, state, now=board.now(), say=say, start_runner=start_runner,
                                   which=shutil.which, scope_ok=scope_ok,
                                   config_path=config_path or str(paths.config_path()),
                                   deadline=min(began + command.PASS_BUDGET_SECONDS,
                                                time.monotonic() + command.PASS_DRIVE_SECONDS), job=job)
                    runner.reap(cfg, board, deadline=time.monotonic() + command.PASS_REAP_SECONDS)
                now = board.now()
                found, pending = [], False
                for job in jobs:
                    when, more = job_deadline(board, cfg, sup, job, now)
                    pending = pending or more
                    if when is not None:
                        found.append(when)
        except BoardUnavailable:
            return PassResult("unreachable")
        finally:
            st.save_state(state)
    finally:
        os.close(lock)
    return PassResult("ok", min(found) if found else None, pending)


# --- the loop ------------------------------------------------------------------------------------------

class Driver:
    """The event loop. `watch` is the session's host process (procwatch.ProcWatch; NoWatch for `drive run`);
    `pass_fn(jobs) -> PassResult` and `open_board()` are injectable; `notify=False` forces the poll fallback."""

    def __init__(self, cfg: dict, *, session: str | None, job: str | None = None, watch=None,
                 config_path: str = "", pass_fn=None, open_board=None, notify: bool = True,
                 clock=time.time, stats_name: str | None = None, say=None) -> None:
        from swarm.supervisor import procwatch
        self.cfg, self.session, self.job = cfg, session, job
        self.board_key = _store_key(cfg)
        self.watch = watch or procwatch.NoWatch()
        self.config_path = config_path
        self.pass_fn = pass_fn or (lambda jobs: one_pass(cfg, jobs, config_path=config_path, say=say))
        self.open_board = open_board
        self.notify = notify
        self.clock = clock
        self.stats_name = stats_name
        self.hub = wake.Hub()
        self.sources: list = []
        self.mode = "poll"
        self.passes = 0
        self.busy = wake.Backoff(BUSY_MIN, BUSY_MAX)
        self.pending = wake.Backoff(PENDING_MIN, PENDING_MAX)
        self.started = clock()
        self.last_pass = 0.0
        self.exit_reason: str | None = None

    # -- sources
    def _start(self, source, reason: str, name: str) -> None:
        self.sources.append(source)
        wake.run_source(self.hub, source, reason, name)

    def setup(self) -> None:
        listen = False
        if self.notify and self.open_board is not None:
            try:
                board = self.open_board()
                if wake.board_pushes(board):
                    self._start(wake.BoardSource(board), wake.CHANGE, "board-listen")
                    listen = True
                else:
                    board.close()
            except Exception as exc:
                log.info("no board push: %s", type(exc).__name__)
        source = wake.file_source(self.board_key) if self.notify else None
        if source is not None:
            self._start(source, wake.CHANGE, "wake-file")
        else:
            self._start(wake.PollSource(self.board_key), wake.CHANGE, "wake-poll")
        if getattr(self.watch, "kind", "none") != "none":
            self._start(self.watch, wake.EXIT, "session-exit")
        self.mode = "listen" if listen else ("notify" if source is not None else "poll")

    def degrade(self) -> None:
        """A notification source failed: poll the wake file from now on (and say so in the state file)."""
        if any(isinstance(s, wake.PollSource) for s in self.sources):
            return
        _log(f"notification failed; polling ({self.session or 'drive run'})")
        self._start(wake.PollSource(self.board_key), wake.CHANGE, "wake-poll")
        self.mode = "poll"

    def close(self) -> None:
        for s in self.sources:
            try:
                s.close()
            except Exception:
                pass

    # -- state for doctor
    def write_state(self) -> None:
        if not self.stats_name:
            return
        from swarm.supervisor.settings import write_private
        t = os.times()
        data = {"pid": os.getpid(), "session": self.session, "job": self.job, "mode": self.mode,
                "board_key": self.board_key, "host_pid": getattr(self.watch, "pid", None),
                "started_at": self.started, "passes": self.passes, "wakeups": self.hub.wakeups,
                "cpu_seconds": t.user + t.system, "exit": self.exit_reason}
        try:
            write_private(self.stats_name, json.dumps(data, sort_keys=True))
        except Exception:
            pass

    # -- the loop
    def stop_reason(self) -> str | None:
        from swarm.supervisor.settings import switched_off
        if switched_off() is not None:
            return "kill switch"
        if not self.watch.alive():
            return "session gone"
        return None

    def jobs(self) -> list:
        return session_jobs(self.board_key, self.session, self.job)

    def timeout(self, res: PassResult) -> float | None:
        """How long to sleep after a pass: until its deadline, or a backoff while something is pending, the
        pass lock was busy or the board was unreachable; None (until an event) when nothing is due."""
        if res.status == "busy":
            return self.busy.next(False)
        self.busy.next(True)
        if res.status == "unreachable":
            return self.pending.next(False)
        waits = []
        if res.pending:
            waits.append(self.pending.next(False))
        else:
            self.pending.next(True)
        if res.deadline is not None:
            waits.append(max(0.0, (res.deadline - dt.datetime.now(dt.timezone.utc)).total_seconds()) + 0.05)
        return min(waits) if waits else None

    def why_stop(self) -> str | None:
        """Why the loop ends now (kill switch, session gone, positively no unfinished job of the session), or
        None. Ledgers that cannot be read now are not "none left" (review rr6): the loop retries them."""
        why = self.stop_reason()
        if why is None and self.jobs() == []:
            why = "no unfinished drive job"
        return why

    def wait(self, timeout) -> set:
        """Sleep until a reason or the timeout; changes that come within MIN_GAP of the previous pass are
        coalesced into one pass (review rr5: a burst of touches is one pass, latency stays under a second)."""
        reasons = self.hub.wait(timeout)
        if reasons == {wake.CHANGE}:
            left = MIN_GAP - (time.monotonic() - self.last_pass)
            if left > 0:
                reasons |= self.hub.wait(left)
        return reasons

    def run(self) -> str:
        self.setup()
        try:
            while True:
                why = self.why_stop()
                if why is not None:
                    self.exit_reason = why
                    return why
                jobs = self.jobs()
                if jobs is None:                 # the ledgers do not read now: retry, with backoff
                    _log(f"ledgers unreadable ({self.session or 'drive run'}); retrying")
                    reasons = self.wait(self.pending.next(False))
                    if wake.EXIT in reasons:
                        self.exit_reason = "session gone"
                        return self.exit_reason
                    continue
                res = self.pass_fn(jobs)
                self.passes += 1
                self.last_pass = time.monotonic()
                if res.status == "off":
                    self.exit_reason = "kill switch"
                    return self.exit_reason
                self.write_state()
                why = self.why_stop()            # the pass may have ended the last job: no sleep first
                if why is not None:
                    self.exit_reason = why
                    return why
                reasons = self.wait(self.timeout(res))
                if wake.ERROR in reasons:
                    self.degrade()
                if wake.EXIT in reasons:
                    self.exit_reason = "session gone"
                    return self.exit_reason
        finally:
            self.write_state()
            self.close()


# --- starting a driver -----------------------------------------------------------------------------------

# --- what the requester's own activation recorded (review rr1) ------------------------------------------

def record_name(session: str) -> str:
    return f"{PREFIX}{_sha(session)}.env.json"


def record_launch_env(session: str, config_path: str | None) -> None:
    """Record, from the requester's own `swarm activate`, the PATH and config its driver runs with: a driver
    started later by a hook, or by anything else, takes these, never the starter's (review rr1: a seat must not
    give a driver its PATH or config, through which a planted harness binary would be launched)."""
    from swarm import paths
    from swarm.supervisor.settings import write_private
    cfgp = os.path.abspath(os.path.expanduser(str(config_path or os.environ.get("SWARM_CONFIG")
                                                   or paths.config_path())))
    write_private(record_name(session), json.dumps({"path": os.environ.get("PATH", os.defpath), "config": cfgp}))


def launch_env(session: str) -> dict | None:
    """The recorded {"path", "config"} of `session`, or None (no activation by this session recorded one)."""
    from swarm.supervisor.settings import read_private
    try:
        data = json.loads(read_private(record_name(session)) or b"null")
    except ValueError:
        return None
    if isinstance(data, dict) and isinstance(data.get("path"), str) and isinstance(data.get("config"), str):
        return data
    return None


def seat_caller(host_pid: int | None = None) -> str | None:
    """Why the calling process may not start a driver (it is a seat), or None. A seat's environment carries the
    runner's tag (an agent can unset it, so its host process's environment and ancestry are checked too)."""
    from swarm.supervisor import procwatch, runner
    if os.environ.get(runner.RUN_ENV) or os.environ.get("SWARM_RESUME_TOKEN"):
        return "a supervisor-launched seat never starts a session driver"
    if host_pid and procwatch.under_runner(host_pid):
        return "its agent host was launched by the supervisor (a seat), which never starts a session driver"
    return None


def _child_env(cfg: dict, config_path: str | None, path: str | None = None) -> dict:
    """The runner's sanitized environment (runner._allowed_env and [supervise] pass_env), plus where the
    library and the config are."""
    from swarm import paths
    from swarm.supervisor import runner, settings as st
    try:
        pass_env = st.settings(cfg)["pass_env"]
    except Exception:
        pass_env = ()
    env = runner._allowed_env(os.environ, pass_env)
    for k in ("CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_CHILD_SESSION", "CLAUDECODE", "CODEX_SESSION_ID",
              "CODEX_THREAD_ID", "SWARM_HOST", runner.RUN_ENV):
        env.pop(k, None)
    env["PYTHONPATH"] = str(paths.LIB_DIR)
    env.update(paths.pycache_env(os.environ))
    if config_path:
        env["SWARM_CONFIG"] = str(config_path)
    if path is not None:
        env["PATH"] = path                      # the requester's recorded PATH, never the starter's
    for k in ("SWARM_NO_SYSTEMD", "SYSTEMROOT", "SystemRoot", "COMSPEC", "PATHEXT", "WINDIR", "USERPROFILE",
              "APPDATA", "LOCALAPPDATA", "TEMP", "TMP"):
        if k in os.environ and k not in env:
            env[k] = os.environ[k]
    return env


def start(cfg: dict, session: str, host_pid: int, *, config_path: str | None = None,
          popen=subprocess.Popen) -> int | None:
    """Start the detached driver of `session`, watching `host_pid`. Its pid, or None when one already runs or
    the session's process cannot be identified (fails closed: nothing is started for a process it could not
    watch)."""
    from swarm.supervisor import procwatch
    if running(session) is not None:
        return None
    token = procwatch.start_token(host_pid)
    if token is None:
        _log(f"not started: the session's process {host_pid} cannot be identified")
        return None
    rec = launch_env(session)
    if rec is None:
        _log(f"not started for {session}: no launch environment recorded by its activation")
        return None
    payload = json.dumps({"session": session, "pid": int(host_pid), "token": token})
    proc = popen([sys.executable, "-m", "swarm.supervisor.session_driver"],
                 env=_child_env(cfg, rec["config"], rec["path"]),
                 stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True,
                 cwd=os.path.expanduser("~"), **_detach())
    try:
        proc.stdin.write(payload.encode())
    finally:
        proc.stdin.close()
    return proc.pid


def _detach() -> dict:
    if compat.IS_WINDOWS:
        return {"creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
                | getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    return {"start_new_session": True}


def ensure(cfg: dict, session: str | None, *, host_pid: int | None = None, config_path: str | None = None,
           popen=subprocess.Popen, activation: bool = False) -> str:
    """Start the session's driver when it owns an unfinished drive job of this board and none runs. Returns
    what happened ("started", "running", "no job", "no session", "no host process", "refused: ..."). Never
    raises. `activation`: the requester's own `swarm activate`, which records the PATH and config every later
    start of this session's driver uses (record_launch_env); any other start (a hook) uses those, and a seat,
    or a caller whose agent host is a seat, never starts one (review rr1)."""
    try:
        if not session:
            return "no session"
        from swarm.supervisor import procwatch
        pid = host_pid or procwatch.nearest_host()
        why = seat_caller(pid)
        if why:
            _log(f"refused for {session}: {why}")
            return f"refused: {why}"
        if activation:
            record_launch_env(session, config_path)
        if os.environ.get("SWARM_SESSION_DRIVER") == "0":
            return "disabled"
        if held(lock_name(session)):
            return "running"
        if not session_jobs(_store_key(cfg), session):
            return "no job"
        if not pid:
            return "no host process"
        return "started" if start(cfg, session, pid, config_path=config_path, popen=popen) else "running"
    except Exception as exc:
        _log(f"start failed: {type(exc).__name__}: {str(exc)[:200]}")
        return f"failed: {type(exc).__name__}"


def hook_session_start(cfg: dict, stdin) -> int:
    """The SessionStart hook: start the driver of this session if it owns a drive job (a main session only:
    a payload with an agent_id is a subagent). Never fails the hook."""
    try:
        payload = json.loads(stdin.read() or "{}")
        if isinstance(payload, dict) and not payload.get("agent_id"):
            ensure(cfg, payload.get("session_id"))
    except Exception:
        pass
    return 0


# --- the process -------------------------------------------------------------------------------------

def serve(cfg: dict, *, session: str | None, job: str | None, watch, config_path: str, lock: str,
          notify: bool = True, say=None) -> str:
    """Hold `lock` and run the loop; the exit reason ("already running" when the lock is held elsewhere)."""
    from swarm.board import open_board
    fd = take_lock(lock)
    if fd is None:
        return "already running"
    try:
        drv = Driver(cfg, session=session, job=job, watch=watch, config_path=config_path,
                     open_board=lambda: open_board(cfg), notify=notify, stats_name=state_name(lock),
                     say=say)
        why = drv.run()
        _log(f"exit ({session or 'drive run'}): {why}; {drv.passes} passes, {drv.hub.wakeups} wakeups, mode {drv.mode}")
        return why
    finally:
        os.close(fd)


def main(argv=None) -> int:
    """`python -m swarm.supervisor.session_driver`, its parameters as JSON on stdin (what start() writes)."""
    from pathlib import Path
    from swarm.cli import load_config
    from swarm.supervisor import procwatch
    try:
        params = json.loads(sys.stdin.read() or "{}")
        session, pid, token = str(params["session"]), int(params["pid"]), params.get("token")
    except Exception:
        return 2
    rec = launch_env(session)          # however it was started: the requester's recorded PATH and config only
    why = "no launch environment recorded by its activation" if rec is None else seat_caller(pid)
    if why:
        _log(f"driver for {session} not run: {why}")
        return 1
    config_path = rec["config"]
    os.environ["PATH"] = rec["path"]   # what the runners (and the harness binaries they resolve) see
    os.environ["SWARM_CONFIG"] = config_path
    try:
        cfg = load_config(Path(config_path).expanduser())
    except Exception as exc:
        _log(f"config: {exc}")
        return 1
    watch = procwatch.ProcWatch(pid, token)
    serve(cfg, session=session, job=None, watch=watch, config_path=config_path, lock=lock_name(session),
          notify=os.environ.get("SWARM_DRIVER_NOTIFY", "1") != "0")
    return 0


if __name__ == "__main__":
    sys.exit(main())
