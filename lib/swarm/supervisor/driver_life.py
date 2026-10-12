"""The life cycle of a drive job (spec 3.1 [F6][RR2][G4], 3.2, 3.10 [RR3], 4.3 [F5], 4.5): deadlines,
ledger-first terminalization, generation rollover, halt, question expiry and the host sweeps.

Everything here decides from the host-private ledger, never from what the board says: a forged board
`met` does not stop a deadline, and a mechanical UNREACHABLE (no receipt, no judge) is recognised by
`led.terminal_intent`. Every terminal or rollover write is ledger first (fsynced by `Ledger.save`), then
the board; each function resumes from the ledger after a crash at any point (`barrier.hit` marks the
points the chaos tests kill at).

The collaborators that belong to other tasks are injected through `Hooks`, so this module has no import
of them: the runner (stopping sessions and confirming their scopes absent, Task 9/8a), the closing gate
(target moved, requirements changed, audit; Tasks 10/11b), `complete_land` (11b), the lead-seat request
with its driver authorization (Tasks 5/8a) and the post-close capture. The defaults are the safe stubs
(no gate, no land journal, no capture).

Ledger conventions this module writes, for the readers of the ledger (the snapshot, `next_actions`):
  * `closing` is "<verdict>: <cause>" while a terminal intent is pending (the spec's `drive.closing`).
  * `watermarks["close_rejected"]` counts the rejected closes; `watermarks["rejected:<candidate>"] = 1`
    marks a goal verdict whose candidate was rejected by the closing gate: that verdict is no longer
    terminal and the goal judge reruns on the new candidate.
  * `watermarks["qdone:<blocker id>"] = 1` marks a question whose default the driver has applied."""
from __future__ import annotations

import datetime as dt
import logging
import re
from dataclasses import dataclass
from typing import Any, Callable

import time

from swarm import compat, receipts
from swarm.board.base import CloseGuard
from swarm.supervisor import barrier, driver_events as ev

log = logging.getLogger("swarm.supervisor.driver_life")

SUPERVISOR = "swarm supervisor"             # the name the driver's board posts carry
CLOSED_BY = "driver"                        # jobs.closed_by of a job the driver closed
MECHANICAL = "swarm supervisor (mechanical)"   # terminal_intent["by"] of a verdict nobody judged
JUDGE_KEY = "driver-"                       # + job: the agent key the mechanical verdict is recorded under
DEFAULTS = {"max_wall_hours": 24, "ruling_deadline_minutes": 120, "close_deadline_minutes": 60,
            "question_expiry": "4h"}
_CANCEL_BY = "cancel_by:"                      # watermark: a person asked to cancel while a close was under way
_HOPEN = "hopen:"                           # watermark: an intent that was open when the halt began
_GEN = re.compile(r"gen(\d+)\|")


# --- collaborators ---------------------------------------------------------------------------

@dataclass(frozen=True)
class LandResult:
    """What 11b's `complete_land` reports: state "none" (no land journal), "done", "pending" (still
    completing: try again next pass) or "partial" (only part of the journal could be completed)."""
    state: str = "none"
    detail: str = ""


@dataclass(frozen=True)
class Gate:
    """The closing gate's answer for a pending MET: "pass", "reject" (the target moved, requirements
    changed, the audit failed: `reason`) or "unknown" (the git host cannot be asked now)."""
    verdict: str = "pass"
    reason: str = ""


def _stub_land(board, cfg, led, now) -> LandResult:
    return LandResult()          # until Task 11b's complete_land is integrated


def _stub_gate(board, cfg, led, intent, now) -> Gate:
    return Gate()                # until Tasks 10/11b wire the closing gate


def landed_gate(board, cfg, led, intent, now) -> Gate:
    """The close-time check of a pending MET of a coding job: the judged candidate must be landed, i.e. the
    head of a group whose land receipt is verified at that sha on every pinned destination (the snapshot's own
    rule, driver_model.candidate_landed). A MET on unlanded work is refused. An artifact job is not checked; a job
    whose owner record cannot be read is "unknown" (fails closed)."""
    if intent.get("verdict") != "met":
        return Gate()
    from swarm.supervisor import driver_model as dm, driver_snapshot as ds
    try:
        recipe = ds.drive_cfg(led.board_key, led.job, cfg or {})["recipe"]
    except ds.DriveConfigError as exc:     # fails closed: a missing or bad owner record never closes a MET
        return Gate("unknown", f"the owner record of this job cannot be read ({exc}): the landing is not known")
    if recipe == "artifact":
        return Gate()
    rc = ds._Receipts(led, led.job, intent.get("generation") or led.generation, now)
    if dm.candidate_landed(ds._integrated(led, rc, {}), intent.get("candidate")):
        return Gate()
    return Gate("reject", f"the judged candidate {str(intent.get('candidate'))[:48]} is not landed on every "
                          "pinned destination (no verified INTEGRATED at that sha)")


def _run_for(cfg: dict, row):
    from swarm.board.autoinit import store_key
    from swarm.supervisor import runner
    return runner._read_run(f"r{row.id}.json", store_key(cfg))


