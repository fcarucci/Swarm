"""What `swarm await` hands to the orchestrating session of a job: the items that need it.

An item is one thing the orchestrator must do or know:
  question        an open question (a blocker of kind "question"): "answer Q492"
  blocker         any other open blocker: "resolve blocker 7"
  addressed       a message addressed to the orchestrator (or its role): "reply to <author>"
  branch-ready    a BRANCH READY post: "review BRANCH READY <branch>@<sha>"
  infra-request   an INFRA REQUEST post
  verdict         a VERDICT post
  order           an ORDER post
  state           the job's status changed since the orchestrator last looked ("job is now paused")

Items collapse by key (one entry per question, per branch@sha, per message), and an item stays open
until it is resolved, not merely read: a question or blocker until it is answered or closed, a
BRANCH READY until it is landed (a FINALIZED / INTEGRATED post with its sha) or superseded by a newer
BRANCH READY of the same branch, any other post until an orchestrator posts after it. A job state
change is announced once, when an await first sees it.

The orchestrator's await state (Board.await_state: cursor, report times, last status) is kept per
(job, session), apart from the read cursor, so two awaits and a read never consume each other.
"""
from __future__ import annotations

import datetime as dt
import re
import time
from dataclasses import dataclass, replace

from swarm.blockers_stale import stale_reason

ORCHESTRATOR_SEATS = ("project_manager", "orchestrator")
POST_PREFIXES = {"BRANCH READY": "branch-ready", "INFRA REQUEST": "infra-request",
                 "VERDICT": "verdict", "ORDER": "order"}
LANDED_PREFIXES = ("FINALIZED ", "INTEGRATED ")
BRANCH_RE = re.compile(r"BRANCH READY\s+(\S+)@([0-9a-fA-F]{7,40})\b")
MESSAGE_KINDS = frozenset({"addressed", "branch-ready", "infra-request", "verdict", "order"})
DEFAULT_POLL = 5.0      # seconds between checks while blocking
REMIND_SECONDS = 60.0   # an open item last reported this long ago is reported again


@dataclass(frozen=True)
class Item:
    kind: str                # see the module docstring
    key: str                 # collapses duplicates: "Q492", "BRANCH READY feat/x@abc1234", "MSG #1203"
    action: str              # what must be done
    job: str
    ref: int                 # the blocker id (question, blocker) or the message id (posts); 0 for state
    created_at: dt.datetime  # first seen; the age counts from here
    age: dt.timedelta
    resolved: bool = False
    detail: str = ""         # state items: the status they announce
    stale: str = ""          # blocker items: why the subject looks resolved already (blockers_stale), or ""


def orchestrator_names(board, job: str) -> set[str]:
    """The names of the active agents holding an orchestrator seat (project_manager, orchestrator)."""
    return {a.name for a in board.agents(job) if a.ended_at is None and a.role in ORCHESTRATOR_SEATS}


def _gist(text: str, limit: int = 80) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[:limit - 1] + "…"


def _message_item(m, owners: set[str], job: str, now: dt.datetime) -> Item | None:
    """The item a post makes, None when it asks nothing of the orchestrator."""
    text = m.message
    age = now - m.created_at
    for prefix, kind in POST_PREFIXES.items():
        if not text.startswith(prefix + " "):
            continue
        if kind == "branch-ready":
            found = BRANCH_RE.match(text)
            if found is None:
                return Item(kind, f"BRANCH READY #{m.id}", f"review the BRANCH READY post #{m.id}",
                            job, m.id, m.created_at, age)
            branch, sha = found.group(1), found.group(2)
            return Item(kind, f"BRANCH READY {branch}@{sha}", f"review BRANCH READY {branch}@{sha}",
                        job, m.id, m.created_at, age)
        return Item(kind, f"{prefix} #{m.id}", f"act on {prefix} #{m.id}: {_gist(text)}",
                    job, m.id, m.created_at, age)
    if m.to_agent in owners:
        return Item("addressed", f"MSG #{m.id}", f"reply to {m.agent_name}: {_gist(text)}",
                    job, m.id, m.created_at, age)
    return None


def _message_resolved(item: Item, later: list, owners: set[str]) -> bool:
    """Whether a message item is settled by the messages posted after it (`later`, in id order)."""
    if item.kind == "branch-ready":
        found = BRANCH_RE.match(item.key)
        if found is None:
            return any(m.agent_name in owners for m in later)
        branch, sha = found.group(1), found.group(2)
        for m in later:
            if m.message.startswith(LANDED_PREFIXES) and sha in m.message:
                return True
            newer = BRANCH_RE.match(m.message)
            if newer and newer.group(1) == branch and newer.group(2) != sha:
                return True   # superseded by a newer BRANCH READY of the same branch
        return False
    return any(m.agent_name in owners for m in later)


