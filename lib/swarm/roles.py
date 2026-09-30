"""Shared role identifiers. Only judge and verifier carry built-in behavior."""
from __future__ import annotations

import re

PROTECTED = frozenset(("judge", "verifier"))
_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}")


def valid_name(value: object) -> bool:
    """Portable in prompt tags, TOML keys and Codex's role__task names."""
    return isinstance(value, str) and _NAME.fullmatch(value) is not None and "__" not in value


def custom_role(value: object) -> str | None:
    return value if valid_name(value) and value not in PROTECTED else None


def from_prompt(prompt: str) -> str | None:
    for line in prompt.splitlines():
        line = line.strip()
        if line.startswith("[swarm role:") and line.endswith("]"):
            value = line[len("[swarm role:"):-1].strip()
            return value if valid_name(value) else None
    return None


def from_task_name(task_name: str) -> str | None:
    """Explicit role__task, or the original verifier*/judge* prefix convention."""
    name = (task_name or "").strip()
    if "__" in name:
        role, task = name.split("__", 1)
        return role if valid_name(role) and task.strip() else None
    for role in ("verifier", "judge"):
        if name.lower().startswith(role):
            return role
    return None