def default_stop_rows(board, cfg: dict, rows: list) -> None:
    """Stop the session (scope, else process group) of every restart row that has a run file here."""
    from swarm.supervisor import runner
    for row in rows:
        run = _run_for(cfg, row)
        if isinstance(run, dict) and (run.get("unit") or run.get("child_pgid") or run.get("child_pid")):
            try:
                runner._stop_session(run, grace=2.0)
            except Exception as exc:
                log.warning("stopping restart %s failed: %s", row.id, type(exc).__name__)


def default_rows_live(board, cfg: dict, rows: list) -> list:
    """The rows whose session is not confirmed gone: a held run lock, a present or unknown scope, a
    live tagged process. A row with neither a run file nor a held lock has nothing to wait for."""
    from swarm.board.autoinit import store_key
    from swarm.supervisor import runner
    live = []
    for row in rows:
        run = _run_for(cfg, row)
        if runner._held(row.id, store_key(cfg)):
            live.append(row)
        elif isinstance(run, dict) and run.get("unit") and not run.get("exited"):
            if runner.unit_presence(run["unit"], run.get("slice")) != "absent":
                live.append(row)
        elif isinstance(run, dict) and not run.get("exited") and runner._session_alive(run):
            live.append(row)
    return live


def _seat_decision(seat) -> dict:
    return {"kind": "seat_request", "id": str(seat.id), "rev": seat.revision, "role": seat.role,
            "model": seat.model, "brief": seat.brief_path, "workdir": seat.workdir}


def seat_receipted(led, seat) -> bool:
    """Whether the board row of `seat` still carries what its receipted `seat_request` decided (role,
    model, brief, workdir). The receipt is bound to a revision and a state change makes a new one, so
    the current row is matched against the receipts of its own and every earlier revision: one that
    matches proves the decision fields have not changed since it was receipted. Fails closed."""
    now = time.time()
    for rev in range(seat.revision, 0, -1):
        r = receipts.find(led.board_key, led.job, "seat_request", str(seat.id), rev=rev,
                          incarnation=led.incarnation, generation=seat.generation, now=now)
        if r is not None and receipts.matches(r, {**_seat_decision(seat), "rev": rev}):
            return True
    return False


def default_request_lead(board, cfg: dict, led, generation: int) -> None:
    """Request generation `generation`'s lead from the previous lead's row (spec 3.1 rollover (d)), but
    only from a row that still matches its receipted seat request (Task 5 ruling: a seat's brief,
    workdir, model and role come from what the host receipted, never from an editable board row).
    The driver-authored authorization of Task 5 is added where that module lands."""
    old = sorted((s for s in board.seats(led.job) if s.seat == "lead" and s.generation < generation),
                 key=lambda x: x.generation, reverse=True)
    if not old:
        board.post(led.job, SUPERVISOR, f"{ev.DRIVER_ROLLOVER} generation {generation}: no earlier lead seat to copy")
        return
    s = next((x for x in old if seat_receipted(led, x)), None)
    if s is None:
        board.post(led.job, SUPERVISOR,
                   f"{ev.DRIVER_ROLLOVER} generation {generation}: no earlier lead seat matches its receipted request")
        return
    board.request_seat(led.job, generation, "lead", role=s.role, harness=s.harness, model=s.model,
                       workdir=s.workdir, brief_path=s.brief_path, title=s.title, task_ref=s.task_ref,
                       complexity=s.complexity, requested_by=SUPERVISOR)


def _no_capture(board, cfg, led) -> None:
    return None                  # step 6 (transcripts, `swarm learn`), best effort, wired by the pass


@dataclass(frozen=True)
class Hooks:
    stop_rows: Callable[[Any, dict, list], None] = default_stop_rows
    rows_live: Callable[[Any, dict, list], list] = default_rows_live
    complete_land: Callable[[Any, dict, Any, dt.datetime], LandResult] = _stub_land
    gate: Callable[[Any, dict, Any, dict, dt.datetime], Gate] = landed_gate
    request_lead: Callable[[Any, dict, Any, int], None] = default_request_lead
    after_close: Callable[[Any, dict, Any], None] = _no_capture


# --- small helpers ---------------------------------------------------------------------------

def _iso(t: dt.datetime) -> str:
    return t.isoformat()


def _ts(text: str | None) -> dt.datetime | None:
    if not text:
        return None
    t = dt.datetime.fromisoformat(text)
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


def _opt(cfg: dict, led, key: str):
    """A drive setting: the job's own budget, else the config (flat or under [drive]), else the default."""
    for src in (led.budgets or {}, cfg or {}, (cfg or {}).get("drive") or {}):
        if isinstance(src, dict) and key in src:
            return src[key]
    return DEFAULTS[key]


def _minutes(value) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().lower()
    units = {"s": 1 / 60, "m": 1.0, "h": 60.0}
    return float(text[:-1]) * units[text[-1]] if text and text[-1] in units else float(text)


def _gen_of(action_id: str) -> int | None:
    m = _GEN.match(action_id or "")
    return int(m.group(1)) if m else None


def _post(board, job: str, text: str) -> None:
    try:
        board.post(job, SUPERVISOR, text)
    except Exception as exc:        # a notice is never worth failing a pass for
        log.warning("post to %s failed: %s", job, type(exc).__name__)


