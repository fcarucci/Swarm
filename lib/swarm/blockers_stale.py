"""Is a blocker's subject already resolved? A helper for `swarm blockers` and `swarm await`."""
from __future__ import annotations

import re

ARTIFACT = re.compile(r"([A-Za-z0-9][A-Za-z0-9._/-]*)@([0-9a-f]{7,40})\b")
PR = re.compile(r"\bPR\s*#(\d+)", re.IGNORECASE)
LANDED = re.compile(r"\b(landed|merged|integrated)\b", re.IGNORECASE)


def stale_reason(blocker, board) -> str | None:
    """Why `blocker` (open) looks resolved already, or None. Resolved subject: its PR landed or
    merged, its artifact was integrated, or a later message names the same branch with another
    sha (superseded). Only messages after the blocker was opened count."""
    if blocker.state != "open":
        return None
    arts = {m.group(1): m.group(2) for m in ARTIFACT.finditer(blocker.reason)}
    prs = set(PR.findall(blocker.reason))
    if not arts and not prs:
        return None
    for m in board.messages_after(0, blocker.job):
        if m.created_at <= blocker.created_at:
            continue
        text = m.message
        if LANDED.search(text):
            for pr in prs:
                if re.search(rf"\bPR\s*#{pr}\b", text, re.IGNORECASE):
                    return f"PR #{pr} landed (#{m.id})"
            for branch, sha in arts.items():
                if any(a.startswith(sha) or sha.startswith(a) for b, a in ARTIFACT.findall(text) if b == branch):
                    return f"{branch}@{sha[:8]} integrated (#{m.id})"
        for branch, sha in ARTIFACT.findall(text):
            old = arts.get(branch)
            if old and not (sha.startswith(old) or old.startswith(sha)):
                return f"{branch} superseded by @{sha[:8]} (#{m.id})"
    return None
