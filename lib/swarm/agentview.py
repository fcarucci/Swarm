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
MESSAGE_WINDOW = 400     # newest messages looked at for each agent's last post
NOTE_MAX = 70


def default_role(key: str, role: str | None) -> str | None:
    """The role `swarm join` gives a CLI member: the one asked for, else `orchestrator` for a key
    that says so (orchestrator, orchestrator-2), else None."""
    return role or (ORCHESTRATOR if ORCHESTRATOR_KEY.fullmatch(key or "") else None)


def _note(text: str) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= NOTE_MAX else text[:NOTE_MAX - 1] + "…"


def _last_posts(board, job: str) -> dict:
    """name -> that agent's newest post on the job (within MESSAGE_WINDOW messages)."""
    last = {}
    for m in board.recent_messages(MESSAGE_WINDOW, job):   # oldest first: the newest wins
        last[m.agent_name] = m
    return last


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
    """`rows` (AgentStatus of `job`) with the display status and a note in current_tool for the
    rows that are not running a tool; the order is kept. See the module doc for the rules."""
    rows = list(rows)
    if not any(a.status in ("idle", "dead") or a.role == ORCHESTRATOR or a.left_reason for a in rows):
        return rows
    now = now or board.now()
    j = board.job_status(job)
    last = _last_posts(board, job)
    replaced = {a.resume_of for a in rows if a.resume_of}   # a replacement took over: not lost
    out = []
    for a in rows:
        status, note = ("left", None) if a.agent_key in replaced else _display(board, j, a, last.get(a.name), now)
        out.append(a if note is None and status == a.status else
                   dataclasses.replace(a, status=status, current_tool=note if note is not None else a.current_tool))
    return out


def _display(board, j, a, post, now) -> tuple[str, str | None]:
    from swarm.board import STUCK_PREFIX
    if a.ended_at is not None and (a.left_reason or "").startswith(STUCK_PREFIX):
        return "dead", "lost: closed as stuck"
    if a.role == ORCHESTRATOR and a.status in ("idle", "dead"):
        return ("standby" if a.status == "idle" else "away"), "orchestrator: not a worker"
    if a.status not in ("idle", "dead"):
        return a.status, None
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
            return "waiting", "waiting on " + _note(why)
        if mine is not None and _WAITS.search(mine.message):
            return "waiting", _note(mine.message)
        return "idle", "silent, no wait stated"
    why = _wait_reason(board, j, now, bounded_only=True)
    if why:
        return "waiting", "waiting on " + _note(why)
    return "dead", "lost: silent, no DONE or verdict"


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
        if a.role == ORCHESTRATOR:
            c["orchestrators"] += 1
        elif a.status in ("running", "started"):
            c["working"] += 1
        elif a.status == "waiting":
            c["waiting"] += 1
            reason = (a.current_tool or "").removeprefix("waiting on ")
            if reason and reason not in waits:
                waits.append(reason)
        elif a.status == "idle":
            c["idle"] += 1
        elif a.status in ("completed", "left", "finished"):
            c["finished"] += 1
        elif a.status == "dead":
            c["lost"] += 1
    return Rollup(waits=tuple(waits), **c)