def _open_rows(board, led, generation: int | None = None) -> list:
    """The job's open restart rows on this host that belong to one of the ledger's intents (a row
    with an unknown launch_id is foreign or forged and is never touched), of one generation if given."""
    mine = {i.launch_id: i for i in led.intents}
    rows = []
    for r in board.restarts(job=led.job, host=compat.node()):
        i = mine.get(r.launch_id)
        if r.ended_at is None and i is not None and (generation is None or _gen_of(i.action_id) == generation):
            rows.append(r)
    return rows


def _conclude(led, intent, outcome: str, charged: float) -> None:
    intent.state, intent.outcome, intent.success, intent.charged_usd = "concluded", outcome, False, charged


def _end_sessions(board, led, outcome: str, generation: int | None = None) -> None:
    """Finish the open rows of the job (their sessions are confirmed gone) and conclude their intents
    as unsuccessful: charged the actual cost when the row has it, else the full reservation (R17).
    An open intent with no board row (never claimed) is concluded with nothing charged."""
    rows = {r.launch_id: r for r in board.restarts(job=led.job)}
    for i in led.intents:
        if i.state == "concluded" or (generation is not None and _gen_of(i.action_id) != generation):
            continue
        r = rows.get(i.launch_id)
        if r is None:
            _conclude(led, i, outcome, 0.0)
            continue
        if r.ended_at is None:
            board.finish_restart(r.id, "cancelled")
            r = next((x for x in board.restarts(job=led.job) if x.id == r.id), r)
        _conclude(led, i, outcome, r.cost_usd if r.cost_usd is not None else i.reserved_usd)


def _release_locks(board, led, generation: int | None = None) -> None:
    for lock in board.drive_locks():
        if lock.job == led.job and (generation is None or lock.generation == generation):
            board.release_drive_locks(lock.launch_id, job=lock.job, holder=lock.holder)


def _release_seats(board, led, generation: int | None, why: str) -> None:
    for s in board.seats(led.job, generation, include_closed=False):
        try:
            board.set_seat_state(s.id, s.revision, "released", job=led.job, last_outcome=why)
        except ValueError:
            continue                 # its launch is still open: the next pass


def _settle_blockers(board, led, how: str, generation: int | None = None) -> None:
    for b in board.blockers(led.job):
        if generation is None or b.generation in (None, generation):
            board.resolve_blocker(b.id, how, actor=SUPERVISOR, state="expired")


# --- terminal records ------------------------------------------------------------------------

def begin_terminal(board, led, verdict: str, scope: str, by: str, cause: str, now: dt.datetime,
                   candidate: str | None = None, req: str | None = None) -> bool:
    """[F6] Durably write the pending terminal intent and `closing` (before any board write). False
    when the job is already terminal or closing. The executor calls this for a receipted goal or ruling
    verdict; `enforce_deadlines` for the mechanical UNREACHABLE."""
    if led.terminal is not None or led.terminal_intent is not None:
        return False
    led.terminal_intent = {"verdict": verdict, "scope": scope, "generation": led.generation,
                           "candidate": candidate, "req": req, "by": by, "at": _iso(now)}
    led.closing = f"{verdict}: {cause}"[:64]
    led.save()
    barrier.hit("terminal_intent_saved")
    return True


def _matches(js, intent: dict) -> bool:
    return (js.verdict == intent["verdict"] and js.verdict_scope == intent["scope"]
            and js.verdict_generation == intent["generation"]
            and (intent["candidate"] is None or js.verdict_candidate == intent["candidate"])
            and (intent["req"] is None or js.verdict_req == intent["req"]))


def _report(led, cause: str) -> str:
    return (f"Recorded by the supervisor itself, not by a judge (mechanical).\n\nCause: {cause}.\n\n"
            f"Sessions launched: {len(led.intents)}; charged: {led.charged_usd():.2f} USD; rounds without "
            f"progress: {led.rounds_without_progress}; ruling attempts: {led.ruling_attempts}.\n"
            f"The goal was not shown to be met; a person may reopen it with a new goal revision.")


def _record_mechanical(board, led) -> bool:
    """Make the board's verdict the ledger's mechanical UNREACHABLE (the supervisor joins as the job's
    judge for the one write). False when it cannot be done now (a live judge holds the seat, the job is
    paused, the board is down): terminalize tries again after the sessions are stopped."""
    intent = led.terminal_intent
    js = board.job_status(led.job)
    if js is None:
        return False
    if _matches(js, intent):
        return True
    if intent["verdict"] != "unreachable" or intent["by"] != MECHANICAL:
        return False                 # a MET is the judge's record: never authored here
    cause = (led.closing or "unreachable").partition(": ")[2] or "no cause recorded"
    key = JUDGE_KEY + led.job
    try:
        name = board.allocate_name(key, led.job, "worker")
        try:
            return bool(board.take_judge_seat(key, led.job)) and board.record_verdict(
                led.job, name, "unreachable", f"mechanical: {cause}", details=_report(led, cause),
                scope=intent["scope"], generation=intent["generation"])
        finally:
            board.close_agent(key, "driver: verdict recorded")
    except Exception as exc:
        log.warning("mechanical verdict for %s not recorded now: %s", led.job, type(exc).__name__)
        return False


