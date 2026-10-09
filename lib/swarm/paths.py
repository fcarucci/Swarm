"""Where things are. PLUGIN_ROOT is the installed plugin (or checkout); everything that must
survive a plugin update lives outside it, under the user's home."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"

LIB_DIR = Path(__file__).resolve().parent.parent
PLUGIN_ROOT = LIB_DIR.parent
DATA_DIR = PLUGIN_ROOT / "data"


def plugin_version(root: Path = PLUGIN_ROOT) -> str:
    """The version in <root>/.claude-plugin/plugin.json, or <root>/.codex-plugin/plugin.json if
    that one is missing or unreadable (a Codex-only plugin cache tree has no .claude-plugin dir);
    "0" if neither is readable. root: this plugin."""
    for manifest in (".claude-plugin", ".codex-plugin"):
        try:
            return str(json.loads((root / manifest / "plugin.json").read_text())["version"])
        except (OSError, ValueError, KeyError):
            continue
    return "0"


def home() -> Path:
    return Path(os.path.expanduser("~"))


def state_dir() -> Path:
    return home() / ".local" / "state" / "swarm"


def share_dir() -> Path:
    return home() / ".local" / "share" / "swarm"


def pycache_dir() -> Path:
    """Where bin/swarm and bin/swarm-hook keep Python bytecode (SWARM_PYCACHE overrides)."""
    return Path(os.environ["SWARM_PYCACHE"]) if os.environ.get("SWARM_PYCACHE") else share_dir() / "pyc"


def host_dir() -> Path:
    """Host-only files (hook logs, stamps, notices, enrolment records): a 0700 directory outside
    every sandbox writable root, reached through safefs.open_base(strict_mode=0o700)."""
    return share_dir() / "host"


def venv_dir() -> Path:
    return Path(os.environ["SWARM_VENV"]) if os.environ.get("SWARM_VENV") else share_dir() / "venv"


def venv_python(venv: Path | None = None) -> Path:
    """The interpreter inside the venv: <venv>/bin/python, <venv>/Scripts/python.exe on Windows.
    (Both names are probed on Windows: a venv made by an MSYS/Git Bash python has bin/.)"""
    v = venv if venv is not None else venv_dir()
    if IS_WINDOWS:
        for cand in (v / "Scripts" / "python.exe", v / "bin" / "python.exe", v / "bin" / "python"):
            if cand.exists():
                return cand
        return v / "Scripts" / "python.exe"
    return v / "bin" / "python"


def launcher_path() -> Path:
    """The user-terminal launcher: ~/.local/bin/swarm (a sh script); ~/.local/bin/swarm.cmd on
    Windows (cmd.exe and PowerShell resolve a bare `swarm` to it when ~/.local/bin is on PATH)."""
    return home() / ".local" / "bin" / ("swarm.cmd" if IS_WINDOWS else "swarm")


def agent_bin() -> Path:
    """The swarm command given to agents and the orchestrator: this plugin's own bin/swarm. It
    exists whenever a hook or the skill runs, which ~/.local/bin/swarm (written by the detached
    bootstrap, a convenience for the user's terminal) may not yet. On Windows it is bin/swarm.cmd,
    runnable from cmd, PowerShell and Git Bash alike (the extensionless sh script is not)."""
    if IS_WINDOWS:
        return PLUGIN_ROOT / "bin" / "swarm.cmd"
    return PLUGIN_ROOT / "bin" / "swarm"


def config_path() -> Path:
    return Path(os.environ.get("SWARM_CONFIG") or "~/.config/swarm/config.toml").expanduser()


def private_dir(d: Path) -> bool:
    """Whether `d` is this user's own directory with no group or other access, not a symlink and
    without an ACL: the launchers' test for the bytecode cache (bin/swarm: -O, ! -L, mode
    d???------ with no ACL mark). False when it is missing or can't be checked. Never raises."""
    import stat
    try:
        st = os.lstat(d)
        ok = stat.S_ISDIR(st.st_mode) and st.st_uid == os.getuid() and not st.st_mode & 0o077
        if ok and hasattr(os, "listxattr"):
            try:
                ok = not any(a.startswith("system.posix_acl") for a in os.listxattr(d, follow_symlinks=False))
            except OSError:
                pass
    except (OSError, AttributeError):
        return False
    return ok


def pycache_env(environ=os.environ) -> dict:
    """What bin/swarm sets for Python's bytecode, for the processes Python itself starts (the events
    listener, a supervisor runner): {"PYTHONPYCACHEPREFIX": dir} when the cache dir ($SWARM_PYCACHE,
    else ~/.local/share/swarm/pyc) is, or can be made, this user's own directory with no group or
    other access, not a symlink and without an ACL (private_dir); else {"PYTHONDONTWRITEBYTECODE":
    "1"} (nothing is cached, nothing written). Never raises."""
    d = Path(environ.get("SWARM_PYCACHE") or Path(environ.get("HOME") or Path.home()) / ".local/share/swarm/pyc")
    try:
        if not os.path.lexists(d):
            d.mkdir(mode=0o700, parents=True)
    except OSError:
        return {"PYTHONDONTWRITEBYTECODE": "1"}
    return {"PYTHONPYCACHEPREFIX": str(d)} if private_dir(d) else {"PYTHONDONTWRITEBYTECODE": "1"}
