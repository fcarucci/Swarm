"""The frozen data model of the job driver (spec 3.2-3.4, plan Task 7a), and the pure decision `next_actions`
(spec 3.3, plan Task 7). The dataclasses hold no logic; `next_actions` and `progress` do no I/O.

`Ledger` is the host-private drive ledger (Task 4, supervisor/ledger.py), referenced here by name only so
this module does not depend on it. `Seat` is the board's row (Task 3); the driver reads it as `SeatView`."""
from __future__ import annotations

from dataclasses import dataclass
import datetime as dt
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


# --- decisions ---------------------------------------------------------------------------------
# Action reasons that count against the single progress counter `rounds_without_progress` (spec 3.3, "One
# progress counter"). A judged round counts unless `progress()` says it moved.
NO_PROGRESS_REASONS = frozenset({"invariant-fallback", "stall", "max-seats-refused", "finalize-blocked"})

# Shipped `[drive]` defaults (spec 4.1 and 3.x); a job's `ledger.budgets` overrides them, and the caller's cfg
# overrides these. `recipe` and `review_role` are the recipe keys of spec 3.6 and 3.7 (coding: a reviewer).
DEFAULT_CFG: dict = {
    "max_sessions": 60, "max_seats": 12, "max_rounds": 8, "no_progress_rounds": 3, "max_attempts": 3,
    "max_cost_usd": 40, "ruling_reserve_usd": 5, "session_usd_cap": 5, "min_session_usd": 0.5,
    "max_session_minutes": 60, "max_wall_hours": 24, "ruling_attempts": 2, "ruling_deadline_minutes": 120,
    "ruling_grace_minutes": 15, "question_expiry": "4h", "stall_minutes": 90, "driver_error_passes": 6,
    "max_concurrent_sessions": 4, "lease_hold_minutes": 60, "recipe": "coding", "review_role": "reviewer",
}

_LIVE_SEAT_STATES = ("requested", "idle", "running")
_DEAD_SEAT_STATES = ("released", "failed", "cancelled")


def _minutes(value: Any) -> float:
    """A duration as minutes: a number, or text such as "4h", "90m", "30s"."""
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().lower()
    units = {"s": 1 / 60, "m": 1.0, "h": 60.0}
    if text and text[-1] in units:
        return float(text[:-1]) * units[text[-1]]
    return float(text)


def _ts(text: str | None) -> datetime | None:
    return dt.datetime.fromisoformat(text) if text else None


def _is_lead_name(name: str) -> bool:
    return name == "lead" or name.startswith("lead~")


def _lead_index(name: str) -> int:
    return 0 if name == "lead" else int(name.split("~", 1)[1])


def _active_lead(seats: dict[str, SeatView]) -> str | None:
    names = [n for n in seats if _is_lead_name(n)]
    return max(names, key=_lead_index) if names else None


def _valid(vd: VerdictView | None, gen: int) -> bool:
    """A verdict counts only when receipted and of this generation (spec 3.6; unreceipted records ignored)."""
    return vd is not None and vd.receipt_id is not None and vd.generation == gen