def _deadline_cause(cfg: dict, led, now: dt.datetime) -> str | None:
    """Spec 2.2 and [F5]: the ruling deadline (from `ruling_due_at`), and `max_wall_hours` plus the ruling
    deadline (from activation), each shifted by the time the job was halted."""
    ruling = dt.timedelta(minutes=_minutes(_opt(cfg, led, "ruling_deadline_minutes")))
    shift = dt.timedelta(seconds=led.halted_total_seconds)
    due = _ts(led.ruling_due_at)
    if due is not None and now >= due + ruling + shift:
        return "ruling deadline passed"
    start = _ts(led.activated_at)
    if start is not None and now >= start + dt.timedelta(hours=float(_opt(cfg, led, "max_wall_hours"))) + ruling + shift:
        return "wall-clock limit and ruling deadline passed"
    return None


def enforce_deadlines(board, cfg: dict, led, now: dt.datetime, *, hooks: Hooks | None = None) -> bool:
    """The minimal recovery path (spec 3.2): reads the ledger only (never the job row's verdict, a recipe
    or a snapshot). Past a deadline with no terminal outcome pending, records the mechanical UNREACHABLE
    ledger-first (then on the board, if it can be now) and starts terminalization. Skips halted jobs.
    True when it did (begin_terminal refuses while an outcome is already pending)."""
    if led.terminal is not None or led.halted is not None:
        return False
    cause = _deadline_cause(cfg, led, now)
    if cause is None:
        return False
    intent = led.terminal_intent
    if intent is not None and intent["verdict"] == "met" and "terminal_stuck" in led.watermarks:
        led.terminal_intent = None         # a pending MET that cannot finish closing: the deadline wins
        led.closing = None
        by = next((k[len(_CANCEL_BY):] for k in led.watermarks if k.startswith(_CANCEL_BY)), None)
        if by is not None:                 # ... unless a person's cancel is waiting: that wins over both
            cancel_job(board, cfg, led, by, now, hooks=hooks)
            return True
    if not begin_terminal(board, led, "unreachable", "ruling", MECHANICAL, cause, now):
        return False
    _post(board, led.job, f"{ev.DRIVER_MECHANICAL_UNREACHABLE} (no judge verdict): {cause}")
    _record_mechanical(board, led)
    barrier.hit("terminal_verdict_recorded")
    return True


def _reject_pending(board, led, reason: str, now: dt.datetime, *, ruling_due: bool = False) -> None:
    """[RR2] A pending MET the gate refuses: durably clear the intent and `closing`, record the rejection in
    the ledger, and let the goal judge run again. `ruling_due` ([G4], a gate that cannot be evaluated):
    also make a ruling due, so the deadlines apply."""
    intent = led.terminal_intent
    led.terminal_intent, led.closing = None, None
    led.watermarks["close_rejected"] = led.watermarks.get("close_rejected", 0) + 1
    if intent and intent.get("candidate"):
        led.watermarks[f"rejected:{intent['candidate']}"[:256]] = 1
    if ruling_due and led.ruling_due_at is None:
        led.ruling_due_at = _iso(now)
    led.save()
    _post(board, led.job, f"{ev.DRIVER_CLOSE_REJECTED} {reason}")


def _check_met(board, cfg, led, js, now, hooks: Hooks) -> str:
    """The driver close guard for a pending MET: "ok", "rejected" (intent cleared) or "wait"."""
    intent = led.terminal_intent
    if intent["scope"] not in ("goal", "ruling") or not _matches(js, intent):
        _reject_pending(board, led, "the board's verdict no longer matches the pending MET", now)
        return "rejected"
    answer = hooks.gate(board, cfg, led, intent, now)
    if answer.verdict == "reject":
        _reject_pending(board, led, answer.reason or "closing gate refused", now)
        return "rejected"
    if answer.verdict == "unknown":
        age = now - (_ts(intent["at"]) or now)
        if age >= dt.timedelta(minutes=_minutes(_opt(cfg, led, "close_deadline_minutes"))):
            _reject_pending(board, led, "gate not evaluable", now, ruling_due=True)
            return "rejected"
        return "wait"
    return "ok"


