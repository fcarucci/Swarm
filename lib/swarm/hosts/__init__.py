"""The agent hosts: get(name) and detection."""
from __future__ import annotations

import importlib
import os
import subprocess
from typing import Callable, Mapping

from .base import Host, SpawnCall  # noqa: F401

HOST_NAMES = ("claude", "codex")
_CLASSES: dict[str, tuple[str, str]] = {"claude": ("claude", "ClaudeHost"), "codex": ("codex", "CodexHost")}
_INSTANCES: dict[str, Host] = {}


def get(name: str) -> Host:
    """The adapter for a registered host. _CLASSES is the registry, checked before the instance
    cache: a host removed from it (tests) is unregistered even if an instance was cached."""
    if name not in _CLASSES:
        raise KeyError(name)
    if name not in _INSTANCES:
        module, cls = _CLASSES[name]
        _INSTANCES[name] = getattr(importlib.import_module(f".{module}", __name__), cls)()
    return _INSTANCES[name]


def detect_hook_host(flag: str | None, payload: dict, env: Mapping[str, str]) -> str:
    """The host a hook runs in: the --host flag from the plugin's hooks file; else Codex if the
    payload has turn_id (every Codex event the swarm uses has one, Claude's never do) or the env
    has PLUGIN_ROOT (Codex-only name). CLAUDE_PLUGIN_ROOT and CLAUDECODE prove nothing: Codex
    sets the first for compatibility and inherits the second from a Claude shell."""
    if flag in HOST_NAMES:
        return flag
    if "turn_id" in payload or env.get("PLUGIN_ROOT"):
        return "codex"
    return "claude"


def _classify(argv: list[str]) -> str | None:
    for arg in argv[:2]:
        base = os.path.basename(arg)
        if base.startswith("codex"):
            return "codex"
        if base == "claude" or "claude-code" in arg:
            return "claude"
    return None


def _proc_parent(pid: int) -> tuple[int, list[str]] | None:
    """(ppid, argv) of pid from /proc, or from `ps` where there is no /proc (macOS)."""
    try:
        stat = open(f"/proc/{pid}/stat").read()
        ppid = int(stat[stat.rindex(")") + 2:].split()[1])
        argv = open(f"/proc/{pid}/cmdline", "rb").read().split(b"\0")
        return ppid, [a.decode(errors="replace") for a in argv if a]
    except FileNotFoundError:
        pass
    except (OSError, ValueError):
        return None
    try:
        out = subprocess.run(["ps", "-o", "ppid=,args=", "-p", str(pid)], capture_output=True,
                             text=True, timeout=2).stdout.strip()
        ppid, _, args = out.partition(" ")
        return int(ppid), args.split()
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def nearest_ancestor_host(parent_of: Callable, pid: int, max_hops: int = 40) -> str | None:
    for _ in range(max_hops):
        if pid <= 1:
            return None
        info = parent_of(pid)
        if not info:
            return None
        ppid, argv = info
        found = _classify(argv)
        if found:
            return found
        pid = ppid
    return None


def detect_cli_host(env: Mapping[str, str], parent_of: Callable | None = None,
                    pid: int | None = None) -> str | None:
    """The host whose shell runs this CLI: SWARM_HOST, else the one host whose variables are set,
    else (both set: one host runs inside the other) the nearest claude/codex ancestor process;
    None for a plain terminal or an undecidable tree."""
    forced = env.get("SWARM_HOST")
    if forced in HOST_NAMES:
        return forced
    codex = bool(env.get("CODEX_THREAD_ID") or env.get("CODEX_SESSION_ID"))
    claude = bool(env.get("CLAUDE_CODE_SESSION_ID") or env.get("CLAUDE_CODE_CHILD_SESSION"))
    if codex != claude:
        return "codex" if codex else "claude"
    if not codex:
        return None
    return nearest_ancestor_host(parent_of or _proc_parent, pid if pid is not None else os.getppid())


def cli_session_id(env: Mapping[str, str]) -> str | None:
    """The calling host session's id for `swarm activate` (None: unknown)."""
    name = detect_cli_host(env)
    try:
        return get(name).cli_session_id(env) if name else None
    except KeyError:
        return None
