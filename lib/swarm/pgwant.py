"""Whether the configured board is a Postgres board, for the launchers that decide what to pip install.

Stdlib only and no import of the rest of the package, so bin/swarm can run it with the system
python3 (`python3 -I pgwant.py [ARGS...]`, exit 0 = Postgres) before any venv exists, and
swarm.winlaunch can import it. The rule is cli.load_config's: Postgres when [board] backend is
"postgres", or when the config has a non-empty [database] section and sets no backend (an install
from before the file-board default). A missing or unreadable config is not Postgres.

    pgwant.py [--config PATH | --config=PATH ...]    (else $SWARM_CONFIG, else ~/.config/swarm/config.toml)
"""
from __future__ import annotations

import os
import sys
import tomllib
from pathlib import Path


def config_arg(argv) -> str | None:
    """The value of the last --config in argv (argparse semantics), else None."""
    found = None
    args = list(argv)
    for i, a in enumerate(args):
        if a == "--config" and i + 1 < len(args):
            found = args[i + 1]
        elif a.startswith("--config="):
            found = a[len("--config="):]
    return found


def default_config() -> Path:
    return Path(os.environ.get("SWARM_CONFIG") or "~/.config/swarm/config.toml").expanduser()


def wanted(path) -> bool:
    try:
        with open(path, "rb") as fh:
            user = tomllib.load(fh)
    except (OSError, ValueError):
        return False
    board = user.get("board")
    backend = board.get("backend") if isinstance(board, dict) else None
    if backend:
        return backend == "postgres"
    db = user.get("database")
    return isinstance(db, dict) and bool(db)


def main(argv) -> int:
    arg = config_arg(argv)
    return 0 if wanted(Path(arg).expanduser() if arg else default_config()) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
