"""The replacement runner: a detached process per replacement, started by `swarm supervise`
(python -m swarm.supervisor.runner --lock-fd N, its run as JSON on stdin). It starts the headless session in its own
process group, binds a Codex thread id to the resume marker, kills the session at the wall
clock, when it never joins the board (enrol deadline), or when its agent row is closed
meanwhile (the sweep found it stuck, or the job was closed), then does the bookkeeping. The run
file (0600) lets `swarm supervise` finish ones whose runner died.

Trust. Run files live in the supervisor's private directory
(settings.ensure_private_dir: 0700, this user's, never a symlink, under no Codex writable root).
The runner never reads its run from a file: start() hands it over the runner's stdin, and the
run file never holds the brief (write_run drops "stdin"). Before reap acts on a run file it is
checked whole (run_problem, _row_problem): a regular file of this user, its restart id its file
name's, every field of the expected type, the unit this restart's scope name, the marker this
restart's resume marker inside the marker directory, the board this one, and the restart row
this job's, replacing this agent, of this host and OS user. A file that fails is logged
("refusing run file") and left alone. A run file of another board (restart ids collide
across boards) is skipped. What the runner reads from the resume marker (sandbox-writable) is
adopted only for an agent row that is this restart's replacement (resume_of), and finish closes
no agent that isn't.

Ownership is a lock, not a pid: r<id>.lock (0600, never replaced; the run file itself is rewritten
by rename, so it can't carry a lock). start() creates and flocks it before the run file exists,
and hands the locked descriptor to the runner (pass_fds), which keeps it for its whole life and
passes it to nobody (the session is started with close_fds). A run file whose lock is held is
live whatever it says; reap finishes a run only while holding its lock itself, so a runner and a
reaper (or two reapers) never finish the same run. There is no window between creating the run
file and owning it.

Boards. Restart ids collide across boards used by one OS user, so
every run file, run lock and output file lives in the board's own subdirectory,
runs/b-<sha256 of the board's store_key>/ (0700, reached through the verified runs directory),
and reap only looks in its own board's. Run files left in runs/ itself by an earlier version are
adopted (moved into the board's subdirectory under both locks) by the reap of the board they name.

Containment. Each session runs in its own transient scope unit when a systemd user manager answers,
`systemd-run --user --scope --unit=swarm-r<id>-<rand>.scope`: a cgroup holds the session and
everything it starts, however it forks, daemonises or clears its environment. The full unit name
(with .scope: systemctl would read a bare name as a .service) is in the run file before the
launch. Stopping it (wall clock, enrol deadline, stuck, leftovers, reap of a dead runner's
session) is `systemctl --user kill` SIGTERM, a grace, SIGKILL, then `stop`; no pid or process
group is signalled. With no systemd user manager (macOS, Windows, minimal Linux; spec 3.11 [T21]) a
drive launch (a run with a launch_id) runs in its own process group (start_new_session), every member
tagged with RUN_ENV: it is stopped by group as below, and its presence (group_presence) is "absent"
only where the members can be listed; any other replacement doesn't launch at all: the restart ends
"refused" (no user systemd manager), and `swarm doctor` fails the scope check. Run files
from before scopes (no unit) are still reaped through their tagged process group, each member
signalled through a pidfd (legacy only).

Launch window. The runner records `unit` before it starts systemd-run and `unit_seen_active`
once systemd reports the scope active. A dead runner's run whose scope was never seen and isn't
loaded is left alone by reap for LAUNCH_GRACE seconds after `launch_epoch` (an orphaned
systemd-run may still create it; its bus calls time out well within that), then finished as
failed; a scope that exists in any state is stopped by name."""
from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
import re
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from swarm import clock, compat
from swarm.supervisor import barrier
from swarm.supervisor.systemd import manager_env as _manager_env

POLL_SECONDS = 5.0
BOARD_EVERY = 30.0
KILL_GRACE = 10.0


LAUNCH_GRACE = 90.0   # seconds an unseen scope of a dead runner may still appear (systemd-run bus timeout 25 s)
NO_MANAGER = "no user systemd manager"
SYSTEMD_RUN = "systemd-run"
SYSTEMCTL = "systemctl"
JAIL_UNAVAILABLE = 125   # launchjail.EXIT_UNAVAILABLE
RUN_ENV = "SWARM_RUN_ID"   # set on every session: marks the processes that belong to one run
TAIL_BYTES = 32 * 1024    # of each of stdout and stderr, kept redacted after a run (r<id>.tail.txt)
TAIL_REDACT_SECONDS = 10.0   # its redaction at most (past it: no tail file)
FINISH_CAPTURE_SECONDS = 10.0   # the replacement's final capture at finish (reap runs in the pass):
                                # past it, a pending final that the sweeps and the pass retry
# Project configuration that appeared in the work dir between the pass and the launch: the
# runner holds the launch (posted once) and checks again every CONFIG_HOLD_POLL seconds, for at
# most CONFIG_HOLD_MAX seconds (None: the replacement's own time limit); approved meanwhile, it
# launches; the job closed or the supervisor switched off, it is cancelled; still unapproved at
# the end, refused. Like the pass's own hold: an approval recovers it.
CONFIG_HOLD_POLL = 30.0
CONFIG_HOLD_MAX: float | None = None


_RUN_NAME = re.compile(r"^r(\d+)\.json$")
_UNIT = re.compile(r"^swarm-r(\d+)-[0-9a-f]+\.scope$")
_TAG = re.compile(r"^r(\d+)-[0-9a-f]+$")
NOT_KEPT = ("stdin",)   # never written to a run file: the brief goes to the runner over a pipe


def _key(board) -> str | None:
    """The board key of `board`: a config (autoinit.store_key), a key already, or None (the legacy
    runs directory itself)."""
    if board is None or isinstance(board, str):
        return board
    from swarm.board.autoinit import store_key
    return store_key(board)


def board_ns(key: str) -> str:
    """The name of board `key`'s subdirectory of the runs directory: a hash, so any key is a
    single safe directory entry."""
    return "b-" + hashlib.sha256(key.encode("utf-8", "surrogatepass")).hexdigest()[:32]


def run_dir(board) -> Path:
    """Where `board`'s (a config or a store key) run, lock and output files live."""
    from swarm.supervisor.settings import runs_dir
    key = _key(board)
    return runs_dir() if key is None else runs_dir() / board_ns(key)


def run_file(restart_id: int, board) -> Path:
    return run_dir(board) / f"r{int(restart_id)}.json"


@contextlib.contextmanager
def _runs_fd(board, create: bool = True):
    """A verified descriptor of `board`'s runs directory (privfs.dir_fd, then its 0700
    subdirectory; None: the runs directory itself): every run file, lock and output file is
    opened relative to it, never through a path."""
    from swarm import safefs
    from swarm.supervisor import privfs
    from swarm.supervisor.settings import PrivateDirError
    key = _key(board)
    with privfs.dir_fd(privfs.RUNS, create) as d:
        if key is None:
            yield d
            return
        try:
            sub = safefs.open_sub(d, board_ns(key), create=create, strict_mode=0o700)
        except safefs.UnsafePathError as exc:
            raise PrivateDirError(str(exc)) from exc
        try:
            yield sub
        finally:
            os.close(sub)


def write_run(run: dict) -> Path:
    """Replace the run file (privfs.write_atomic: a fresh O_EXCL file renamed over it; a planted
    link at its name is replaced, never written through), in its board's directory. The brief is
    never written."""
    from swarm.supervisor import privfs
    p = run_file(run["restart_id"], run["board"])
    with _runs_fd(run["board"]) as d:
        privfs.write_atomic(d, p.name, json.dumps({k: v for k, v in run.items() if k not in NOT_KEPT}))
    return p


def _read_run(name: str, board):
    """A run file's JSON, read through its board's runs directory descriptor without following a
    link (privfs.read: a regular file of this user with one link); None if it isn't, or can't be
    read or parsed."""
    from swarm.supervisor import privfs
    from swarm.supervisor.settings import PrivateDirError
    try:
        with _runs_fd(board, create=False) as d:
            data = privfs.read(d, name)
        return json.loads(data) if data is not None else None
    except (OSError, ValueError, PrivateDirError):
        return None


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _marker_problem(cfg: dict, run: dict) -> str | None:
    """None if run["marker"] is exactly this restart's resume marker, in the marker directory."""
    from swarm.supervisor import markers
    want = markers.resume_marker_path(cfg, run["job"], run["restart_id"])
    got = str(run["marker"])
    if os.path.normpath(got) != got or not os.path.isabs(got) or Path(got).name != want.name or \
            os.path.realpath(Path(got).parent) != os.path.realpath(want.parent):
        return f"marker {got!r} is not this restart's resume marker {str(want)!r}"
    return None


def run_problem(cfg: dict, run, restart_id: int | None = None) -> str | None:
    """Why this run must not be acted on (None: it may). Types and shapes only, plus the marker
    and the board; the restart row is _row_problem's."""
    from swarm.board import RESTART_OUTCOMES
    from swarm.board.autoinit import store_key
    if not isinstance(run, dict):
        return "not a JSON object"
    rid = run.get("restart_id")
    if not _is_int(rid) or rid <= 0:
        return "restart_id is not a positive integer"
    if restart_id is not None and rid != restart_id:
        return f"its restart_id {rid} is not its file name's ({restart_id})"
    board = run.get("board")
    if not isinstance(board, str):
        return "it names no board"
    if board != store_key(cfg):
        return f"its board {board!r} is not this one"
    for k in ("job", "name", "resume_of", "marker"):
        if not isinstance(run.get(k), str) or not run[k]:
            return f"{k} is not a non-empty string"
    if run.get("harness") not in ("claude", "codex"):
        return "harness is not claude or codex"
    for k in ("agent_key", "session_id", "post_pending", "refused_reason", "config", "child_start",
              "runner_start", "cwd", "stdin", "child_token"):
        if run.get(k) is not None and not isinstance(run[k], str):
            return f"{k} is not a string"
    if run.get("outcome") is not None and run["outcome"] not in RESTART_OUTCOMES:
        return "outcome is not a restart outcome"
    for k in ("limit_seconds", "enrol_seconds"):
        if not _is_num(run.get(k)) or run[k] < 0:
            return f"{k} is not a number"
    for k in ("started_epoch", "launch_epoch"):
        if run.get(k) is not None and not _is_num(run[k]):
            return f"{k} is not a number"
    for k in ("child_pid", "child_pgid", "runner_pid"):
        if run.get(k) is not None and (not _is_int(run[k]) or run[k] <= 1):
            return f"{k} is not a process id"
    for k in ("exited", "board_done", "posted", "unit_seen_active"):
        if run.get(k) is not None and not isinstance(run[k], bool):
            return f"{k} is not true or false"
    argv = run.get("argv")
    if argv is not None and not (isinstance(argv, list) and all(isinstance(a, str) for a in argv)):
        return "argv is not a list of strings"
    unit = run.get("unit")
    if unit is not None:
        m = _UNIT.match(unit) if isinstance(unit, str) else None
        if not m or int(m.group(1)) != rid:
            return f"unit {unit!r} is not this restart's scope (swarm-r{rid}-<hex>.scope)"
    if run.get("contained") not in (None, "group", "job"):
        return "contained is not group or job"
    if run.get("slice") is not None and not (isinstance(run["slice"], str) and _SLICE.fullmatch(run["slice"])):
        return "slice is not a systemd slice name"
    tag = run.get("run_tag")
    if tag is not None:
        m = _TAG.match(tag) if isinstance(tag, str) else None
        if not m or int(m.group(1)) != rid:
            return "run_tag is not this restart's"
    return _marker_problem(cfg, run)


