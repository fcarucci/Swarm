"""Which edition of the plugin this is. The Codex directory edition (scripts/build-codex-directory)
ships a one-line file `EDITION` next to this module and has no hooks: the CLI then stops
describing hook behaviour (prompt tags, trust steps, delivery by hooks) that does not exist there.
Stdlib only."""
from __future__ import annotations

from pathlib import Path

DIRECTORY = "codex-directory"


def name() -> str | None:
    try:
        text = (Path(__file__).with_name("EDITION")).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return text or None


def is_directory() -> bool:
    return name() == DIRECTORY


def delivery() -> str:
    """How a queued post reaches the board: the hooks flush the spool in the full edition; here the
    next swarm command that runs outside the sandbox does."""
    return "the next time swarm runs outside the sandbox" if is_directory() else \
        "automatically within seconds by the swarm hooks"
