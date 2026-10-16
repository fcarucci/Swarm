"""The frozen data model of the job driver (spec 3.2-3.4, plan Task 7a), and the pure decision `next_actions`
(spec 3.3, plan Task 7). The dataclasses hold no logic; `next_actions` and `progress` do no I/O.

`Ledger` is the host-private drive ledger (Task 4, supervisor/ledger.py), referenced here by name only so
this module does not depend on it. `Launch` is the board's row (Task 3); the driver reads it as `LaunchView`."""
from __future__ import annotations

from dataclasses import dataclass, replace
import datetime as dt
from datetime import datetime
from typing import Any

# Action kinds the pure `next_actions` may return (spec 3.2-3.3, plus the rollover completion of 3.5-3.6).
ACTION_KINDS = (
    "launch", "record_unreachable", "close", "hold", "nothing", "rollover", "stop_launch", "complete_land",
    "escalate_landing", "fix_lead", "no_progress",
)

# Verdict values a judge may return; a `not_met` in drive mode carries `open` (spec 3.6).
VERDICTS = ("met", "not_met", "unreachable")


@dataclass(frozen=True, slots=True)
class LaunchView:
    """One row of the `launches` table (spec 3.4). `launch` is the stable name, unique per job and generation."""
    id: int
    job: str
    name: str
    role: str
    harness: str
    model: str | None
    workdir: str | None
    brief: str | None
    state: str
    agent_key: str | None
    session_id: str | None
    generation: int
    revision: int
    attempts: int
    last_outcome: str | None
    queued_by: str
    updated_at: datetime | None


@dataclass(frozen=True, slots=True)
class Question:
    """An open human question of one launch, receipted by the requester or the asking agent (spec 3.10)."""
    id: int
    job: str
    launch: str
    generation: int
    text: str
    default: str | None
    until: datetime | None
    receipt_id: int | None
    opened_at: datetime | None = None   # the receipted time it was opened; None: the driver's first sight of it


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
    """One consistent read of a drive job for the pure `next_actions` (spec 3.2). `live` holds the launch
    names with a live attempt; `handoffs`, `verdicts`, `goal_verdict`, `questions` are receipted only."""
    job: str
    generation: int
    goal: str
    status: str
    halted: bool
    ledger: Any
    launches: dict[str, LaunchView]
    live: set[str]
    handoffs: list[HandoffView]
    reviews: dict[str, str]
    verdicts: dict[str, VerdictView]
    goal_verdict: VerdictView | None
    goal_ready: dict | None
    questions: dict[str, list[Question]]
    wakes: dict[str, list[int]]
    integrated: dict[str, dict]
    rounds: dict[str, int]
    now: datetime
    # The job's learnings step has not run since its terminal verdict, or agents are unharvested (swarm.learnings;
    # filled in by the pass only once a terminal goal or ruling verdict is there).
    learn_pending: bool = False


@dataclass(frozen=True, slots=True)
class Action:
    """One decision of `next_actions`. `action_id` is `gen|kind|launch|artifact-group|round|event-id`; `attempt`
    numbering is the caller's. `not_before` is set on `hold`; `responsible` names who is making progress on
    `nothing` (a running launch or runner)."""
    kind: str
    action_id: str
    launch: str | None
    prompt_kind: str | None
    artifact: str | None = None
    ruling: bool = False
    reason: str = ""
    not_before: datetime | None = None
    responsible: str | None = None


# --- decisions ---------------------------------------------------------------------------------
# Action reasons that count against the single progress counter `rounds_without_progress` (spec 3.3, "One
# progress counter"). A judged round counts unless `progress()` says it moved.
NO_PROGRESS_REASONS = frozenset({"invariant-fallback", "stall", "finalize-blocked"})

# Concluded attempts that never count against `max_attempts` (spec 3.3 [F12], 4.3 [F5]): an outage, a lost
# claim race, an attempt a halt ended.
UNCOUNTED_OUTCOMES = ("outage", "lost", "halted", "interrupted by driver restart", "cancelled-before-start", "blocked")

# Shipped `[drive]` defaults (spec 4.1 and 3.x); a job's `ledger.budgets` overrides them, and the caller's cfg
# overrides these. `recipe` and `review_role` are the recipe keys of spec 3.6 and 3.7 (coding: a reviewer).
DEFAULT_CFG: dict = {
    "max_sessions": 60, "max_rounds": 8, "no_progress_rounds": 3, "max_attempts": 3,
    "max_cost_usd": 40, "ruling_reserve_usd": 5, "session_usd_cap": 5, "min_session_usd": 0.5,
    "max_session_minutes": 60, "max_wall_hours": 24, "ruling_attempts": 2, "ruling_deadline_minutes": 120,
    "ruling_grace_minutes": 15, "question_expiry": "4h", "stall_minutes": 90, "driver_error_passes": 6,
    "max_concurrent_sessions": 4, "lease_hold_minutes": 60, "recipe": "coding", "review_role": "reviewer",
}

_LIVE_LAUNCH_STATES = ("queued", "done", "running")
_DEAD_LAUNCH_STATES = ("failed",)


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