def _row_problem(board, run: dict) -> str | None:
    """None if the board's restart row run["restart_id"] is this run's: its job, replacing
    run["resume_of"], started by this host and OS user."""
    import getpass
    row = next((r for r in board.restarts(job=run["job"]) if r.id == run["restart_id"]), None)
    if row is None:
        return f"no restart row {run['restart_id']} on job {run['job']!r}"
    if row.old_agent_key != run["resume_of"]:
        return f"restart row {row.id} replaces another agent"
    if row.host != compat.node() or row.os_user != getpass.getuser():
        return f"restart row {row.id} belongs to another host or OS user"
    return None


def _refuse_file(p: Path, why: str) -> None:
    from swarm.supervisor.settings import log
    log(f"refusing run file {p.name}: {why}; nothing done, the file is left in place")


def has_run(cfg: dict, restart_id: int) -> bool:
    """Whether a run file for restart_id exists that isn't another board's: in this
    board's directory, or a legacy one in the runs directory itself not yet adopted. An
    unreadable one counts: whatever it is, its restart is left alone."""
    from swarm.board.autoinit import store_key
    here = store_key(cfg)
    for key in (here, None):
        p = run_file(restart_id, key)
        if not os.path.lexists(p):
            continue
        r = _read_run(p.name, key)
        if not (isinstance(r, dict) and isinstance(r.get("board"), str) and r["board"] != here):
            return True
    return False


def lock_file(restart_id: int, board) -> Path:
    return run_dir(board) / f"r{int(restart_id)}.lock"


def own(restart_id: int, board) -> int | None:
    """Take restart_id's run lock of `board` (exclusive, non-blocking): the locked descriptor, or
    None when a runner (or a reaper) holds it."""
    from swarm.supervisor import privfs
    with _runs_fd(board) as d:
        try:
            fd = privfs.lock(d, lock_file(restart_id, board).name)
        except PermissionError:   # a planted link: nobody owns this run through it
            from swarm.supervisor.settings import log
            log(f"refusing run lock r{int(restart_id)}.lock: not a private regular file (a planted link?)")
            return None
    try:
        compat.flock(fd, compat.LOCK_EX | compat.LOCK_NB)
        return fd
    except OSError:
        os.close(fd)
        return None


def _held(restart_id: int, board) -> bool:
    """Whether a runner (or a reaper) holds restart_id's run lock of `board`. Read-only: creates
    nothing (a missing lock file is a lock nobody holds). The caps count running replacements
    from the board's open restart rows instead (every OS user's, runner alive or not)."""
    from swarm.supervisor import privfs
    try:
        with _runs_fd(board, create=False) as d:
            fd = privfs.open_existing(d, lock_file(restart_id, board).name, os.O_RDONLY)
    except FileNotFoundError:
        return False
    except Exception:
        return True    # can't tell: count it (the cap errs on the safe side)
    try:
        compat.flock(fd, compat.LOCK_EX | compat.LOCK_NB)
    except OSError:
        return True
    finally:
        os.close(fd)   # closing releases a lock we took
    return False


def _proc_start(pid) -> str | None:
    """The kernel start time of `pid` (/proc/<pid>/stat field 22), None when unknown. With the pid
    it identifies a process: a recycled pid has another start time."""
    try:
        stat = Path(f"/proc/{int(pid)}/stat").read_text(encoding="utf-8")
        return stat.rsplit(")", 1)[1].split()[19]
    except (OSError, ValueError, TypeError, IndexError):
        return None


def _group_alive(pgid: int) -> bool:
    """Any process in process group `pgid` (only to tell apart "gone" and "someone else's" in the log)."""
    try:
        os.killpg(int(pgid), 0)
        return True
    except PermissionError:
        return True
    except (ProcessLookupError, OSError, TypeError, ValueError):
        return False