def terminalize(board, cfg: dict, led, now: dt.datetime, *, hooks: Hooks | None = None) -> bool:
    """Spec 3.1 terminalization, resumable at every step from the ledger. True once the job is closed and
    `led.terminal` committed; False while it waits (sessions still stopping, the land journal or the gate
    pending) or when the pending MET was rejected."""
    hooks = hooks or Hooks()
    intent = led.terminal_intent
    if led.terminal is not None:
        repair_claim(board, led)   # a claim the commit could not clear is cleared on a later pass
        return False
    if intent is None or (led.halted is not None and intent["verdict"] != "cancelled"):
        return False
    js = board.job_status(led.job)
    if js is None:
        return False
    if intent["verdict"] == "cancelled":
        return _terminalize_cancelled(board, cfg, led, intent, js, now, hooks)
    reasons: list = []
    if js.status != "active":
        # Crashed after the close: only when the LEDGER says this driver was closing (the marker is
        # written before close_job). The board's closed_by is text anyone can write.
        if js.status in ("completed", "failed") and js.closed_by == CLOSED_BY \
                and _closing_key(intent) in led.watermarks \
                and (js.status == "completed") == (intent["verdict"] == "met"):
            return _commit(board, cfg, led, intent, now, reasons, hooks)
        if js.closed_by == CLOSED_BY and "tamper_closed" not in led.watermarks:
            led.watermarks["tamper_closed"] = 1
            led.save()
            _post(board, led.job, f"{ev.DRIVER_TAMPER} the job was closed as the driver without the driver's own close")
        return False                 # closed by someone else: tamper handling is the pass's
    # step 0: finish a half-done landing first ([RR5])
    land = hooks.complete_land(board, cfg, led, now)
    if land.state == "pending":
        if now - (_ts(intent["at"]) or now) < dt.timedelta(minutes=_minutes(_opt(cfg, led, "close_deadline_minutes"))):
            return False
        land = LandResult("partial", land.detail or "land journal still pending at the close deadline")
    if land.state == "partial":
        reasons.append(f"partial land recorded: {land.detail}"[:1000])
        _post(board, led.job, f"{ev.DRIVER_LAND_PARTIAL} {land.detail}")
    # steps 1-2: `closing` is set; stop every session and confirm the scopes are gone
    rows = _open_rows(board, led)
    if rows:
        hooks.stop_rows(board, cfg, rows)
        if hooks.rows_live(board, cfg, rows):
            _note_stuck(board, cfg, led, intent, now)
            return False
    _end_sessions(board, led, "cancelled")
    led.save()
    if intent["verdict"] == "met":
        verdict = _check_met(board, cfg, led, js, now, hooks)
        if verdict == "rejected":
            by = next((k[len(_CANCEL_BY):] for k in led.watermarks if k.startswith(_CANCEL_BY)), None)
            if by is not None:         # a person's cancel was waiting behind the close: it applies now
                return cancel_job(board, cfg, led, by, now, hooks=hooks)
        if verdict != "ok":
            return False
    barrier.hit("mid_terminalization")       # sessions gone, nothing settled yet
    # steps 3-4: settle the questions, release the locks and seats
    _settle_blockers(board, led, f"job concluded: {intent['verdict']}")
    _release_locks(board, led)
    _release_seats(board, led, None, "job concluded")
    # step 5: the verdict is on the board, then the guarded close
    for a in board.agents(led.job, include_departed=False):
        if a.role == "judge" and intent["by"] == MECHANICAL:
            board.close_agent(a.agent_key, "driver: job concluding")       # its session is gone (step 2)
    if not _record_mechanical(board, led) and intent["by"] == MECHANICAL:
        return False
    js = board.job_status(led.job)
    reason = js.verdict_reason if js is not None and js.verdict_reason else (led.closing or "")
    status = "completed" if intent["verdict"] == "met" else "failed"
    led.watermarks[_closing_key(intent)] = 1       # ledger first: the close below is this driver's own
    led.save()
    if not board.close_job(led.job, status, f"{intent['verdict']}: {reason}"[:500], forced=False,
                           closed_by=CLOSED_BY, guard=CloseGuard(settled=True, pending_ok=True)):
        return False
    barrier.hit("terminal_closed_before_ledger")
    return _commit(board, cfg, led, intent, now, reasons, hooks)


def cancel_job(board, cfg: dict, led, actor: str, now: dt.datetime, *, reason: str | None = None,
               hooks: Hooks | None = None) -> bool:
    """CANCELLED: a person ended the job (`swarm deactivate`, or the pass found it closed by one). The
    caller has checked that `actor` is a person. Ledger first (the terminal intent, over whatever intent
    was pending: a person's word ends the job, even a halted one), then the sessions are stopped and
    confirmed gone, the questions settled, the locks and seats released, the board job closed if it is
    still open, the claim cleared and `led.terminal` committed. Resumable like terminalize, which it
    calls: True once done, False while the sessions are still stopping or when it is already terminal."""
    if led.terminal is not None:
        repair_claim(board, led)
        return False
    pending = led.terminal_intent
    if pending is not None and _closing_key(pending) in led.watermarks:
        # its close already began: finish that, but keep the person's cancel pending, to be applied
        # should the closing gate reject the MET meanwhile
        led.watermarks[f"{_CANCEL_BY}{actor}"[:256]] = 1
        led.save()
        return terminalize(board, cfg, led, now, hooks=hooks)
    cause = (reason or "cancelled by a person").strip()[:200]
    led.terminal_intent = {"verdict": "cancelled", "scope": "job", "generation": led.generation,
                           "candidate": None, "req": None, "by": actor, "at": _iso(now)}
    led.closing = f"cancelled: {cause}"[:64]
    led.save()
    barrier.hit("terminal_intent_saved")
    return terminalize(board, cfg, led, now, hooks=hooks)


