"""Default model per role. [models] mode: default = set the model only when the spawn
didn't pick one; enforce = always replace it; off = never touch it. [models.<host>] maps roles to
model names, passed through unchanged (the host validates them)."""
from __future__ import annotations

from swarm import roles

ROLES = ("worker", "verifier", "judge", "helper")


def role_of(prompt: str, spawned_by_member: bool, hint: str | None = None) -> str:
    """Explicit role, otherwise helper for a member's child or worker for a root's child."""
    tag = roles.from_prompt(prompt or "") or hint
    return tag if roles.valid_name(tag) else "helper" if spawned_by_member else "worker"


def mode(cfg: dict) -> str:
    m = str((cfg.get("models") or {}).get("mode", "default"))
    return m if m in ("default", "enforce", "off") else "default"


def model_for(cfg: dict, host: str, role: str, *, fallback: str = "worker") -> str | None:
    if mode(cfg) == "off":
        return None
    section = (cfg.get("models") or {}).get(host)
    if not isinstance(section, dict):
        return None
    m = section.get(role) or section.get(fallback) or section.get("worker")
    return str(m) if m else None


def choose(cfg: dict, host: str, role: str, requested: str | None, *, fallback: str = "worker") -> str | None:
    m = model_for(cfg, host, role, fallback=fallback)
    if not m or (mode(cfg) == "default" and requested) or m == requested:
        return None
    return m


def spawn_hint(cfg: dict, host: str) -> str | None:
    section = (cfg.get("models") or {}).get(host)
    custom = sorted(r for r in section if roles.valid_name(r) and r not in ROLES) if isinstance(section, dict) else []
    pairs = [(r, model_for(cfg, host, r)) for r in (*ROLES, *custom)]
    pairs = [(r, m) for r, m in pairs if m]
    if not pairs:
        return None
    return "Spawn with these models (spawn tool `model` field): " + ", ".join(f"{r}={m}" for r, m in pairs)