def _ps_members(pgid: int, want: str) -> list[int] | None:
    """Without /proc (macOS, BSD): the group's live members from `ps -A -o pid=,pgid=,stat=`, each kept only
    when `ps -E` (its environment, readable for this user's own processes) carries `want`. None when ps
    cannot be run or read (the caller then knows nothing)."""
    try:
        out = subprocess.run([compat.ps_path(), "-A", "-o", "pid=,pgid=,stat="], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    found = []
    for line in out.stdout.splitlines():
        parts = line.split()
        if len(parts) < 3 or not parts[0].isdigit() or not parts[1].isdigit():
            continue
        if int(parts[1]) != pgid or parts[2].startswith("Z"):
            continue
        try:
            env = subprocess.run([compat.ps_path(), "-E", "-ww", "-p", parts[0], "-o", "command="], capture_output=True,
                                 text=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            return None
        if want in env.stdout.split():
            found.append(int(parts[0]))
    return found


def _list_members(pgid, tag: str | None, since, env: str = RUN_ENV) -> list[int] | None:
    """_members, or None when the members cannot be listed here (no /proc and no usable ps)."""
    if not tag or not pgid:
        return []
    if not os.path.isdir("/proc/self"):
        try:
            return _ps_members(int(pgid), f"{env}={tag}")
        except (TypeError, ValueError):
            return []
    return _members(pgid, tag, since, env)


def _members(pgid, tag: str | None, since, env: str = RUN_ENV) -> list[int]:
    """The processes of group `pgid` that belong to this run: their environment carries
    <env>=<tag> (RUN_ENV, set on the session, inherited by what it starts; swarm.bg uses its own
    variable) and they started no earlier than the session's leader (`since`, /proc start
    ticks). A group id reused by an unrelated group has no such member. No tag: none (nothing is
    ever signalled blind). Without /proc, `ps` lists them (_ps_members; no start-time check there)."""
    if not tag or not pgid:
        return []
    if not os.path.isdir("/proc/self"):
        try:
            return _ps_members(int(pgid), f"{env}={tag}") or []
        except (TypeError, ValueError):
            return []
    want = f"{env}={tag}".encode()
    try:
        since = int(since or 0)
        pgid = int(pgid)
        pids = [int(d) for d in os.listdir("/proc") if d.isdigit()]
    except (OSError, ValueError, TypeError):
        return []
    out = []
    for pid in pids:
        try:
            fields = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").rsplit(")", 1)[1].split()
            if int(fields[2]) != pgid or fields[0] == "Z" or int(fields[19]) < since:
                continue
            if want in Path(f"/proc/{pid}/environ").read_bytes().split(b"\0"):
                out.append(pid)
        except (OSError, ValueError, IndexError):
            continue
    return out


def _leader_held(proc) -> bool:
    """The session leader is our child and not yet waited for: its pid, and so its group id, can't
    be reused, and signalling the whole group is safe."""
    return proc is not None and proc.returncode is None


def _signal_run(pgid: int, sig, proc, tag: str | None, since) -> None:
    if _leader_held(proc):
        try:
            os.killpg(int(pgid), sig)
        except (ProcessLookupError, PermissionError, OSError):
            pass
        return
    members = _members(pgid, tag, since)
    if members and not os.path.isdir("/proc/self") and not hasattr(os, "pidfd_open"):
        # no /proc and no pidfds (macOS): the group whose members were just seen carrying this run's tag
        try:
            os.killpg(int(pgid), sig)
        except (ProcessLookupError, PermissionError, OSError):
            pass
        return
    for pid in members:   # only this run's own processes, one by one
        _signal_member(pid, pgid, tag, since, sig)


def _signal_member(pid: int, pgid, tag, since, sig, env: str = RUN_ENV) -> bool:
    """Signal `pid` through a pidfd opened first, then checked: the pidfd pins the process, so if
    the pid was recycled since _members saw it, the check reads the new process (and refuses it),
    and the signal can only reach the process that passed the check. No pidfd support: nothing.
    Whether the signal was sent."""
    if not (hasattr(os, "pidfd_open") and hasattr(signal, "pidfd_send_signal")):
        return False
    try:
        fd = os.pidfd_open(pid)
    except OSError:
        return False
    try:
        if pid in _members(pgid, tag, since, env):
            signal.pidfd_send_signal(fd, sig)
            return True
    except OSError:
        pass
    finally:
        os.close(fd)
    return False


def _stop_group(pgid: int, proc=None, grace: float | None = None, *, tag: str | None = None,
                since=None) -> None:
    """SIGTERM the session's processes, wait up to `grace` (KILL_GRACE) seconds for all of them
    (not just the leader) to be gone, then SIGKILL whatever is left. While the leader is our
    unwaited child the group is signalled whole; after that (and always in reap) only the
    members that carry this run's tag (_members): a reused group id is never signalled."""
    grace = KILL_GRACE if grace is None else grace
    _signal_run(pgid, signal.SIGTERM, proc, tag, since)
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        if proc is not None:
            proc.poll()
        if not _leader_held(proc) and not _members(pgid, tag, since):
            return
        time.sleep(0.05)
    if proc is not None:
        proc.poll()
    _signal_run(pgid, signal.SIGKILL, proc, tag, since)
    if proc is not None:
        try:
            proc.wait(5)
        except subprocess.TimeoutExpired:
            pass


# What a replacement (and systemd-run/systemctl) inherits from the
# supervisor's environment. Never PGPASSWORD, cloud keys or tokens: only what a session needs to
# find its home, config and user manager, plus [supervise] pass_env, named by the user.
ENV_ALLOW = ("PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LANGUAGE", "TERM", "TZ", "TMPDIR",
             "XDG_RUNTIME_DIR", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME",
             "DBUS_SESSION_BUS_ADDRESS", "SWARM_CONFIG", "SWARM_VENV", "CLAUDE_CONFIG_DIR", "CODEX_HOME")
# Windows: what any process needs to start at all there (no secrets: system paths and the user's folders)
ENV_ALLOW_WINDOWS = ("SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "PATHEXT", "USERPROFILE", "USERNAME",
                     "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "PROGRAMFILES", "PROGRAMFILES(X86)",
                     "COMMONPROGRAMFILES", "TEMP", "TMP", "HOMEDRIVE", "HOMEPATH", "NUMBER_OF_PROCESSORS",
                     "PROCESSOR_ARCHITECTURE", "OS")


def _allowed_env(src, pass_env=()) -> dict:
    """The allowlisted part of `src`: ENV_ALLOW and LC_*, never PG* or a secret-looking name
    (the redactor's key pattern), plus the names in pass_env (the user's explicit opt-in)."""
    from swarm.transcripts import _KEY_FULL
    out = {}
    for k, v in src.items():
        if k in pass_env:
            out[k] = v
        elif (k in ENV_ALLOW or k.startswith("LC_") or (compat.IS_WINDOWS and k.upper() in ENV_ALLOW_WINDOWS)) \
                and not k.startswith("PG") and not _KEY_FULL.fullmatch(k):
            out[k] = v
    return out


SYSTEMCTL_TIMEOUT = 15.0   # a systemctl call's own cap (unit_presence never waits past its deadline)
CGROUP_ROOT = "/sys/fs/cgroup"   # cgroup v2 mount; tests point it at a fake tree
_SLICE = re.compile(r"^[a-z0-9][a-z0-9@._-]{0,127}\.slice$")


def _systemctl(*args: str, timeout: float = SYSTEMCTL_TIMEOUT) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run([SYSTEMCTL, "--user", *args], env=_manager_env(), capture_output=True,
                              text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None


def scope_available() -> bool:
    """A systemd user manager answers, so sessions can be put in transient scopes. Never with
    SWARM_NO_SYSTEMD=1 (the tests)."""
    if os.environ.get("SWARM_NO_SYSTEMD") == "1":
        return False
    r = _systemctl("is-system-running")
    return r is not None and r.stdout.strip() in ("running", "degraded", "starting", "initializing")


def _unit_state(unit: str) -> tuple[str, str]:
    """(LoadState, ActiveState) of a unit; ("", "") when systemctl can't be asked. A collected
    scope (and one not created yet) is ("not-found", "inactive")."""
    r = _systemctl("show", "-p", "ActiveState,LoadState", unit)
    if r is None or r.returncode != 0:
        return "", ""
    kv = dict(line.split("=", 1) for line in r.stdout.splitlines() if "=" in line)
    return kv.get("LoadState", ""), kv.get("ActiveState", "")


def _unit_exists(state: tuple[str, str]) -> bool:
    load, active = state
    return load not in ("not-found", "") or active not in ("inactive", "")


def _unit_active(unit: str) -> bool:
    return _unit_state(unit)[1] in ("active", "activating", "deactivating", "reloading")


def _unit_slice(unit: str) -> str | None:
    """The slice systemd put the unit in (`systemctl show -p Slice`), None when it can't say."""
    r = _systemctl("show", "-p", "Slice", unit)
    if r is None or r.returncode != 0:
        return None
    for line in r.stdout.splitlines():
        if line.startswith("Slice="):
            value = line.split("=", 1)[1].strip()
            return value if _SLICE.fullmatch(value) else None
    return None


def _cgroup_presence(unit: str, slice_name: str) -> str:
    """[F16] The bus-free check, used only when systemctl can't answer, and only with the slice the
    run recorded at launch (never a guessed default). Only on cgroup v2, with the user manager's
    slice tree readable and that slice present: the scope's directory missing is "absent"; a
    present scope is "present" when its cgroup.events says populated 1 (this covers processes in
    child cgroups too), "absent" when populated 0. Any other layout (cgroup v1, a missing slice,
    an unreadable or unexpected events file) is "unknown"."""
    if not (_UNIT.fullmatch(unit) and _SLICE.fullmatch(slice_name) and hasattr(os, "getuid")):
        return "unknown"
    root = CGROUP_ROOT
    if not os.access(os.path.join(root, "cgroup.controllers"), os.R_OK):   # the cgroup v2 root
        return "unknown"
    uid = os.getuid()
    parent = os.path.join(root, "user.slice", f"user-{uid}.slice", f"user@{uid}.service")
    if not os.access(parent, os.R_OK):
        return "unknown"
    slice_dir = os.path.join(parent, slice_name)
    if not os.access(slice_dir, os.R_OK):
        return "unknown"
    scope = os.path.join(slice_dir, unit)
    if not os.path.isdir(scope):
        return "absent"
    try:
        with open(os.path.join(scope, "cgroup.events"), encoding="ascii", errors="replace") as f:
            populated = _populated(f.read())
    except OSError:
        return "unknown"
    if populated == "1":
        return "present"
    if populated == "0":
        return "absent"
    return "unknown"


def _populated(text: str) -> str | None:
    """The value of the `populated` key of a cgroup.events file ("0" or "1"), read as key/value
    lines (the file also has `frozen N`, and newer kernels add more). None unless exactly one
    `populated` line holds 0 or 1."""
    vals = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0] == "populated":
            vals.append(parts[1])
    return vals[0] if len(vals) == 1 and vals[0] in ("0", "1") else None


def unit_presence(unit: str, slice_name: str | None = None, deadline: float | None = None) -> str:
    """"present" | "absent" | "unknown" for a session's scope. systemctl answers first: "not-found"
    is absent, any loaded unit present. When systemctl can't answer, the cgroup check runs, and only
    with `slice_name`: the slice the run recorded at launch (run["slice"]). None, or a name that is
    not a slice name, is "unknown": the fallback never guesses a slice. `deadline` (a clock.monotonic
    value) caps every systemctl wait to the time left; with none left, or none left after a slow
    systemctl, it is "unknown" and the caller holds (asks again next pass)."""
    timeout = SYSTEMCTL_TIMEOUT
    if deadline is not None:
        left = float(deadline) - clock.monotonic()
        if left <= 0:
            return "unknown"
        timeout = min(SYSTEMCTL_TIMEOUT, left)
    r = _systemctl("show", "-p", "LoadState,ActiveState", unit, timeout=timeout)
    if r is not None and r.returncode == 0:
        kv = dict(line.split("=", 1) for line in r.stdout.splitlines() if "=" in line)
        load = kv.get("LoadState", "")
        if load == "not-found":
            return "absent"
        if load:
            return "present"
    if deadline is not None and clock.monotonic() >= float(deadline):
        return "unknown"   # the systemctl wait used up the time: no cgroup read past the deadline
    if not isinstance(slice_name, str):
        return "unknown"
    return _cgroup_presence(unit, slice_name)


# -- usage and failure classes (the output the runner already captured) ----------------------

@dataclass(frozen=True)
class Usage:
    cost_usd: float | None   # Claude's total_cost_usd; None for Codex (the driver prices its tokens)
    tokens_in: int
    tokens_out: int


BINARY_FAILURE = "could not start the session:"   # the runner's own line when the binary can't run
_BINARY_LINE = re.compile(r"^\s*(FileNotFoundError|PermissionError)\b", re.MULTILINE)
_PROVIDER = re.compile(r"overload|rate[ _-]?limit|authentication|permission_error|api_error|unauthori"
                       r"|credit balance|quota", re.IGNORECASE)
_PROVIDER_STATUS = {401, 403, 408, 429}


def _json_objects(tail: str):
    """Every JSON object in `tail`, in order: whole lines first (the tail may start mid-line, and
    a broken line is skipped), else the whole text (a pretty-printed object)."""
    found = []
    for line in tail.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if isinstance(obj, dict):
                found.append(obj)
    if not found:
        try:
            obj = json.loads(tail)
        except ValueError:
            return found
        if isinstance(obj, dict):
            found.append(obj)
    return found


def _claude_result(tail: str) -> dict | None:
    """The last `type: result` object of Claude's `--output-format json` output, else None."""
    res = [o for o in _json_objects(tail) if o.get("type") == "result"]
    return res[-1] if res else None


def _number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _count(v) -> int:
    return int(v) if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else 0


def parse_usage(harness: str, stdout_tail: str) -> Usage | None:
    """What one session cost, from its captured output. Claude: the last result object's
    `total_cost_usd` and `usage` (input and output tokens, cache tokens not counted: the cost is
    authoritative); no numeric cost means unreadable.
    Codex: the summed `usage` of its turn.completed events (tokens, no cost). None when the
    usage can't be read (killed, truncated, malformed): the driver then charges the reservation."""
    if harness == "claude":
        res = _claude_result(stdout_tail or "")
        if res is None or not _number(res.get("total_cost_usd")) or res["total_cost_usd"] < 0:
            return None
        usage = res.get("usage") if isinstance(res.get("usage"), dict) else {}
        return Usage(float(res["total_cost_usd"]), _count(usage.get("input_tokens")),
                     _count(usage.get("output_tokens")))
    if harness == "codex":
        turns = [o for o in _json_objects(stdout_tail or "") if o.get("type") == "turn.completed"
                 and isinstance(o.get("usage"), dict)]
        if not turns:
            return None
        # PROVISIONAL: summed per turn.completed. Whether codex reports per-turn or thread-cumulative
        # usage is unverified (no live capture yet); summing can only overcharge, which is the safe
        # direction for the budget. Verify against a live `codex exec --json` capture.
        tin = sum(_count(t["usage"].get("input_tokens")) for t in turns)
        tout = sum(_count(t["usage"].get("output_tokens")) for t in turns)
        return Usage(None, tin, tout)
    return None


def _provider_class(status, text: str) -> bool:
    if _number(status) and (int(status) in _PROVIDER_STATUS or 500 <= int(status) < 600):
        return True
    return bool(_PROVIDER.search(text or ""))


def classify_failure(harness: str, tail: str) -> str | None:
    """"outage" when the provider failed (overload, rate limit, auth, API error: Claude's JSON
    result with is_error, Codex's error events of the same classes): not the attempt's fault, so
    it is not counted against the attempt limits. "error" when the harness binary could not run
    (the runner's own line; G9: doctor reports it, it is not an outage). None otherwise."""
    tail = tail or ""
    if BINARY_FAILURE in tail or _BINARY_LINE.search(tail):
        return "error"
    if harness == "claude":
        res = _claude_result(tail)
        if res is not None and res.get("is_error") is True:
            text = " ".join(str(res.get(k) or "") for k in ("subtype", "result", "error", "message"))
            return "outage" if _provider_class(res.get("api_error_status"), text) else None
        return None
    if harness == "codex":
        for o in _json_objects(tail):
            if o.get("type") not in ("error", "turn.failed"):
                continue
            err = o.get("error") if isinstance(o.get("error"), dict) else {}
            text = " ".join(str(x) for x in (o.get("message"), err.get("message"), err.get("type")) if x)
            if _provider_class(err.get("status") or o.get("status"), text):
                return "outage"
    return None


def _price(cfg: dict, tokens: int) -> float:
    """What `tokens` cost on Codex, which reports tokens and no cost: [drive] codex_usd_per_mtok."""
    rate = float(((cfg or {}).get("drive") or {}).get("codex_usd_per_mtok", 10))
    return tokens * rate / 1_000_000


def _read_drive_usage(cfg: dict, run: dict) -> None:
    """Once per drive run: read what the session cost from its kept output tail and keep it in the run
    (cost_usd, tokens_in, tokens_out; None when unreadable: the ledger then charges the reservation)
    with whether it ended in a provider outage. A retried finish reuses it."""
    if "usage_read" in run:
        return
    text = ""
    try:
        with _runs_fd(run["board"], create=False) as d:
            text = _read_tail(d, f"r{int(run['restart_id'])}.tail.txt", 2 * TAIL_BYTES + 4096)
    except Exception:
        pass
    u = parse_usage(run["harness"], text)
    cost = None if u is None else (u.cost_usd if u.cost_usd is not None else _price(cfg, u.tokens_in + u.tokens_out))
    run.update(usage_read=True, cost_usd=cost, tokens_in=None if u is None else u.tokens_in,
               tokens_out=None if u is None else u.tokens_out,
               outage=classify_failure(run["harness"], text) == "outage")


def _stop_unit(unit: str, proc=None, grace: float | None = None) -> None:
    """SIGTERM every process of the session's scope, wait up to `grace` (KILL_GRACE) seconds for
    the scope to be empty (and our systemd-run child, if the scope wasn't created yet, to end),
    then SIGKILL what is left and stop the unit."""
    grace = KILL_GRACE if grace is None else grace
    _systemctl("kill", "--signal=SIGTERM", unit)
    if proc is not None and proc.poll() is None and not _unit_exists(_unit_state(unit)):
        proc.terminate()   # still systemd-run, before its scope exists: our unwaited child, safe
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        leader_running = proc is not None and proc.poll() is None   # a zombie would keep the scope populated
        if not leader_running and not _unit_active(unit):
            break
        time.sleep(0.1)
    else:
        _systemctl("kill", "--signal=SIGKILL", unit)
        if proc is not None and proc.poll() is None:
            proc.kill()
    _systemctl("stop", unit)
    if proc is not None:
        try:
            proc.wait(5)
        except subprocess.TimeoutExpired:
            pass


def _stop_session(run: dict, proc=None, grace: float | None = None) -> None:
    """Stop everything of the run's session: its scope, its Job Object (Windows), else its process group."""
    if run.get("contained") == "job":
        _job_stop(int(run["restart_id"]), proc, grace)
        return
    if run.get("unit"):
        _stop_unit(run["unit"], proc, grace)
    else:
        _stop_group(int(run.get("child_pgid") or run.get("child_pid")), proc, grace,
                    tag=run.get("run_tag"), since=run.get("child_start"))


def group_presence(run: dict) -> str:
    """"present" | "absent" | "unknown" for a run contained in its process group (no scope unit, spec 3.11
    [T21]): a tagged member is evidence of presence; no member is "absent" only where the members can be
    listed (/proc, or `ps` where there is none); otherwise "unknown" (never "absent" without positive
    evidence). A run that never started a session is "absent"."""
    if run.get("contained") == "job":   # Windows: the job of a live runner; a dead runner's job died with it
        active = _job_active(int(run.get("restart_id") or 0))
        if active is not None:
            return "present" if active else "absent"
        from swarm.supervisor import procwatch
        pid = run.get("child_pid")
        return "present" if pid and procwatch.ProcWatch(int(pid), run.get("child_token")).alive() else "absent"
    pgid = run.get("child_pgid") or run.get("child_pid")
    if not pgid:
        return "absent"
    found = _list_members(pgid, run.get("run_tag"), run.get("child_start"))
    if found:
        return "present"
    return "unknown" if found is None else "absent"


def session_presence(run: dict, deadline: float | None = None) -> str:
    """The session of a run: its scope (unit_presence) or, without one, its process group (group_presence)."""
    if run.get("unit"):
        return unit_presence(run["unit"], run.get("slice"), deadline)
    return group_presence(run)


def _session_alive(run: dict) -> bool:
    """Anything of the run's session still running (its scope active; else its process group, where an
    unknown answer counts as alive)."""
    if run.get("unit"):
        return _unit_active(run["unit"])
    return group_presence(run) != "absent"


def start(cfg: dict, run: dict, popen=subprocess.Popen) -> int:
    """Lock the run, write the run file (without the brief) and start the detached runner with
    the locked descriptor (it owns the run from then on); the run itself, brief included, goes to
    it over its stdin, never through a file. Its pid."""
    from swarm import paths
    if compat.IS_WINDOWS:
        return _start_windows(run, popen)
    fd = own(run["restart_id"], run["board"])
    if fd is None:
        raise RuntimeError(f"restart r{run['restart_id']} already has a runner")
    try:
        write_run(run)
        env = {**os.environ, "PYTHONPATH": str(paths.LIB_DIR), **paths.pycache_env(os.environ)}
        proc = popen([sys.executable, "-m", "swarm.supervisor.runner", "--lock-fd", str(fd)],
                     env=env, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True, pass_fds=(fd,))
    finally:
        os.close(fd)   # the runner's inherited copy keeps the lock
    pipe = getattr(proc, "stdin", None)
    if pipe is not None:
        try:
            pipe.write(json.dumps(run).encode())
        finally:
            pipe.close()
    return proc.pid


def _launch_argv(cfg: dict, run: dict) -> tuple[list, str | None]:
    """The argv the scope runs: a drive launch's (run["launch_id"]) wrapped in its credential jail
    (launchhome.wrap), any other replacement's as it is. (argv, why refused)."""
    argv = list(run["argv"])
    if not run.get("launch_id"):
        return argv, None
    from swarm.supervisor import launchhome
    launch = str(run.get("launch") or run["name"])
    if run.get("harness") == "codex":
        from swarm.supervisor.launch import codex_wrapper
        try:
            wrapped = codex_wrapper(cfg)
        except ValueError as exc:
            return argv, str(exc)
        if wrapped:   # codex runs as another OS user: that user is the boundary, nothing local to jail
            _note_wrapper()
            run["launch_mode"] = "off"
            return argv, None
    try:
        launched, env, mode = launchhome.launch(cfg, argv, job=run["job"], launch=launch, workdir=run["cwd"],
                                              read_only=bool(run.get("read_only")))
    except (launchhome.LaunchIsolationError, OSError) as exc:
        return argv, str(exc)
    run["launch_mode"] = mode
    if mode != "off":   # jailed or best-effort: its broker serves the launch's directory
        run["launch_home"] = str(launchhome.home_dir(run["job"], launch))
    if env is not None:
        run["launch_env"] = env
        _note_best_effort()
    return launched, None


_BEST_EFFORT_LOGGED = False
_WRAPPER_LOGGED = False


def _note_wrapper() -> None:
    """Log once per process that codex launches go through a wrapper and are not jailed."""
    global _WRAPPER_LOGGED
    if _WRAPPER_LOGGED:
        return
    _WRAPPER_LOGGED = True
    from swarm.supervisor.settings import log
    log("launch isolation: codex launches run through [harness.codex] wrapper: not jailed here "
        "(the wrapper's OS user is the isolation boundary)")


def _note_best_effort() -> None:
    """Log that a launch starts best-effort (no filesystem jail): once per process on macOS/Windows (that is how
    those hosts are), every time on Linux with its reason (a fallback there is a security event)."""
    global _BEST_EFFORT_LOGGED
    from swarm.supervisor import launchhome
    from swarm.supervisor.settings import log
    note = "launch isolation: " + launchhome.BEST_EFFORT_NOTE.format(platform=sys.platform)
    if launchhome.jail_platform():
        log(f"{note}: {launchhome.last_fallback_reason[0] or 'no user namespaces'}")
        return
    if _BEST_EFFORT_LOGGED:
        return
    _BEST_EFFORT_LOGGED = True
    log(note)


LAUNCH_TRANSCRIPT_MAX = 64 * 1024 * 1024


def _keep_launch_transcript(run: dict) -> None:
    """A jailed launch's transcript lives in its launch directory, where the host's capture does not look (Task 20
    rr14): copy it into the run's directory (r<id>.transcript.jsonl, private), read without following a link,
    and let finish capture it from there."""
    from swarm.supervisor import privfs, launchbroker
    from swarm.supervisor.settings import log
    try:
        data = launchbroker.read_launch_transcript(run["launch_home"], run.get("harness") or "claude",
                                               run.get("agent_key") or run.get("session_id"), LAUNCH_TRANSCRIPT_MAX)
        if data is None:
            return
        name = f"r{run['restart_id']}.transcript.jsonl"
        with _runs_fd(run["board"]) as d:
            privfs.write_atomic(d, name, data)
        run["transcript_copy"] = str(run_file(run["restart_id"], run["board"]).with_name(name))
        write_run(run)
    except OSError as exc:
        log(f"restart {run['restart_id']}: the launch's transcript was not kept ({type(exc).__name__})")


def _start_windows(run: dict, popen) -> int:
    """Windows passes no descriptors to a child: the run lock is checked free here, then the runner takes it
    itself (`--own`) BEFORE it writes the run file, and says `owned` on its stdout; this returns only then, so
    a run file never exists unowned (as with the POSIX hand-off). The runner is detached, with no console."""
    from swarm import paths
    fd = own(run["restart_id"], run["board"])
    if fd is None:
        raise RuntimeError(f"restart r{run['restart_id']} already has a runner")
    os.close(fd)
    env = {**os.environ, "PYTHONPATH": str(paths.LIB_DIR), **paths.pycache_env(os.environ)}
    flags = (subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
             | getattr(subprocess, "CREATE_NO_WINDOW", 0))
    proc = popen([sys.executable, "-m", "swarm.supervisor.runner", "--own"], env=env, stdin=subprocess.PIPE,
                 stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, close_fds=True, creationflags=flags)
    try:
        proc.stdin.write(json.dumps(run).encode())
    finally:
        proc.stdin.close()
    line = proc.stdout.readline().strip()
    proc.stdout.close()
    if line != b"owned":
        raise RuntimeError(f"restart r{run['restart_id']}: the runner did not take its run lock ({line[:40]!r})")
    return proc.pid


# -- Windows: a launch in a Job Object (spec 3.11 [T21]) -----------------------------------------------

_JOBS: dict = {}   # restart id -> the Job Object handle this runner holds (closing it kills the launch)


def _k32():
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateJobObjectW.restype = wintypes.HANDLE
    k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    k32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    k32.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
                                              ctypes.c_void_p]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    return k32


