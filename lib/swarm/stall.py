"""PM-STALL: an item the orchestrator has left unhandled for too long is announced on the board.

An orchestrator item is an open blocker waiting on the orchestrator, or a message addressed to it (to a
holder of that role, or to the role: @PM). It is handled when the blocker is resolved, or when the orchestrator
later posted to the poster. `check` posts `PM-STALL <item> age=<m>` once the oldest such item is
`[job] orchestrator_stall_minutes` (default 5) old, then again every that many minutes while it is
unhandled. The PM-STALL posts on the board are the only state, so any process may run the check (the
session driver's tick, the await poll loop) without a flood: a repost waits a full interval after the last.
With `[job] notify_on_stall` on, each post also reaches the person through `swarm.notify`."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from swarm import notify as notify_mod

SYSTEM_NAME = "swarm"                      # who posts the PM-STALL notice (as the other board notices)
SYSTEM_NAMES = frozenset({SYSTEM_NAME, "swarm supervisor", "swarm-pause"})
ORCHESTRATOR_ROLES = ("project_manager", "orchestrator")
ROLE_ADDRESSES = frozenset({"@pm", "pm", "@orchestrator", "orchestrator", "@project_manager", "project_manager"})
DEFAULT_MINUTES = 5.0
MAX_AGE = dt.timedelta(hours=24)           # an item older than this is history, not a stall to announce
PREFIX = "PM-STALL "


@dataclass(frozen=True)
class Item:
    key: str                               # blocker#<id> or msg#<id>
    text: str                              # what must be done, one line
    since: dt.datetime


def minutes(cfg: dict | None) -> float:
    """[job] orchestrator_stall_minutes; 0 turns the escalation off."""
    value = ((cfg or {}).get("job") or {}).get("orchestrator_stall_minutes", DEFAULT_MINUTES)
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return DEFAULT_MINUTES


def notify_on(cfg: dict | None) -> bool:
    return bool(((cfg or {}).get("job") or {}).get("notify_on_stall", False))


def _holders(board, job: str) -> set[str]:
    return {a.name for a in board.agents(job) if a.role in ORCHESTRATOR_ROLES}


def _short(text: str, limit: int = 80) -> str:
    one = " ".join((text or "").split())
    return one if len(one) <= limit else one[:limit - 3] + "..."


def unhandled(board, job: str, now: dt.datetime | None = None) -> list[Item]:
    """The orchestrator's unhandled items on `job`, oldest first."""
    now = now or board.now()
    holders = _holders(board, job)
    mine = {n.casefold() for n in holders}
    items: list[Item] = []
    for b in board.blockers(job):
        target = (b.waiting_on or "").casefold()
        if target in ROLE_ADDRESSES or target.lstrip("@") in mine:
            items.append(Item(f"blocker#{b.id}", f"{b.kind} blocker #{b.id}: {_short(b.reason)}", b.created_at))
    msgs = board.messages_after(0, job)
    for m in msgs:
        if now - m.created_at > MAX_AGE or m.agent_name in SYSTEM_NAMES or m.agent_name in holders:
            continue
        to = (m.to_agent or "").casefold()
        if not (to in ROLE_ADDRESSES or to in mine):
            continue
        replied = any(r.id > m.id and r.agent_name in holders and r.to_agent == m.agent_name for r in msgs)
        if not replied:
            items.append(Item(f"msg#{m.id}", f"message #{m.id} from {m.agent_name}: {_short(m.message)}", m.created_at))
    return sorted(items, key=lambda i: i.since)


def age_minutes(item: Item, now: dt.datetime) -> int:
    return max(0, int((now - item.since).total_seconds() // 60))


def waiting_text(board, job: str, now: dt.datetime | None = None) -> str:
    """"waiting on ORCHESTRATOR since <age>" for the oldest unhandled item, "" when there is none."""
    now = now or board.now()
    items = unhandled(board, job, now)
    return f"waiting on ORCHESTRATOR since {age_minutes(items[0], now)}m" if items else ""


def _last_posted(board, job: str) -> dict[str, dt.datetime]:
    """When each item was last announced: the newest PM-STALL post naming it."""
    last: dict[str, dt.datetime] = {}
    for m in board.messages_after(0, job):
        if m.agent_name == SYSTEM_NAME and m.message.startswith(PREFIX):
            last[m.message[len(PREFIX):].split(" ", 1)[0]] = m.created_at
    return last


def check(board, job: str, cfg: dict | None, now: dt.datetime | None = None, notifier=None) -> list[str]:
    """Post a PM-STALL for every item that has waited `orchestrator_stall_minutes` and was not announced
    within that interval; return the posted lines. Never raises on a board that refuses the post."""
    limit = minutes(cfg)
    if limit <= 0:
        return []
    now = now or board.now()
    due = [i for i in unhandled(board, job, now) if age_minutes(i, now) >= limit]
    if not due:
        return []
    last = _last_posted(board, job)
    interval = dt.timedelta(minutes=limit)
    posted = []
    for item in due:
        if item.key in last and now - last[item.key] < interval:
            continue
        line = f"{PREFIX}{item.key} age={age_minutes(item, now)} ({item.text})"
        try:
            board.post(job, SYSTEM_NAME, line)
        except Exception:
            continue
        posted.append(line)
        if notify_on(cfg):
            send = notifier or notify_mod.send
            send(cfg, f"the swarm is waiting on your PM: {item.text} age={age_minutes(item, now)}")
    return posted
