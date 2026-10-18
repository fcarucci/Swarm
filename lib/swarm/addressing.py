"""Role addressing for `swarm post --to`: `@EL`, `@QA`, `@PM`, `@judge`, `@<role>` resolve to the
agents that hold that launch on the job now; a plain name must be an agent of the job (or the key an active agent joined with). A recipient
that doesn't resolve is an error (AddressError), never a message nobody will read.

Launches are the agents' roles (`[swarm role: <role>]` in a subagent's prompt, `swarm join --role`),
plus "judge" and "verifier" (the board derives those). Matching ignores case, and a few short
aliases name the usual launches of the engineering team:

    @EL  -> engineering_lead        @QA -> qa
    @PM  -> project_manager, else orchestrator (the first launch with a holder)
    @product -> product_manager
"""
from __future__ import annotations

from swarm import roles

ALIASES = {"el": ("engineering_lead",), "qa": ("qa",),
           "pm": ("project_manager", "orchestrator"), "product": ("product_manager",)}
MAX_RECIPIENTS = 8   # one stored message per holder: a launch held by more agents than this is refused


class AddressError(ValueError):
    """The post's author or recipient doesn't check out; the message says why and what to do."""


class NoHolder(AddressError):
    """`@role` is a valid launch role that no agent holds now: the post is queued for it, not refused.
    `token` is the address without its @."""
    def __init__(self, message: str, token: str):
        super().__init__(message)
        self.token = token


def launch_candidates(token: str) -> tuple[str, ...]:
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


def queue_name(token: str) -> str:
    """The addressee a post to a launch nobody holds is kept under: its next holder reads it on joining."""
    return f"queue.{token.lower()}"


def resolve(board, job: str, to: str) -> list[str]:
    """The agent names `to` addresses on `job`: [to] for a plain name of an agent of the job; for
    `@role` the active holders of the launch, in roster order; [to] for a name of the person behind the job
    (owners.person_names: owner, user, human, the job's creator, [board] owner_names). AddressError if the name is unknown,
    or the launch has no holder (the error lists who is on the job)."""
    agents = board.agents(job)
    live = [a for a in agents if a.ended_at is None]
    if not to.startswith("@"):
        if any(a.name == to for a in agents):
            return [to]
        by_key = [a.name for a in live if getattr(a, "agent_key", None) == to]   # the key it joined with
        if len(by_key) == 1:
            return by_key
        from swarm.owners import is_person
        if is_person(board, job, to):   # the person behind the job (owner, user, human, its creator,
            return [to]                 # [board] owner_names): what human_question reads as a wait on a person
        raise AddressError(f"no agent named {to!r} on job {job} (see `swarm who --job {job}`; address a "
                           f"launch with @role, e.g. @EL); nothing was posted")
    token = to[1:]
    if not roles.valid_name(token.lower()):
        raise AddressError(f"{to!r} is not a role address (use @EL, @QA, @PM, @judge or @<role>)")
    for launch in launch_candidates(token):
        names = [a.name for a in live if a.role == launch]
        if names:
            if len(names) > MAX_RECIPIENTS:
                raise AddressError(f"{to} has {len(names)} holders on {job}: address one by name")
            return names
    on_job = ", ".join(sorted({a.role for a in live if a.role})) or "none"
    raise NoHolder(f"nobody holds {to} on job {job} now (launches with a holder: {on_job}); "
                   f"queued for the launch", token)