def _job_contain(rid: int, proc) -> None:
    """Put the (suspended) session in a new Job Object that kills every process in it when its last handle
    closes (JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE: if this runner dies, the launch dies with it), then resume it.
    Anything it starts is in the job too. Raises when the job cannot be made: the caller stops the session."""
    import ctypes
    k32 = _k32()
    job = k32.CreateJobObjectW(None, None)
    if not job:
        raise ctypes.WinError(ctypes.get_last_error())

    class BASIC(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", ctypes.c_uint32), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", ctypes.c_uint32),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", ctypes.c_uint32),
                    ("SchedulingClass", ctypes.c_uint32)]

    class IO(ctypes.Structure):
        _fields_ = [(n, ctypes.c_uint64) for n in ("r", "w", "o", "rb", "wb", "ob")]

    class EXTENDED(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", BASIC), ("IoInfo", IO), ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t)]

    info = EXTENDED()
    info.BasicLimitInformation.LimitFlags = 0x2000          # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    try:
        if not k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):   # Extended limits
            raise ctypes.WinError(ctypes.get_last_error())
        if not k32.AssignProcessToJobObject(job, int(proc._handle)):
            raise ctypes.WinError(ctypes.get_last_error())
    except BaseException:
        k32.CloseHandle(job)
        raise
    _JOBS[rid] = job
    ntdll = ctypes.WinDLL("ntdll")
    ntdll.NtResumeProcess.argtypes = [ctypes.c_void_p]
    ntdll.NtResumeProcess(int(proc._handle))