def _active_lead(launches: dict[str, LaunchView]) -> str | None:
    names = [n for n in launches if _is_lead_name(n)]
    return max(names, key=_lead_index) if names else None


def _valid(vd: VerdictView | None, gen: int) -> bool:
    """A verdict counts only when receipted and of this generation (spec 3.6; unreceipted records ignored)."""
    return vd is not None and vd.receipt_id is not None and vd.generation == gen


def requeue_action_id(led, action_id: str, launch: str | None) -> str:
    """An explicit run grants a fresh attempt allowance, retaining old charges."""
    epoch = led.watermarks.get(f"requeue:{led.generation}:{launch}", 0) if launch else 0
    suffix = f"|requeue{epoch}"
    return action_id + suffix if epoch and not action_id.endswith(suffix) else action_id


class _Ctx:
    """Read-only view of one snapshot plus the helpers every step shares."""

    def __init__(self, s: Snapshot, cfg: dict) -> None:
        self.s = s
        self.cfg = {"landing_required": True, **DEFAULT_CFG, **cfg, **(s.ledger.budgets or {})}
        self.now = s.now
        self.live = s.live
        self.launches = s.launches
        self.lead = _active_lead(s.launches)
        self.open = [i for i in s.ledger.intents if i.state != "concluded"]
        self.exhausted = False
        self.expiry = dt.timedelta(minutes=_minutes(self.cfg["question_expiry"]))
        # The ruling's trigger: its due time. The caller records ledger.ruling_due_at at the first pass that
        # finds a due reason (Task 8); until it does, the quiesce holds from now.
        self.rtrig = s.ledger.ruling_due_at or "unrecorded"

    def aid(self, prompt: str, launch: str | None, group: str | None, rnd: int, trigger: str) -> str:
        """`gen<N>|<prompt kind>|<launch>|<group>|<round>|<trigger>`: retries of one trigger share the id."""
        aid = f"gen{self.s.generation}|{prompt}|{launch or '-'}|{group or '-'}|{rnd}|{trigger}"
        return requeue_action_id(self.s.ledger, aid, launch)

    def act(self, kind: str, launch: str | None, group: str | None, rnd: int, *, prompt: str | None = None,
            trigger: object = None, artifact: str | None = None, ruling: bool = False, reason: str = "",
            not_before: datetime | None = None, responsible: str | None = None) -> Action:
        slot = prompt or kind
        trig = "-" if trigger is None else str(trigger)
        return Action(kind=kind, action_id=self.aid(slot, launch, group, rnd, trig), launch=launch, prompt_kind=prompt,
                      artifact=artifact, ruling=ruling, reason=reason, not_before=not_before,
                      responsible=responsible)

    def attempts(self, action_id: str) -> int:
        """Attempts of one logical action so far. Outage attempts do not count (spec 3.3 [F12])."""
        return sum(1 for i in self.s.ledger.intents
                   if i.action_id == action_id and i.outcome not in UNCOUNTED_OUTCOMES)

    def finished(self, action_id: str) -> bool:
        """An ended launch is never retried for the same trigger. A lost claim did not launch."""
        retryable = ("lost", "halted", "interrupted by driver restart", "cancelled-before-start", "blocked")
        return any(i.action_id == action_id and i.state == "concluded" and i.outcome not in retryable
                   for i in self.s.ledger.intents)

    def inflight(self, group: str) -> bool:
        """An unconcluded launch for this artifact group (or the goal/ruling slot) is still running."""
        return any((i.action_id or "").split("|")[3:4] == [group] for i in self.open)

    def launch_has_open_intent(self, name: str) -> bool:
        return any(i.launch == name for i in self.open)

    def last_launch(self, name: str) -> str:
        ids = [i.launch_id for i in self.s.ledger.intents if i.launch == name]
        return ids[-1] if ids else "none"

    def gated(self, name: str) -> datetime | None:
        """The time a launch's open question holds it until (spec 3.10, [F1]): capped at question_expiry."""
        until = None
        for question in self.s.questions.get(name, []):
            if question.receipt_id is None:
                continue
            end = min(question.until or self.now + self.expiry, self.now + self.expiry)
            if end > self.now and (until is None or end > until):
                until = end
        return until

    def is_lead_wakeable(self) -> bool:
        return self.lead is not None and self.launches[self.lead].state == "done" and self.gated(self.lead) is None

    def sessions(self) -> int:
        return session_count(self.s.ledger)

    def due_reason(self) -> str | None:
        """Why a ruling is due now (spec 3.3 step 3, 4.1, 3.6, 3.8 [F19], 3.2 [F22], 3.5 [F3])."""
        led, cfg = self.s.ledger, self.cfg
        if led.rounds_without_progress >= cfg["no_progress_rounds"]:
            return "no-progress"
        # 4.1: per group, `>=` (the 8th judged round is the cap). `rounds` already excludes met, landed and
        # withdrawn groups (driver_snapshot._rounds), so a group still in flight reaches the cap here.
        if any(judged >= cfg["max_rounds"] for judged in self.s.rounds.values()):
            return "max-rounds"
        cap, reserve, charged = cfg["max_cost_usd"], cfg["ruling_reserve_usd"], led.charged_usd()
        if cap - reserve - charged < cfg["min_session_usd"]:                 # 4.1: launches keep the reserve
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
        if any(n != "lead" and _is_lead_name(n) and self.launches[n].state == "failed" for n in self.launches):
            return "lead-successor-failed"                                    # [F3]
        return None

    def last_activity(self) -> datetime | None:
        """The newest activity time that a stall wake reacts to: launch rows, hand-offs and verdicts. A woken lead
        updates its own launch row, so the clock restarts and wakes recur every stall_minutes (bounded by budgets)."""
        stamps = [x.updated_at for x in self.launches.values() if x.updated_at is not None]
        stamps += [h.at for h in self.s.handoffs if h.at is not None]
        stamps += [vd.at for vd in self.s.verdicts.values() if vd.at is not None]
        return max(stamps) if stamps else None

    def ruling_action(self, reason: str) -> Action:
        return self.act("launch", None, "ruling", 0, prompt="ruling", trigger=self.rtrig, ruling=True,
                        reason=reason, artifact=(self.s.goal_ready or {}).get("candidate"))


