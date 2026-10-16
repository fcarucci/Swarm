"""The systemd user manager, as the supervisor uses it: the bus environment for `systemd-run --user
--scope` (each launch's or replacement's containment, see swarm.supervisor.runner), the linger check, and
the removal of the legacy swarm-supervise timer.

Swarm installs no system timer of any kind: no systemd timer or service unit, no launchd agent, no
Task Scheduler task, no cron entry. Drive jobs are driven by the event-driven session driver
(swarm.supervisor.session_driver); `swarm drive run` is the same loop in the foreground and
`swarm supervise` is a one-shot pass a person runs by hand. Earlier builds could install
swarm-supervise.timer and swarm-supervise.service in the user unit directory: `swarm bootstrap` and
`swarm upgrade` remove them (remove_legacy), `swarm doctor` fails while they are there, and
`swarm uninstall` removes them too."""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

# The names of the legacy units (earlier builds). Kept only so they can be found and removed.
LEGACY_SERVICE = "swarm-supervise.service"
LEGACY_TIMER = "swarm-supervise.timer"
LEGACY_FOUND = "legacy swarm-supervise timer found; run swarm upgrade to remove it"


def unit_dir() -> Path:
    from swarm import paths
    return Path(os.environ.get("XDG_CONFIG_HOME") or paths.home() / ".config") / "systemd" / "user"


def manager_env(env=None) -> dict:
    """`env` (default: the runner's allowlisted environment) with XDG_RUNTIME_DIR set when missing:
    the user manager's bus lives there. Agent shells lack it, and systemctl --user then fails."""
    from swarm import compat
    from swarm.supervisor.runner import _allowed_env
    env = _allowed_env(os.environ) if env is None else dict(env)
    if not env.get("XDG_RUNTIME_DIR"):
        d = Path(f"/run/user/{compat.uid()}")
        if d.is_dir():
            env["XDG_RUNTIME_DIR"] = str(d)
    return env


def _disabled() -> bool:
    return os.environ.get("SWARM_NO_SYSTEMD") == "1"


def legacy_files() -> list[Path]:
    """The legacy timer's files that exist: the unit files and the enablement link
    (timers.target.wants). Symlinks count even when dangling."""
    d = unit_dir()
    candidates = (d / LEGACY_TIMER, d / LEGACY_SERVICE, d / "timers.target.wants" / LEGACY_TIMER)
    return [p for p in candidates if os.path.lexists(p)]


def stop_legacy(run=None) -> str | None:
    """Disable and stop the legacy timer and its service through the user manager. Returns an error
    text, or None (also when there is no systemctl or SWARM_NO_SYSTEMD is set: nothing to stop)."""
    run = run or subprocess.run
    if _disabled() or shutil.which("systemctl") is None:
        return None
    for command in (["systemctl", "--user", "disable", "--now", LEGACY_TIMER],
                    ["systemctl", "--user", "stop", LEGACY_SERVICE]):
        try:
            result = run(command, capture_output=True, text=True, timeout=20, env=manager_env())
        except (OSError, subprocess.SubprocessError) as exc:
            return f"`{' '.join(command)}`: {type(exc).__name__}"
        if result.returncode != 0 and "not loaded" not in (result.stderr or "").lower() \
                and "does not exist" not in (result.stderr or "").lower():
            if "bus" in (result.stderr or "").lower():
                return f"`{' '.join(command)}`: no user systemd bus in this shell"
            return f"`{' '.join(command)}` exited {result.returncode}"
    return None


def remove_legacy(run=None):
    """Remove the legacy swarm-supervise timer and service when their files are there: stop and
    disable them (best effort), delete the files, reload the manager. A Step for bootstrap's output,
    or None when there was nothing to remove. The files are deleted even when the manager can't be
    asked (no user bus in this shell): with them gone nothing starts the timer again."""
    from swarm.bootstrap import Step
    files = legacy_files()
    if not files:
        return None
    run = run or subprocess.run
    err = stop_legacy(run)
    failed = []
    for path in files:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            failed.append(f"{path}: {type(exc).__name__}")
    if not _disabled() and shutil.which("systemctl") is not None:
        try:
            run(["systemctl", "--user", "daemon-reload"], capture_output=True, text=True, timeout=20,
                env=manager_env())
        except (OSError, subprocess.SubprocessError):
            pass
    if failed:
        return Step("legacy timer", "failed", f"could not remove the legacy {LEGACY_TIMER}: "
                                              f"{'; '.join(failed)}; delete those files by hand")
    detail = (f"removed the legacy {LEGACY_TIMER} and {LEGACY_SERVICE} (Swarm installs no system timers: "
              f"the session driver drives jobs)")
    if err:
        detail += f"; {err}: it stays loaded until `systemctl --user daemon-reload` or the next login"
    return Step("legacy timer", "changed", detail)


def linger(run=subprocess.run) -> bool | None:
    import getpass
    try:
        result = run(["loginctl", "show-user", getpass.getuser(), "-p", "Linger"],
                     capture_output=True, text=True, timeout=10, env=manager_env())
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    value = (result.stdout or "").strip()
    return True if value == "Linger=yes" else False if value == "Linger=no" else None