def _job_active(rid: int) -> int | None:
    """How many processes the launch's Job Object holds (None: this process holds no job for the run)."""
    import ctypes
    job = _JOBS.get(rid)
    if job is None:
        return None
    buf = (ctypes.c_uint32 * 12)()                    # JOBOBJECT_BASIC_ACCOUNTING_INFORMATION (48 bytes)
    if not _k32().QueryInformationJobObject(job, 1, buf, ctypes.sizeof(buf), None):
        return None
    return int(buf[10])                               # ActiveProcesses (after 4 times, page faults, total)


def _job_stop(rid: int, proc=None, grace: float | None = None) -> None:
    """End every process of the launch's job: TerminateJobObject (Windows has no SIGTERM for a console-less
    process tree), then close the job."""
    job = _JOBS.pop(rid, None)
    if job is not None:
        k32 = _k32()
        k32.TerminateJobObject(job, 1)
        k32.CloseHandle(job)
    elif proc is not None and proc.poll() is None:
        proc.kill()
    if proc is not None:
        try:
            proc.wait(5)
        except subprocess.TimeoutExpired:
            pass


def _child_env(token: str | None = None, tag: str | None = None, pass_env=(), drive: bool = False) -> dict:
    """The replacement's environment: an allowlist of ours (_allowed_env) plus the names in
    [supervise] pass_env, never what would make it look like it runs inside another Claude or
    Codex session (tests/support.HOST_ENV); plus its resume token if any and its run tag (RUN_ENV:
    how reap recognises the session's processes)."""
    from swarm.supervisor.markers import TOKEN_ENV
    drop = ("CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_CHILD_SESSION", "CLAUDECODE", "CLAUDE_PLUGIN_ROOT",
            "CODEX_SESSION_ID", "CODEX_THREAD_ID", "PLUGIN_ROOT", "SWARM_HOST", TOKEN_ENV, RUN_ENV)
    env = {k: v for k, v in _allowed_env(os.environ, pass_env).items() if k not in drop}
    if drive:   # a drive launch never gets a push credential variable, whatever pass_env names (Task 20)
        from swarm import launchjail
        env = launchjail.scrub_env(env)   # XDG_RUNTIME_DIR comes back for systemd-run (manager_env); the jail drops it
    if token:
        env[TOKEN_ENV] = token
    if tag:
        env[RUN_ENV] = tag
    return env


def _token_for(run: dict) -> tuple[str | None, str | None]:
    """A Codex replacement's resume token, stored in its marker before the session starts (the
    hooks bind by it when they run before the runner saw thread.started: markers docstring).
    (token, None), or (None, outcome) when it can't be stored: the marker is gone ("cancelled":
    deactivated meanwhile) or busy ("failed"). Claude replacements are bound up front: no token."""
    import secrets
    from swarm.supervisor import markers
    if run["harness"] != "codex" or run.get("agent_key"):
        return None, None
    token = secrets.token_hex(16)
    if markers.set_resume_token(Path(run["marker"]), token):
        return token, None
    return None, ("failed" if markers.read_marker(Path(run["marker"])) is not None else "cancelled")


def _marker_session(marker: str) -> str | None:
    """The session the (sandbox-writable) resume marker is bound to, read safely
    (markers.read_marker: never blocks on a FIFO, never follows a link)."""
    from swarm.supervisor import markers
    m = markers.read_marker(Path(marker))
    sid = m.get("session_id") if m else None
    return str(sid) if sid else None


def _agent_row(cfg, job, key):
    from swarm.board import open_board
    with open_board(cfg) as b:
        return next((a for a in b.agents(job) if a.agent_key == key), None)


def _kill_switch(cfg: dict, run: dict) -> str | None:
    """Why the replacement must not be launched now: a kill switch ([supervise] enabled, the off
    files, the job's --no-supervise) or its job closed; None to go ahead. A board that can't be
    asked doesn't stop it (the enrol deadline does, if the board stays away)."""
    from swarm.board import open_board
    from swarm.supervisor.settings import off_reason
    why = off_reason(cfg, drive=bool(run.get("launch_id")))   # a drive launch ignores [supervise] enabled
    if why:
        return f"the supervisor is switched off ({why})"
    try:
        with open_board(cfg) as b:
            js = b.job_status(run["job"])
    except Exception:
        return None
    if js is None or js.status != "active":
        return "its job is closed"
    if not js.supervise:
        return "supervise is off for its job"
    return None


def _replacement_row(a, run: dict) -> bool:
    """Whether agent row `a` is this run's replacement (it resumes run["resume_of"] on its job):
    only such a row is ever taken for the replacement or closed by its runner."""
    return a is not None and a.resume_of == run["resume_of"] and a.job == run["job"]


def _feed(proc, data: bytes) -> None:
    """Write the brief to the session's stdin, then close it (a thread: a session that never
    reads must not block the runner's clock)."""
    try:
        proc.stdin.write(data)
    except (BrokenPipeError, OSError, ValueError):
        pass
    finally:
        try:
            proc.stdin.close()
        except (BrokenPipeError, OSError, ValueError):
            pass


def execute(cfg: dict, run: dict, *, popen=subprocess.Popen, clock=time.monotonic, sleep=time.sleep,
            poll: float = POLL_SECONDS, board_every: float = BOARD_EVERY, lock_fd: int | None = None) -> str:
    """Run the replacement to its end and finish it; the outcome (board.RESTART_OUTCOMES).
    `lock_fd`: the run lock handed over by start(); without it the lock is taken here
    (RuntimeError if another runner owns the run). It is held until this returns."""
    if lock_fd is None:
        lock_fd = own(run["restart_id"], run["board"])
        if lock_fd is None:
            raise RuntimeError(f"restart r{run['restart_id']} already has a runner")
    os.set_inheritable(lock_fd, False)   # never passed on to the session
    try:
        return _execute(cfg, run, popen=popen, clock=clock, sleep=sleep, poll=poll, board_every=board_every)
    finally:
        os.close(lock_fd)
        from swarm.supervisor import wake
        wake.touch_cfg(cfg)   # spec 3.11 [T21]: a runner's exit wakes the session drivers (after its lock is free)


