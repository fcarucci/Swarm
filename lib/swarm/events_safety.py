"""The safety nets: what the hourly cron check used to do, as a core supervision check.

Each pass of `swarm supervise` (and `swarm events check`) asks, per open job:
  * is the listener up? (only when [events] enabled)
  * is the orchestrator's `swarm event wait` armed? (the waiter's heartbeat, job_data
    events.waiter.<slug>, a UTC ISO time refreshed by Board.wait_event; stale or missing = not armed.
    Only checked for a job that has pending orchestrator events or live agents)
  * has the job stalled? (no activity for [events] stall_minutes)
  * are there events for the orchestrator unacked for [events] unacked_minutes, or messages addressed
    to it unread for as long?

Problems go out as ONE SWARM-ALERT event per job and pass (Board.post_event, deduped per hour by
its key; a board without events gets a plain board notice instead). The checks are deterministic. Where a
model reads something (optional [events] triage = true: one Haiku call turns the findings into one line),
the model is [events] model from the job's own events.model data or the global config: never inherited from
the orchestrator or the environment.
"""
from __future__ import annotations

import dataclasses
import json
import datetime as _dt
import subprocess
import time

from swarm import events_listener

WATCHER_NAME = "swarm-watch"


@dataclasses.dataclass(frozen=True)
class Problem:
    job: str | None
    code: str      # listener-down | waiter-stale | stalled | unacked | unread
    text: str


def _now(now) -> _dt.datetime:
    if now is None:
        return _dt.datetime.now(_dt.timezone.utc)
    if isinstance(now, (int, float)):
        return _dt.datetime.fromtimestamp(now, _dt.timezone.utc)
    return now if now.tzinfo else now.replace(tzinfo=_dt.timezone.utc)


def _aware(ts):
    if ts is None:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=_dt.timezone.utc)


