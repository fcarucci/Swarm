"""What `who`, `status` and `watch` show for an agent: the truth, not just the clock.

The board stores and derives only hook contact (board.derive_agent_status: started / running /
idle / dead / completed / left). Read alone that misleads: an agent that finished and went quiet
looks dead, a CLI-only orchestrator looks like an idle worker, and an agent parked on CI looks
as stalled as a hung one. `annotate` rewrites the DISPLAY status of each row from what the board
already knows (posts, the verdict, the job's wait and blockers); nothing is stored or written.

The rules, first match wins, for the rows whose derived status is idle or dead (running, started,
completed and left rows keep theirs; a row the supervisor closed as stuck is `dead`):

  orchestrator  role "orchestrator" (`swarm join --role orchestrator`, or a --key that starts with
                "orchestrator", see ORCHESTRATOR_KEY): `standby` when idle, `away` when silent
                for dead_minutes. Never a worker: it is left out of the working/waiting/lost counts.
  finished      its last post is a hand-off (DONE, VERIFIED or FAILED), or it is the judge that
                recorded a `met` verdict since it joined. It went quiet because it is done.
                Or the job's goal was met after it last made contact: it went quiet while the
                job was concluding, so it owes nothing.
  waiting       alive (idle, not dead) and the job says what it waits on (`swarm wait --on`, or an
                open blocker), or the agent's own last post says it waits ("waiting for CI").
                A BOUNDED, unexpired job wait also covers an agent silent past dead_minutes.
  idle          silent past idle_minutes with no stated reason: possibly stalled.
  dead          silent past dead_minutes while it still owed work: no hand-off, no verdict, no wait.

`rollup` counts the rows the same way: N working, N waiting, N finished, N lost.
"""
from __future__ import annotations

import dataclasses
import re

ORCHESTRATOR = "orchestrator"
ORCHESTRATOR_KEY = re.compile(r"orchestrator(?:[-_.:].*)?", re.IGNORECASE)
_HANDOFF = re.compile(r"\s*(DONE|VERIFIED|FAILED)\b")
_WAITS = re.compile(r"\b(waiting|waits?|blocked|parked)\b|\bCI\b", re.IGNORECASE)
NOTE_MAX = 70


_SHOWN = []


def _shown_type():
    """ShownAgent: an AgentStatus as shown (`status` is the display status, `note` says why).
    Built on first use: this module is imported by the CLI on every hook call, the board package is not."""
    if not _SHOWN:
        from swarm.board.base import AgentStatus

        @dataclasses.dataclass(frozen=True)
        class ShownAgent(AgentStatus):
            note: str | None = None
        _SHOWN.append(ShownAgent)
    return _SHOWN[0]


def is_orchestrator(a) -> bool:
    """A member that only orchestrates, from what the board holds: its role says so, or it has no
    role and joined through the CLI under a key named orchestrator... (`swarm join --key
    orchestrator-2` stores no role), or it has no role, no harness and never ran a tool (a CLI
    join: a hook-registered agent has a harness and its tool calls counted)."""
    if a.role == ORCHESTRATOR:
        return True
    return a.role is None and (ORCHESTRATOR_KEY.fullmatch(a.agent_key or "") is not None
                               or (a.harness is None and a.tool_calls == 0 and a.current_tool is None))


def default_role(key: str, role: str | None) -> str | None:
    """The role `swarm join` gives a CLI member: the one asked for, else `orchestrator` for a key
    that says so (orchestrator, orchestrator-2), else None."""
    return role or (ORCHESTRATOR if ORCHESTRATOR_KEY.fullmatch(key or "") else None)


def _note(text: str) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= NOTE_MAX else text[:NOTE_MAX - 1] + "…"


def _wait_reason(board, j, now, *, bounded_only: bool) -> str | None:
    """What the job says it waits on, or None. bounded_only: only a wait that has an end in the
    future (it vouches for agents that went quiet for a long time)."""
    if j is None:
        return None
    if j.waiting_on and (j.waiting_until is None and not bounded_only or
                         j.waiting_until is not None and j.waiting_until > now):
        return j.waiting_on
    if j.open_blockers and not bounded_only:
        blockers = board.blockers(j.job)
        if blockers:
            return blockers[0].waiting_on
    return None