def open_workdir(cfg: dict, path: str, check=None) -> tuple[int | None, str | None]:
    """The replacement's work directory, opened right before the launch by walking
    from / one component at a time with O_DIRECTORY | O_NOFOLLOW (a symlink swapped in anywhere
    since the pass checked it fails the walk), with the allowlist rechecked on what was opened:
    under an allowed root, no dot-directory below it, a directory of this user, never the home
    directory or above. (descriptor, None), or (None, why not). The session is then started in
    that directory by descriptor, never by a path that could be re-resolved.

    Every directory opened from the allowed root down to the work
    dir is also checked for unapproved project configuration on its descriptor
    (command.workdir_config_problem, the approval store read once), so the check and the launch
    see the same inodes. `check(fd, path) -> why | None` replaces that check (approve uses it to
    collect what is there)."""
    from swarm import paths
    from swarm.supervisor import command
    from swarm.supervisor.settings import settings
    bad = f"work dir {path} not under [supervise] allowed_workdirs"
    if not isinstance(path, str) or not os.path.isabs(path) or os.path.normpath(path) != path:
        return None, f"{bad} (not an absolute normalised path)"
    p = Path(path)
    home = Path(os.path.realpath(paths.home()))
    if p == home or p in home.parents:
        return None, f"{bad} (it is the home directory or above it)"
    roots = [Path(os.path.realpath(os.path.expanduser(r))) for r in settings(cfg)["allowed_workdirs"]]
    root = next((r for r in roots if p == r or r in p.parents), None)
    if root is None:
        return None, bad
    if any(part.startswith(".") for part in p.relative_to(root).parts):
        return None, f"{bad} (a dot-directory below {root})"
    if check is None:
        approvals = command.load_approvals()

        def check(fd, where):
            return command.workdir_config_problem(fd, where, approvals)
    # from the root ("/"; on Windows the drive or share root, opened by compat's checked path emulation)
    fd = (compat.open_root(p.anchor) if compat.IS_WINDOWS
          else compat.open("/", os.O_RDONLY | compat.O_DIRECTORY | compat.O_CLOEXEC))
    try:
        for i, part in enumerate(p.parts[1:], start=2):
            try:
                nfd = compat.open(part, os.O_RDONLY | compat.O_DIRECTORY | compat.O_NOFOLLOW | compat.O_CLOEXEC, dir_fd=fd)
            except OSError as exc:
                os.close(fd)
                return None, f"{bad} ({part!r} is a symlink or not a directory now: {exc.strerror})"
            os.close(fd)
            fd = nfd
            if i >= len(root.parts):   # the allowed root and every directory below it
                why = check(fd, str(Path(*p.parts[:i])))
                if why:
                    os.close(fd)
                    return None, why
        st = os.fstat(fd)
        if st.st_uid != compat.uid():
            os.close(fd)
            return None, f"{bad} (it belongs to another user)"
        return fd, None
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        raise


def _open_workdir_checked(cfg: dict, path: str) -> tuple[int | None, str | None, bool]:
    """open_workdir, and whether a refusal is for project configuration (recoverable by an
    approval) rather than the path itself. (descriptor, why not, config)."""
    from swarm.supervisor import command
    approvals = command.load_approvals()
    config: list = []

    def check(fd, where):
        why = command.workdir_config_problem(fd, where, approvals)
        if why:
            config.append(why)
        return why
    fd, why = open_workdir(cfg, path, check=check)
    return fd, why, bool(why and config and why == config[-1])


def _post_hold(cfg: dict, run: dict, why: str) -> None:
    """The one board post of a held launch (recorded in the run file: never twice)."""
    from swarm.board import open_board
    from swarm.supervisor.settings import log
    from swarm.supervisor.stuck import SUPERVISOR_NAME
    from swarm.textsafe import strip_controls
    log(f"restart {run['restart_id']} of {run['name']} on {run['job']} held: {why}")
    if run.get("hold_posted"):
        return
    try:
        with open_board(cfg) as b:
            b.post(run["job"], SUPERVISOR_NAME,
                   f"restart of {run['name']} waiting for approval: {strip_controls(why)}")
        run["hold_posted"] = True
        write_run(run)
    except Exception as exc:
        log(f"restart {run['restart_id']}: hold post failed ({type(exc).__name__}); retried")


def _open_workdir_or_hold(cfg: dict, run: dict, *, clock, sleep) -> tuple[int | None, str | None, str | None]:
    """The work dir opened right before the launch; project configuration found there (planted
    after the pass) holds the launch until it is approved (see CONFIG_HOLD_MAX). (descriptor, why
    refused, why cancelled)."""
    fd, why, config = _open_workdir_checked(cfg, run["cwd"])
    if fd is not None or not config:
        return fd, why, None
    limit = float(run["limit_seconds"]) if CONFIG_HOLD_MAX is None else \
        min(float(CONFIG_HOLD_MAX), float(run["limit_seconds"]))
    deadline = clock() + limit
    while clock() < deadline:
        _post_hold(cfg, run, why)
        sleep(max(0.0, min(CONFIG_HOLD_POLL, deadline - clock())))
        off = _kill_switch(cfg, run)
        if off:
            return None, None, off
        fd, why, config = _open_workdir_checked(cfg, run["cwd"])
        if fd is not None or not config:
            return fd, why, None
    return None, f"{why} (not approved within {limit / 60:.1f} min of holding the launch)", None


def _fresh_output(name: str, board) -> int:
    """A new, empty output file (privfs.fresh_file: O_CREAT | O_EXCL | O_NOFOLLOW); whatever was at
    the name, a leftover or a planted link, is moved aside to a random name, never opened."""
    from swarm.supervisor import privfs
    from swarm.supervisor.settings import log
    with _runs_fd(board) as d:
        fd, moved = privfs.fresh_file(d, name)
    if moved:
        log(f"{name} already existed (a leftover or a planted link): moved aside to {moved}, not opened")
    return fd


def _execute(cfg: dict, run: dict, *, popen, clock, sleep, poll: float, board_every: float) -> str:
    from swarm.supervisor import markers
    from swarm.supervisor.settings import ensure_private_dir, log, runs_dir
    ensure_private_dir(cfg)
    rid = run["restart_id"]
    run.update(runner_pid=os.getpid(), runner_start=_proc_start(os.getpid()))
    write_run(run)
    off = _kill_switch(cfg, run)   # right before the launch
    if off:
        log(f"restart {rid} of {run['name']} on {run['job']} not launched: {off}")
        run.update(exited=True, outcome="cancelled", agent_key=run.get("session_id"),
                   refused_reason=off)
        write_run(run)
        _keep_output_tail(rid, run["board"])
        finish(cfg, run, "cancelled")
        return "cancelled"
    # spec 3.11 [T21]: a drive launch with no user manager runs in its own process group; any other replacement
    # is refused there, as before (the legacy supervisor's restarts need a scope)
    scoped = scope_available()
    token, refused = (None, "refused") if not (scoped or run.get("launch_id")) else _token_for(run)
    if refused == "refused":
        run["refused_reason"] = NO_MANAGER
    if refused:
        run.update(exited=True, outcome=refused, agent_key=run.get("session_id"))
        write_run(run)
        _keep_output_tail(rid, run["board"])
        finish(cfg, run, refused)
        return refused
    wfd, bad_cwd, off = _open_workdir_or_hold(cfg, run, clock=clock, sleep=sleep)   # right before the launch
    if off:
        log(f"restart {rid} of {run['name']} on {run['job']} not launched: {off}")
        run.update(exited=True, outcome="cancelled", agent_key=run.get("session_id"), refused_reason=off)
        write_run(run)
        _keep_output_tail(rid, run["board"])
        finish(cfg, run, "cancelled")
        return "cancelled"
    if bad_cwd:
        log(f"restart {rid} of {run['name']} on {run['job']} not launched: {bad_cwd}")
        run.update(exited=True, outcome="refused", agent_key=run.get("session_id"), refused_reason=bad_cwd)
        write_run(run)
        _keep_output_tail(rid, run["board"])
        finish(cfg, run, "refused")
        return "refused"
    launch_argv, jail_refused = _launch_argv(cfg, run)   # Task 20: a drive launch runs without push credentials
    if jail_refused:
        os.close(wfd)
        log(f"restart {rid} of {run['name']} on {run['job']} not launched: {jail_refused}")
        run.update(exited=True, outcome="refused", agent_key=run.get("session_id"), refused_reason=jail_refused)
        write_run(run)
        _keep_output_tail(rid, run["board"])
        finish(cfg, run, "refused")
        return "refused"
    import secrets
    run["run_tag"] = f"r{rid}-{secrets.token_hex(8)}"
    from swarm.supervisor.settings import settings
    env = _child_env(token, run["run_tag"], settings(cfg)["pass_env"], drive=bool(run.get("launch_id")))
    if run.get("launch_env"):   # a best-effort launch (no jail): the allowlist, no credential helper, its broker
        from swarm.supervisor import launchhome
        env = launchhome.best_effort_child_env(env, run["launch_env"], run["launch_home"])
    if scoped:
        run.update(unit=f"swarm-r{rid}-{secrets.token_hex(4)}.scope", unit_seen_active=False,
                   launch_epoch=time.time())
        argv = [SYSTEMD_RUN, "--user", "--scope", f"--unit={run['unit']}", "--collect", "--quiet", "--",
                *launch_argv]
        env = _manager_env(env)
    else:   # its own process group (start_new_session), every member tagged with RUN_ENV; Windows: a Job Object
        run.update(unit=None, contained="job" if compat.IS_WINDOWS else "group", launch_epoch=time.time())
        argv = list(launch_argv)
    write_run(run)   # the unit name is known before anything runs in it
    fd_err = _fresh_output(f"r{rid}.err", run["board"])
    try:
        # the child changes into the opened directory itself (/proc/self/fd/<n> is the open
        # directory, not a path lookup), before systemd-run execs; nothing re-resolves the path
        if compat.IS_WINDOWS:   # no descriptor cwd: the checked directory by path; suspended until it is in its job
            proc = popen(argv, cwd=compat._dirs.get(wfd), env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=fd_err, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | 0x4
                         | getattr(subprocess, "CREATE_NO_WINDOW", 0))   # 0x4: CREATE_SUSPENDED
            try:
                _job_contain(rid, proc)
            except BaseException:
                proc.kill()
                raise
        else:
            where = ({"cwd": f"/proc/self/fd/{wfd}"} if os.path.isdir("/proc/self/fd")
                     else {"preexec_fn": lambda: os.fchdir(wfd)})   # no /proc (macOS): the child enters it itself
            proc = popen(argv, pass_fds=(wfd,), env=env, stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, stderr=fd_err, start_new_session=True, **where)
    except OSError as exc:   # no such binary, bad cwd: the session never started
        os.close(wfd)
        os.write(fd_err, f"could not start the session: {type(exc).__name__}\n".encode())
        os.close(fd_err)
        run.update(exited=True, outcome="failed", agent_key=run.get("session_id"))
        write_run(run)
        _keep_output_tail(rid, run["board"])
        finish(cfg, run, "failed")
        return "failed"
    os.close(fd_err)
    os.close(wfd)
    from swarm.supervisor import procwatch
    run.update(child_pid=proc.pid, child_pgid=proc.pid,   # start_new_session: its own group
               child_start=_proc_start(proc.pid), agent_key=run.get("session_id"), started_epoch=time.time(),
               child_token=procwatch.start_token(proc.pid))
    write_run(run)
    broker = None
    if run.get("launch_home"):   # a jailed launch reaches the board only through its broker (Task 20)
        from swarm.supervisor import launchbroker
        broker = launchbroker.for_run(cfg, run, host_pid=proc.pid, token=token)
        if broker is not None:
            broker.start()
    feeder = threading.Thread(target=_feed, args=(proc, str(run["stdin"]).encode()), daemon=True)
    feeder.start()
    last_line = {"text": ""}

    def pump():   # stdout -> r<id>.out; Codex: bind the thread id from the first event
        fd_out = _fresh_output(f"r{rid}.out", run["board"])
        with os.fdopen(fd_out, "wb") as fh, proc.stdout:
            for raw in proc.stdout:
                fh.write(raw)
                line = raw.decode("utf-8", "replace").strip()
                if line:
                    last_line["text"] = line
                if run["harness"] == "codex" and not run.get("agent_key"):
                    try:
                        ev = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(ev, dict) and ev.get("type") == "thread.started" and ev.get("thread_id"):
                        tid = str(ev["thread_id"])
                        run["agent_key"] = tid
                        markers.bind_session(Path(run["marker"]), tid)
                        try:
                            from swarm.board import open_board
                            with open_board(cfg) as b:
                                b.set_restart_agent(rid, tid)
                        except Exception:
                            pass   # recorded again at finish
                        write_run(run)
    t = threading.Thread(target=pump, daemon=True)
    t.start()
    t0 = clock()
    next_board, enrolled, outcome = t0, False, None
    ignored: set = set()   # marker-bound keys already logged as not ours
    while proc.poll() is None:
        barrier.hit("runner_mid_session")
        now = clock()
        if now - t0 >= float(run["limit_seconds"]):
            outcome = "timeout"
            break
        if run.get("unit") and not run.get("unit_seen_active") and _unit_state(run["unit"])[1] == "active":
            run["unit_seen_active"] = True
            run["slice"] = _unit_slice(run["unit"]) or run.get("slice")   # for unit_presence [F16]
            write_run(run)
        if now >= next_board:
            next_board = now + board_every
            key = run.get("agent_key")
            adopt = False
            if not key and run["harness"] == "codex":   # a hook bound it by token before thread.started
                key, adopt = _marker_session(run["marker"]), True   # sandbox-writable: checked below
            try:
                a = _agent_row(cfg, run["job"], key) if key else None
            except Exception:
                a = None   # board trouble: judge again next round
            if a is not None and not _replacement_row(a, run):
                if key not in ignored:
                    ignored.add(key)
                    log(f"restart {rid}: agent {key} is not a replacement of {run['resume_of']}; ignored")
                a = None
            elif adopt and a is not None:
                run["agent_key"] = key
                write_run(run)
            if a is not None and a.ended_at is None:
                enrolled = True
            elif a is not None:   # its row ended while the session runs (also before we saw it active)
                outcome = "stuck" if (a.left_reason or "").startswith("stuck:") else "cancelled"
                break
            if not enrolled and now - t0 >= float(run["enrol_seconds"]):
                outcome = "not_enrolled"
                break
        sleep(poll)
    if outcome is not None:
        _stop_session(run, proc)
    else:
        proc.wait()
        if _session_alive(run):   # it left processes behind: they end with it
            _stop_session(run)
        t.join(5)
        if proc.returncode == 0:
            outcome = "completed"
        elif run.get("launch_home") and run.get("launch_mode") == "required" and proc.returncode == JAIL_UNAVAILABLE:
            outcome = "refused"   # the jail could not be built: nothing ran (not an attempt to retry blindly)
            run["refused_reason"] = "the launch jail could not be built (launchjail exit 125)"
        else:
            try:
                sub = json.loads(last_line["text"]).get("subtype")
            except (ValueError, AttributeError):
                sub = None
            outcome = "max_turns" if sub == "error_max_turns" else "failed"
    if broker is not None:
        broker.stop()
    if run.get("launch_home"):
        _keep_launch_transcript(run)
    t.join(5)
    feeder.join(1)
    run.update(exited=True, outcome=outcome)
    write_run(run)
    _keep_output_tail(rid, run["board"])   # the session is over: no raw output outlives it, whatever finish does
    finish(cfg, run, outcome)
    return outcome