class _Ctx:
    """Read-only view of one snapshot plus the helpers every step shares."""

    def __init__(self, s: Snapshot, cfg: dict) -> None:
        self.s = s
        self.cfg = {**DEFAULT_CFG, **cfg, **(s.ledger.budgets or {})}
        self.now = s.now
        self.live = s.live
        self.seats = s.seats
        self.lead = _active_lead(s.seats)
        self.open = [i for i in s.ledger.intents if i.state != "concluded"]
        self.exhausted = False
        self.expiry = dt.timedelta(minutes=_minutes(self.cfg["question_expiry"]))
        # The ruling's trigger: its due time. The caller records ledger.ruling_due_at at the first pass that
        # finds a due reason (Task 8); until it does, the quiesce holds from now.
        self.rtrig = s.ledger.ruling_due_at or "unrecorded"

    def aid(self, prompt: str, seat: str | None, group: str | None, rnd: int, trigger: str) -> str:
        """`gen<N>|<prompt kind>|<seat>|<group>|<round>|<trigger>`: retries of one trigger share the id."""
        return f"gen{self.s.generation}|{prompt}|{seat or '-'}|{group or '-'}|{rnd}|{trigger}"

    def act(self, kind: str, seat: str | None, group: str | None, rnd: int, *, prompt: str | None = None,
            trigger: object = None, artifact: str | None = None, ruling: bool = False, reason: str = "",
            not_before: datetime | None = None, responsible: str | None = None) -> Action:
        slot = prompt or kind
        trig = "-" if trigger is None else str(trigger)
        return Action(kind=kind, action_id=self.aid(slot, seat, group, rnd, trig), seat=seat, prompt_kind=prompt,
                      artifact=artifact, ruling=ruling, reason=reason, not_before=not_before,
                      responsible=responsible)

    def attempts(self, action_id: str) -> int:
        """Attempts of one logical action so far. Outage attempts do not count (spec 3.3 [F12])."""
        return sum(1 for i in self.s.ledger.intents
                   if i.action_id == action_id and i.outcome not in ("outage", "lost"))

    def inflight(self, group: str) -> bool:
        """An unconcluded launch for this artifact group (or the goal/ruling slot) is still running."""
        return any((i.action_id or "").split("|")[3:4] == [group] for i in self.open)

    def seat_has_open_intent(self, name: str) -> bool:
        return any(i.seat == name for i in self.open)

    def last_launch(self, name: str) -> str:
        ids = [i.launch_id for i in self.s.ledger.intents if i.seat == name]
        return ids[-1] if ids else "none"

    def gated(self, name: str) -> datetime | None:
        """The time a seat's open question holds it until (spec 3.10, [F1]): capped at question_expiry."""
        until = None
        for question in self.s.questions.get(name, []):
            if question.receipt_id is None:
                continue
            end = min(question.until or self.now + self.expiry, self.now + self.expiry)
            if end > self.now and (until is None or end > until):
                until = end
        return until

    def is_lead_wakeable(self) -> bool:
        return self.lead is not None and self.seats[self.lead].state == "idle" and self.gated(self.lead) is None

    def sessions(self) -> int:
        return sum(1 for i in self.s.ledger.intents if i.outcome != "lost")

    def due_reason(self) -> str | None:
        """Why a ruling is due now (spec 3.3 step 3, 4.1, 3.6, 3.8 [F19], 3.2 [F22], 3.5 [F3])."""
        led, cfg = self.s.ledger, self.cfg
        if led.errors >= cfg["driver_error_passes"]:
            return "driver-errors"
        if led.rounds_without_progress >= cfg["no_progress_rounds"]:
            return "no-progress"
        cap, reserve, charged = cfg["max_cost_usd"], cfg["ruling_reserve_usd"], led.charged_usd()
        if cap - reserve - charged < cfg["min_session_usd"]:                 # 4.1: seats keep the reserve
            return "budget-cost"
        if self.sessions() >= cfg["max_sessions"]:
            return "budget-sessions"
        activated = _ts(led.activated_at)
        if activated is not None and self.now - activated >= dt.timedelta(hours=cfg["max_wall_hours"]):
            return "wall-clock"
        for entry in self.s.integrated.values():
            since = _ts((entry or {}).get("lease_held_since"))
            if since is not None and self.now - since > dt.timedelta(minutes=cfg["lease_hold_minutes"]):
                return "lease-hold"                                           # [F19]
        if any(n != "lead" and _is_lead_name(n) and self.seats[n].state == "failed" for n in self.seats):
            return "lead-successor-failed"                                    # [F3]
        return None

    def last_activity(self) -> datetime | None:
        stamps = [x.updated_at for x in self.seats.values() if x.updated_at is not None]
        stamps += [h.at for h in self.s.handoffs if h.at is not None]
        stamps += [vd.at for vd in self.s.verdicts.values() if vd.at is not None]
        return max(stamps) if stamps else None

    def ruling_action(self, reason: str) -> Action:
        return self.act("launch", None, "ruling", 0, prompt="ruling", trigger=self.rtrig, ruling=True,
                        reason=reason)


def _terminal(c: _Ctx) -> list[Action] | None:
    """Step 2 (spec 3.3): a receipted terminal goal or ruling verdict closes the job (spec 3.7)."""
    gv = c.s.goal_verdict
    if _valid(gv, c.s.generation) and gv.verdict in ("met", "unreachable") and gv.scope in ("goal", "ruling"):
        return [c.act("close", None, None, 0, prompt="close", trigger=gv.receipt_id, reason=gv.verdict)]
    return None