def _mins(a: _dt.datetime, b: _dt.datetime) -> int:
    return int((a - b).total_seconds() // 60)


def waiter_age(board, job: str, now: _dt.datetime, targets=None) -> float | None:
    """Seconds since the freshest events.waiter.* heartbeat of the job, None if none was ever set."""
    from swarm.board.base import event_waiter_key
    allowed = None if targets is None else {event_waiter_key(t) for t in targets}
    best = None
    for k, v in board.job_data(job).items():
        if not k.startswith("events.waiter.") or (allowed is not None and k not in allowed):
            continue
        try:
            t = _dt.datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        except ValueError:
            continue
        age = (now - _aware(t)).total_seconds()
        best = age if best is None else min(best, age)
    return best


def event_waiter_age(board, job: str, now: _dt.datetime, target) -> float | None:
    """Freshness of a waiter whose actual selector receives this event, not a slug guess.

    Old producers have no coverage record. Their shared orchestrator key proves only
    unaddressed/@orchestrator coverage, never that an arbitrary role is being watched.
    """
    from swarm.board.base import event_waiter_key
    data = board.job_data(job)
    ages, described = [], set()
    for key, raw in data.items():
        if not key.startswith('events.waiter_scope.'):
            continue
        try:
            record = json.loads(raw)
            at = _aware(_dt.datetime.fromisoformat(record['at']))
            targets = record['targets']
            if targets is not None and not isinstance(targets, list):
                continue
            described.add(record['legacy_key'])
            if targets is None or target in targets:
                ages.append((now - at).total_seconds())
        except (ValueError, TypeError, KeyError):
            continue
    consumers = (None, '@pm') if target in (None, '') else (target,)
    for consumer in consumers:
        key = event_waiter_key(consumer)
        if key in described or key not in data:
            continue
        try:
            ages.append((now - _aware(_dt.datetime.fromisoformat(data[key]))).total_seconds())
        except (ValueError, TypeError):
            continue
    return min(ages, default=None)


def _pending(board, job: str):
    fn = getattr(board, "pending_events", None)
    return list(fn(job)) if fn else []


def _orchestrators(board, job: str, roles) -> list:
    return [r for r in board.roster(job) if r.active and (r.role or "").lower() in roles]


class DriveLedgers(dict):
    """job -> ledger, plus `unreadable`: the ledger directory could not be read in full (a planted lock, a bad
    file), so which jobs are drive jobs is not known and the watchdog must not act on a guess."""
    unreadable = False


def drive_ledgers(cfg: dict) -> DriveLedgers:
    """The unfinished host-private ledger of every drive job owned by this host. The board's `drive.enabled`
    flag is board-writable and decides nothing here (spec 4.0): a job is a drive job for the watchdog only when
    this host holds its ledger. Empty when there is no ledger directory. When it cannot be read, the result
    is empty with `unreadable` set: it fails closed (no model call is made, see report)."""
    import os
    from swarm.board.autoinit import store_key
    from swarm.supervisor import ledger, settings
    out = DriveLedgers()
    try:
        if not os.path.lexists(settings.private_dir() / ledger.SUBDIR):
            return out
        entries = ledger.unfinished(store_key(cfg))
    except Exception:
        out.unreadable = True
        return out
    for e in entries:
        if isinstance(e, ledger.Ledger):
            out[e.job] = e
        else:
            out.unreadable = True            # a bad entry: its job is unknown
    return out


def driver_stale(led, cfg: dict, now: _dt.datetime) -> bool:
    """The driver has not completed a pass over this ledger for more than two stall periods (2x timer_minutes)."""
    from swarm.supervisor import settings
    try:
        last = led.last_pass or led.activated_at
        t = _aware(_dt.datetime.fromisoformat(last))
        return (now - t).total_seconds() > 2 * 60 * float(settings.settings(cfg)["timer_minutes"])
    except Exception:
        return True


def check_job(board, s: dict, js, now: _dt.datetime, drive=None, cfg=None) -> list[Problem]:
    """`drive`: the drive ledgers of this host (drive_ledgers). A drive job gets no waiter-stale, unacked, unread or
    stalled alerts (nobody waits for it; the driver raises DRIVER-STALLED), but one outside check: its driver
    has not passed for more than two stall periods (2x [supervise] timer_minutes)."""
    job, out = js.job, []
    if drive and job in drive:
        if cfg is not None and driver_stale(drive[job], cfg, now):
            out.append(Problem(job, "driver-stalled", "the driver has not passed over this drive job for more "
                               "than two stall periods (is the session driver running? `swarm drive run` drives it by hand)"))
        return out
    pending = [e for e in _pending(board, job) if not (
        getattr(e, 'kind', None) == 'TASK-EVIDENCE' and getattr(e, 'source', None) == 'swarm-task')]
    old = [e for e in pending if getattr(e, "to", None) in (None, "") or str(e.to).lstrip("@") in
           {str(r).lower() for r in s["orchestrator_roles"]} | {"pm"}]
    oldest = min((_aware(e.created_at) for e in old), default=None)
    if oldest is not None and _mins(now, oldest) >= s["unacked_minutes"]:
        out.append(Problem(job, "unacked", f"{len(old)} event(s) for the orchestrator unacked for "
                           f"{_mins(now, oldest)} min (oldest: {old[0].kind})"))
    direct = {}
    for event in pending:
        if event not in old:
            direct.setdefault(event.to, []).append(event)
    for target, events in sorted(direct.items(), key=lambda item: str(item[0])):
        oldest_direct = min(_aware(e.created_at) for e in events)
        if _mins(now, oldest_direct) >= s['unacked_minutes']:
            out.append(Problem(job, "unacked", f"{len(events)} event(s) for {target} unacked for "
                               f"{_mins(now, oldest_direct)} min (oldest: {events[0].kind})"))
    live = [r for r in board.roster(job) if r.active]
    missing = []
    for event in old:
        target = getattr(event, 'to', None)
        # Match Board.event_targets(): @pm/@orchestrator receive unaddressed events,
        # while distinct roles remain distinct consumers. A named/EL waiter cannot arm PM.
        age = event_waiter_age(board, job, now, target)
        if age is None or age > s['waiter_stale_seconds']:
            missing.append((target or 'unaddressed', age))
    if missing:
        target, age = missing[0]
        out.append(Problem(job, 'waiter-stale', f"no matching `swarm event wait` is armed for {target} "
                           f"({'never' if age is None else f'last beat {int(age)} s ago'}) "
                           f"and {len(missing)} matching event(s) are pending"))
    elif live and not old:
        targets = (None, '@pm', *("@" + r for r in s['orchestrator_roles']))
        age = waiter_age(board, job, now, targets)
        if age is not None and age > s['waiter_stale_seconds']:
            out.append(Problem(job, 'waiter-stale', 'no orchestrator `swarm event wait` is armed '
                               f'(last beat {int(age)} s ago) and 0 matching event(s) are pending'))
    last = _aware(js.last_activity_at)
    if live and last is not None and _mins(now, last) >= s["stall_minutes"]:
        out.append(Problem(job, "stalled", f"no job activity for {_mins(now, last)} min "
                           f"({len(live)} agent(s) on the roster)"))
    for orch in _orchestrators(board, job, {r.lower() for r in s["orchestrator_roles"]}):
        st = board.sync_state(orch.agent_key)
        if st is None:
            continue
        cursor = getattr(st, "last_read_id", 0) or 0
        unread = [m for m in board.messages_after(cursor, job)
                  if m.to_agent == orch.name and m.agent_name != orch.name
                  and _mins(now, _aware(m.created_at)) >= s["unacked_minutes"]]
        if unread:
            out.append(Problem(job, "unread", f"{len(unread)} message(s) to {orch.name} unread for "
                               f"{_mins(now, _aware(unread[0].created_at))}+ min"))
    return out


def helper_problems(health, now: _dt.datetime) -> list[Problem]:
    """"forwarder down": a source helper (e.g. `gh webhook forward`) that is not running, or a health file
    gone stale (the listener that supervises the helpers is dead; `listener-down` says so too)."""
    if not health or not health.get("helpers"):
        return []
    if now.timestamp() - float(health.get("beat") or 0) > 60:
        return [Problem(None, "forwarder-down", "helper health is stale: the listener's helper supervisor is not running")]
    def retry(v) -> str:
        at = v.get("retry_at")
        if not isinstance(at, (int, float)):
            return ""
        return (f"; next try in {max(0, int(at - now.timestamp()))} s (each try re-reads its config and "
                f"credentials; the wait is capped at 5 min)")
    return [Problem(None, "forwarder-down", f"helper {k} is down (restarted {v.get('restarts', 0)}x){retry(v)}")
            for k, v in sorted(health["helpers"].items()) if not v.get("up")]


def check(board, cfg: dict, now=None, *, listener_up=None, health=None) -> list[Problem]:
    """Every problem of every open job (plus the listener) right now."""
    s = events_listener.settings(cfg)
    n = _now(now)
    problems: list[Problem] = []
    if health is None:
        health = events_listener.read_health()
    if s["enabled"]:
        up = events_listener.probe(cfg) if listener_up is None else listener_up
        if not up:
            problems.append(Problem(None, "listener-down", "the events listener is not answering"))
    if s["enabled"]:
        problems += helper_problems(health, n)
    drive = drive_ledgers(cfg)
    seen = set()
    for js in board.jobs(False):
        seen.add(js.job)
        if js.status == "active":
            problems += check_job(board, s, js, n, drive, cfg)
    for job in sorted(set(drive) - seen):    # a paused drive job is not in the active list: still watched
        js = board.job_status(job)
        if js is not None and js.status == "paused":
            problems += check_job(board, s, js, n, drive, cfg)
    return problems


# ---- the model, when one must read -------------------------------------------------------------------

def model_for(board, cfg: dict, job: str | None) -> str:
    """The model for a watcher pass: the job's events.model, else [events] model. Never inherited."""
    if job:
        own = board.job_data(job).get("events.model")
        try:
            if own and events_listener.settings({"events": {"model": own}}):
                return own
        except events_listener.EventSettingsError:
            pass
    return events_listener.settings(cfg)["model"]


def triage_command(model: str, problems: list[Problem]) -> list[str]:
    body = "; ".join(f"{p.code}: {p.text}" for p in problems)
    prompt = ("You are a watchdog. In ONE line of at most 300 characters say what is wrong with this "
              "swarm job and what the orchestrator should do first. Findings: " + body)
    return ["claude", "-p", "--model", model, "--no-session-persistence", prompt]


def triage(board, cfg: dict, job: str | None, problems: list[Problem], run=subprocess.run) -> str | None:
    """One line from the watcher model, or None (off, failed, empty): the deterministic text stands."""
    if not events_listener.settings(cfg)["triage"] or not problems:
        return None
    try:
        r = run(triage_command(model_for(board, cfg, job), problems), capture_output=True, text=True,
                timeout=90, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    line = " ".join((r.stdout or "").split())[:300]
    return line if r.returncode == 0 and line else None


# ---- reporting -----------------------------------------------------------------------------------

def report(board, cfg: dict, problems: list[Problem], now=None, run=subprocess.run) -> int:
    """One SWARM-ALERT per job (listener problems go to every active job). Returns how many were new."""
    if not problems:
        return 0
    s = events_listener.settings(cfg)
    n = _now(now)
    bucket = int(n.timestamp() // (s["alert_repeat_minutes"] * 60))
    drive = drive_ledgers(cfg)      # once per report: a drive job, or an unreadable ledger, gets no model call
    jobs = {p.job for p in problems if p.job}
    if any(p.job is None for p in problems):
        jobs |= {js.job for js in board.jobs(False) if js.status == "active"}
    new = 0
    for job in sorted(jobs):
        mine = [p for p in problems if p.job in (job, None)]
        text = "SWARM-ALERT " + "; ".join(f"{p.code}: {p.text}" for p in mine)
        line = None if (job in drive or drive.unreadable) else triage(board, cfg, job, mine, run=run)   # no model call outside a launch
        if line:
            text = f"SWARM-ALERT {line} [{', '.join(p.code for p in mine)}]"
        text = text[:events_listener.TEXT_MAX]
        key = ",".join(sorted(p.code for p in mine)) + f"@{bucket}"
        if callable(getattr(board, "post_event", None)):
            _, created = board.post_event(job, "SWARM-ALERT", key, text, to=None, source="safety")
            new += bool(created)
        else:   # a board without events: one plain notice per bucket
            seen = board.job_data(job).get("events.alert_bucket")
            if seen != f"{key}":
                board.post(job, WATCHER_NAME, text[:200])
                board.set_job_data(job, "events.alert_bucket", key[:200])
                new += 1
    return new


def run_check(board, cfg: dict, say=print, now=None, listener_up=None, run=subprocess.run) -> list[Problem]:
    problems = check(board, cfg, now, listener_up=listener_up)
    new = report(board, cfg, problems, now, run=run)
    for p in problems:
        say(f"events check: {p.job or '-'} {p.code}: {p.text}")
    if not problems:
        say("events check: nothing wrong")
    return problems