def candidate_landed(integrated: dict, candidate: str | None) -> bool:
    """Whether the judged `candidate` (a sha, or `ref@sha`) is the landed head of a group: `landed` is set only
    from a land receipt verified at that sha on every pinned destination (driver_snapshot._landed), so this is
    "INTEGRATED, on all destinations, at exactly the candidate"."""
    sha = (candidate or "").rpartition("@")[2].strip().lower()
    if len(sha) < 7:
        return False
    for entry in (integrated or {}).values():
        landed = str((entry or {}).get("landed") or "").lower()
        if len(landed) >= 7 and (landed.startswith(sha) or sha.startswith(landed)):
            return True
    return False


def _terminal(c: _Ctx) -> list[Action] | None:
    """A terminal verdict closes after learnings; a MET waits only for pinned destinations."""
    gv = c.s.goal_verdict
    if _valid(gv, c.s.generation) and gv.verdict in ("met", "unreachable") and gv.scope in ("goal", "ruling"):
        if gv.verdict == "met" and c.cfg["landing_required"] and not candidate_landed(
                c.s.integrated, gv.candidate):
            return _met_landing(c, gv)
        return _learn_first(c, gv.receipt_id) or [
            c.act("close", None, None, 0, prompt="close", trigger=gv.receipt_id, reason=gv.verdict)]
    return None


def _met_landing(c: _Ctx, gv: VerdictView) -> list[Action]:
    """MET is settled goal evidence: retry landing, or notify on backoff, never rule unreachable."""
    candidate = gv.candidate or ""
    sha = candidate.rpartition("@")[2]
    h = next((h for h in reversed(c.s.handoffs) if h.artifact.endswith("@" + sha)), None)
    artifact = h.artifact if h else candidate
    group = artifact.partition("@")[0] if "@" in artifact else "landing"
    if c.inflight(group):
        return [c.act("nothing", None, None, 0, responsible="integrator", reason="met, landing in flight")]
    key = f"met-landing:{gv.receipt_id}"
    until = c.s.ledger.watermarks.get(key + ":retry-at", 0)
    if until > c.now.timestamp():
        return [c.act("hold", None, group, 0, reason="met, landing retry backoff",
                      not_before=dt.datetime.fromtimestamp(until, dt.timezone.utc))]
    base = c.aid("integrate", None, group, 0, str(gv.receipt_id))
    prior = [i for i in c.s.ledger.intents if i.action_id.startswith(base + "|retry")]
    n = sum(i.outcome not in UNCOUNTED_OUTCOMES for i in prior)
    error = (c.s.integrated.get(group) or {}).get("landing_error")
    reason = error or (prior[-1].outcome if prior else None) or "integrator attempts exhausted"
    if ("@" not in artifact or n >= c.cfg["max_attempts"]
            or (prior and prior[-1].outcome not in UNCOUNTED_OUTCOMES
                and c.s.ledger.watermarks.get(key + ":notified", 0) < len(prior))):
        if "@" not in artifact:
            reason = "no artifact identifies the candidate to land"
        return [c.act("escalate_landing", None, group, n, trigger=gv.receipt_id, artifact=artifact,
                      reason=f"met, landing blocked: {reason}")]
    action = c.act("launch", None, group, 0, prompt="integrate", trigger=gv.receipt_id,
                   artifact=artifact, reason="met, candidate awaits landing")
    return [replace(action, action_id=base + f"|retry{len(prior)}")]