def _ruling(c: _Ctx) -> list[Action] | None:
    """Step 3 (spec 3.3, 3.6 ruling judge, 2.2 deadlines): quiesce, then one ruling judge, then mechanical
    UNREACHABLE when ruling attempts or the ruling deadline run out."""
    led = c.s.ledger
    reason = c.due_reason()
    if led.ruling_due_at is None and reason is None:
        return None
    since = _ts(led.ruling_due_at) or c.now
    reason = reason or "ledger"
    elapsed = c.now - since
    if c.live:                                                                # quiesce (spec 3.6)
        grace = dt.timedelta(minutes=c.cfg["ruling_grace_minutes"])
        if elapsed < grace:
            return [c.act("hold", None, "ruling", 0, prompt="quiesce", trigger=c.rtrig, ruling=True,
                          reason=reason, not_before=since + grace)]
        return [c.act("stop_seat", name, "ruling", 0, prompt="quiesce", trigger=c.seats[name].revision,
                      ruling=True, reason=reason)
                for name in sorted(c.live)]
    deadline = dt.timedelta(minutes=c.cfg["ruling_deadline_minutes"])
    if led.ruling_attempts >= c.cfg["ruling_attempts"] or elapsed >= deadline:  # 2.2: mechanical
        cause = "ruling attempts exhausted" if led.ruling_attempts >= c.cfg["ruling_attempts"] \
            else "ruling deadline passed"
        return [c.act("record_unreachable", None, "ruling", 0, prompt="mechanical", trigger=c.rtrig,
                      ruling=True, reason=cause)]
    if c.inflight("ruling"):
        return [c.act("nothing", None, None, 0, responsible="ruling", reason="ruling-in-flight")]
    if led.charged_usd() >= c.cfg["max_cost_usd"]:                            # reserve gone too (4.1)
        return [c.act("record_unreachable", None, "ruling", 0, prompt="mechanical", trigger=c.rtrig,
                      ruling=True, reason="budget reserve spent")]
    return [c.ruling_action(reason)]


def _verdict(c: _Ctx, artifact: str) -> VerdictView | None:
    vd = c.s.verdicts.get(artifact)
    return vd if _valid(vd, c.s.generation) else None


def _owner_seat(c: _Ctx, owner: str) -> str | None:
    """The seat that resumes a fix round (spec 3.4 ownership transfer): the owner, or its successor
    `<owner>~<n>` when the owner is released, failed or cancelled."""
    st = c.seats.get(owner)
    if st is None:
        return owner
    if owner in c.live:
        return None
    if st.state not in _DEAD_SEAT_STATES:
        return owner
    base = owner.split("~", 1)[0]
    alive = sorted(n for n in c.seats if n.startswith(base + "~") and c.seats[n].state in _LIVE_SEAT_STATES)
    if alive:
        return alive[0]
    n = 1
    while f"{base}~{n}" in c.seats:
        n += 1
    return f"{base}~{n}"


def _pipeline(c: _Ctx) -> list[Action] | None:
    """Step 4 (spec 3.3, 3.6, 3.8): review, judge, fix on the owner, integrate, FINALIZE_BLOCKED. Withdrawn
    groups (F9) and groups with a launch in flight are skipped. Out-of-attempts actions set `exhausted`.
    The trigger of each action is the record that caused it (plan 88854be)."""
    s, cfg = c.s, c.cfg
    latest: dict[str, HandoffView] = {}
    count: dict[str, int] = {}
    for h in s.handoffs:
        latest[h.group] = h
        count[h.group] = count.get(h.group, 0) + 1
    out: list[Action] = []
    for group, h in latest.items():
        ig = s.integrated.get(group) or {}
        if ig.get("withdrawn") or c.inflight(group):
            continue
        art, rnd, rec = h.artifact, count[group], h.receipt_id
        owner = s.ledger.owners.get(group) or h.by
        vd = _verdict(c, art)
        review = s.reviews.get(art)
        act = None
        if vd is None:
            if cfg["review_role"] and review is None:                         # unreviewed → reviewer (3.6)
                act = c.act("launch", None, group, rnd, prompt="review", trigger=rec, artifact=art)
            elif review == "changes":                                          # CHANGES → fix round (3.6)
                act = _fix(c, group, art, rnd, owner, "changes", rec)
            else:
                act = c.act("launch", None, group, rnd, prompt="judge", trigger=rec, artifact=art)
        elif vd.verdict == "not_met":                                          # not_met → fix (3.6)
            act = _fix(c, group, art, rnd, owner, "not-met", vd.receipt_id)
        elif vd.verdict == "met":
            if cfg["recipe"] == "artifact":                                    # 3.7 [G3]: no integration
                continue
            if ig.get("blocked"):                                              # FINALIZE_BLOCKED (3.8)
                act = _fix(c, group, art, rnd, owner, "finalize-blocked", ig.get("blocked_id", rec))
            elif not ig.get("landed"):                                         # met → integrator (3.8)
                act = c.act("launch", None, group, rnd, prompt="integrate", trigger=rec, artifact=art)
        if act is None:
            continue
        if c.attempts(act.action_id) >= cfg["max_attempts"]:                  # out of attempts (3.2)
            c.exhausted = True
            continue
        out.append(act)
    return out or None


