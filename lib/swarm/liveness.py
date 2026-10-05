"""Evidence that a job is still being worked on that the board's own agent rows can't give
(they go stale when the hooks can't reach the database, and say nothing about the orchestrating
session): used by cli.OrchestratorWatch for the auto-close and expiry sweeps. Local files only,
never the board; every function fails closed to "no evidence" (False) and never raises."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

LOG_TAIL_BYTES = 256 * 1024
# What hooks.py logs (type name of the driver error) when the board can't be reached.
OUTAGE_MARKERS = ("OperationalError", "ConnectionTimeout", "BoardUnavailable", "InterfaceError", "PoolTimeout")


def board_outage_since(since_ts: float) -> bool:
    """Whether a hook on this machine logged a board connection failure at or after `since_ts`
    (epoch seconds): the agents' last_seen rows can't be trusted for that long, because a hook
    that can't reach the database can't record that its agent is alive. Reads the tail of
    hook-errors.log in the host-only dir."""
    try:
        from swarm import paths, safefs
        with safefs.dir_fd(paths.host_dir(), create=False, strict_mode=0o700) as d:
            got = safefs.read_tail(d, "hook-errors.log", LOG_TAIL_BYTES)
        if got is None:
            return False
        for line in reversed(got[0].decode("utf-8", "replace").splitlines()):
            try:
                at = time.mktime(time.strptime(line[:19], "%Y-%m-%d %H:%M:%S"))
            except ValueError:
                continue
            if at < since_ts:
                return False       # lines are in time order: nothing older matters
            if any(m in line[19:] for m in OUTAGE_MARKERS):
                return True
        return False
    except Exception:
        return False


def _proc_start(pid: int) -> str | None:
    """Field 22 of /proc/<pid>/stat (start time in ticks), None if the process is gone or there
    is no /proc."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        return stat.rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError):
        return None


def _pid_alive(pid: int, proc_start: str | None) -> bool:
    started = _proc_start(pid)
    if started is not None:                  # Linux: the same process, not a recycled pid
        return proc_start is None or str(proc_start) == started
    if os.name != "posix" or Path("/proc").is_dir():   # (os.kill would terminate on Windows)
        return False
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def claude_session_alive(session_id: str | None) -> bool:
    """Whether the Claude Code session `session_id` is a running process on this machine: its
    own registry entry (<claude config dir>/sessions/<pid>.json: pid, sessionId, procStart) names
    a process that is still the one that wrote it. True while it is blocked on a question or
    waiting at the prompt: such a session makes no tool call, so the hooks don't see it."""
    if not session_id:
        return False
    try:
        d = Path(os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude").expanduser() / "sessions"
        for p in d.glob("*.json"):
            if not p.stem.isdigit():
                continue
            try:
                rec = json.loads(p.read_text())
            except (OSError, ValueError):
                continue
            if isinstance(rec, dict) and rec.get("sessionId") == session_id and isinstance(rec.get("pid"), int):
                if _pid_alive(rec["pid"], rec.get("procStart")):
                    return True
        return False
    except Exception:
        return False