def _learn_first(c: _Ctx, trigger) -> list[Action] | None:
    """The learnings step before a close (swarm.learnings): while it is pending, launch the driver-owned
    `learner` launch (its session runs `swarm learn` over the job; the drive loop itself makes no LLM call), or
    wait for the one running. Its attempts are ledger intents like any launch, so a crashed driver resumes it.
    None (close now) once the step is done; a hold once the learner has used max_attempts without finishing."""
    if not c.s.learn_pending:
        return None
    if c.inflight("learn") or "learner" in c.live:
        return [c.act("nothing", None, None, 0, responsible="learner", reason="responsible")]
    action = c.act("launch", None, "learn", 0, prompt="learn", trigger=trigger, reason="learnings step before close")
    if c.finished(action.action_id):
        # Never close past an undone learnings step: the job stays open (hold) for a person's `swarm learn`
        # or `swarm deactivate --no-learn`; the unharvested markers stay on it.
        return [c.act("hold", None, None, 0, prompt="learn", trigger=trigger,
                      reason="learnings step failed: attempts exhausted; not closing")]
    return [action]


def _ruling(c: _Ctx) -> list[Action] | None:
    """Quiesce for a ruling; exhausted attempts and deadlines wait for a person."""
    led = c.s.ledger
    vd = c.s.goal_verdict
    if _engineers_pending(c) and (c.s.goal_ready or any(_is_lead_name(h.by) for h in c.s.handoffs)
            or _valid(vd, c.s.generation) and vd.scope == "goal" and vd.verdict == "not_met"):
        return None
    if (_valid(vd, c.s.generation) and vd.scope == "ruling" and vd.verdict == "not_met"
            and same_head((c.s.goal_ready or {}).get("candidate"), vd.candidate)
            and c.lead and (c.launches[c.lead].state == "queued" or c.lead in c.live)):
        return None
    reason = c.due_reason()
    if led.ruling_due_at is None and reason is None:
        return None
    since = _ts(led.ruling_due_at) or c.now
    reason = reason or "ledger"
    if c.lead and c.launches[c.lead].last_outcome == "no hand-off":
        reason = f"{c.lead}: no hand-off after recovery round: {reason}"
    elapsed = c.now - since
    if c.live:                                                                # quiesce (spec 3.6)
        grace = dt.timedelta(minutes=c.cfg["ruling_grace_minutes"])
        if elapsed < grace:
            return [c.act("hold", None, "ruling", 0, prompt="quiesce", trigger=c.rtrig, ruling=True,
                          reason=reason, not_before=since + grace)]
        return [c.act("stop_launch", name, "ruling", 0, prompt="quiesce", trigger=c.launches[name].revision,
                      ruling=True, reason=reason)
                for name in sorted(c.live)]
    deadline = dt.timedelta(minutes=c.cfg["ruling_deadline_minutes"])
    if led.ruling_attempts >= c.cfg["ruling_attempts"] or elapsed >= deadline:  # 2.2: mechanical
        cause = "ruling attempts exhausted" if led.ruling_attempts >= c.cfg["ruling_attempts"] \
            else "ruling deadline passed"
        return [budget_hold(c, cause)]
    if c.inflight("ruling"):
        return [c.act("nothing", None, None, 0, responsible="ruling", reason="ruling-in-flight")]
    if led.charged_usd() >= c.cfg["max_cost_usd"]:                            # reserve gone too (4.1)
        return [budget_hold(c, "budget reserve spent")]
    prior = [i for i in led.intents if i.launch == "ruling-judge" and i.state == "concluded"
             and i.outcome not in UNCOUNTED_OUTCOMES]
    if prior:
        return [c.act("launch", None, "ruling", len(prior), prompt="ruling", trigger=c.rtrig,
                      ruling=True, reason=reason, artifact=(c.s.goal_ready or {}).get("candidate"))]
    return [c.ruling_action(reason)]


def _ruling_fix(c: _Ctx) -> list[Action] | None:
    vd = c.s.goal_verdict
    if (not _valid(vd, c.s.generation) or vd.scope not in ("goal", "ruling") or vd.verdict != "not_met"
            or c.s.ledger.watermarks.get(f"{vd.scope}-fix") == vd.receipt_id):
        return None
    if vd.scope == "goal" and _goal_candidate(c) and not _unchanged_goal(c, _goal_candidate(c)):
        return None
    return [c.act("fix_lead", c.lead, None, 0, prompt="fix", trigger=vd.receipt_id,
                  reason=vd.reason, artifact=vd.candidate)]


def _judge_retries(c: _Ctx) -> list[Action] | None:
    """Missing verdicts keep their original task, with fresh action ids and bounded retry frequency."""
    if _judges_wait(c):
        return None
    for name, row in c.launches.items():
        if not c.s.ledger.watermarks.get(f"verdict-missing:{name}"):
            continue
        if name in c.live or c.launch_has_open_intent(name):
            return [c.act("nothing", name, None, 0, responsible=name, reason="verdict-retry-running")]
        if row.state != "queued":
            continue
        until = c.s.ledger.watermarks.get(f"retry-at:{name}", 0)
        if until > c.now.timestamp():
            return [c.act("hold", name, None, 0, reason="verdict-retry-backoff",
                          not_before=dt.datetime.fromtimestamp(until, dt.timezone.utc))]
        prior = next((i for i in reversed(c.s.ledger.intents) if i.launch == name), None)
        if prior is None:
            continue
        return [_resume_action(c, name, row, prior, "verdict-retry")]
    return None