def _fix(c: _Ctx, group: str, art: str, rnd: int, owner: str, reason: str, trigger: object) -> Action | None:
    seat = _owner_seat(c, owner)
    if seat is None or c.gated(seat) is not None:
        return None
    return c.act("launch", seat, group, rnd, prompt="fix", trigger=trigger, artifact=art, reason=reason)


def _seat_requests(c: _Ctx) -> list[Action]:
    """Step 5 (spec 3.4): fill requested seats within the host cap. `max_seats` counts distinct seats of the
    generation, dead ones included (spec 4.1): a request beyond the cap is refused, and wakes the lead once
    (counted, spec 3.3 [F11]). Launch attempts are counted per trigger (the seat revision)."""
    out: list[Action] = []
    refused: list[int] = []
    room = c.cfg["max_concurrent_sessions"] - len(c.live)     # per job; the host-wide cap is Task 8
    admitted = sum(1 for st in c.seats.values() if st.state != "requested")
    for name in sorted(c.seats, key=lambda n: c.seats[n].id):
        st = c.seats[name]
        if st.state != "requested" or c.gated(name) is not None:
            continue
        if admitted >= c.cfg["max_seats"]:
            refused.append(st.revision)
            continue
        admitted += 1
        if room <= 0:
            continue
        act = c.act("launch", name, None, 0, prompt="seat", trigger=st.revision, reason="request")
        if c.attempts(act.action_id) >= c.cfg["max_attempts"]:
            c.exhausted = True
            continue
        out.append(act)
        room -= 1
    if refused and c.is_lead_wakeable():
        out.append(c.act("launch", c.lead, None, 0, prompt="wake", trigger=min(refused),
                         reason="max-seats-refused"))
    return out


def _seat_wakes(c: _Ctx) -> list[Action]:
    """Step 6 (spec 3.4, 3.10): an idle non-lead seat with pending wake events resumes, unless gated. The
    trigger is the smallest pending event id (coalesced wakes)."""
    out: list[Action] = []
    for name in sorted(c.s.wakes):
        ids = c.s.wakes[name] or []
        st = c.seats.get(name)
        if not ids or st is None or st.state != "idle" or name in c.live or _is_lead_name(name):
            continue
        if c.gated(name) is not None:
            continue
        out.append(c.act("launch", name, None, 0, prompt="wake", trigger=min(ids), reason="wake"))
    return out


def _stalls(c: _Ctx) -> list[Action] | None:
    """Step 7 (spec 3.9): a running seat that is not live is relaunched (stall, counted); with seats live and no
    progress for `stall_minutes`, the lead is woken (counted). The relaunch trigger is the lost launch."""
    out = []
    for name in sorted(c.seats):
        st = c.seats[name]
        if st.state == "running" and name not in c.live and not c.seat_has_open_intent(name) \
                and c.gated(name) is None:
            out.append(c.act("launch", name, None, 0, prompt="relaunch", trigger=c.last_launch(name),
                             reason="stall"))
    if out:
        return out
    last = c.last_activity()
    if c.live and c.is_lead_wakeable() and last is not None \
            and c.now - last >= dt.timedelta(minutes=c.cfg["stall_minutes"]):
        return [c.act("launch", c.lead, None, 0, prompt="stall", trigger=last.isoformat(), reason="stall")]
    return None


def _lead_wake(c: _Ctx) -> list[Action] | None:
    """Step 8 (spec 3.5, [F3]): the active lead resumes on lead-wake events while idle. A failed first lead gets
    one successor `lead~1`; its failure is a ruling (see `_due_reason`)."""
    name = c.lead
    if name is None:
        return None
    st = c.seats[name]
    if st.state == "failed" and name == "lead":
        return [c.act("launch", "lead~1", None, 0, prompt="lead", trigger=st.revision, reason="lead-successor")]
    if c.gated(name) is not None:
        return None
    if st.state == "idle" and c.s.wakes.get(name):
        return [c.act("launch", name, None, 0, prompt="wake", trigger=min(c.s.wakes[name]), reason="wake")]
    return None


