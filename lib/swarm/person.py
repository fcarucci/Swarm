"""Who may run a command: everyone.

The owner removed person-only command gating on 2026-10-17. Until then `deactivate`, `drive budget`,
`ack` and the like were refused inside agent hosts and enrolled sessions, which left jobs nobody could
close or acknowledge. This module keeps the names the rest of the code imports (require, caller,
Caller, PersonRefused, forget, host_pid, agent_host, codex_session, session_of, broker_attested,
broker_attest_name) and answers "allowed" for every caller. PersonRefused is never raised here; it stays
importable for the handlers that still catch it.
"""
from __future__ import annotations

import dataclasses
import os


class PersonRefused(RuntimeError):
    """Kept for the `except person.PersonRefused` sites; nothing raises it any more."""


@dataclasses.dataclass(frozen=True)
class Caller:
    """What a hook grant used to describe: the launch and whether it was the job's requester session."""
    launch: str | None = None
    requester: bool = True


HOST_VARS = ("CLAUDE_CODE_SESSION_ID", "CLAUDE_AGENT_ID", "CODEX_SESSION_ID", "CODEX_THREAD_ID")


def broker_attest_name(token: str) -> str:
    return f"attest-{token}"


def broker_attested(env=None, attest_dir=None) -> int | None:
    """No broker attestation is needed: nothing is gated on it."""
    return None


def host_pid(parent_of=None, pid: int | None = None, max_hops: int = 40) -> int | None:
    """The process that identifies this command for the audit receipts: the caller's own pid (grants no
    longer bind to an agent host's process)."""
    return pid if pid is not None else os.getpid()


def forget() -> None:
    """Nothing is cached per command any more."""


def caller(board_key: str, job: str, led, args, now: float | None = None) -> Caller | None:
    """Every command runs as the requester."""
    return Caller(launch=None, requester=True)


def codex_session(env, ancestor=None) -> bool:
    env = os.environ if env is None else env
    return bool(env.get("CODEX_SESSION_ID") or env.get("CODEX_THREAD_ID"))


def agent_host(env, ancestor=None) -> str | None:
    """Agent hosts are not refused anything: None for everyone (the launch sandbox is unaffected)."""
    return None


def session_of(env) -> str | None:
    env = os.environ if env is None else env
    return env.get("CLAUDE_CODE_SESSION_ID") or env.get("CODEX_SESSION_ID") or None


def require(action: str, job: str, *, board_key: str, who: Caller | None, env=None, confirm=None,
            ancestor=None) -> str:
    """Every caller may act. Returns "requester"; the parameters are kept for the callers and tests."""
    return "requester"