def _resume_action(c: _Ctx, name: str, row: LaunchView, prior, trigger: str) -> Action:
    parts = prior.action_id.split("|")
    kind, group = parts[1], parts[3]
    artifact = next((h.artifact for h in reversed(c.s.handoffs) if h.group == group), None)
    if kind in ("goal", "ruling"):
        artifact = (c.s.goal_ready or {}).get("candidate")
    reason = next((k[len("ruling-trigger:"):] for k in c.s.ledger.watermarks
                   if k.startswith("ruling-trigger:")), trigger)
    return c.act("launch", name, None if group == "-" else group, row.revision,
                 prompt=kind, trigger=trigger, artifact=artifact, ruling=kind == "ruling", reason=reason)


def _recover_launches(c: _Ctx) -> list[Action] | None:
    """A driver interruption and the promised lead recovery precede mechanical ruling triggers."""
    for name, row in c.launches.items():
        if row.role == "judge" and _judges_wait(c):
            continue
        if _is_lead_name(name) and row.last_outcome == "blocked":
            if name in c.live or c.launch_has_open_intent(name):
                return [c.act("nothing", name, None, 0, responsible=name, reason="blocked-retry-running")]
            until = c.s.ledger.watermarks.get(f"retry-at:{name}", 0)
            if until > c.now.timestamp():
                return [c.act("hold", name, None, 0, reason="blocked-retry-backoff",
                              not_before=dt.datetime.fromtimestamp(until, dt.timezone.utc))]
        if _is_lead_name(name) and c.s.ledger.watermarks.get(f"lead-recovery:{name}") and (
                name in c.live or c.launch_has_open_intent(name)):
            return [c.act("nothing", name, None, 0, responsible=name, reason="lead-recovery-running")]
        if row.state != "queued" or name in c.live or c.launch_has_open_intent(name) or c.gated(name):
            continue
        if row.last_outcome not in ("halted", "interrupted by driver restart", "blocked") and not (
                _is_lead_name(name) and c.s.ledger.watermarks.get(f"lead-recovery:{name}")):
            continue
        prior = next((i for i in reversed(c.s.ledger.intents) if i.launch == name), None)
        if prior is not None:
            return [_resume_action(c, name, row, prior, "recovery")]
    return None


def _verdict(c: _Ctx, artifact: str) -> VerdictView | None:
    vd = c.s.verdicts.get(artifact)
    return vd if _valid(vd, c.s.generation) else None


def _owner_launch(c: _Ctx, owner: str) -> str | None:
    """The launch that resumes a fix round (spec 3.4 ownership transfer): the owner, or its successor
    `<owner>~<n>` when the owner is released, failed or cancelled."""
    st = c.launches.get(owner)
    if st is None:
        return owner
    if owner in c.live:
        return None
    if st.state not in _DEAD_LAUNCH_STATES:
        return owner
    return None


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
        if _is_lead_name(h.by) and (_engineers_pending(c) or _unchanged_goal(c, art)):
            continue
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
                if _engineers_pending(c):
                    continue
                act = c.act("launch", None, group, rnd, prompt="judge", trigger=rec, artifact=art)
        elif vd.verdict == "not_met":                                          # not_met → fix (3.6)
            act = _fix(c, group, art, rnd, owner, "not-met", vd.receipt_id)
        elif vd.verdict == "met":
            if not cfg["landing_required"]:                                    # no pinned destination
                continue
            if ig.get("blocked"):                                              # FINALIZE_BLOCKED (3.8)
                act = _fix(c, group, art, rnd, owner, "finalize-blocked", ig.get("blocked_id", rec))
            elif not ig.get("landed"):                                         # met → integrator (3.8)
                act = c.act("launch", None, group, rnd, prompt="integrate", trigger=rec, artifact=art)
        if act is None:
            continue
        if c.finished(act.action_id):
            c.exhausted = True
            continue
        out.append(act)
    return out or None


def _fix(c: _Ctx, group: str, art: str, rnd: int, owner: str, reason: str, trigger: object) -> Action | None:
    launch = _owner_launch(c, owner)
    if launch is None or c.gated(launch) is not None:
        return None
    return c.act("launch", launch, group, rnd, prompt="fix", trigger=trigger, artifact=art, reason=reason)


def _queued_launches(c: _Ctx) -> list[Action]:
    """Start queued launches in insertion order, within this job's concurrency budget."""
    room = c.cfg["max_concurrent_sessions"] - len(c.live)
    out = []
    for name in sorted(c.launches, key=lambda n: c.launches[n].id):
        st = c.launches[name]
        if name == "ruling-judge":
            continue  # ruling retries go through the head and generation guards
        if st.role == "judge" and _judges_wait(c):
            continue
        if room <= 0:
            break
        if st.state != "queued" or name in c.live or c.launch_has_open_intent(name) or c.gated(name):
            continue
        out.append(c.act("launch", name, None, 0, prompt="launch", trigger=st.revision, reason="queued"))
        room -= 1
    return out