def collect_items(board, job: str, session: str) -> list[Item]:
    """Every item of `job` for the orchestrating `session`, resolved ones included (`resolved` says
    which). Duplicates collapse into one item: the newest post, aged from the first one. Reads only;
    nothing is recorded (see report and await_items)."""
    now = board.now()
    owners = orchestrator_names(board, job)
    messages = board.messages_after(0, job)
    items: dict[str, Item] = {}
    for b in board.blockers(job):
        if b.kind == "question":
            item = Item("question", f"Q{b.id}", f"answer Q{b.id}", job, b.id, b.created_at, now - b.created_at)
        else:
            item = Item("blocker", f"B{b.id}", f"resolve blocker {b.id} ({b.kind})", job, b.id,
                        b.created_at, now - b.created_at)
        items[item.key] = replace(item, stale=stale_reason(b, board) or "")
    for m in messages:
        if m.agent_name in owners:
            continue
        item = _message_item(m, owners, job, now)
        if item is None:
            continue
        prior = items.get(item.key)
        if prior is not None:
            first = min(prior.created_at, item.created_at)
            item = replace(item, created_at=first, age=now - first)
        items[item.key] = item
    out = []
    for item in items.values():
        if item.kind in MESSAGE_KINDS:
            later = [m for m in messages if m.id > item.ref]
            item = replace(item, resolved=_message_resolved(item, later, owners))
        out.append(item)
    state = board.await_state(job, session)
    status = board.job_status(job)
    if state["status"] is not None and status is not None and status.status != state["status"]:
        out.append(Item("state", f"STATE {status.status}", f"job is now {status.status}", job, 0,
                        now, dt.timedelta(0), detail=status.status))
    return sorted(out, key=lambda i: (i.created_at, i.ref))


def is_resolved(item: Item, board) -> bool:
    """Whether `item` is settled now, asked afresh of the board (slices that show or count items use
    this rather than a stored flag). A state item is settled once the job has moved on from it."""
    if item.kind == "state":
        status = board.job_status(item.job)
        return status is None or status.status != item.detail
    if item.kind in ("question", "blocker"):
        blocker = board.blocker(item.ref)
        return blocker is None or blocker.state != "open"
    owners = orchestrator_names(board, item.job)
    return _message_resolved(item, board.messages_after(item.ref, item.job), owners)


def _due(open_items: list[Item], state: dict, now: dt.datetime, remind: float) -> bool:
    """Whether an await should return now: an item newer than the cursor, an item never reported, or
    one last reported `remind` seconds or more ago (so a dropped wake-up comes back on the next call)."""
    for item in open_items:
        if item.kind in MESSAGE_KINDS and item.ref > state["cursor"]:
            return True
        last = state["reported"].get(item.key)
        if last is None or now - dt.datetime.fromisoformat(last) >= dt.timedelta(seconds=remind):
            return True
    return False


def _record(board, job: str, session: str, open_items: list[Item], now: dt.datetime) -> None:
    """Record that `open_items` were reported at `now`: the cursor moves to the newest message item,
    each item gets its report time, items no longer open lose theirs, and the job status is stored."""
    state = board.await_state(job, session)
    keys = {i.key for i in open_items}
    reported = {k: v for k, v in state["reported"].items() if k in keys}
    reported.update({i.key: now.isoformat() for i in open_items})
    cursor = max([state["cursor"]] + [i.ref for i in open_items if i.kind in MESSAGE_KINDS])
    status = board.job_status(job)
    board.save_await_state(job, session, {"cursor": cursor, "reported": reported,
                                          "status": status.status if status is not None else None})


def report(board, job: str, session: str) -> list[Item]:
    """`swarm await --once`: every open item now, reported and recorded. Never blocks."""
    items = [i for i in collect_items(board, job, session) if not i.resolved]
    _record(board, job, session, items, board.now())
    return items


def await_items(board, job: str, session: str, *, timeout: float | None = None, poll: float = DEFAULT_POLL,
                remind: float = REMIND_SECONDS, clock=time.monotonic, on_poll=None) -> list[Item]:
    """`swarm await`: block until an open item is due (see _due), then report and return every open
    item. Returns [] when `timeout` seconds pass first; nothing is recorded then. `on_poll` runs once per
    check (the PM-STALL check: the await is the one process that runs while a non-drive job's orchestrator
    is idle)."""
    start = clock()
    while True:
        if on_poll is not None:
            on_poll()
        now = board.now()
        open_items = [i for i in collect_items(board, job, session) if not i.resolved]
        state = board.await_state(job, session)
        if state["status"] is None:   # the first call records the job's status; no state item then
            _record(board, job, session, [], now)
            state = board.await_state(job, session)
        if open_items and _due(open_items, state, now, remind):
            _record(board, job, session, open_items, now)
            return open_items
        left = None if timeout is None else timeout - (clock() - start)
        if left is not None and left <= 0:
            return []
        board.wait_for_change(poll if left is None else min(poll, left))


def _age(delta: dt.timedelta) -> str:
    secs = max(0, int(delta.total_seconds()))
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m"
    if secs < 86400:
        return f"{secs // 3600}h{(secs % 3600) // 60:02d}m"
    return f"{secs // 86400}d{(secs % 86400) // 3600:02d}h"


def format_item(item: Item) -> str:
    stale = f" POSSIBLY STALE: {item.stale}" if item.stale else ""
    return f"{item.kind:<13} {item.key:<30} age {_age(item.age):<7} {item.action}{stale}"
