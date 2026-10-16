"""Seat continuity: who held a seat before, who holds it now, and the posts waiting for it.

- previous_seat / rejoin: `swarm activate` on a closed job puts the reopening session back on the
  board in its previous role under its previous name (allocate_name revives a departed key).
- judge_refusal: the one message every judge path gives when the seat is taken, with the holder's
  name and how long it has held it.
- role queue: a post to `@PM` (or any `@role`) with no holder is stored as a message addressed to
  the queue name for that role (queue_name) and delivered to a holder when one joins
  (deliver_queued, called from Board.allocate_name). The queue is the board's own message log, so
  every backend has it with no schema of its own.
"""
from __future__ import annotations

from swarm import addressing

QUEUE_PREFIX = "queue."          # the addressee a queued role post is stored under: queue.<token>
DELIVERED_TAG = "[queued #"      # first words of a delivery: "[queued #12 from X for @PM] text"
JUDGE_ROLE = "judge"


def age_text(seconds: float) -> str:
    """"45s", "7m", "3h 5m", "2d 4h": how long ago, to the two coarsest units."""
    s = max(0, int(seconds))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h {s % 3600 // 60}m" if s % 3600 // 60 else f"{s // 3600}h"
    return f"{s // 86400}d {s % 86400 // 3600}h" if s % 86400 // 3600 else f"{s // 86400}d"


def previous_seat(board, job: str, session: str | None):
    """(name, role, agent_key) the reopening session last held on `job`, None if it held none. Read
    before the job is reopened. The session's seat is the agent that joined with its session id as
    key, or, when `session` is the one the job is bound to, the orchestrator that joined with the
    documented `--key orchestrator` (orchestrator-2, ...): that key says nothing of the session."""
    if not session:
        return None
    agents = board.agents(job)
    for a in reversed(agents):
        if getattr(a, "agent_key", None) == session and a.role:
            return a.name, a.role, a.agent_key
    js = board.job_status(job)
    if js is None or getattr(js, "session_id", None) != session:
        return None
    for a in reversed(agents):
        k = getattr(a, "agent_key", None) or ""
        if k.casefold().startswith("orchestrator") and a.role:
            return a.name, a.role, k
    return None


def rejoin(board, job: str, session: str | None, seat) -> str | None:
    """Put the agent of `seat` (previous_seat's result) back on `job` in its role; the name it held
    comes back with it when free. Returns the name now held, None when there was nothing to
    rejoin or the board refused (a paused job: the caller carries on, rejoin is a courtesy)."""
    if not session or seat is None:
        return None
    try:
        return board.allocate_name(seat[2], job, seat[1])
    except ValueError:
        return None


def judge_holder(board, job: str, key: str | None = None):
    """The live judge of `job` other than the agent `key`, None when the seat is free. A judge that
    has completed or died is not a holder: take_judge_launch hands its seat to a fresh judge."""
    return next((a for a in board.agents(job, include_departed=False)
                 if a.role == JUDGE_ROLE and a.ended_at is None and a.agent_key != key
                 and a.status not in ("completed", "dead")), None)


def judge_refusal(board, job: str, key: str | None = None) -> str | None:
    """"judge seat held by <name> (joined <age> ago)", None when the seat is free (or `key` holds it)."""
    from swarm.textsafe import term_safe
    holder = judge_holder(board, job, key)
    if holder is None:
        return None
    ago = age_text((board.now() - holder.joined_at).total_seconds())
    return f"judge seat held by {term_safe(holder.name)} (joined {ago} ago)"


# ---- the role queue -------------------------------------------------------------------------

def queue_name(token: str) -> str:
    """The addressee a post to `@token` is stored under while no one holds the seat."""
    return QUEUE_PREFIX + token.lower()


def queue_post(board, job: str, name: str, message: str, token: str, agent_key: str | None = None):
    """Store `message` for the seat `@token`: nobody holds it now, a holder gets it on joining."""
    return board.post(job, name, message, to=queue_name(token), agent_key=agent_key)


def _tokens_for(role: str) -> list[str]:
    """The `@tokens` whose seat is `role`: the role itself and every alias that lists it."""
    out = [role.lower()]
    out += [t for t, seats in addressing.ALIASES.items() if role.lower() in seats and t not in out]
    return out


def deliver_queued(board, job: str, name: str, role: str | None) -> int:
    """Hand the posts queued for `role` (under any of its tokens) to the agent `name` that just
    took the seat: each becomes a post addressed to `name`, once (a delivery already on the board
    is not repeated). Returns how many were delivered."""
    if not role:
        return 0
    queues = {queue_name(t) for t in _tokens_for(role)}
    msgs = board.messages_after(0, job)
    done = {m.message.split("]", 1)[0] for m in msgs
            if m.agent_name == "swarm" and m.to_agent == name and m.message.startswith(DELIVERED_TAG)}
    n = 0
    for m in msgs:
        if m.to_agent not in queues:
            continue
        head = f"{DELIVERED_TAG}{m.id} from {m.agent_name} for @{m.to_agent[len(QUEUE_PREFIX):]}"
        if head in done:
            continue
        board.post(job, "swarm", f"{head}] {m.message}", to=name)
        n += 1
    return n