def _launch_wakes(c: _Ctx) -> list[Action]:
    """Step 6 (spec 3.4, 3.10): an idle non-lead launch with pending wake events resumes, unless gated. The
    trigger is the smallest pending event id (coalesced wakes)."""
    out: list[Action] = []
    for name in sorted(c.s.wakes):
        ids = c.s.wakes[name] or []
        st = c.launches.get(name)
        if not ids or st is None or st.state != "done" or name in c.live or _is_lead_name(name):
            continue
        if st.role == "judge" and _judges_wait(c):
            continue
        if c.gated(name) is not None:
            continue
        out.append(c.act("launch", name, None, 0, prompt="wake", trigger=min(ids), reason="wake"))
    return out


def _stalls(c: _Ctx) -> list[Action] | None:
    """Step 7 (spec 3.9): a running launch that is not live is relaunched (stall, counted); with launches live and no
    progress for `stall_minutes`, the lead is woken (counted). The relaunch trigger is the lost launch."""
    out = []
    for name in sorted(c.launches):
        st = c.launches[name]
        if st.role == "judge" and _judges_wait(c):
            continue
        if st.state == "running" and name not in c.live and not c.launch_has_open_intent(name) \
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
    st = c.launches[name]
    if c.gated(name) is not None:
        return None
    if st.state == "done" and c.s.wakes.get(name):
        return [c.act("launch", name, None, 0, prompt="wake", trigger=min(c.s.wakes[name]), reason="wake")]
    return None


def _ts_of(gr: dict) -> datetime | None:
    at = gr.get("at")
    if isinstance(at, str):
        return _ts(at)
    return at if isinstance(at, datetime) else None


def _goal(c: _Ctx) -> list[Action] | None:
    """GOAL-READY with nothing live launches the goal judge once every group is landed
    (when destinations are pinned, at least one group) or judged met (no destinations).
    The verdict binds pinned requirements (`req`); GOAL-READY without them is not judged."""
    s = c.s
    gr = s.goal_ready
    if gr is None or not gr.get("req") or c.live or c.inflight("goal") or _engineers_pending(c):
        return None
    if _unchanged_goal(c, gr.get("candidate")):
        return None
    gv = s.goal_verdict
    at = _ts_of(gr)
    if _valid(gv, s.generation) and (gv.verdict != "not_met" or at is None or gv.at is None or at <= gv.at):
        return None
    no_landing = not c.cfg["landing_required"]
    landed = 0
    for group, h in {x.group: x for x in s.handoffs}.items():
        ig = s.integrated.get(group) or {}
        if ig.get("withdrawn"):
            continue
        if no_landing:
            vd = _verdict(c, h.artifact)
            if vd is None or vd.verdict != "met":
                return None
        elif not ig.get("landed") or ig.get("blocked"):
            return None
        landed += 1
    if not no_landing and not landed:
        # a coding job with no (non-withdrawn) hand-off group has landed nothing: "no groups" is not "all
        # integrated" (an engineer's DONE that was never receipted leaves none). Not ready: the fallback
        # wakes the lead for work, never the goal judge on an unlanded branch.
        return None
    action = c.act("launch", None, "goal", 0, prompt="goal", trigger=gr.get("id"), artifact=gr.get("candidate"),
                   reason="goal-ready")
    return None if c.finished(action.action_id) else [action]


def _fallback(c: _Ctx) -> list[Action]:
    """Step 10 (spec 3.3 invariant): nothing live, nothing pending. A gated launch holds the job until its question
    expires (F1; the lead is not woken, the gated launch is responsible). Otherwise wake the lead (counted), or
    rule when the lead cannot run."""
    gates = []
    for name in sorted(c.launches):
        until = c.gated(name)
        if until is not None:
            gates.append((until, name))
    if gates:
        until, _ = min(gates)
        return [c.act("hold", None, None, 0, prompt="question", trigger=until.isoformat(),
                      reason="question-gated", not_before=until)]
    lead = c.lead
    if _unchanged_goal(c, _goal_candidate(c)):
        return [goal_hold(c, "waiting for the work the judge asked for; needs a person: "
                          + (c.s.goal_verdict.next or c.s.goal_verdict.reason))]
    if lead is not None and c.launches[lead].state in ("done", "queued"):
        return [c.act("launch", lead, None, 0, prompt="fallback", trigger=c.s.ledger.rounds_without_progress,
                      reason="invariant-fallback")]
    if lead and c.launches[lead].last_outcome == "no hand-off":
        return [c.ruling_action(f"{lead}: no hand-off after recovery round")]
    return [c.ruling_action("lead-unavailable")]          # the lead cannot run: a ruling (spec 3.3 step 10)


