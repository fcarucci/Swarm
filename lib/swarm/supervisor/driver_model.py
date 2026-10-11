"""The frozen data model of the job driver (spec 3.2-3.4, plan Task 7a). Dataclasses only: no I/O and no
decisions. `next_actions` (Task 7) consumes these; the snapshot reader (Task 7) builds them.

`Ledger` is the host-private drive ledger (Task 4, supervisor/ledger.py), referenced here by name only so
this module does not depend on it. `Seat` is the board's row (Task 3); the driver reads it as `SeatView`."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

# Action kinds the pure `next_actions` may return (spec 3.2-3.3, plus the rollover completion of 3.5-3.6).
ACTION_KINDS = (
    "launch", "record_unreachable", "close", "hold", "nothing", "rollover", "stop_seat", "complete_land",
)

# Verdict values a judge may return; a `not_met` in drive mode carries `open` (spec 3.6).
VERDICTS = ("met", "not_met", "unreachable")


@dataclass(frozen=True, slots=True)
class SeatView:
    """One row of the `seats` table (spec 3.4). `seat` is the stable name, unique per job and generation."""
    id: int
    job: str
    seat: str
    role: str
    title: str | None
    harness: str
    model: str | None
    workdir: str | None
    brief_path: str | None
    task_ref: str | None
    complexity: str
    requested_by: str | None
    requested_at: datetime | None
    state: str
    agent_key: str | None
    session_id: str | None
    generation: int
    revision: int
    launches: int
    last_outcome: str | None
    wake_reason: str | None
    wake_watermark: int | None
    updated_at: datetime | None


@dataclass(frozen=True, slots=True)
class Question:
    """An open human question of one seat, receipted by the requester or the asking agent (spec 3.10)."""
    id: int
    job: str
    seat: str
    generation: int
    text: str
    default: str | None
    until: datetime | None
    receipt_id: int | None


@dataclass(frozen=True, slots=True)
class HandoffView:
    """A receipted `swarm done --artifact REF` hand-off. `group` is the artifact group (the branch, or the
    `report:<path>` for report artifacts), `artifact` is `group@<sha>` at this revision."""
    artifact: str
    group: str
    by: str
    at: datetime | None
    receipt_id: int | None


@dataclass(frozen=True, slots=True)
class VerdictView:
    """A receipted judge verdict (spec 3.6-3.7). `open` is the unmet-criteria count of a `not_met`;
    `progress` is the judge's explicit progress flag, stored as a bool (None when not given)."""
    verdict: str
    scope: str
    artifact: str | None
    candidate: str | None
    req: str | None
    generation: int
    open: int | None
    progress: bool | None
    reason: str
    next: str
    by: str | None
    at: datetime | None
    receipt_id: int | None


@dataclass(frozen=True, slots=True)
class Snapshot:
    """One consistent read of a drive job for the pure `next_actions` (spec 3.2). `live` holds the seat
    names with a live attempt; `handoffs`, `verdicts`, `goal_verdict`, `questions` are receipted only."""
    job: str
    generation: int
    goal: str
    status: str
    halted: bool
    ledger: Any
    seats: dict[str, SeatView]
    live: set[str]
    handoffs: list[HandoffView]
    reviews: dict[str, str]
    verdicts: dict[str, VerdictView]
    goal_verdict: VerdictView | None
    goal_ready: dict | None
    questions: dict[str, list[Question]]
    wakes: dict[str, list[int]]
    integrated: dict[str, dict]
    now: datetime


@dataclass(frozen=True, slots=True)
class Action:
    """One decision of `next_actions`. `action_id` is `gen|kind|seat|artifact-group|round|event-id`; `attempt`
    numbering is the caller's. `not_before` is set on `hold`; `responsible` names who is making progress on
    `nothing` (a running seat or runner)."""
    kind: str
    action_id: str
    seat: str | None
    prompt_kind: str | None
    artifact: str | None = None
    ruling: bool = False
    reason: str = ""
    not_before: datetime | None = None
    responsible: str | None = None
