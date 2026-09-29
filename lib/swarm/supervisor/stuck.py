"""Stuck detection: stuck_reason is pure; orchestrator_gone reads this machine's
markers only (files); close_stuck_owned acts, owner-only."""
from __future__ import annotations

import datetime as _dt
import time
from dataclasses import dataclass
from pathlib import Path

SUPERVISOR_NAME = "swarm supervisor"          # the name the supervisor's board posts carry
WAITING_PREFIX = "supervisor: restarting "    # the job's waiting_on while a restart is due


def effective_contact(last_contact: _dt.datetime, out, dead: _dt.timedelta) -> _dt.datetime | None:
    """The last contact to judge by: moved to the outage's recovery when an outage covers it;
    None while that outage is still open (nobody is stuck during an outage)."""
    if out is not None and out.affects(last_contact, dead):
        return None if out.recovered is None else max(last_contact, out.recovered)
    return last_contact


def stuck_reason(a, js, now: _dt.datetime, board_cfg: dict, sup: dict, out=None) -> str | None:
    if a.ended_at is not None:
        return None
    dead = _dt.timedelta(minutes=float(board_cfg["dead_minutes"]))
    silent = _dt.timedelta(minutes=float(sup["silent_minutes"]))
    contact = effective_contact(a.last_contact_at, out, dead)
    if contact is None:
        return None
    quiet = now - contact
    if a.current_tool is not None and a.status in ("idle", "dead") and quiet > dead:
        return "tool"
    if a.current_tool is None and quiet > dead:
        return "dead"
    posted_recently = a.last_post_at is not None and now - a.last_post_at <= silent
    waiting = bool(js.waiting_on) and not str(js.waiting_on).startswith(WAITING_PREFIX)
    if not waiting and quiet > silent and not posted_recently:
        return "silent"
    return None


def orchestrator_gone(cfg: dict, job: str, dead_minutes: float, now_ts: float | None = None) -> bool | None:
    """Whether the job's orchestrating session on this machine is gone: None if this machine has
    no orchestrator marker of the job (can't tell), True if every one's .seen heartbeat is
    missing or older than dead_minutes and the marker itself is older than that (a just
    activated job hasn't been seen yet), else False. Resume markers are not orchestrators."""
    from swarm.cli import _read_marker, orchestrator_seen_path
    mdir = Path(cfg["hook"]["marker_dir"]).expanduser()
    now_ts = time.time() if now_ts is None else now_ts
    limit = now_ts - float(dead_minutes) * 60
    found = False
    for p in sorted(mdir.glob("*.json")) if mdir.is_dir() else []:
        m = _read_marker(p)
        if "--resume-r" in p.name or not isinstance(m, dict) or "resume" in m or m.get("job") != job:
            continue
        found = True
        try:
            seen = orchestrator_seen_path(p).stat().st_mtime
        except FileNotFoundError:
            seen = None
        try:
            born = p.stat().st_mtime
        except FileNotFoundError:
            continue
        if (seen is not None and seen >= limit) or (seen is None and born >= limit):
            return False
    return True if found else None


@dataclass(frozen=True)
class Closed:
    job: str
    name: str
    agent_key: str
    reason: str


def _spent(deadline: float | None) -> bool:
    return deadline is not None and time.monotonic() > deadline


def find_stuck_owned(board, cfg: dict, now=None, deadline: float | None = None) -> list[tuple]:
    """(job status, agent, reason) for each of this machine+user's stuck agents on open,
    supervised jobs; [] when the supervisor is off. Read-only. Orphaned: the job's
    orchestrator on this machine is gone and every active agent of the job (anyone's) is dead
    (an agent in a timed-out tool call is a live process: its job isn't orphaned). Stops scanning once `deadline` (time.monotonic()) has passed:
    the jobs not scanned are the next sweep's."""
    from swarm import transcripts
    from swarm.supervisor import outage
    from swarm.supervisor.settings import enabled, settings
    if not enabled(cfg):
        return []
    sup, bcfg = settings(cfg), cfg["board"]
    out = outage.current()
    found = []
    for js in board.jobs(False):
        if _spent(deadline):
            break
        if not js.supervise or js.status != "active":
            continue
        now_j = now or board.now()
        active = board.agents(js.job, include_departed=False)
        reasons = {a.agent_key: stuck_reason(a, js, now_j, bcfg, sup, out) for a in active}
        # orphaned: every agent dead at once plus no .seen heartbeat. An agent in
        # a timed-out tool call is a live process, so the job isn't orphaned: it stays "tool"
        if active and all(r == "dead" for r in reasons.values()) and \
                orchestrator_gone(cfg, js.job, float(bcfg["dead_minutes"])):
            reasons = {k: "orphaned" for k in reasons}
        found += [(js, a, reasons[a.agent_key]) for a in active
                  if reasons.get(a.agent_key) and transcripts.owns(a, cfg)]
    return found


def close_stuck_owned(board, cfg: dict, deadline: float | None = None, now=None) -> list[Closed]:
    """Close what find_stuck_owned finds (sweep_jobs calls it): a compare-and-set on last_seen
    (any hook contact since the read keeps the agent), the final transcript, a post, the job's
    wait flag while a restart is due (only if the job wasn't waiting on anything), a log line.
    Close first, then capture: the compare-and-set is what proves the agent stuck, and a
    missing transcript never keeps a stuck agent open. A capture that fails (or finds no file
    yet) is logged and left pending; lost.finalize_pending_owned retries it at the next sweeps."""
    from swarm.board import STUCK_PREFIX
    from swarm.supervisor import lost
    from swarm.supervisor.settings import log
    closed: list[Closed] = []
    flagged: set[str] = set()     # jobs whose wait flag this sweep set (js is the pre-sweep read)
    for js, a, why in find_stuck_owned(board, cfg, now, deadline):
        if _spent(deadline):   # the hook's budget is spent: the next sweep closes the rest
            break
        if not board.close_agent(a.agent_key, STUCK_PREFIX + why, seen_before=a.last_contact_at):
            continue
        try:
            lost.capture_final(board, cfg, js, a, deadline)
        except Exception as exc:
            log(f"transcript of {a.name} on {js.job} not captured: {type(exc).__name__}")
        board.post(js.job, SUPERVISOR_NAME,
                   f"closed {a.name}: stuck ({why}); the supervisor restarts it if its budget allows")
        if not js.waiting_on and js.job not in flagged:
            board.set_waiting(js.job, WAITING_PREFIX + a.name)
            flagged.add(js.job)
        log(f"closed {a.name} ({a.agent_key}) on {js.job}: {STUCK_PREFIX}{why}")
        closed.append(Closed(js.job, a.name, a.agent_key, why))
    return closed