def _terminalize_cancelled(board, cfg, led, intent: dict, js, now, hooks: Hooks) -> bool:
    rows = _open_rows(board, led)
    if rows:
        hooks.stop_rows(board, cfg, rows)
        if hooks.rows_live(board, cfg, rows):
            _note_stuck(board, cfg, led, intent, now)
            return False
    _end_sessions(board, led, "cancelled")
    led.save()
    barrier.hit("mid_terminalization")
    _settle_blockers(board, led, "job concluded: cancelled")
    _release_locks(board, led)
    _release_seats(board, led, None, "job concluded")
    if js.status == "active":      # a person's deactivate has usually closed it already
        cause = (led.closing or "cancelled").partition(": ")[2] or "cancelled"
        if not board.close_job(led.job, "cancelled", f"cancelled: {cause}"[:500], forced=False, closed_by=CLOSED_BY):
            return False
        barrier.hit("terminal_closed_before_ledger")
    return _commit(board, cfg, led, intent, now, [], hooks)


def _closing_key(intent: dict) -> str:
    return f"closing:{intent['at']}"[:256]


def _stuck_after(cfg: dict, led, intent: dict, now: dt.datetime) -> bool:
    return now - (_ts(intent["at"]) or now) >= dt.timedelta(minutes=_minutes(_opt(cfg, led, "close_deadline_minutes")))


def _note_stuck(board, cfg: dict, led, intent: dict, now: dt.datetime) -> None:
    """Sessions still unconfirmed gone at the close deadline: tell the people, once ([M5]); the close
    itself keeps waiting (never close past a writer that may still run), but enforce_deadlines may now
    supersede a pending MET."""
    if "terminal_stuck" in led.watermarks or not _stuck_after(cfg, led, intent, now):
        return
    led.watermarks["terminal_stuck"] = 1
    led.save()
    _post(board, led.job, f"{ev.DRIVER_TERMINAL_STUCK} {intent['verdict']}: sessions not confirmed gone at the close deadline")


def repair_claim(board, led) -> bool:
    """A terminal job whose board claim `drive.enabled` is still set (the clearing failed once): clear
    it again. True when the claim is clear."""
    if led.terminal is None:
        return False
    try:
        if board.job_data(led.job).get("drive.enabled") is not None:
            board.set_job_data(led.job, "drive.enabled", None)
        return True
    except Exception as exc:
        log.warning("drive.enabled of %s not cleared: %s", led.job, type(exc).__name__)
        return False


def _commit(board, cfg, led, intent: dict, now, reasons: list, hooks: Hooks) -> bool:
    """[RR2] `led.terminal` is set only now, after the board close succeeded; then step 6, best effort."""
    led.terminal = {"verdict": intent["verdict"], "scope": intent["scope"], "at": _iso(now)}
    if reasons:
        led.terminal["reasons"] = reasons
    led.save()
    repair_claim(board, led)       # every terminal transition clears the claim; terminalize retries a failure
    try:
        hooks.after_close(board, cfg, led)
    except Exception as exc:
        log.warning("post-close capture for %s failed: %s", led.job, type(exc).__name__)
    return True


# --- rollover --------------------------------------------------------------------------------

def _board_rollover(board, cfg, led, hooks: Hooks) -> bool:
    """Rollover step (d), board half: idempotent, so a crash after the ledger write is finished here."""
    changed = False
    have = board.job_data(led.job).get("drive.generation")
    if have is not None and have != str(led.generation) or (have is None and led.generation > 1):
        board.set_job_data(led.job, "drive.generation", str(led.generation))
        changed = True
    if led.generation > 1 and board.seat(led.job, led.generation, "lead") is None:
        hooks.request_lead(board, cfg, led, led.generation)
        changed = True
    return changed


def rollover(board, cfg: dict, led, now: dt.datetime, *, hooks: Hooks | None = None) -> bool:
    """Spec 3.1 rollover, resumable: (a) stop generation n's sessions, (b) wait for the scopes to be
    absent, (c) release its locks and reservations, release its seats, expire its open blockers, (d) the
    ledger first (generation, counters), then the board (job data, the new lead seat). True when it
    advanced or repaired the generation, False while waiting or when there is nothing to do."""
    hooks = hooks or Hooks()
    if led.terminal is not None or led.terminal_intent is not None or led.halted is not None:
        return False
    if led.generation_requested <= led.generation:
        return _board_rollover(board, cfg, led, hooks)         # finishes a rollover cut short after (d)
    old = led.generation
    rows = _open_rows(board, led, old)
    if rows:
        hooks.stop_rows(board, cfg, rows)
        if hooks.rows_live(board, cfg, rows):
            return False
    _end_sessions(board, led, "cancelled", old)
    barrier.hit("mid_rollover")              # the old generation's sessions ended, nothing released or advanced
    _release_locks(board, led, old)
    _release_seats(board, led, old, "generation rolled over")
    _settle_blockers(board, led, "generation rolled over", old)
    led.generation = led.generation_requested
    led.ruling_due_at, led.ruling_attempts, led.rounds_without_progress, led.errors = None, 0, 0, 0
    led.watermarks = {k: v for k, v in led.watermarks.items() if k.startswith("qdone:")}   # [L3] once-only
    led.mirror_fetch_pending = []
    led.save()
    barrier.hit("mid_rollover_after_ledger")
    _board_rollover(board, cfg, led, hooks)
    return True


