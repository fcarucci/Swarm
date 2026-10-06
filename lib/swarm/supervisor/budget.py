"""Pure replacement budgets and backoff."""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass

from swarm.board import AgentStatus, Restart
from swarm.board.base import restart_overlaps, restart_reserved_minutes


@dataclass(frozen=True)
class Decision:
    go: bool
    why: str
    attempt: int = 0
    minutes: float = 0.0
    cap: str | None = None
    not_before: _dt.datetime | None = None


def charged_minutes(r: Restart, now: _dt.datetime) -> float:
    if r.outcome == "refused":
        return 0.0
    end = r.ended_at or now
    return max(0.0, min((end - r.at).total_seconds() / 60, float(r.minutes_cap)))


def reserved_minutes(r: Restart, now: _dt.datetime) -> float:
    """What a restart holds of the minute budgets when deciding: a running one its whole cap (it
    may use all of it, and two launches in a row must not both count on the same minutes), an
    ended one what it used (charged_minutes)."""
    return restart_reserved_minutes(r)


def day_rows(rows: list[Restart], day_start: _dt.datetime) -> list[Restart]:
    """The restarts that count toward the day beginning at day_start: every
    one that ran at any time since then, including one started before and still running (it
    holds its whole cap) or ended after it."""
    return [r for r in rows if restart_overlaps(r, day_start)]


def host_running(rows: list[Restart]) -> int:
    """Replacements running on a host: its open restart rows, whichever OS user started them
    and whether or not their runner is alive."""
    return sum(1 for r in rows if r.ended_at is None)


def lineage_root(agent_key: str, by_key: dict[str, AgentStatus]) -> str:
    seen = set()
    key = agent_key
    while key in by_key and by_key[key].resume_of and key not in seen:
        seen.add(key)
        key = by_key[key].resume_of
    return key


def decide(sup: dict, *, lineage: str, job_restarts: list[Restart],
           host_restarts: list[Restart], closed_at: _dt.datetime, now: _dt.datetime,
           running: int, outage_started: _dt.datetime | None = None) -> Decision:
    cap_n = int(sup["max_concurrent_replacements"])
    if running >= cap_n:
        return Decision(False, f"{running}/{cap_n} replacements already running on this host")

    cutoff = now - _dt.timedelta(hours=24)
    mine = sorted((r for r in job_restarts if r.agent_key == lineage and r.at > cutoff),
                  key=lambda r: r.id)
    if outage_started is not None and any(
            r.reason == "outage" and r.at >= outage_started for r in mine):
        return Decision(False, "already restarted once after the board outage", cap="outage")

    per_agent, per_job = int(sup["max_restarts_per_agent"]), int(sup["max_restarts_per_job"])
    if len(mine) >= per_agent:
        return Decision(False, f"restart budget spent ({len(mine)}/{per_agent} restarts of this "
                               f"agent; [supervise] max_restarts_per_agent)", cap="agent")
    if len(job_restarts) >= per_job:
        return Decision(False, f"the job's restart budget is spent ({len(job_restarts)}/{per_job}; "
                               f"[supervise] max_restarts_per_job)", cap="job")

    job_used = sum(reserved_minutes(r, now) for r in job_restarts)
    day_used = sum(reserved_minutes(r, now) for r in host_restarts)
    job_left = float(sup["max_restart_minutes"]) - job_used
    day_left = float(sup["daily_restart_minutes"]) - day_used
    minutes = min(float(sup["max_minutes"]), job_left, day_left)
    if minutes < float(sup["min_minutes"]):
        if day_left <= job_left:
            return Decision(False, f"today's restart minutes on this host are spent ({day_used:.0f}/"
                                   f"{sup['daily_restart_minutes']}; [supervise] "
                                   "daily_restart_minutes)", cap="daily_minutes")
        return Decision(False, f"the job's restart minutes are spent ({job_used:.0f}/"
                               f"{sup['max_restart_minutes']}; [supervise] "
                               "max_restart_minutes)", cap="job_minutes")

    if outage_started is None:
        steps = sup["backoff_minutes"]
        wait = float(steps[min(len(mine), len(steps) - 1)])
        ref = closed_at
        if mine:
            last = mine[-1]
            ref = max(ref, last.ended_at or last.at)
        not_before = ref + _dt.timedelta(minutes=wait)
        if now < not_before:
            return Decision(False, f"backoff: next try at {not_before.astimezone():%H:%M}",
                            not_before=not_before)

    return Decision(True, "ok", attempt=len(mine) + 1, minutes=minutes)
