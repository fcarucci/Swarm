"""An addressed post longer than the board's message cap is delivered whole: the full text goes
to a file under the swarm data dir (never /tmp) and the message carries a head plus the path.
The cap itself is the board's (stored, enforced by every backend), so broadcasts stay capped."""
from __future__ import annotations

import uuid
from pathlib import Path

from swarm import paths
from swarm.board.base import normalize_message
from swarm.textsafe import strip_controls


def attach_overflow(job: str, message: str, cap: int) -> str:
    """`message` unchanged when it fits `cap` once normalised; else its head plus the path of a
    file that holds all of it (newlines kept, terminal controls dropped)."""
    text, truncated = normalize_message(message, cap)
    if not truncated:
        return message
    folder = paths.share_dir() / "posts" / job
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = folder / f"{uuid.uuid4().hex[:12]}.txt"
    path.write_text(strip_controls(message, keep_newlines=True) + "\n")
    path.chmod(0o600)
    tail = f" … full text: {path}"
    head, _ = normalize_message(message, max(cap - len(tail), 1))
    return head.rstrip("…") + tail
