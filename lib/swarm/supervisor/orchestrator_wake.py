"""The orchestrator wake (spec requirement 2, slice b): an orchestrating session that activated an open job of this
host keeps exactly one `swarm await --job J` running for it, through its own background Bash (Claude Code:
run_in_background, whose exit is a task notification that wakes an idle session).

Why the hooks start it and not the session driver (supervisor/session_driver.py): a detached driver process cannot
start a background Bash task of the Claude session, and only such a task's exit wakes the session. So the
orchestrator's own hook (the path that lists its pending events) names every owned job whose wake is NOT ARMED,
on each tool call, until the agent starts the await. Drive jobs keep their driver and are never listed here.

Armed means: the await of (session, job) holds the lock `orchestrator-<sha256>.lock` for its whole life
(`hold`), the same kind of lock the session driver uses. A second await finds it held and exits, so no second
instance runs, and a finished await releases it, so the next tool call re-arms. Nothing is installed, and with no
active job nothing here reads the board or takes a lock.

The await (supervisor-side: `swarm await`, slice a) calls `hold(session, job)` before it blocks and keeps the
descriptor open until it exits."""
from __future__ import annotations

import hashlib

from swarm.supervisor import session_driver

PREFIX = "orchestrator-"


def lock_name(session: str, job: str) -> str:
    digest = hashlib.sha256(f"{session}\0{job}".encode("utf-8", "surrogatepass")).hexdigest()
    return f"{PREFIX}{digest}.lock"


def hold(session: str, job: str):
    """The lock of (session, job), taken for the caller's life: its descriptor, or None when an await of the
    same session and job already runs (the caller exits at once: one instance)."""
    return session_driver.take_lock(lock_name(session, job))


def armed(session: str, job: str) -> bool:
    """Whether an await of (session, job) runs now (a shared, non-blocking probe)."""
    return session_driver.held(lock_name(session, job))


def owned_pairs(cfg: dict) -> list[tuple[str, str]]:
    """(session, job) for every open job of this host that a session activated, except drive jobs (their
    session driver wakes them). Sorted. Reads no ledger and takes no lock when no job is active."""
    from swarm.jobmarkers import _markers
    pairs = {(m["session_id"], m["job"]) for m in _markers(cfg)
             if m.get("session_id") and "resume" not in m and m.get("job")}
    if not pairs:
        return []
    drive = session_driver.unfinished(session_driver._store_key(cfg))
    if drive is None:   # the drive ledgers are unreadable now: unknown, so arm nothing (never a second driver)
        return []
    drive_jobs = {led.job for led in drive}
    return sorted((s, j) for s, j in pairs if j not in drive_jobs)


def pending_lines(cfg: dict, session: str) -> list[str]:
    """The hook's lines for `session`: one per owned job whose wake is NOT ARMED."""
    return [f"[swarm] orchestrator wake NOT ARMED for {job}: run `swarm await --job {job}` as a background Bash "
            f"command (run_in_background: true). It exits when something needs you; start it again after each exit."
            for s, job in owned_pairs(cfg) if s == session and not armed(s, job)]