# --- halt ------------------------------------------------------------------------------------

def settle_halt(board, cfg: dict, led, now: dt.datetime, *, hooks: Hooks | None = None) -> bool:
    """[F5] Stop the halted job's sessions, conclude what they were doing as `halted` (not counted against
    attempts or sessions; cost charged as everywhere: actual usage, else the reservation, [RR4]) and
    idle every seat with wake reason `halted`. Idempotent: the pass repeats it while the job is halted.
    True once no session is left."""
    hooks = hooks or Hooks()
    if led.halted is None:
        return True
    rows = _open_rows(board, led)
    if rows:
        hooks.stop_rows(board, cfg, rows)
        if hooks.rows_live(board, cfg, rows):
            return False
    _end_sessions(board, led, "halted")
    for i in led.intents:                  # a reconcile may have concluded a halt-killed attempt as cancelled:
        # decided from the ledger alone: the intents open when the halt was written (the board's
        # ended_at is a column a writer could move)
        if i.state == "concluded" and i.outcome == "cancelled" and f"{_HOPEN}{i.launch_id}"[:256] in led.watermarks:
            i.outcome = "halted"
    led.save()
    for s in board.seats(led.job, include_closed=False):
        if s.state == "running":
            try:
                board.set_seat_state(s.id, s.revision, "idle", job=led.job, last_outcome="halted",
                                     wake_reason="halted")
            except ValueError:
                return False
    return True


def halt(board, cfg: dict, led, actor: str, now: dt.datetime, *, reason: str | None = None,
         hooks: Hooks | None = None) -> bool:
    """[F5] Halt the job: ledger first, then the board flag, then stop its sessions. A second halt keeps
    the first one's actor and time. True when the job was newly halted."""
    new = led.halted is None
    if new:
        led.halted = {"by": actor, "at": _iso(now), **({"reason": reason} if reason else {})}
        for i in led.intents:
            if i.state != "concluded":
                led.watermarks[f"{_HOPEN}{i.launch_id}"[:256]] = 1
        led.save()
        try:
            board.set_job_data(led.job, "drive.halted", actor)
        except Exception as exc:
            log.warning("board halt flag for %s not written: %s", led.job, type(exc).__name__)
    settle_halt(board, cfg, led, now, hooks=hooks)
    return new


def unhalt(board, cfg: dict, led, actor: str, now: dt.datetime) -> bool:
    """[F5] End the halt: every deadline shifts by the halted time (`halted_total_seconds`). Seats stay idle
    with wake reason `halted`; the next pass wakes them."""
    if led.halted is None:
        return False
    lasted = max(0.0, (now - (_ts(led.halted["at"]) or now)).total_seconds())
    led.halted_total_seconds += lasted
    led.watermarks = {k: v for k, v in led.watermarks.items() if not k.startswith(_HOPEN)}
    led.halted = None
    led.save()
    try:
        board.set_job_data(led.job, "drive.halted", None)
    except Exception as exc:
        log.warning("board halt flag for %s not cleared: %s", led.job, type(exc).__name__)
    _post(board, led.job, f"{ev.DRIVER_UNHALT} by {actor}: deadlines shifted by {int(lasted)} s")
    return True


# --- questions -------------------------------------------------------------------------------

def _question_opened(led, q, now: dt.datetime) -> dt.datetime:
    """When a question was opened, from the receipted view (`opened_at`) or else the ledger's own first
    sight of it; never the board row's created_at, which a writer can backdate. First sight is later than
    the truth, so the cap can only come late, never early."""
    if getattr(q, "opened_at", None) is not None:
        return q.opened_at
    key = f"qopen:{q.id}"
    seen = led.watermarks.get(key)
    if seen is None:
        led.watermarks[key] = int(now.timestamp())
        return now
    return dt.datetime.fromtimestamp(seen, dt.timezone.utc)


def apply_question_expiry(board, cfg: dict, led, now: dt.datetime, questions) -> list[int]:
    """[RR3] Apply the default of every receipted question whose deadline has passed, once, never while
    the job is halted or closing. The deadline is the question's `until` (a question with none gets the
    cap alone), capped at `question_expiry` after it was opened (the receipted time, see _question_opened),
    plus `halted_total_seconds`; an unreceipted question, or one that is not this job's open question on
    the board, is ignored. Returns the blocker ids settled."""
    if led.halted is not None or led.terminal is not None or led.terminal_intent is not None:
        return []
    expiry = dt.timedelta(minutes=_minutes(_opt(cfg, led, "question_expiry")))
    shift = dt.timedelta(seconds=led.halted_total_seconds)
    done = []
    dirty = False
    for q in questions:
        key = f"qdone:{q.id}"
        if q.receipt_id is None or key in led.watermarks or q.default is None:
            continue
        b = board.blocker(q.id)
        if b is None or b.job != led.job or b.kind != "question":
            continue
        before = len(led.watermarks)
        opened = _question_opened(led, q, now)
        dirty = dirty or len(led.watermarks) != before
        cap = opened + expiry
        if now < (min(q.until, cap) if q.until is not None else cap) + shift:
            continue
        if board.resolve_blocker(q.id, q.default, actor="swarm", state="expired"):   # once: False if closed
            _post_expired(board, b, q.default)
            done.append(q.id)
            led.watermarks[key] = 1
    if done or dirty:
        led.save()
    return done