def _next_actions(s: Snapshot, cfg: dict) -> list[Action]:
    """The pure decision of one driver pass for one job (spec 3.3, steps 1-10, invariant included). Never
    returns an empty list: an active unhalted job always gets a launch, a hold, a record, or a `nothing` that
    names who is responsible."""
    c = _Ctx(s, cfg)
    if s.halted or s.status != "active":                                       # step 1
        return [c.act("nothing", None, None, 0, reason="halted" if s.halted else "inactive")]
    if s.ledger.terminal is not None:
        return [c.act("nothing", None, None, 0, reason="terminal")]
    for step in (_terminal, _ruling_fix, _judge_retries, _recover_launches, _ruling):                    # steps 2, 3, 4
        found = step(c)
        if found:
            return found
    accumulated = _queued_launches(c) + _launch_wakes(c)                           # steps 5, 6
    if accumulated:
        return accumulated
    for step in (_pipeline, _stalls, _lead_wake, _goal):                                  # steps 7, 8, 9
        found = step(c)
        if found:
            return found
    if c.live or c.open:
        return [c.act("nothing", None, None, 0, responsible=min(c.live) if c.live else "runner",
                      reason="responsible")]
    if c.exhausted:                                                            # 3: nothing else to do
        return [c.ruling_action("attempts-exhausted")]
    return _fallback(c)


def session_count(led) -> int:
    """Session allowances reset per generation; monetary charges remain job-wide."""
    return sum(1 for i in led.intents if i.action_id.startswith(f"gen{led.generation}|")
               and i.outcome not in UNCOUNTED_OUTCOMES)


def reservation_reason(d: dict, led, ruling: bool, now=None) -> str | None:
    """Why the job cannot reserve another session, shared with queued-launch diagnostics."""
    if now is not None and led.activated_at:
        activated = dt.datetime.fromisoformat(led.activated_at)
        if now - activated >= dt.timedelta(hours=d["max_wall_hours"]):
            return "wall-clock budget exhausted"
    sessions = session_count(led)
    if sessions >= d["max_sessions"]:
        return "session budget exhausted"
    left = d["max_cost_usd"] - led.charged_usd() - (0 if ruling else d["ruling_reserve_usd"])
    usd = min(float(d["session_usd_cap"]), left)
    if usd < d["min_session_usd"] and not (ruling and usd > 0):
        return "cost budget cannot fund a session while keeping the ruling reserve"
    return None


def budget_hold(c: _Ctx, reason: str) -> Action:
    return c.act("hold", None, None, 0, prompt="budget", reason=reason,
                 not_before=c.now + dt.timedelta(seconds=300))


def ruling_head(candidate: str | None) -> str:
    return (candidate or "unknown").rpartition("@")[2].lower()


def same_head(candidate: str | None, judged: str | None) -> bool:
    head, prior = ruling_head(candidate), ruling_head(judged)
    return head == prior or (min(len(head), len(prior)) >= 7
                             and (head.startswith(prior) or prior.startswith(head)))


def _engineers_pending(c: _Ctx) -> bool:
    return any(row.role == "engineer" and (row.state in ("queued", "running")
               or name in c.live or c.launch_has_open_intent(name)) for name, row in c.launches.items())


def _judges_wait(c: _Ctx) -> bool:
    return _engineers_pending(c) and bool(c.s.goal_ready or any(_is_lead_name(h.by) for h in c.s.handoffs))


def _goal_candidate(c: _Ctx) -> str | None:
    gr = c.s.goal_ready or {}
    h = next((h for h in reversed(c.s.handoffs) if _is_lead_name(h.by)), None)
    return h.artifact if h and (h.receipt_id or 0) > gr.get("id", 0) else gr.get("candidate")


def _unchanged_goal(c: _Ctx, candidate: str | None) -> bool:
    vd = c.s.goal_verdict
    return (bool(candidate) and _valid(vd, c.s.generation) and vd.scope == "goal"
            and vd.verdict == "not_met" and same_head(candidate, vd.candidate))


def goal_hold(c: _Ctx, reason: str) -> Action:
    return replace(budget_hold(c, reason), prompt_kind="goal-hold")


def _same_sha_handoff(c: _Ctx) -> list[Action] | None:
    vd = c.s.goal_verdict
    if not _valid(vd, c.s.generation) or vd.scope != "goal" or vd.verdict != "not_met":
        return None
    if not _unchanged_goal(c, _goal_candidate(c)):
        return None
    repeats = [h for h in c.s.handoffs if _is_lead_name(h.by) and h.receipt_id is not None
               and h.receipt_id > vd.receipt_id and same_head(h.artifact, vd.candidate)]
    gr = c.s.goal_ready or {}
    name = c.lead or "lead"
    ready_id = gr.get("id", 0) if same_head(gr.get("candidate"), vd.candidate) else 0
    rec = max([h.receipt_id for h in repeats] + [ready_id])
    if rec <= vd.receipt_id or rec <= c.s.ledger.watermarks.get(f"no-progress:{name}", 0):
        return None
    if not repeats:
        repeats = [HandoffView(gr.get("candidate"), "goal", name, None, rec)]
    h = repeats[-1]
    reason = (f"lead: same head {ruling_head(h.artifact)} already ruled not_met #{vd.receipt_id}; "
              "waiting for the work the judge asked for")
    if len(repeats) >= 2 or c.s.ledger.watermarks.get(f"no-progress-verdict:{name}") == vd.receipt_id:
        return [goal_hold(c, "lead hands off an unchanged head; needs a person: " + (vd.next or vd.reason))]
    return [c.act("no_progress", name, None, 0, trigger=rec, artifact=h.artifact, reason=reason)]