def annotate(board, job: str, rows, now=None) -> list:
    """`rows` (AgentStatus of `job`) as ShownAgent where the display differs from the stored row
    (status, role orchestrator, and a note saying why); the order is kept. Each agent's last post
    is read for all the idle and dead ones in one query (board.last_posts). See the module doc."""
    rows = list(rows)
    if not any(a.status in ("idle", "dead") or a.left_reason or a.role is None or a.role == ORCHESTRATOR
               for a in rows):
        return rows
    now = now or board.now()
    j = board.job_status(job)
    posts = board.last_posts(job, [a.name for a in rows if a.status in ("idle", "dead")])   # one query
    replaced = {a.resume_of for a in rows if a.resume_of}   # a replacement took over: not lost
    out = []
    for a in rows:
        status, note = ("left", None) if a.agent_key in replaced else _display(board, j, a, now, posts)
        role = ORCHESTRATOR if is_orchestrator(a) else a.role
        out.append(a if note is None and status == a.status and role == a.role else
                   _shown_type()(**{f.name: getattr(a, f.name) for f in dataclasses.fields(a)} | {
                       "status": status, "role": role, "note": note}))
    return out


def _display(board, j, a, now, posts) -> tuple[str, str | None]:
    from swarm.board import STUCK_PREFIX
    if a.ended_at is not None and (a.left_reason or "").startswith(STUCK_PREFIX):
        return "dead", "closed as stuck"
    if is_orchestrator(a) and a.status in ("idle", "dead"):
        return ("standby" if a.status == "idle" else "away"), "orchestrator, not a worker"
    if a.status not in ("idle", "dead"):
        return a.status, None
    post = posts.get(a.name)
    mine = post if post is not None and post.created_at >= a.joined_at else None
    if mine is not None and _HANDOFF.match(mine.message):
        return "finished", "posted " + _note(mine.message)
    if (j is not None and j.verdict == "met" and (a.role == "judge" or j.verdict_by == a.name)
            and j.verdict_at is not None and j.verdict_at >= a.joined_at):
        return "finished", "recorded verdict met"
    if j is not None and j.verdict == "met" and j.verdict_at is not None and j.verdict_at >= a.last_contact_at:
        return "finished", "went quiet before the goal was met"
    if a.status == "idle":
        why = _wait_reason(board, j, now, bounded_only=False)
        if why:
            return "waiting", "on " + _note(why)
        if mine is not None and _WAITS.search(mine.message):
            return "waiting", "last post: " + _note(mine.message)
        return "idle", "silent, no wait stated"
    why = _wait_reason(board, j, now, bounded_only=True)
    if why:
        return "waiting", "on " + _note(why)
    return "dead", "silent, no DONE or verdict"


@dataclasses.dataclass(frozen=True)
class Rollup:
    working: int = 0
    waiting: int = 0
    idle: int = 0
    finished: int = 0
    lost: int = 0
    orchestrators: int = 0
    waits: tuple = ()      # what the waiting agents wait on, distinct, in row order

    def text(self) -> str:
        """"1 working, 1 waiting (on CI), 3 finished, 0 lost, 1 orchestrator"."""
        wait = f" (on {'; '.join(self.waits)})" if self.waits else ""
        parts = [f"{self.working} working", f"{self.waiting} waiting{wait}"]
        if self.idle:
            parts.append(f"{self.idle} idle (no reason given)")
        parts += [f"{self.finished} finished", f"{self.lost} lost"]
        if self.orchestrators:
            parts.append(f"{self.orchestrators} orchestrator" + ("s" if self.orchestrators > 1 else ""))
        return ", ".join(parts)

    def short(self) -> str:
        """For a table cell: "1 working, 1 waiting, 3 finished" (the zero ones left out)."""
        pairs = (("working", self.working), ("waiting", self.waiting), ("idle", self.idle),
                 ("finished", self.finished), ("lost", self.lost))
        return ", ".join(f"{n} {w}" for w, n in pairs if n) or "-"


def rollup(rows) -> Rollup:
    """Count annotated rows (annotate's result). Only rows that were never shown to belong to a
    live worker count as finished/lost; a departed row is finished unless the supervisor lost it."""
    c = dict(working=0, waiting=0, idle=0, finished=0, lost=0, orchestrators=0)
    waits = []
    for a in rows:
        if is_orchestrator(a):
            c["orchestrators"] += 1
        elif a.status in ("running", "started"):
            c["working"] += 1
        elif a.status == "waiting":
            c["waiting"] += 1
            note = getattr(a, "note", None) or ""
            reason = note.removeprefix("on ") if note.startswith("on ") else ""
            if reason and reason not in waits:
                waits.append(reason)
        elif a.status == "idle":
            c["idle"] += 1
        elif a.status in ("completed", "left", "finished"):
            c["finished"] += 1
        elif a.status == "dead":
            c["lost"] += 1
    return Rollup(waits=tuple(waits), **c)
