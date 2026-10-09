"""Background shell commands of swarm members: the `swarm bg` wrapper, `bg list` and `bg reap`.

The PreToolUse hook rewrites a member's background shell call (Claude Code: Bash with
run_in_background) to run under `swarm bg --job J --key K --as NAME -- '<command>'`. The
wrapper starts the command in a process group of its own, with SWARM_BG_TAG=<random> in its
environment, records it on the board (Board.bg_start: pid, pgid, /proc start ticks, host, boot
id and pid namespace, tag), forwards the signals it gets to the group, and records the exit code.

A running command is orphaned when its agent is finished or its job is closed
(Board.bg_orphans). `reap` stops orphans of THIS host only, and only processes it can prove are
the recorded command's: same boot and pid namespace, the recorded group, started no earlier than
the recorded leader, the row's tag in their environment, each signalled through a pidfd opened
and re-checked first (swarm.supervisor.runner's pattern for its replacement sessions). A reused
pid has another start time and no tag, so it is never signalled. SIGTERM, up to GRACE seconds,
then SIGKILL. See docs/superpowers/specs/2026-10-09-orphan-commands-design.md.
"""
from __future__ import annotations

import os
import re
import secrets
import shlex
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from swarm import compat

TAG_ENV = "SWARM_BG_TAG"
GRACE = 10.0          # seconds between SIGTERM and SIGKILL
AGENT_GRACE = 30.0    # SubagentStop: how long a finished agent's commands get before the reap
RECORD_SECONDS = 10.0  # the wrapper's board write at most (it never delays the command itself)

# Leading VAR=value assignments of a simple command, and `env`/`export` ones: never recorded.
_VALUE = r"(?:'[^']*'|\"(?:[^\"\\]|\\.)*\"|[^\s;&|()]*)+"
_ASSIGNS = re.compile(rf"(^|[;&|(\n]\s*|\b(?:env|export)\s+)((?:[A-Za-z_][A-Za-z0-9_]*={_VALUE}\s*)+)")


# --------------------------------------------------------------------------- identity of a process

def proc_start(pid) -> int | None:
    """/proc/<pid>/stat field 22 (start time, clock ticks since boot); None when unknown."""
    from swarm.supervisor.runner import _proc_start
    value = _proc_start(pid)
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


def boot_key() -> str | None:
    """"<boot id>/<pid namespace inode>" of this process: a pid and a start time only identify a
    process within one boot and one pid namespace. None where /proc can't tell (not Linux)."""
    try:
        boot = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
        ns = os.readlink("/proc/self/ns/pid")
    except (OSError, UnicodeDecodeError):
        return None
    m = re.fullmatch(r"pid:\[(\d+)\]", ns)
    return f"{boot}/{m.group(1)}" if boot and m else None


def this_host() -> str:
    return compat.node()


def clean_command(command: str) -> str:
    """What is recorded of a command line: env assignments dropped (VAR=x cmd, env VAR=x,
    export VAR=x), credentials redacted (swarm.transcripts.redact, bounded), one line. The board
    cuts it to BG_COMMAND_MAX."""
    text = _ASSIGNS.sub(lambda m: m.group(1), command if isinstance(command, str) else "")
    try:
        from swarm.transcripts import redact
        text, _ = redact(text, time.monotonic() + 1.0)
    except Exception:
        text = "(command not recorded: redaction failed)"
    return " ".join(text.split())


# --------------------------------------------------------------------------- the wrapper

def _config_args(cfg: dict) -> list[str]:
    """`--config PATH` when the config in use is not the default one (the wrapper must record on
    the same board as the hook that wrapped the call)."""
    from swarm.cli import DEFAULT_CONFIG
    path = cfg.get("_config_path")
    return ["--config", str(path)] if path and Path(path) != DEFAULT_CONFIG else []


def _wrap_prefix(job: str, agent_key: str, name: str, cfg: dict) -> str:
    from swarm.paths import agent_bin
    words = [str(agent_bin()), *_config_args(cfg), "bg", "--job", job, "--key", agent_key, "--as", name, "--"]
    return " ".join(shlex.quote(w) for w in words) + " "


def wrap_command(command: str, job: str, agent_key: str, name: str, cfg: dict) -> str:
    """`command` as the hook rewrites a member's background shell call: run under `swarm bg`."""
    return _wrap_prefix(job, agent_key, name, cfg) + shlex.quote(command)


