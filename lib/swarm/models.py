"""Default model per role. [models] mode: default = set the model only when the spawn
didn't pick one; enforce = always replace it; off = never touch it. [models.<host>] maps roles to
model names, passed through unchanged (the host validates them)."""
from __future__ import annotations

ROLES = ("worker", "verifier", "judge", "helper")


def role_of(prompt: str, spawned_by_member: bool, hint: str | None = None) -> str:
    """The role a spawn asks for: helper when a member spawns it, else its [swarm role: ...] tag,
    else `hint` (the host's reading where the tags are unreadable: Codex's task name)."""
    if spawned_by_member:
        return "helper"
    from swarm.cli import ROLE_TAG           # one tag parser: the hooks' own (lazy: stdlib-only import path)
    from swarm.hooks import _tag_of
    tag = _tag_of(prompt or "", ROLE_TAG) or hint
    return tag if tag in ("verifier", "judge") else "worker"


def mode(cfg: dict) -> str:
    m = str((cfg.get("models") or {}).get("mode", "default"))
    return m if m in ("default", "enforce", "off") else "default"


def model_for(cfg: dict, host: str, role: str) -> str | None:
    if mode(cfg) == "off":
        return None
    section = (cfg.get("models") or {}).get(host)
    if not isinstance(section, dict):
        return None
    m = section.get(role) or section.get("worker")
    return str(m) if m else None


def choose(cfg: dict, host: str, role: str, requested: str | None) -> str | None:
    m = model_for(cfg, host, role)
    if not m or (mode(cfg) == "default" and requested) or m == requested:
        return None
    return m


def spawn_hint(cfg: dict, host: str) -> str | None:
    pairs = [(r, model_for(cfg, host, r)) for r in ROLES]
    pairs = [(r, m) for r, m in pairs if m]
    if not pairs:
        return None
    return "Spawn with these models (spawn tool `model` field): " + ", ".join(f"{r}={m}" for r, m in pairs)
