"""Factories for the drive driver's model, shared by the Task 7-8c tests.

Every factory takes keyword overrides for every field of the type it builds (tests/test_drive_model.py
checks this), and every default is a fresh container, so tests never share state. `L` mirrors the field
names of eng-ledger's `Ledger` (lib/swarm/supervisor/ledger.py @ 60f507d); `spent_usd` is a shorthand that
seeds one concluded intent, because the Ledger has no spent_usd field (it has charged_usd())."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from dataclasses import InitVar
from typing import Any

import support  # noqa: F401  (puts lib/ on sys.path)
from swarm.supervisor import driver_model as dm

T0 = dt.datetime(2026, 10, 10, 12, 0, tzinfo=dt.timezone.utc)


def iso(t: dt.datetime) -> str:
    return t.isoformat()


def minutes(n: float) -> dt.timedelta:
    return dt.timedelta(minutes=n)


def seat(name: str = "a", state: str = "idle", role: str = "engineer", **kw: Any) -> dm.SeatView:
    base = dict(
        id=1, job="J", seat=name, role=role, title=None, harness="claude", model="haiku",
        workdir="/home/claude/src/wt-" + name, brief_path="/home/claude/src/briefs/" + name + ".md",
        task_ref=None, complexity="standard", requested_by="lead", requested_at=T0, state=state,
        agent_key=None, session_id=None, generation=1, revision=1, launches=0, last_outcome=None,
        wake_reason=None, wake_watermark=None, updated_at=T0,
    )
    return dm.SeatView(**{**base, **kw})


def q(id: int = 1, seat: str = "a", **kw: Any) -> dm.Question:
    base = dict(
        id=id, job="J", seat=seat, generation=1, text="which way?", default="left",
        until=T0 + minutes(240), receipt_id=1,
    )
    return dm.Question(**{**base, **kw})


def ho(artifact: str = "feat/a@" + "a" * 40, by: str = "a", **kw: Any) -> dm.HandoffView:
    base = dict(artifact=artifact, group=str(artifact).partition("@")[0], by=by, at=T0, receipt_id=1)
    return dm.HandoffView(**{**base, **kw})


def v(verdict: str = "not_met", scope: str = "artifact", **kw: Any) -> dm.VerdictView:
    base = dict(
        verdict=verdict, scope=scope, artifact=None, candidate=None, req=None, generation=1,
        open=None, progress=None,
        reason="", next="", by="judge", at=T0, receipt_id=1,
    )
    return dm.VerdictView(**{**base, **kw})


@dataclass
class Intent:
    """Stand-in for eng-ledger's Intent (same field names)."""
    launch_id: str
    action_id: str
    attempt: int
    seat: str | None
    reserved_usd: float
    reserved_minutes: float
    locks: list
    state: str
    outcome: str | None = None
    success: bool | None = None
    charged_usd: float | None = None


@dataclass
class L:
    """Lightweight stand-in for eng-ledger's Ledger: the same field names and defaults, and charged_usd()."""
    board_key: str = "board-key"
    job: str = "J"
    generation: int = 1
    generation_requested: int = 1
    activated_at: str = ""
    budgets: dict = field(default_factory=dict)
    intents: list = field(default_factory=list)
    owners: dict = field(default_factory=dict)
    integrated: dict = field(default_factory=dict)
    ruling_due_at: str | None = None
    ruling_attempts: int = 0
    rounds_without_progress: int = 0
    errors: int = 0
    closing: str | None = None
    halted: dict | None = None
    terminal: dict | None = None
    mirror_fetch_pending: list = field(default_factory=list)
    last_pass: str | None = None
    watermarks: dict = field(default_factory=dict)
    terminal_intent: dict | None = None
    halted_total_seconds: float = 0.0
    requester_session: str | None = None
    pinned_requirements: dict = field(default_factory=dict)
    spent_usd: InitVar[float | None] = None

    def __post_init__(self, spent_usd: float | None) -> None:
        if spent_usd is not None:
            self.intents.append(Intent(
                launch_id="L0", action_id="gen1|launch|a|-|1|-", attempt=1, seat="a",
                reserved_usd=spent_usd, reserved_minutes=0.0, locks=[], state="concluded",
                outcome="completed", success=True, charged_usd=spent_usd,
            ))

    def charged_usd(self) -> float:
        total = 0.0
        for i in self.intents:
            if i.state == "concluded":
                total += i.charged_usd or 0.0
            else:
                total += i.reserved_usd
        return total


def intent(state: str = "concluded", reserved: float = 0.0, charged: float | None = None,
           seat: str | None = "a", **kw: Any) -> Intent:
    base = dict(launch_id="L0", action_id="gen1|launch|" + str(seat) + "|-|1|-", attempt=1, seat=seat,
                reserved_usd=reserved, reserved_minutes=0.0, locks=[], state=state, outcome=None,
                success=None, charged_usd=charged)
    return Intent(**{**base, **kw})


def snap(**kw: Any) -> dm.Snapshot:
    base = dict(
        job="J", generation=1, goal="g", status="active", halted=False, ledger=L(), seats={},
        live=set(), handoffs=[], reviews={}, verdicts={}, goal_verdict=None, goal_ready=None,
        questions={}, wakes={}, integrated={}, now=T0,
    )
    return dm.Snapshot(**{**base, **kw})
