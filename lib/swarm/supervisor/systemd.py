"""The supervisor's private systemd user units."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

SERVICE = "swarm-supervise.service"
TIMER = "swarm-supervise.timer"

# The PATH the service unit sets (Environment=PATH= below, %h resolved): doctor's harness check
# must look a binary up on this same PATH, or a pass that finds `claude`/`codex` in doctor's shell
# can still fail in the timer, which never gets the user's shell rc files.
SERVICE_PATH_DIRS = (".local/bin", "/usr/local/bin", "/usr/bin", "/bin")


def service_path() -> str:
    """The resolved PATH the service unit's `Environment=PATH=...` sets, for shutil.which."""
    from swarm import paths
    home = paths.home()
    return os.pathsep.join(str(home / d) if not d.startswith("/") else d for d in SERVICE_PATH_DIRS)


def _pct_escape(value: str) -> str:
    """systemd unit files use % to introduce specifiers (%h, %%, ...); a literal % must be
    doubled or a path containing one silently corrupts the unit."""
    return value.replace("%", "%%")


def _quote_if_needed(value: str) -> str:
    """C-style double-quote a value for a systemd unit line (ExecStart= argv splitting,
    Environment= assignments) when it contains whitespace; systemd would otherwise split it into
    more than one argument/assignment."""
    if not re.search(r"\s", value):
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def unit_dir() -> Path:
    from swarm import paths
    return Path(os.environ.get("XDG_CONFIG_HOME") or paths.home() / ".config") / "systemd" / "user"


def service_text(launcher: Path, config: Path) -> str:
    exec_start = _quote_if_needed(_pct_escape(str(launcher)))
    swarm_config = _quote_if_needed(f"SWARM_CONFIG={_pct_escape(str(config))}")
    return ("# Written by `swarm bootstrap` ([supervise] enabled = true): one supervisor pass.\n"
            "[Unit]\nDescription=swarm supervisor: close stuck agents, restart them headless\n\n"
            "[Service]\nType=oneshot\n"
            f"ExecStart={exec_start} supervise\n"
            f"Environment={swarm_config}\n"
            "Environment=PATH=%h/.local/bin:/usr/local/bin:/usr/bin:/bin\n"
            "# the replacements' runners outlive this oneshot\nKillMode=process\nTimeoutStartSec=110\n")


def timer_text(minutes: int) -> str:
    return ("# Written by `swarm bootstrap` ([supervise] enabled = true).\n"
            "[Unit]\nDescription=swarm supervisor timer\n\n"
            f"[Timer]\nOnBootSec=2min\nOnUnitActiveSec={int(minutes)}min\nAccuracySec=15s\n\n"
            "[Install]\nWantedBy=timers.target\n")


def _disabled() -> bool:
    return os.environ.get("SWARM_NO_SYSTEMD") == "1"


def install(config: Path, minutes: int, run=subprocess.run):
    from swarm import paths
    from swarm.bootstrap import Step
    from swarm.safefile import write_preserving
    if _disabled():
        return Step("supervisor", "skipped", "SWARM_NO_SYSTEMD set")
    if shutil.which("systemctl") is None:
        return Step("supervisor", "manual", f"no systemctl here: run `{paths.agent_bin()} supervise` "
                                            f"every {minutes} minutes (cron)")
    directory = unit_dir()
    directory.mkdir(parents=True, exist_ok=True)
    changed = False
    for name, content in ((SERVICE, service_text(paths.agent_bin(), config)),
                          (TIMER, timer_text(minutes))):
        path = directory / name
        if not path.exists() or path.read_text() != content:
            write_preserving(path, content, mode=0o600)
            changed = True
    commands = ([["systemctl", "--user", "daemon-reload"]] if changed else []) + [
        ["systemctl", "--user", "enable", "--now", TIMER]]
    for command in commands:
        try:
            result = run(command, capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.SubprocessError) as exc:
            return Step("supervisor", "failed", f"`{' '.join(command)}`: {type(exc).__name__}")
        if result.returncode != 0:
            if "bus" in (result.stderr or "").lower():
                return Step("supervisor", "failed", "no user systemd manager (fix: loginctl enable-linger $USER)")
            return Step("supervisor", "failed", f"`{' '.join(command)}` exited {result.returncode}")
    return Step("supervisor", "changed" if changed else "ok", f"{TIMER}: every {minutes} min")


def timer_state(run=subprocess.run) -> dict:
    try:
        result = run(["systemctl", "--user", "show", TIMER, "-p", "ActiveState", "-p", "UnitFileState"],
                     capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return {}
    if result.returncode != 0:
        return {}
    return dict(line.split("=", 1) for line in (result.stdout or "").splitlines() if "=" in line)


def linger(run=subprocess.run) -> bool | None:
    import getpass
    try:
        result = run(["loginctl", "show-user", getpass.getuser(), "-p", "Linger"],
                     capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    value = (result.stdout or "").strip()
    return True if value == "Linger=yes" else False if value == "Linger=no" else None