def next_actions(s: Snapshot, cfg: dict) -> list[Action]:
    """Limits wait for a person; a judged head never spends another ruling session."""
    c = _Ctx(s, cfg)
    if s.halted or s.status != "active" or s.ledger.terminal is not None:
        return _next_actions(s, cfg)
    # Closing and an exhausted learnings hold need no session; preserve both past the limits.
    # An unlanded MET still needs a funded integrator.
    terminal = _terminal(c)
    if terminal and all(a.kind == "close" or a.kind == "hold" and a.prompt_kind == "learn"
                        for a in terminal):
        return terminal
    if s.ledger.watermarks.get("goal-hold"):
        vd = s.goal_verdict
        return [goal_hold(c, "goal judge held; needs a person: " + ((vd.next or vd.reason) if vd else "judge launch cap"))]
    if _valid(s.goal_verdict, s.generation) and s.goal_verdict.scope == "goal":
        fix = _ruling_fix(c)
        if fix:
            return fix
    no_progress = _same_sha_handoff(c)
    if no_progress:
        return no_progress
    reason = reservation_reason(c.cfg, s.ledger, False, s.now)
    if reason:
        return [budget_hold(c, reason)]
    actions = _next_actions(s, cfg)
    if any(a.kind == "launch" and (a.ruling or a.prompt_kind in ("goal", "judge", "ruling")
           or a.launch in c.launches and c.launches[a.launch].role == "judge")
           and _unchanged_goal(c, a.artifact or _goal_candidate(c)) for a in actions):
        return [goal_hold(c, "head already ruled not_met; needs a person: "
                          + (s.goal_verdict.next or s.goal_verdict.reason))]
    for index, a in enumerate(actions):
        if a.kind != "launch" or not (a.prompt_kind == "goal" or a.launch == "goal-judge"):
            continue
        a = replace(a, prompt_kind="goal", artifact=a.artifact or _goal_candidate(c))
        actions[index] = a
        head = ruling_head(a.artifact or _goal_candidate(c))
        prior = [i for i in s.ledger.intents if i.action_id.startswith(f"gen{s.generation}|")
                 and (i.launch == "goal-judge" or "|goal|" in i.action_id)
                 and i.outcome not in ("lost", "cancelled-before-start")]
        prefix = f"goal-head:{s.generation}:"
        unlaunched = {i.launch_id for i in s.ledger.intents if i.outcome in ("lost", "cancelled-before-start")}
        ruled = any(value and same_head(key[len(prefix):].split(":", 1)[0], head)
                    and key.rpartition(":")[2] not in unlaunched
                    for key, value in s.ledger.watermarks.items() if key.startswith(prefix))
        if ruled or len(prior) >= 6 or _unchanged_goal(c, head):
            return [goal_hold(c, f"goal judge launch limit (generation {s.generation}, head {head}); needs a person")]
        if _engineers_pending(c):
            return [c.act("nothing", None, None, 0, responsible="engineers", reason="waiting for queued engineers")]
    for a in actions:
        limit = c.cfg["ruling_attempts"] if a.ruling else c.cfg["max_attempts"]
        if a.kind == "launch" and c.attempts(a.action_id) >= limit:
            return [budget_hold(c, f"attempt limit reached ({limit}) for {a.launch or a.prompt_kind}")]
    if not any(a.ruling and a.kind == "launch" or a.launch == "ruling-judge" and a.kind == "launch"
               for a in actions):
        return actions
    vd = s.goal_verdict
    head = ruling_head((s.goal_ready or {}).get("candidate"))
    if (_valid(vd, s.generation) and vd.scope == "ruling" and vd.verdict == "not_met"
            and same_head(head, vd.candidate)):
        lead_actions = _recover_launches(c) or _queued_launches(c)
        if c.lead and c.launches[c.lead].state == "queued":
            ready = [a for a in (lead_actions or []) if a.launch == c.lead]
            if ready:
                return ready
        if c.lead and c.launches[c.lead].state in ("done", "failed"):
            return [budget_hold(c, f"ruling launch limit (head {head} already ruled not_met #{vd.receipt_id})")]
        return [c.act("hold", None, None, 0,
                      reason=f"waiting for the lead's fix round (head {head} already ruled not_met #{vd.receipt_id})")]
    key = f"ruling-head:{s.generation}:{head}"
    prior = [i for i in s.ledger.intents if i.launch == "ruling-judge"
             and i.action_id.startswith(f"gen{s.generation}|")
             and i.outcome not in ("lost", "cancelled-before-start")]
    ruled = s.ledger.watermarks.get(key, 0) or any(
        s.ledger.watermarks.get(f"{key}:{i.launch_id}", 0) for i in prior)
    if ruled or len(prior) >= 3:
        return [budget_hold(c, f"ruling launch limit (generation {s.generation}, head {head})")]
    return actions


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