def is_wrapped(command: str, job: str, agent_key: str, name: str, cfg: dict) -> bool:
    """Whether `command` is exactly what wrap_command made for this agent (so it is not wrapped
    twice). A command that merely contains a `swarm bg` call is not: it is wrapped like any other."""
    prefix = _wrap_prefix(job, agent_key, name, cfg)
    if not command.startswith(prefix):
        return False
    try:
        return len(shlex.split(command[len(prefix):])) == 1
    except ValueError:
        return False


LAUNCHER_VARS = ("PYTHONPATH", "PYTHONPYCACHEPREFIX", "PYTHONDONTWRITEBYTECODE")


def caller_env(env) -> dict:
    """The environment the agent's shell had before bin/swarm (the launcher) changed it: the
    launcher records each variable it sets as _SWARM_PRE_<VAR> (only when it was set) and marks
    the record with _SWARM_LAUNCHER; both are removed too. Without the mark: `env` as is."""
    env = dict(env)
    if env.pop("_SWARM_LAUNCHER", None) is not None:
        for var in LAUNCHER_VARS:
            env.pop(var, None)
    for var in LAUNCHER_VARS:
        pre = env.pop(f"_SWARM_PRE_{var}", None)
        if pre is not None:
            env[var] = pre
    return env


def _argv(command: list[str]) -> list[str]:
    """One word: a shell command line (bash -c, else sh -c). More: an argv run as is."""
    if len(command) == 1:
        shell = shutil.which("bash") or "/bin/sh"
        return [shell, "-c", command[0]]
    return list(command)


def _spawn(argv: list[str], env: dict) -> subprocess.Popen:
    """The command in a new process group of its own (pgid = its pid), inheriting cwd and stdio."""
    if sys.version_info >= (3, 11) and not compat.IS_WINDOWS:
        return subprocess.Popen(argv, env=env, process_group=0)
    return subprocess.Popen(argv, env=env, preexec_fn=None if compat.IS_WINDOWS else os.setpgrp)


def _record(cfg: dict, job: str, agent_key: str | None, name: str, command: str, child, tag: str):
    """Board.bg_start for the started child; (board, id), or (None, reason) when it can't be."""
    from swarm.board import ensure_initialized, open_board
    from swarm.board.autoinit import enabled
    board = None
    try:
        if enabled():   # a board one schema behind (no bg_commands yet) is set up first, as the CLI does
            ensure_initialized(cfg)
        board = open_board(cfg, init_timeout=RECORD_SECONDS)
        with board.op_timeout(RECORD_SECONDS):
            bid = board.bg_start(job, agent_key, name, clean_command(command), host=this_host(),
                                 boot=boot_key(), pid=child.pid, pgid=child.pid,
                                 proc_start=proc_start(child.pid), tag=tag)
        return board, bid
    except Exception as exc:
        if board is not None:
            board.close()
        return None, f"{type(exc).__name__}: {exc}"[:200]


def run_wrapper(cfg: dict, job: str | None, agent_key: str | None, name: str | None, command: list[str]) -> int:
    """`swarm bg --job J --key K --as NAME -- COMMAND...`: run it, recorded. Always runs the
    command (an unreachable board leaves it unrecorded, with one line on stderr); exits with its
    exit code (128+N when signal N ended it)."""
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        print("swarm bg: nothing to run (swarm bg --job J --as NAME -- COMMAND)", file=sys.stderr)
        return 2
    tag = secrets.token_hex(12)
    env = {**caller_env(os.environ), TAG_ENV: tag}   # exactly the caller's, plus the tag
    try:
        child = _spawn(_argv(command), env)
    except OSError as exc:
        print(f"swarm bg: cannot start it: {exc}", file=sys.stderr)
        return 127

    def forward(sig, _frame):
        try:   # our unwaited child: its pid, and so its group id, can't have been reused
            if hasattr(os, "killpg"):
                os.killpg(child.pid, sig)
            else:
                child.send_signal(sig)
        except OSError:
            pass
    saved = {}
    for signame in ("SIGTERM", "SIGINT", "SIGHUP", "SIGQUIT"):
        if hasattr(signal, signame):
            try:
                saved[signame] = signal.signal(getattr(signal, signame), forward)
            except (OSError, ValueError):
                pass
    board, bid = (None, "no job or name given") if not (job and name) else \
        _record(cfg, job, agent_key, name, " ".join(command) if len(command) > 1 else command[0], child, tag)
    if board is None:
        print(f"[swarm bg] not recorded on the board ({bid}); the command runs anyway", file=sys.stderr)
    while True:
        try:
            rc = child.wait()
            break
        except KeyboardInterrupt:
            continue
    code = rc if rc >= 0 else 128 - rc
    for signame, handler in saved.items():   # (in-process callers get theirs back)
        try:
            signal.signal(getattr(signal, signame), handler)
        except (OSError, ValueError, TypeError):
            pass
    if board is not None:
        try:
            with board:
                with board.op_timeout(RECORD_SECONDS):
                    board.bg_end(bid, "exited", code)
        except Exception:
            pass
    return code