def finish(cfg: dict, run: dict, outcome: str) -> bool:
    """The bookkeeping of an ended replacement: its agent row (stopped when completed, else closed
    "limit:<outcome>" if still active), its final transcript, the restart row, a board post (only
    by the call that finished the restart row: never twice), the job's supervisor wait flag; then
    the resume marker, the session output (a redacted tail kept, the raw files deleted), the log
    and the run file. False when the board is unreachable or the marker couldn't be removed: the
    run file stays, recording how far it got, for `reap` to finish."""
    barrier.hit("before_finish")
    from swarm.board import open_board
    from swarm.supervisor import lost, markers
    from swarm.supervisor.settings import log
    from swarm.supervisor.stuck import SUPERVISOR_NAME, WAITING_PREFIX
    key = run.get("agent_key")
    if run.get("launch_id"):
        _read_drive_usage(cfg, run)
    if not run.get("board_done"):
        try:
            with open_board(cfg) as b:
                if key:
                    a = next((x for x in b.agents(run["job"]) if x.agent_key == key), None)
                    if a is not None and not _replacement_row(a, run):
                        log(f"restart {run['restart_id']}: agent {key} is not a replacement of "
                            f"{run['resume_of']}; left alone")
                        key = a = None
                    else:
                        b.set_restart_agent(run["restart_id"], key)
                if key:
                    if a is not None and a.ended_at is None:
                        if outcome == "completed":
                            b.agent_stopped(key)
                        else:
                            b.close_agent(key, f"limit:{outcome}")
                            from swarm import learnings
                            learnings.mark_ended(b, cfg, run["job"], a, f"limit:{outcome}")
                    a = next((x for x in b.agents(run["job"]) if x.agent_key == key), a)
                    js = b.job_status(run["job"])
                    if a is not None and js is not None:
                        try:
                            lost.capture_final(b, cfg, js, a, time.monotonic() + FINISH_CAPTURE_SECONDS,
                                               path=run.get("transcript_copy"))
                        except Exception as exc:
                            log(f"transcript of {run['name']} ({key}) not captured: {type(exc).__name__}")
                _finish_row_and_post(b, run, outcome, SUPERVISOR_NAME)
                js = b.job_status(run["job"])
                if js is not None and js.waiting_on == WAITING_PREFIX + run["name"]:
                    b.set_waiting(run["job"], None)
        except Exception as exc:
            log(f"finish of restart {run['restart_id']} ({run['name']}) deferred: {type(exc).__name__}")
            return False
        run.update(board_done=True, outcome=run.get("outcome") or outcome)
        _save_progress(run)
    if run.get("launch_id") and not run.get("ledger_done"):
        from swarm.supervisor import driver_exec
        if not driver_exec.conclude_run(cfg, run, outcome, run.get("cost_usd"), bool(run.get("outage"))):
            log(f"ledger of restart {run['restart_id']} busy; finish retried later")
            return False
        _release_drive_locks(cfg, run)
        run["ledger_done"] = True
        _save_progress(run)
    bad_marker = _marker_problem(cfg, run)
    if bad_marker:
        log(f"restart {run['restart_id']}: {bad_marker}; not removed")
    elif not markers.remove_resume_marker(Path(run["marker"])):
        log(f"resume marker of restart {run['restart_id']} ({run['name']}) busy; removal retried later")
        return False
    log(f"restart {run['restart_id']} of {run['name']} on {run['job']} ended: {outcome}")
    from swarm.supervisor import privfs
    with _runs_fd(run["board"]) as d:
        privfs.unlink(d, run_file(run["restart_id"], run["board"]).name)
    return True


def _release_drive_locks(cfg: dict, run: dict) -> None:
    """Release the drive locks of an ended launch, once its scope unit is confirmed gone (writer
    fencing, spec 3.9): with the unit present or unknown they stay until they expire and the host
    sweep takes them."""
    from swarm.supervisor.settings import log
    unit = run.get("unit")
    if unit and unit_presence(unit, run.get("slice")) != "absent":
        return
    try:
        from swarm.board import open_board
        with open_board(cfg) as b:
            b.release_drive_locks(run["launch_id"], job=run["job"], holder=run["action_id"])
    except Exception as exc:
        log(f"drive locks of restart {run['restart_id']} not released: {type(exc).__name__}")


def _finish_row_and_post(b, run: dict, outcome: str, poster: str) -> None:
    """Finish the restart row and post its end, exactly once in the normal case. The message is
    recorded in the run file (post_pending) before the row is finished, so a crash or a failed post
    after the row is finished leaves the post to the retry. A post is made by the call that
    finished the row, or by a retry of a recorded one; a stale run file with no record whose row is
    already finished posts nothing. At least once, not exactly once, only if the process dies
    between the post and recording it (the board's post isn't idempotent)."""
    spent = {k: run[k] for k in ("cost_usd", "tokens_in", "tokens_out") if run.get(k) is not None}
    if run.get("launch_id"):
        # Settle before removing the run file, so a refusal's full reason stays on the launch.
        from swarm.supervisor import driver_exec, ledger
        b.finish_restart(run["restart_id"], outcome, **spent)
        if not driver_exec.conclude_run({}, run, outcome, run.get("cost_usd"), bool(run.get("outage"))):
            raise ledger.LedgerConflict("launch settlement deferred until its intent is concluded")
        led = ledger.load(run["board"], run["job"])
        if led is not None:
            rows = {r.launch_id: r for r in b.restarts(run["job"]) if r.id == run["restart_id"]}
            driver_exec._settle_launches(b, {}, led, rows, b.now())
        run.update(posted=True, post_pending=None)
        _save_progress(run)
        return
    if run.get("posted"):
        b.finish_restart(run["restart_id"], outcome, **spent)
        return
    recorded = bool(run.get("post_pending"))
    if not recorded:
        attempt = next((r.attempt for r in b.restarts(job=run["job"]) if r.id == run["restart_id"]), "?")
        why = f" ({run['refused_reason']})" if run.get("refused_reason") else ""
        run["post_pending"] = f"{run['name']} (restart {attempt}) ended: {outcome}{why}"
        write_run(run)   # an OSError here defers the finish: nothing is lost
    transitioned = b.finish_restart(run["restart_id"], outcome, **spent)
    if transitioned or recorded:
        b.post(run["job"], poster, run["post_pending"])
    run.update(posted=True, post_pending=None)
    _save_progress(run)


