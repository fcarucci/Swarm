"""Where things are. PLUGIN_ROOT is the installed plugin (or checkout); everything that must
survive a plugin update lives outside it, under the user's home."""
from __future__ import annotations

import json
import os
from pathlib import Path

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


def host_dir() -> Path:
    """Host-only files (hook logs, stamps, notices, enrolment records): a 0700 directory outside
    every sandbox writable root, reached through safefs.open_base(strict_mode=0o700)."""
    return share_dir() / "host"


def venv_dir() -> Path:
    return Path(os.environ["SWARM_VENV"]) if os.environ.get("SWARM_VENV") else share_dir() / "venv"


def launcher_path() -> Path:
    return home() / ".local" / "bin" / "swarm"


def agent_bin() -> Path:
    """The swarm command given to agents and the orchestrator: this plugin's own bin/swarm. It
    exists whenever a hook or the skill runs, which ~/.local/bin/swarm (written by the detached
    bootstrap, a convenience for the user's terminal) may not yet."""
    return PLUGIN_ROOT / "bin" / "swarm"


def config_path() -> Path:
    return Path(os.environ.get("SWARM_CONFIG") or "~/.config/swarm/config.toml").expanduser()
