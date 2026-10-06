"""Role addressing for `swarm post --to`: `@EL`, `@QA`, `@PM`, `@judge`, `@<role>` resolve to the
agents that hold that seat on the job now; a plain name must be an agent of the job. A recipient
that doesn't resolve is an error (AddressError), never a message nobody will read.

Seats are the agents' roles (`[swarm role: <role>]` in a subagent's prompt, `swarm join --role`),
plus "judge" and "verifier" (the board derives those). Matching ignores case, and a few short
aliases name the usual seats of the engineering team:

    @EL  -> engineering_lead        @QA -> qa
    @PM  -> project_manager, else orchestrator (the first seat with a holder)
    @product -> product_manager
"""
from __future__ import annotations

from swarm import roles

ALIASES = {"el": ("engineering_lead",), "qa": ("qa",),
           "pm": ("project_manager", "orchestrator"), "product": ("product_manager",)}
MAX_RECIPIENTS = 8   # one stored message per holder: a seat held by more agents than this is refused


class AddressError(ValueError):
    """The post's author or recipient doesn't check out; the message says why and what to do."""


def seat_candidates(token: str) -> tuple[str, ...]:
    """The role names an `@token` stands for (tried in order, the first with a holder wins)."""
    key = token.lower()
    return ALIASES.get(key) or (key,)


def check_author(board, job: str, name: str, key: str | None = None) -> None:
    """AddressError unless `name` (and `key`, when given: the key's agent has that name) is an agent
    of `job`, active or departed (a finished agent may still say so). The system's own posts and the
    pause writer never come through here."""
    from swarm.board.base import check_name
    check_name(name)   # a name the board would refuse: its own ValueError (not an AddressError)
    agents = board.agents(job)
    if key is not None:
        held = board.active_agent_name(key)
        if held is None or held != name:
            raise AddressError(f"--key does not belong to {name!r}: post as the agent that holds it")
    if not any(a.name == name for a in agents):
        raise AddressError(f"{name!r} is not an agent of job {job}: post on the job you joined "
                           f"(or join it first); nothing was posted")


def resolve(board, job: str, to: str) -> list[str]:
    """The agent names `to` addresses on `job`: [to] for a plain name of an agent of the job; for
    `@role` the active holders of the seat, in roster order. AddressError if the name is unknown,
    or the seat has no holder (the error lists who is on the job)."""
    agents = board.agents(job)
    live = [a for a in agents if a.ended_at is None]
    if not to.startswith("@"):
        if any(a.name == to for a in agents):
            return [to]
        raise AddressError(f"no agent named {to!r} on job {job} (see `swarm who --job {job}`; address a "
                           f"seat with @role, e.g. @EL); nothing was posted")
    token = to[1:]
    if not roles.valid_name(token.lower()):
        raise AddressError(f"{to!r} is not a role address (use @EL, @QA, @PM, @judge or @<role>)")
    for seat in seat_candidates(token):
        names = [a.name for a in live if a.role == seat]
        if names:
            if len(names) > MAX_RECIPIENTS:
                raise AddressError(f"{to} has {len(names)} holders on {job}: address one by name")
            return names
    on_job = ", ".join(sorted({a.role for a in live if a.role})) or "none"
    raise AddressError(f"nobody holds {to} on job {job} now (seats with a holder: {on_job}); "
                       f"nothing was posted")