def _post_expired(board, b, default: str) -> None:
    try:
        board.post(b.job, "swarm", f"Blocker {b.id} expired: {default or b.reason}")
        registry = getattr(board, "plugin_registry", None)
        if registry:
            registry.blocker_expired(board, b)
    except Exception as exc:
        log.warning("expiry notice for blocker %s failed: %s", b.id, type(exc).__name__)


def expire_orphan_questions(board, job: str, now: dt.datetime, *, board_key: str | None = None) -> list[int]:
    """The orphan `human_question` path: a board job that claims drive mode (`drive.enabled`) but has no
    ledger on this host (DRIVER-ORPHAN) has nobody to apply its question defaults, and the generic sweep
    skips drive-claiming jobs. Apply the defaults of its overdue open questions from the board's own rows
    (there is no ledger to decide from), once each, never while the board says the job is halted. Returns
    the blocker ids settled.

    Contract: the caller (the orphan pass) calls this only for a job whose owner host is this one and that
    has no ledger here. Given `board_key`, a ledger on this host is checked and ends the call: such a job
    is ruled by `apply_question_expiry`, from the ledger."""
    if board_key is not None:
        from swarm.supervisor import ledger as _ledger
        try:
            if _ledger.load(board_key, job) is not None:
                return []
        except _ledger.LedgerBadError:
            return []                  # a ledger exists, however damaged: the driver's, not ours
    data = board.job_data(job)
    if data.get("drive.enabled") != "1" or data.get("drive.halted"):
        return []
    js = board.job_status(job)
    if js is None or js.status not in ("active", "paused"):      # decision deadlines continue while paused
        return []
    done = []
    for b in board.blockers(job):
        if b.kind == "question" and b.until is not None and b.until <= now and b.default_value is not None \
                and board.resolve_blocker(b.id, b.default_value, actor="swarm", state="expired"):
            _post_expired(board, b, b.default_value)
            done.append(b.id)
    return done


# --- host sweeps -----------------------------------------------------------------------------

def _ledger_launch(board_key: str, job: str, launch_id: str) -> str:
    """This host's ledger on a launch: "open", "concluded", "unknown" (no ledger or no such intent) or
    "bad" (unreadable ledger)."""
    from swarm.supervisor import ledger as _ledger
    try:
        led = _ledger.load(board_key, job)
    except _ledger.LedgerBadError:
        return "bad"
    for i in (led.intents if led is not None else ()):
        if i.launch_id == launch_id:
            return "concluded" if i.state == "concluded" else "open"
    return "unknown"


def sweep_locks_by_host(board, cfg: dict, now: dt.datetime, *, hooks: Hooks | None = None,
                        board_key: str | None = None) -> int:
    """Spec 4.5 / Task 3 review L5: sweep `drive_locks` by HOST (every lock whose host is this one,
    whatever its job), never by a cascade from a job: a cascade would skip writer fencing. A lock whose
    holder's launch has ended is released. A lock held by a still-open launch is released only once it
    has expired AND its holder is fenced (session stopped and confirmed gone); until then it keeps
    blocking. The number released."""
    hooks = hooks or Hooks()
    if board_key is None:
        try:                              # never weaker without the key: the ledger is asked whenever it can be
            from swarm.board.autoinit import store_key
            board_key = store_key(cfg)
        except Exception:
            board_key = None
    rows: dict = {}
    n = 0
    for lock in board.drive_locks():
        if lock.host != compat.node():
            continue
        if lock.job not in rows:
            by_launch: dict = {}
            for r in board.restarts(job=lock.job):
                by_launch.setdefault(r.launch_id, []).append(r)    # every row of a launch id, forged copies too
            rows[lock.job] = by_launch
        rs = rows[lock.job].get(lock.launch_id, [])
        if any(r.host != lock.host for r in rs):
            log.warning("lock %s of %s: a launch row is on another host; kept", lock.launch_id, lock.job)
            continue                      # not ours to fence or end: it keeps blocking
        open_rows = [r for r in rs if r.ended_at is None]
        if not open_rows:
            # A board row alone (missing, or an "ended" copy) never frees a lock: positive local evidence is
            # needed: the ledger says the launch is concluded, or (no ledger intent for it) every row is
            # ended and no session of any of them is live here.
            seen = _ledger_launch(board_key, lock.job, lock.launch_id) if board_key is not None else "unknown"
            if seen in ("open", "bad"):
                continue
            if seen != "concluded" and (not rs or hooks.rows_live(board, cfg, rs)):
                continue
        else:
            if lock.expires_at > now:
                continue                                    # live and valid
            hooks.stop_rows(board, cfg, rs)                 # expired: fence the holder (every row) first
            if hooks.rows_live(board, cfg, rs):
                continue
            for r in open_rows:
                board.finish_restart(r.id, "timeout")
        n += board.release_drive_locks(lock.launch_id, job=lock.job, holder=lock.holder)
    return n
