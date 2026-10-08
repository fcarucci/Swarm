"""The agent title: a short, optional display label (`EL`, `PM`, `Eng: board view`) kept apart from
the role. A subagent's spawn prompt sets it with a `[swarm title: <text>]` line (read like the
`[swarm job: ...]` and `[swarm role: ...]` lines); `swarm join --title` and `swarm title` set it
too. Display only: nothing keys off it. The stored form is board.base.clean_title."""
from __future__ import annotations

from swarm.board.base import TITLE_MAX, clean_title  # noqa: F401  (re-exported)

TITLE_TAG = "[swarm title:"


def from_prompt(prompt: str) -> str | None:
    """The cleaned value of the first `[swarm title: <text>]` line of a spawn prompt (a whole line,
    like the other tags), or None when there is none or it is empty."""
    for line in (prompt or "").splitlines():
        s = line.strip()
        if s.startswith(TITLE_TAG) and s.endswith("]"):
            return clean_title(s[len(TITLE_TAG):-1])
    return None