# --------------------------------------------------------------------------- reap

def _pidfd_ok() -> bool:
    return hasattr(os, "pidfd_open") and hasattr(signal, "pidfd_send_signal")


def members(row) -> list[int]:
    """The live processes that provably belong to `row` (see the module docstring)."""
    from swarm.supervisor.runner import _members
    return _members(row.pgid, row.tag, row.proc_start, TAG_ENV)


def _signal(row, pids, sig) -> int:
    from swarm.supervisor.runner import _signal_member
    return sum(1 for pid in pids if _signal_member(pid, row.pgid, row.tag, row.proc_start, sig, TAG_ENV))


def where(row, host: str, boot: str | None) -> str:
    """"local" (this host, boot and pid namespace: reapable), "foreign" (another host, or another
    pid namespace of this boot: never touched here), "rebooted" (this host, another boot: the
    processes are gone), or "unverifiable" (no record to check a process against)."""
    if row.host != host:
        return "foreign"
    if not (row.boot and row.pgid and row.proc_start is not None and row.tag and boot):
        return "unverifiable"
    rboot, _, rns = row.boot.partition("/")
    mboot, _, mns = boot.partition("/")
    if rboot != mboot:
        return "rebooted"
    return "local" if rns == mns else "foreign"


def _who(row) -> str:
    from swarm.textsafe import term_safe
    return term_safe(f"#{row.id} {row.agent_name} on {row.job}")


def reap(board, rows, *, grace: float = GRACE, dry_run: bool = False, say=print,
         host: str | None = None, boot: str | None = None) -> list[tuple]:
    """Stop these orphaned rows' processes (see the module docstring) and record each outcome
    (Board.bg_end). Returns [(row, outcome or None, detail)]: None = left alone (another host,
    can't verify, dry run, still alive after SIGKILL)."""
    host = this_host() if host is None else host
    boot = boot_key() if boot is None else boot
    out, live = [], []
    for row in rows:
        place = where(row, host, boot)
        if place == "foreign":
            out.append((row, None, f"on {row.host}, not this host: left to that host's reap"))
        elif place == "unverifiable":
            out.append((row, None, "no pid/start/tag record to verify it against: left alone"))
        elif place == "rebooted":
            out.append((row, "gone", "the host rebooted since it started"))
        elif not _pidfd_ok():
            out.append((row, None, "no pidfd support here: nothing is signalled blind"))
        else:
            pids = members(row)
            if pids:
                live.append((row, pids))
                continue
            now = proc_start(row.pid)
            if now is not None and now == row.proc_start:   # its leader, but without the tag: unproven
                out.append((row, None, "its leader runs without the recorded tag (environment "
                                       "cleared?): not provably this command, left alone"))
                continue
            why = ("its pid now belongs to another process (reused): not signalled"
                   if now is not None else "no longer running")
            out.append((row, "gone", why))
    if dry_run:
        for row, pids in live:
            say(f"would reap {_who(row)}: {len(pids)} process(es) of group {row.pgid}")
        for row, outcome, why in out:
            say(f"{'would mark' if outcome else 'not reaping'} {_who(row)}: {outcome + ', ' if outcome else ''}{why}")
        return [(r, None, "dry run") for r, _ in live] + [(r, None, "dry run") for r, _, _ in out]
    signalled_at = None
    if live:
        try:
            signalled_at = board.now()   # a wrapper's "exited 143" from here on is this reap's doing
        except Exception:
            signalled_at = None
    for row, pids in live:
        _signal(row, pids, signal.SIGTERM)
    deadline = time.monotonic() + max(0.0, grace)
    left = list(live)
    while left and time.monotonic() < deadline:
        time.sleep(0.05)
        left = [(r, p) for r, p in left if members(r)]
    killed = {r.id for r, _ in left}
    for row, _ in left:
        _signal(row, members(row), signal.SIGKILL)
    end = time.monotonic() + 2.0
    while left and time.monotonic() < end:
        time.sleep(0.05)
        left = [(r, p) for r, p in left if members(r)]
    stuck = {r.id for r, _ in left}
    for row, pids in live:
        if row.id in stuck:
            out.append((row, None, f"{len(members(row))} process(es) still running after SIGKILL"))
        elif row.id in killed:
            out.append((row, "killed", f"{len(pids)} process(es): SIGTERM, then SIGKILL after {grace:g}s"))
        else:
            out.append((row, "reaped", f"{len(pids)} process(es): SIGTERM"))
    for row, outcome, why in out:
        if outcome is not None:
            try:
                board.bg_end(row.id, outcome, None, why,
                             signalled_at=signalled_at if outcome in ("reaped", "killed") else None)
            except Exception as exc:
                why = f"{why} (not recorded: {type(exc).__name__})"
        say(f"{outcome or 'left'} {_who(row)}: {why}")
    return out