def _ts_of(gr: dict) -> datetime | None:
    at = gr.get("at")
    if isinstance(at, str):
        return _ts(at)
    return at if isinstance(at, datetime) else None


def _goal(c: _Ctx) -> list[Action] | None:
    """Step 9 (spec 3.7, 3.7 [G3], [F9]): GOAL-READY with nothing live and every non-withdrawn group integrated
    (coding) or judged met (artifact) launches the goal judge. The verdict must bind the pinned requirements
    (`req`), so a GOAL-READY without one is not judged."""
    s = c.s
    gr = s.goal_ready
    if gr is None or not gr.get("req") or c.live or c.inflight("goal"):
        return None
    gv = s.goal_verdict
    at = _ts_of(gr)
    if _valid(gv, s.generation) and (gv.verdict != "not_met" or at is None or gv.at is None or at <= gv.at):
        return None
    for group, h in {x.group: x for x in s.handoffs}.items():
        ig = s.integrated.get(group) or {}
        if ig.get("withdrawn"):
            continue
        if c.cfg["recipe"] == "artifact":
            vd = _verdict(c, h.artifact)
            if vd is None or vd.verdict != "met":
                return None
        elif not ig.get("landed") or ig.get("blocked"):
            return None
    return [c.act("launch", None, "goal", 0, prompt="goal", trigger=gr.get("id"), artifact=gr.get("candidate"),
                  reason="goal-ready")]


def _fallback(c: _Ctx) -> list[Action]:
    """Step 10 (spec 3.3 invariant): nothing live, nothing pending. A gated seat holds the job until its question
    expires (F1; the lead is not woken, the gated seat is responsible). Otherwise wake the lead (counted), or
    rule when the lead cannot run."""
    gates = []
    for name in sorted(c.seats):
        until = c.gated(name)
        if until is not None:
            gates.append((until, name))
    if gates:
        until, _ = min(gates)
        return [c.act("hold", None, None, 0, prompt="question", trigger=until.isoformat(),
                      reason="question-gated", not_before=until)]
    lead = c.lead
    if lead is not None and c.seats[lead].state in ("idle", "requested"):
        return [c.act("launch", lead, None, 0, prompt="fallback", trigger=c.s.ledger.rounds_without_progress,
                      reason="invariant-fallback")]
    return [c.ruling_action("lead-unavailable")]          # the lead cannot run: a ruling (spec 3.3 step 10)


def next_actions(s: Snapshot, cfg: dict) -> list[Action]:
    """The pure decision of one driver pass for one job (spec 3.3, steps 1-10, invariant included). Never
    returns an empty list: an active unhalted job always gets a launch, a hold, a record, or a `nothing` that
    names who is responsible."""
    c = _Ctx(s, cfg)
    if s.halted or s.status != "active":                                       # step 1
        return [c.act("nothing", None, None, 0, reason="halted" if s.halted else "inactive")]
    if s.ledger.terminal is not None:
        return [c.act("nothing", None, None, 0, reason="terminal")]
    for step in (_terminal, _ruling, _pipeline):                               # steps 2, 3, 4
        found = step(c)
        if found:
            return found
    accumulated = _seat_requests(c) + _seat_wakes(c)                           # steps 5, 6
    if accumulated:
        return accumulated
    for step in (_stalls, _lead_wake, _goal):                                  # steps 7, 8, 9
        found = step(c)
        if found:
            return found
    if c.live or c.open:
        return [c.act("nothing", None, None, 0, responsible=min(c.live) if c.live else "runner",
                      reason="responsible")]
    if c.exhausted:                                                            # 3: nothing else to do
        return [c.ruling_action("attempts-exhausted")]
    return _fallback(c)


def counts_as_no_progress(action: Action) -> bool:
    """True for the wakes and refusals that always advance `rounds_without_progress` (spec 3.3)."""
    return action.reason in NO_PROGRESS_REASONS


def progress(prev: VerdictView | None, new: VerdictView, integrated: bool = False) -> bool:
    """Spec 3.6 (plan eceb35e): a judged round is progress when the judge passed `--progress yes` (its
    `progress` is True), or an artifact group was integrated, or its artifact is new and `open` decreased.
    A goal verdict's decreased `open` also counts."""
    if integrated or new.progress is True:
        return True
    if prev is None:
        return False
    if prev.open is None or new.open is None or new.open >= prev.open:
        return False
    if new.scope == "goal":
        return True
    return new.artifact != prev.artifact