def _save_progress(run: dict) -> None:
    try:
        write_run(run)
    except OSError:
        pass


def _read_tail(d: int, name: str, limit: int) -> str:
    from swarm.supervisor import privfs
    got = privfs.read_tail(d, name, limit)   # never through a link
    if got is None:
        return ""
    data, dropped = got
    return ("…(earlier output dropped)\n" if dropped else "") + data.decode("utf-8", "replace")


def _keep_output_tail(restart_id: int, board) -> None:
    """Replace the raw session output r<id>.out/.err by one redacted, bounded file r<id>.tail.txt
    (0600; pruned after [supervise] run_output_days). Read and written through the runs
    directory's descriptor: a planted link is neither read nor written through."""
    from swarm import transcripts
    from swarm.supervisor import privfs
    rid = int(restart_id)
    try:
        with _runs_fd(board) as d:
            parts = [(label, _read_tail(d, f"r{rid}.{ext}", TAIL_BYTES)) for label, ext in (("stdout", "out"), ("stderr", "err"))]
            text = "".join(f"== {label} (last {TAIL_BYTES // 1024} KB, redacted)\n{body}\n"
                           for label, body in parts if body)
            try:
                if text:
                    privfs.write_atomic(d, f"r{rid}.tail.txt",
                                        transcripts.redact(text, time.monotonic() + TAIL_REDACT_SECONDS)[0])
            except Exception:
                pass   # out of time or failed: no tail file; the raw files go anyway
            for ext in ("out", "err"):
                privfs.unlink(d, f"r{rid}.{ext}")
    except Exception:
        pass


def _prune_outputs(days: int, board) -> None:
    """Delete kept tails older than `days`, and the lock files of finished runs (no run file,
    lock free: taken while unlinking), in `board`'s runs directory. Raw output never needs
    pruning: it is turned into a tail as soon as its session is over (by the runner, or by reap
    for a dead runner)."""
    from swarm.supervisor import privfs
    now = time.time()
    try:
        with _runs_fd(board, create=False) as d:
            names = compat.listdir(d)
            for n in names:   # kept tails, and output moved aside (privfs.fresh_file)
                if re.match(r"^r\d+\.(tail\.txt|(out|err)\.stale-[0-9a-f]+)$", n):
                    m = privfs.mtime(d, n)
                    if m is not None and now - m > days * 86400:
                        privfs.unlink(d, n)
            for n in names:
                mm = re.match(r"^r(\d+)\.lock$", n)
                if not mm:
                    continue
                rid = int(mm.group(1))
                if privfs.exists(d, run_file(rid, board).name):
                    continue
                fd = own(rid, board)
                if fd is not None:
                    try:
                        if not privfs.exists(d, run_file(rid, board).name):
                            privfs.unlink(d, n)
                    finally:
                        os.close(fd)
    except (OSError, Exception):
        pass


def _run_paths(board) -> list[tuple[int, Path]]:
    """(restart id, path) of every r<id>.json in `board`'s runs directory, by id."""
    from swarm.supervisor.settings import PrivateDirError
    try:
        with _runs_fd(board, create=False) as d:
            names = compat.listdir(d)
    except (OSError, PrivateDirError):
        return []
    out = []
    for n in names:
        m = _RUN_NAME.match(n)
        if m:
            out.append((int(m.group(1)), run_dir(board) / n))
    return sorted(out)


def _adopt_legacy(here: str) -> int:
    """Move the run files an earlier version left in the runs directory itself that name
    this board (with their output) into this board's directory, each under its legacy lock and
    its new one (a held lock: a live runner of the earlier version, left alone). Files of other
    boards, and unreadable ones, stay. How many were adopted."""
    from swarm.supervisor import privfs
    from swarm.supervisor.settings import log
    n = 0
    for rid, p in _run_paths(None):
        r = _read_run(p.name, None)
        if not (isinstance(r, dict) and r.get("board") == here):
            continue
        old = own(rid, None)
        if old is None:
            continue
        try:
            new = own(rid, here)
            if new is None:
                continue
            try:
                with _runs_fd(None) as src, _runs_fd(here) as dst:
                    if privfs.exists(dst, p.name):
                        log(f"legacy run file {p.name} not adopted: this board's directory has one")
                        continue
                    for name in (f"r{rid}.out", f"r{rid}.err", f"r{rid}.tail.txt", p.name):
                        if privfs.exists(src, name):
                            compat.rename(name, name, src_dir_fd=src, dst_dir_fd=dst)
                    privfs.unlink(src, f"r{rid}.lock")
                n += 1
                log(f"adopted legacy run file {p.name} into this board's runs directory")
            finally:
                os.close(new)
        except OSError as exc:
            log(f"legacy run file {p.name} not adopted: {type(exc).__name__}")
        finally:
            os.close(old)
    return n


def reap(cfg: dict, board, deadline: float | None = None) -> int:
    """Finish the runs whose runner is gone (it died, or finish was deferred: board down, marker
    busy), each while holding its run lock (a held lock means a live runner: skipped). The outcome
    is the one recorded, else "failed". A session that outlived its runner is stopped, its whole
    process group, once its wall clock is up ("timeout"), and left alone before. Its raw output is
    turned into the redacted tail first. Each run file is checked first against `board` (run_problem,
    _row_problem); one that fails is refused and left alone. The number finished."""
    from swarm.board.autoinit import store_key
    from swarm.supervisor.settings import ensure_private_dir, settings
    ensure_private_dir(cfg)
    here = store_key(cfg)
    _adopt_legacy(here)
    n = 0
    for rid, p in _run_paths(here):
        if deadline is not None and time.monotonic() >= deadline:
            break      # `deadline` (time.monotonic()): the rest is finished by the next pass
        fd = own(rid, here)
        if fd is None:
            continue   # its runner (or another reap) owns it
        try:
            r = _read_run(p.name, here)   # as it is now that we own it
            if r is None:
                if os.path.lexists(p):
                    _refuse_file(p, "not a readable regular file of this user")
                continue
            if isinstance(r, dict) and isinstance(r.get("board"), str) and r["board"] != here:
                continue   # another board's run: its own board's passes finish it
            why = run_problem(cfg, r, rid) or _row_problem(board, r)
            if why:
                _refuse_file(p, why)
                continue
            outcome = r.get("outcome") or "failed"
            pgid = r.get("child_pgid") or r.get("child_pid")
            clock_up = time.time() - float(r.get("started_epoch") or r.get("launch_epoch") or 0) \
                >= float(r["limit_seconds"])
            if r.get("unit") and not r.get("exited"):
                state = _unit_state(r["unit"])
                if state == ("", ""):
                    continue   # systemd can't be asked now: judge next time
                if _unit_exists(state):
                    if state[1] in ("active", "activating", "reloading") and not clock_up:
                        continue   # the session runs on without its runner: stopped at its clock
                    _stop_unit(r["unit"], grace=2.0)
                    outcome = "timeout" if clock_up else outcome
                elif not r.get("unit_seen_active") and \
                        time.time() - float(r.get("launch_epoch") or 0) < LAUNCH_GRACE:
                    continue   # its scope may still appear (an orphaned systemd-run)
            elif not r.get("exited") and pgid and _session_alive(r):   # legacy run file: no unit
                # the session (any of its processes) outlived its runner: we enforce its clock
                if time.time() - float(r.get("started_epoch") or 0) < float(r["limit_seconds"]):
                    continue
                _stop_session(r, grace=2.0)
                outcome = "timeout"
            elif not r.get("exited") and not r.get("unit") and pgid and _group_alive(pgid):
                from swarm.supervisor.settings import log
                log(f"restart {rid}: process group {pgid} has no process of this run (its id was "
                    f"reused, or it cleared its environment); not signalled")
            if r.get("launch_id") and not r.get("exited") and outcome == "failed":
                from swarm.supervisor import procwatch
                owner = r.get("driver_pid")
                token = r.get("driver_token")
                if owner is None or token is None or procwatch.start_token(owner) != token:
                    r["driver_interrupted"] = True
                    outcome = "cancelled"
            _keep_output_tail(rid, here)
            n += finish(cfg, r, outcome)
        finally:
            os.close(fd)
    for key in (here, None):
        try:
            _prune_outputs(settings(cfg)["run_output_days"], key)
        except Exception:
            pass
    return n


def main(argv: list[str] | None = None) -> int:
    """python -m swarm.supervisor.runner [--lock-fd N], the run as JSON on stdin (start()). No
    run is ever read from a file."""
    from swarm.cli import load_config
    from swarm.supervisor.settings import log
    args = list(argv if argv is not None else sys.argv[1:])
    lock_fd = None
    take = "--own" in args          # Windows (_start_windows): take the run lock here, then say so
    if take:
        args.remove("--own")
    if "--lock-fd" in args:
        i = args.index("--lock-fd")
        lock_fd = int(args[i + 1])
        del args[i:i + 2]
    stream = getattr(sys.stdin, "buffer", sys.stdin)
    raw = stream.read()
    try:
        run = json.loads(raw)
    except ValueError:
        run = None
    if not isinstance(run, dict) or not (run.get("config") is None or isinstance(run["config"], str)):
        log("runner: no valid run on stdin; nothing done")
        return 1
    cfg = load_config(Path(run["config"])) if run.get("config") else load_config()
    why = run_problem(cfg, run)
    if why or not (isinstance(run.get("argv"), list) and run["argv"] and isinstance(run.get("cwd"), str)):
        log(f"runner: refusing its run: {why or 'no command'}; nothing done")
        return 1
    if take:
        lock_fd = own(run["restart_id"], run["board"])
        print("owned" if lock_fd is not None else "busy", flush=True)   # the last thing it writes to stdout
        if lock_fd is None:
            return 1
    execute(cfg, run, lock_fd=lock_fd)
    return 0


if __name__ == "__main__":
    sys.exit(main())