def orphans(board, job: str | None = None, agent: str | None = None) -> list:
    """The orphaned rows of `job` (None: every job), only those of agent key `agent` if given."""
    rows = board.bg_orphans(job)
    return [r for r in rows if agent is None or r.agent_key == agent]


def reap_orphans(board, *, job: str | None = None, agent: str | None = None, grace: float = GRACE,
                 dry_run: bool = False, say=print) -> list[tuple]:
    """Reap the orphans of `job` / `agent` that are on this host."""
    host = this_host()
    rows = [r for r in orphans(board, job, agent) if r.host == host]
    return reap(board, rows, grace=grace, dry_run=dry_run, say=say, host=host) if rows else []


def has_local_running(board, job: str, agent: str | None = None) -> bool:
    """Whether `job` has running background commands on this host (of `agent` if given): one
    indexed read, so the hooks spawn a reaper only when there is something to reap."""
    host = this_host()
    return any(r.host == host and (agent is None or r.agent_key == agent)
               for r in board.bg_commands(job, running=True))


def spawn_reaper(cfg: dict, job: str, agent: str | None = None, delay: float = 0.0) -> None:
    """A detached `swarm bg reap --job J [--agent K --delay S]`: the hooks and the auto-close
    sweep never wait out the reap's grace themselves. Never raises."""
    from swarm.paths import agent_bin
    argv = [str(agent_bin()), *_config_args(cfg), "bg", "reap", "--job", job]
    if agent:
        argv += ["--agent", agent]
    if delay:
        argv += ["--delay", f"{delay:g}"]
    try:
        subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         close_fds=True, start_new_session=True)
    except OSError:
        pass


# --------------------------------------------------------------------------- CLI

def _state(board, row, orphan_ids) -> str:
    if row.ended_at is None:
        return "orphaned" if row.id in orphan_ids else "running"
    return f"exited {row.exit_code}" if row.outcome == "exited" else row.outcome or "ended"


def cmd_list(board, job: str | None, only_orphans: bool, show_all: bool, say=print) -> int:
    from swarm.textsafe import term_safe
    running = board.bg_commands(job, running=True)
    orphan_ids = {r.id for r in board.bg_orphans(job, running)}
    rows = (board.bg_commands(job) if show_all else running)
    if only_orphans:
        rows = [r for r in rows if r.id in orphan_ids]
    if not rows:
        say("no orphaned background commands" if only_orphans else "no background commands")
        return 0
    say(f"{'ID':>5}  {'JOB':<16} {'AGENT':<20} {'STATE':<10} {'HOST':<14} {'PGID':>7}  {'STARTED':<16} COMMAND")
    for r in rows:
        started = r.started_at.astimezone().strftime("%Y-%m-%d %H:%M")
        say(term_safe(f"{r.id:>5}  {r.job[:16]:<16} {r.agent_name[:20]:<20} {_state(board, r, orphan_ids):<10} "
                      f"{r.host[:14]:<14} {r.pgid or '-':>7}  {started:<16} {r.command[:120]}"))
    return 0


def cmd_reap(board, job: str | None, agent: str | None, delay: float, dry_run: bool, say=print) -> int:
    if delay > 0 and not dry_run:
        time.sleep(delay)   # SubagentStop's grace: the orphan test below runs after it, fresh
    done = reap_orphans(board, job=job, agent=agent, dry_run=dry_run, say=say)
    if not done:
        say("no orphaned background commands on this host")
    return 0
