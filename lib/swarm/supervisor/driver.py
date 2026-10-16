"""The job driver's pass (spec 3.2, [F2], [F17], plan Task 8c): `drive` is called first by `command._pass`.

It enumerates the host-private ledgers by file name (never the board's active jobs, so an untrusted board
writer cannot make a job drop out), and for each unfinished one runs, inside its own try/except and deadline:
deadlines (8b), reconcile (8a), the tamper check, the halt/terminalize/rollover steps (8b), then the snapshot
(7b), the pure `next_actions` (7) and the executor (8a). A bad ledger, a ledger without an owner record, a
board drive job without a ledger and a job whose owner record lacks its recipe keys are held with a once-only
`DRIVER-*` post; they never stop the pass for the other jobs. The result is the set of job names the other
passes must skip."""
from __future__ import annotations

import datetime as dt
import logging
import os
import time
import traceback
from types import SimpleNamespace

from swarm.supervisor import driver_events as ev, driver_exec, ledger

log = logging.getLogger("swarm.supervisor.driver")

SUPERVISOR = "swarm supervisor"
JOB_SECONDS = 30.0            # one job's share of the pass (spec 3.2)
RECONCILE_SECONDS = 20.0
ACTIVATION_GRACE_SECONDS = 120.0
CLOSED = ("completed", "cancelled", "failed")


def _modules():
    """The collaborating modules, imported when a pass runs (tests replace this with fakes)."""
    from swarm.supervisor import driver_exec, driver_life, driver_model, driver_snapshot
    try:
        from swarm.supervisor import driver_land      # Task 11b: absent on builds without landing
    except ImportError:
        driver_land = None
    try:
        from swarm import drive_cli                   # Task 6b: the control requests of Codex sessions
    except ImportError:
        drive_cli = None
    return SimpleNamespace(exec=driver_exec, life=driver_life, model=driver_model, snapshot=driver_snapshot,
                           land=driver_land, control=drive_cli)


def _iso(now: dt.datetime) -> str:
    return now.isoformat()


def _ts(text):
    if not text:
        return None
    t = dt.datetime.fromisoformat(text)
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


def _flat_cfg(cfg: dict, led) -> dict:
    """The drive settings: the config's `[drive]` section over the shipped defaults, then the job's budgets."""
    from swarm.supervisor import driver_model
    section = (cfg or {}).get("drive")
    return {**driver_model.DEFAULT_CFG, **(section if isinstance(section, dict) else {}), **(led.budgets or {})}


def _mutate(led, fn) -> None:
    """Apply `fn(led)` and save, redoing both after a ledger conflict (the copy is refreshed from disk)."""
    for _ in range(5):
        fn(led)
        try:
            led.save()
            return
        except ledger.LedgerConflict:
            fresh = ledger.load(led.board_key, led.job)
            if fresh is None:
                raise
            led.__dict__.update(fresh.__dict__)
    raise ledger.LedgerConflict(f"job {led.job!r}: the ledger kept changing")


def _notice(board, state: dict, key: str, job: str, text: str) -> bool:
    """Post `text` on the job once (the key is remembered in the supervisor state). True when posted now."""
    posted = state.setdefault("posted", {})
    if key in posted:
        return False
    from swarm.board import JobPaused, PAUSE_WRITER
    try:
        try:
            board.post(job, SUPERVISOR, text[:1900])
        except JobPaused:          # a DRIVER-* notice is never suppressible: the pause writer may post
            board.post(job, PAUSE_WRITER, text[:1900])
    except Exception as exc:       # a notice is never worth failing a pass for
        log.warning("post to %s failed: %s", job, type(exc).__name__)
        return False
    posted[key] = {"job": job, "on": dt.date.today().isoformat()}
    return True


def _forget(state: dict, prefix: str) -> None:
    posted = state.get("posted")
    if isinstance(posted, dict):
        for k in [k for k in posted if k.startswith(prefix)]:
            del posted[k]


# --- enumeration ---------------------------------------------------------------------------------

def _scan_files(key: str, *, read_only: bool = False):
    """(ledgers, bad): every ledger file of this board read on its own, so one unreadable entry (a planted
    lock, a bad body) holds only itself. Never sourced from the board."""
    from swarm import compat
    from swarm.supervisor import privfs
    leds, bad = [], []
    with privfs.dir_fd(ledger.SUBDIR, create=not read_only) as d:
        for name in sorted(compat.listdir(d)):
            if not ledger._FILE.fullmatch(name):
                continue
            data = None
            try:
                data = privfs.read(d, name, ledger.MAX_BYTES)
                if not read_only:
                    with privfs.locked(d, ledger._lock_name(name)):
                        data = privfs.read(d, name, ledger.MAX_BYTES)
                led = ledger._parse(data, None, None) if data is not None else None
                if led is None:
                    raise ValueError("not a regular private file")
                if ledger._name(led.board_key, led.job) != name:
                    raise ValueError("file name does not match its key")
            except Exception as exc:
                bad.append((ledger._hint(data), name, f"{type(exc).__name__}: {exc}"[:200]))
                continue
            if led.board_key == key and led.terminal is None:
                leds.append(led)
    return leds, bad


def _enumerate(board, cfg: dict, state: dict, key: str, say):
    """(ledgers, held): every unfinished ledger of this board, and the job names held for a bad ledger.
    When `ledger.unfinished` raises (a planted lock) the ledger directory is listed by file name instead:
    each file stands alone, and the board is never asked which jobs exist (a board writer must not be able
    to make a job drop out, [F2])."""
    held: set = set()
    from swarm.supervisor import settings
    if not os.path.lexists(settings.private_dir() / ledger.SUBDIR):
        return [], held          # no ledger directory yet: nothing to read, and a read creates nothing
    try:
        entries = ledger.unfinished(key)
    except Exception as exc:
        log.warning("ledger enumeration failed: %s", type(exc).__name__)
        leds, bad = _scan_files(key)
        entries = list(leds) + [ledger.LedgerBad(h, n, w) for h, n, w in bad]
    leds = []
    for e in entries:
        if isinstance(e, ledger.Ledger):
            leds.append(e)
        else:
            hint = e.job_hint
            if hint:
                _bad(board, state, hint, e.why, say)
                held.add(hint)
            else:
                log.warning("bad ledger file %s: %s", e.path, e.why)
    return leds, held


def _claimed_jobs(board):
    out = []
    for js in board.jobs(False):
        try:
            if (board.job_data(js.job) or {}).get("drive.enabled") == "1":
                out.append(js)
        except Exception:
            continue
    return out


def _bad(board, state, job, why, say) -> None:
    if _notice(board, state, f"ledger-bad|{job}", job,
               f"{ev.DRIVER_LEDGER_BAD} the drive ledger of this job is unreadable ({why}); the job holds until a "
               f"person runs `swarm drive repair --job {job}`"):
        say(f"drive: ledger of {job} is bad; held")


def _orphans(board, cfg: dict, state: dict, key: str, known: set, now, say) -> set:
    """Board jobs that claim drive here (an owner record on this host) but have no ledger: after the
    activation grace they are DRIVER-ORPHAN, held."""
    from swarm import enrolment
    out: set = set()
    for js in _claimed_jobs(board):
        if js.job in known or js.status != "active":
            continue
        rec = enrolment.find_job_owner(key, js.job)
        if rec is None or not getattr(rec, "owner", None):
            continue                  # another host's job: its owner host reports it
        since = js.activated_at or js.created_at
        out.add(js.job)               # claimed: no other pass touches it, orphan or still activating
        if since is not None and (now - since).total_seconds() < ACTIVATION_GRACE_SECONDS:
            continue
        if _notice(board, state, f"orphan|{js.job}", js.job,
                   f"{ev.DRIVER_ORPHAN} this job claims drive mode but has no ledger on its owner host; it holds "
                   "until a person repairs or deactivates it"):
            say(f"drive: {js.job} claims drive without a ledger; held")
    return out


# --- tamper ([F17], [G6]) ------------------------------------------------------------------------

DEACTIVATE_HOWS = ("requester:deactivate", "tty:deactivate")


def _person_deactivated(led, key: str, now: dt.datetime) -> bool:
    """A person's `swarm deactivate` of this job, receipted by Task 6b on either path (the requester session's
    hook and the tty): kind person, record id `deactivate:<generation>`, rev 1, of this incarnation and
    generation, unexpired, whose decision is {deactivate, this job}. One per generation: a valid cancel ends the
    ledger, and a cancel that cannot complete holds the job."""
    from swarm import receipts
    rid = f"deactivate:{led.generation}"
    r = receipts.find(key, led.job, "person", rid, rev=1, incarnation=led.incarnation, generation=led.generation,
                      now=now.timestamp())
    return (r is not None and r.launch is None and r.action_id in DEACTIVATE_HOWS
            and receipts.matches(r, {"kind": "person", "id": rid, "rev": 1, "action": "deactivate",
                                     "target": led.job}))


def tamper(led, js, owner, data: dict) -> str | None:
    """What the board row says that the ledger and the owner record do not allow, or None."""
    if js is None:
        return "job row missing"
    if not js.supervise:
        return "supervise switched off"
    if (data or {}).get("drive.enabled") != "1":
        return "drive flag cleared"
    return None


def _repair(board, led, owner, js) -> bool:
    """Rewrite the board row from the ledger and the owner record, where the backend allows."""
    try:
        if js is not None and js.status in CLOSED:
            board.open_job(led.job, None, None, None, None, goal=js.goal)
        if js is not None:
            board.set_job_supervise(led.job, True)
            board.set_job_data(led.job, "drive.enabled", "1")
        return js is not None
    except Exception as exc:
        log.warning("repair of %s failed: %s", led.job, type(exc).__name__)
        return False


def _cancel(board, cfg: dict, led, now: dt.datetime, m) -> bool:
    """CANCELLED: a person's (the requester's receipted) deactivate. Task 8b's `driver_life.cancel_job` does it
    (ledger first: stops the sessions, closes, commits `terminal`, clears the claim); until it lands the job
    simply holds. False when the cancel is not (yet) complete: it is retried next pass."""
    cancel = getattr(m.life, "cancel_job", None)
    if cancel is None:
        log.warning("job %s: cancelled by its requester, but driver_life.cancel_job is not there yet", led.job)
        return False
    return bool(cancel(board, cfg, led, "requester session", now, reason="deactivate by the requester session"))


def _own_close(led, js, m) -> bool:
    """The closed row is the driver's own close only when the LEDGER says it was closing: 8b's `_closing_key`
    marker, written (and saved) before `close_job`, and a status that matches the pending outcome. The board's
    `closed_by` is board-writable and never trusted ([H1])."""
    intent = led.terminal_intent
    if intent is None or m.life._closing_key(intent) not in led.watermarks:
        return False
    return js.status == ("completed" if intent["verdict"] == "met" else "failed")


def _check_tamper(board, cfg, state, led, js, owner, data, now, m, say) -> str | None:
    """None when the row agrees; else "cancelled" (the requester's deactivate, done) or "held"."""
    if getattr(led, "state", "active") == "activating":
        return None                                    # the activation is still being completed
    if js is not None and js.status in CLOSED and _own_close(led, js, m):
        return None                                    # the driver's own close; terminalize commits it
    if js is not None and js.status in CLOSED and _person_deactivated(led, led.board_key, now):
        if js.status == "completed":
            return "completed" if m.life.complete_by_person(board, cfg, led, now) else "held"
        if _cancel(board, cfg, led, now, m):
            say(f"drive: {led.job} cancelled by its requester")
            return "cancelled"
        _notice(board, state, f"cancel|{led.job}", led.job,
                f"{ev.DRIVER_TAMPER} the requester session deactivated this job but the cancellation could not "
                "be completed; the job holds and the cancel is retried each pass")
        return "held"
    what = tamper(led, js, owner, data)
    if what is None and js is not None and js.status == "paused":
        rec = board.open_pause(led.job)
        m.life.halt(board, cfg, led, "person: pause", now, reason=rec.reason if rec else None)
        _notice(board, state, f"pause|{led.job}", led.job,
                "Job paused; swarm resume --job " + led.job + " re-queues driver launches")
        return "held"

    if what is None:
        _forget(state, f"tamper|{led.job}|")
        return None
    repaired = _repair(board, led, owner, js)
    if not led.halted:
        m.life.halt(board, cfg, led, "driver (tamper)", now, reason=f"tamper: {what}"[:200])
    if _notice(board, state, f"tamper|{led.job}|{what}", led.job,
               f"{ev.DRIVER_TAMPER} {what}: " + (
                   "the job is paused and stays paused: a person must `swarm resume` it and unhalt it"
                   if js is not None and js.status == "paused" else
                   ("the row was restored from the ledger; " if repaired else "the row could not be restored; ")
                   + "the job is halted until a person unhalts it")):
        say(f"drive: tamper on {led.job}: {what}")
    return "held"


# --- one job -------------------------------------------------------------------------------------

def _over(jdeadline: float) -> bool:
    return time.monotonic() >= jdeadline


def _terminal_from(snap, led, board, now, m, action) -> bool:
    """The terminal records next_actions asks for: begin the ledger-first terminal intent, then terminalize."""
    life = m.life
    if action.kind == "record_unreachable":
        life.begin_terminal(board, led, "unreachable", "ruling", life.MECHANICAL, action.reason or "ruling", now)
    elif action.kind == "close":
        gv = snap.goal_verdict
        if gv is None:
            return False
        if gv.verdict == "unreachable" and not led.watermarks.get(f"escalated:{gv.receipt_id}"):
            text = f"{ev.DRIVER_ESCALATE} {gv.by}: unreachable ruling: {gv.reason}; next: {gv.next}"
            board.post(led.job, SUPERVISOR, text, to=led.requester_session or "owner")
            board.post_event(led.job, "launch-failed", f"unreachable:{gv.receipt_id}", text,
                             to=led.requester_session or "owner")
            _mutate(led, lambda l: l.watermarks.__setitem__(f"escalated:{gv.receipt_id}", 1))
        life.begin_terminal(board, led, gv.verdict, gv.scope, gv.by or "judge", action.reason or gv.verdict,
                            now, candidate=gv.candidate, req=gv.req)
    return True


def _stop_launch(board, cfg, led, launch: str, m) -> None:
    life = m.life
    launches = {i.launch_id for i in led.intents if i.launch == launch and i.state != "concluded"}
    rows = [r for r in life._open_rows(board, led) if r.launch_id in launches]
    if rows:
        life.Hooks().stop_rows(board, cfg, rows)


def _escalate_landing(board, led, snap, now, reason: str) -> None:
    """Use F03's launch failure notification path and keep MET active for retry."""
    key = f"met-landing:{snap.goal_verdict.receipt_id}"
    if led.watermarks.get(key + ":retry-at", 0) > now.timestamp():
        return
    # The model counts this candidate's attempts in its action; remember all completed retries here.
    n = sum(1 for i in led.intents if "|integrate|" in i.action_id
            and f"|{snap.goal_verdict.receipt_id}|retry" in i.action_id)
    def mark(l):
        l.watermarks[key + ":retry-at"] = int(now.timestamp()) + 300
        l.watermarks[key + ":notified"] = n
    _mutate(led, mark)
    text = f"{ev.DRIVER_ESCALATE} {reason}; retry in 300s"
    target = led.requester_session or "owner"
    board.post_event(led.job, "launch-failed", f"met-landing:{led.generation}:{int(now.timestamp())}",
                     text, to=target)
    board.post(led.job, SUPERVISOR, text, to=target)


def _act(board, cfg, sup, led, snap, actions, now, start_runner, config_path, jdeadline, m) -> None:
    for a in actions:
        if _over(jdeadline):
            return
        if a.kind == "launch":
            result = m.exec.execute(board, cfg, sup, led, a, start_runner=start_runner, now=now, config_path=config_path)
            if result is None and a.reason == "met, candidate awaits landing":
                reason = m.exec.reservation_reason(m.exec._dcfg(cfg, led), led, False, now)
                _escalate_landing(board, led, snap, now, "met, landing blocked: " + (reason or "integrator refused"))
        elif a.kind == "escalate_landing":
            _escalate_landing(board, led, snap, now, a.reason)
        elif a.kind in ("record_unreachable", "close"):
            if _terminal_from(snap, led, board, now, m, a):
                m.life.terminalize(board, cfg, led, now)
        elif a.kind == "fix_lead":
            gv = snap.goal_verdict
            lead = board.get_launch(led.job, led.generation, a.launch) if a.launch else None
            if lead is None:
                m.life.default_request_lead(board, cfg, led, led.generation)
                lead = board.get_launch(led.job, led.generation, "lead")
            board.post(led.job, SUPERVISOR, f"{gv.scope.title()} not_met: {gv.reason}; next: {gv.next}", to=lead.name)
            if lead.state != "running" and lead.name not in snap.live:
                m.exec._queue_existing(board, lead)
            def fix(l):
                l.ruling_due_at, l.ruling_attempts, l.rounds_without_progress, l.errors = None, 0, 0, 0
                l.watermarks[f"{gv.scope}-fix"] = gv.receipt_id
                key = f"requeue:{l.generation}:{lead.name}"
                l.watermarks[key] = l.watermarks.get(key, 0) + 1
            _mutate(led, fix)
        elif a.kind == "no_progress":
            m.exec.note_no_progress(board, led, snap, a)
        elif a.prompt_kind == "goal-hold":
            m.exec.hold_goal(board, led, now, a.reason)
        elif a.kind == "rollover":
            m.life.rollover(board, cfg, led, now)
        elif a.kind == "stop_launch" and a.launch:
            _stop_launch(board, cfg, led, a.launch, m)
        # hold, nothing and complete_land (terminalize step 0) need no action here


def _with_learn(board, cfg, led, snap):
    """The snapshot with `learn_pending` (swarm.learnings.learn_pending): only once a terminal goal or ruling
    verdict of this generation is there, i.e. the job is about to close and its learnings step must run first.
    Deterministic board reads, no LLM; a failed read keeps the step pending (fail closed: never a close past it)."""
    try:
        gv = snap.goal_verdict
        if gv is None or gv.verdict not in ("met", "unreachable") or gv.generation != snap.generation:
            return snap
        from dataclasses import replace
        from swarm import learnings
        return replace(snap, learn_pending=learnings.learn_pending(board, cfg, led.job, gv.at))
    except Exception as exc:
        # fail closed: an unverified learnings step stays pending (the close waits; the learner launch is
        # launched or awaited), never a close past it
        log.warning("learnings check of %s failed: %s", led.job, type(exc).__name__)
        try:
            from dataclasses import replace
            return replace(snap, learn_pending=True)
        except Exception:
            return snap


def _learn_holding(actions) -> bool:
    """The model is holding a terminal verdict for its learnings step (driver_model._learn_first): launching or
    awaiting the learner, or holding after its attempts ran out."""
    return any(a.prompt_kind == "learn" or (a.kind == "nothing" and a.responsible == "learner") for a in actions)


def _note_learn_hold(led, snap, actions, m) -> None:
    """Ledger first: while the model holds a receipted terminal verdict for its learnings step, the ledger
    says so (watermark LEARN_HOLD = the verdict's receipt id), so the ledger-only deadline dispatcher
    (driver_life.enforce_deadlines) never turns a MET held for its learnings into a mechanical UNREACHABLE.
    Only while the model actually holds for it (a verdict that is not closing yet, e.g. an unlanded MET, gets
    no hold). Cleared once the step is done (the close follows) or nothing waits for it any more."""
    key = m.life.LEARN_HOLD
    held = bool(getattr(snap, "learn_pending", False)) and _learn_holding(actions)
    gv = getattr(snap, "goal_verdict", None)
    rid = getattr(gv, "receipt_id", None) or 1
    if held and led.watermarks.get(key) != rid:
        _mutate(led, lambda l: l.watermarks.__setitem__(key, rid))
    elif not held and key in led.watermarks:
        _mutate(led, lambda l: l.watermarks.pop(key, None))


def _plan(board, cfg, led, dcfg, now, m):
    """Snapshot, progress rule and the model's actions. A ruling that has just become due is recorded in
    the ledger first, so its action ids carry the due time as their trigger from the start."""
    driver_exec.refresh_goal_hold(board, led)
    snap = _with_learn(board, cfg, led, m.snapshot.read(board, led, led.job, cfg=dcfg, now=now))
    m.exec.settle_progress(led, snap, now)
    actions = m.model.next_actions(snap, dcfg)
    if led.ruling_due_at is None and any(a.ruling for a in actions) and m.exec.note_ruling_due(led, now):
        actions = m.model.next_actions(snap, dcfg)
    _note_learn_hold(led, snap, actions, m)
    return snap, actions


def _watch_stalled(board, state, led, actions, now, sup) -> None:
    """DRIVER-STALLED (spec 3.13): the model names no responsible actor (nothing live, no launch, no record) for
    more than two stall periods (2x [supervise] timer_minutes). The first such pass is remembered in the ledger; any other pass forgets it."""
    idle = (led.halted is None and led.terminal_intent is None
            and not any(i.state != "concluded" for i in led.intents)
            and all(a.kind in ("hold", "nothing") and not a.responsible for a in actions))
    since = led.watermarks.get("stalled.since")
    if not idle:
        if since is not None:
            _mutate(led, lambda l: l.watermarks.pop("stalled.since", None))
            _forget(state, f"stalled|{led.job}")
        return
    if since is None:
        _mutate(led, lambda l: l.watermarks.setdefault("stalled.since", int(now.timestamp())))
        return
    limit = 2 * 60 * float((sup or {}).get("timer_minutes", 5))
    if now.timestamp() - since > limit:
        _notice(board, state, f"stalled|{led.job}", led.job,
                f"{ev.DRIVER_STALLED} no launch, launch or record is responsible for this job since "
                f"{dt.datetime.fromtimestamp(since, dt.timezone.utc).isoformat()}")


CONTROL_KEY = "ctlreq"          # ledger watermark: the highest control request id handled


def _control_mark(state: dict, led, job: str) -> int:
    if led is not None:
        return int(led.watermarks.get(CONTROL_KEY, 0))
    return int((state.get("ctlreq") or {}).get(job, 0))      # a ledger that does not read keeps no watermark


def _control_advance(state: dict, led, job: str, rid: int) -> None:
    if led is not None:
        _mutate(led, lambda l: l.watermarks.__setitem__(CONTROL_KEY, max(rid, int(l.watermarks.get(CONTROL_KEY, 0)))))
    else:
        marks = state.setdefault("ctlreq", {})
        marks[job] = max(rid, int(marks.get(job, 0)))


def _control(board, cfg, state, led, job: str, now, jdeadline: float, m) -> bool:
    """Task 6b's control requests (a Codex session's ledger-writing commands): the receipted requests after the
    watermark, in id order, each applied by `drive_cli.apply_control_request` (which posts CONTROL-APPLIED or
    CONTROL-REFUSED) and consumed either way. `led` is None for a ledger that does not read (a `repair`
    request). Stops at the job's time slot. True when the ledger on disk may have changed (reloaded here)."""
    ctl = getattr(m, "control", None)
    if ctl is None:   # never in an integrated build: say so loudly, a Codex launch's requests would wait for ever
        log.error("control requests of %s cannot be applied: swarm.drive_cli is not in this build", job)
        from swarm.supervisor import driver_events as ev
        _notice(board, state, f"control-missing|{job}", job,
                f"{ev.DRIVER_ERROR} control requests cannot be applied: swarm.drive_cli is not in this build")
        return False
    from swarm.board.autoinit import store_key
    bk = store_key(cfg)
    reqs = sorted(ctl.find_control_requests(board, bk, job, led, now, after=_control_mark(state, led, job)),
                  key=lambda r: r.id)
    changed = False
    for req in reqs:
        if _over(jdeadline):
            break
        try:
            applied, why = ctl.apply_control_request(board, cfg, led, req, now)
        finally:
            _control_advance(state, led, job, req.id)       # a request is handled once, whatever came of it
        changed = True
        log.info("control request %s of %s: %s (%s)", req.id, job, "applied" if applied else "refused", why)
        if led is None or applied:
            fresh = ledger.load(bk, job) if led is None else ledger.load(led.board_key, job)
            if fresh is None:
                break
            if led is not None:
                led.__dict__.update(fresh.__dict__)
            else:
                break            # a repaired ledger is driven from the next pass
    return changed


LAND_PENDING_KEY = "land.pending"
LAND_PENDING_PASSES = 6        # consecutive passes with a pending landing before a ruling is made due


def _land_env(cfg, led, owner, snap, say, jdeadline, m, config_path=None):
    """A `land.LandEnv` from the owner record (`target_branch`, `config_dirs`, remotes, credentials), the
    ledger (`activation_heads`) and the snapshot's judged SHAs; `left` is what is left of the job's slot.
    Fields this build's LandEnv does not have (11b's API is still moving) are left out."""
    import dataclasses
    from swarm import land
    d = dict(getattr(owner, "owner", None) or {})
    judged = {v.artifact.partition("@")[2] for v in snap.verdicts.values()
              if getattr(v, "verdict", None) == "met" and getattr(v, "artifact", None)}
    urls = sorted((d.get("remotes") or {}).values())
    team = getattr(m.land, "team_ci", None)
    tci = (team(config_path) if team is not None else None) or {}
    if tci.get("evidence_command"):     # the project's own exact-SHA check wins, as in the team recipe (ci_adapter)
        ci = m.land.ci_status_evidence(tci["evidence_command"])
    else:
        ci = m.land.ci_status_cli(tci.get("repository") or (m.land.repo_slug(urls[0]) if urls else None))
    want = {"led": led, "owner": d, "judged": lambda: set(judged), "ci_status": ci,
            "target": d.get("target_branch") or "main", "say": say,
            "left": lambda: max(0.0, jdeadline - time.monotonic()),   # land._budget calls it before each step
            "activation_heads": dict(getattr(led, "activation_heads", None) or {}),
            "config_dirs": dict(d.get("config_dirs") or {})}
    have = {f.name for f in dataclasses.fields(land.LandEnv)}
    return land.LandEnv(**{k: v for k, v in want.items() if k in have})


def _idle_integrator(board, led, branch: str, m) -> None:
    """The integrator launch of a group goes idle once its hand-off is landed or refused."""
    name = m.exec.launch_name("integrator", branch)
    for launch in board.launches(led.job, led.generation):
        if launch.name == name and launch.state == "running":
            try:
                board.set_launch_state(launch.id, launch.revision, "done", job=led.job, last_outcome="landed")
            except ValueError as exc:
                log.warning("integrator %s of %s not idled: %s", name, led.job, exc)


def _land(board, cfg, led, owner, snap, flat, now, say, jdeadline, m, config_path=None) -> None:
    """Task 11b's `driver_land.run`, once per pass before any other action: executes the receipted land
    requests. A finished request leaves its integrator launch idle. After `land_pending_passes` consecutive
    passes with a landing still pending a ruling is made due (the lease or the CI never comes)."""
    if getattr(m, "land", None) is None:
        return
    env = _land_env(cfg, led, owner, snap, say, jdeadline, m, config_path)
    results = m.land.run(board, cfg, env, led.job)
    pending = any(r.pending for _req, r in results)
    for request, res in results:
        if not res.pending:
            if not res.ok:
                branch = (request.artifact or "").partition("@")[0]
                def note(l):
                    l.integrated.setdefault(branch, {})["landing_error"] = res.why
                _mutate(led, note)
            _idle_integrator(board, led, (request.artifact or "").partition("@")[0], m)
    count = led.watermarks.get(LAND_PENDING_KEY, 0)
    if pending:
        _mutate(led, lambda l: l.watermarks.__setitem__(LAND_PENDING_KEY, count + 1))
        if count + 1 >= int(flat.get("land_pending_passes", LAND_PENDING_PASSES)):
            m.exec.note_ruling_due(led, now)
    elif count:
        _mutate(led, lambda l: l.watermarks.pop(LAND_PENDING_KEY, None))


def _step(board, cfg, sup, state, led, owner, now, say, start_runner, config_path, jdeadline, m) -> None:
    life, exe = m.life, m.exec
    flat = _flat_cfg(cfg, led)
    exe.reconcile(board, cfg, led, now, min(jdeadline, time.monotonic() + RECONCILE_SECONDS))
    if led.terminal is not None:
        return
    _control(board, cfg, state, led, led.job, now, jdeadline, m)     # before anything is decided from the ledger
    js = board.job_status(led.job)
    verdict = _check_tamper(board, cfg, state, led, js, owner, board.job_data(led.job), now, m, say)
    if verdict is not None:
        return
    if led.halted is not None:
        life.settle_halt(board, flat, led, now)
        return
    if led.terminal_intent is not None:
        m.life.terminalize(board, flat, led, now)
        return
    life.rollover(board, flat, led, now)
    if led.generation_requested > led.generation:
        return
    try:
        dcfg = m.snapshot.drive_cfg(led.board_key, led.job, flat)
    except m.snapshot.DriveConfigError as exc:
        _notice(board, state, f"config|{led.job}", led.job,
                f"{ev.DRIVER_CONFIG} the owner record of this job lacks what the driver needs ({exc}); the job holds")
        for row in board.launches(led.job, led.generation):
            driver_exec.skip_launch(board, led, row.name, f"owner configuration invalid: {exc}")
        return
    snap, actions = _plan(board, cfg, led, dcfg, now, m)
    budget = next((a for a in actions if a.prompt_kind in ("budget", "goal-hold")), None)
    if budget:
        if budget.prompt_kind == "goal-hold":
            driver_exec.hold_goal(board, led, now, budget.reason)
        else:
            driver_exec.hold_budget(board, led, driver_exec._dcfg(cfg, led), now, budget.reason)
        for row in board.launches(led.job, led.generation):
            if row.state == "queued":
                driver_exec.skip_launch(board, led, row.name, budget.reason)
        return
    driver_exec.clear_budget_hold(board, led)
    _watch_stalled(board, state, led, actions, now, sup)
    _land(board, cfg, led, owner, snap, flat, now, say, jdeadline, m, config_path)
    for row in board.launches(led.job, led.generation):
        if row.state == "queued":
            holds = [a.reason for a in actions if a.kind in ("hold", "nothing") and a.reason
                     and a.launch in (None, row.name)]
            reason = driver_exec.reservation_reason(driver_exec._dcfg(cfg, led), led, False, now)
            if not reason and row.name in snap.questions:
                reason = "launch has an open question"
            if not reason and led.watermarks.get(f"retry-at:{row.name}", 0) > now.timestamp():
                reason = "scheduled retry backoff has not elapsed"
            if not reason and any(i.launch == row.name and i.state != "concluded" for i in led.intents):
                reason = "an open attempt of this launch is still pending"
            if not reason and len(snap.live) >= flat["max_concurrent_sessions"]:
                reason = "job concurrency limit reached"
            reason = reason or "; ".join(holds) or "pass deadline reached before launch execution"
            driver_exec.skip_launch(board, led, row.name, reason)
    _act(board, cfg, sup, led, snap, actions, now, start_runner, config_path, jdeadline, m)
    queued = board.launches(led.job, led.generation, include_closed=False)
    stranded = any(s.state == "queued" for s in queued) and not any(s.state == "running" for s in queued)
    previous = led.watermarks.get("queued.empty_passes", 0)
    _mutate(led, lambda l: l.watermarks.__setitem__("queued.empty_passes", previous + 1 if stranded else 0))
    if stranded and previous:
        _notice(board, state, f"queued-bug|{led.job}", led.job,
                f"{ev.DRIVER_BUG} queued launches with none running for more than one pass")
    elif not stranded:
        _forget(state, f"queued-bug|{led.job}")


def _report_skips(board, led) -> None:
    """A queued launch explains every held pass, with one durable notice per job."""
    data = board.job_data(led.job)
    for row in board.launches(led.job, led.generation):
        key = f"skip-passes:{led.generation}:{row.name}:{row.revision}"
        if row.state != "queued":
            continue
        reason_key = f"drive.skip.{led.generation}.{row.name}"
        reason = data.get(reason_key)
        if not reason:
            if led.halted:
                reason = "job is halted: " + str(led.halted.get("reason", ""))
            elif led.generation_requested > led.generation:
                reason = "generation rollover is waiting for previous sessions to stop"
            elif led.terminal_intent or led.terminal:
                reason = "job is closing or finished"
            elif any(i.launch == row.name and i.state != "concluded" for i in led.intents):
                reason = "an open attempt of this launch is still pending"
            else:
                reason = "driver pass did not reach launch execution"
            driver_exec.skip_launch(board, led, row.name, reason)
        count = led.watermarks.get(key, 0) + 1
        _mutate(led, lambda l: l.watermarks.__setitem__(key, count))
        if count > 1 and not led.watermarks.get("qdone:driver-skip"):
            board.post(led.job, SUPERVISOR, f"{ev.DRIVER_SKIP} {row.name}: {reason}")
            _mutate(led, lambda l: l.watermarks.__setitem__("qdone:driver-skip", 1))


class _DriverLog(logging.Handler):
    """Size-capped private logging shared by session drivers, without following planted paths."""
    def emit(self, record):
        from swarm.supervisor import privfs
        try:
            text = (self.format(record) + "\n").encode("utf-8", "replace")[-1024 * 1024:]
            with privfs.dir_fd() as d, privfs.locked(d, "driver.log.lock"):
                previous = privfs.read_tail(d, "driver.log", 1024 * 1024)
                if previous and len(previous[0]) + len(text) > 1024 * 1024:
                    privfs.write_atomic(d, "driver.log.1", previous[0])
                    privfs.write_atomic(d, "driver.log", "")
                privfs.append(d, "driver.log", text)
        except Exception:
            self.handleError(record)


def configure_log() -> None:
    logger = logging.getLogger("swarm.supervisor")
    if not any(isinstance(h, _DriverLog) for h in logger.handlers):
        handler = _DriverLog()
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        logger.addHandler(handler)


def _failed(board, state, led, exc, now, flat: dict, m) -> None:
    """A broken driver is retried and escalated, never judged as a failed goal."""
    configure_log()
    frames = traceback.extract_tb(exc.__traceback__)
    step = frames[-1].name if frames else "_step"
    cause = f"{type(exc).__name__}: {str(exc)[:500]} (step {step})"
    trace = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    log.warning("driver step for %s failed: %s", led.job, cause,
                exc_info=(type(exc), exc, exc.__traceback__))
    try:
        def count(l):
            l.errors += 1
            l.watermarks["driver-error:retry-at"] = int(now.timestamp()) + min(300, 2 ** min(l.errors, 9))
        _mutate(led, count)
        board.set_job_data(led.job, f"drive.error.{led.generation}", trace[-1500:])
        _notice(board, state, f"error|{led.job}|{led.generation}", led.job,
                f"{ev.DRIVER_ERROR} {cause}; see driver.log")
        if led.errors >= int(flat["driver_error_passes"]):
            text = f"{ev.DRIVER_ESCALATE} {cause}; driver will retry with backoff; see driver.log"
            target = led.requester_session or "owner"
            board.post_event(led.job, "launch-failed",
                             f"driver-error:{led.generation}:{int(now.timestamp())}", text, to=target)
            board.post(led.job, SUPERVISOR, text, to=target)
    except Exception as exc2:
        log.warning("driver error recovery for %s failed: %s", led.job, type(exc2).__name__, exc_info=True)


def drive(board, cfg: dict, sup: dict, state: dict, *, now: dt.datetime, say=print, start_runner=None, which=None,
          scope_ok=None, config_path: str = "", deadline: float | None = None, job: str | None = None) -> set[str]:
    """One drive step of the pass over every unfinished ledger of this board; `deadline` is a
    time.monotonic() value. Returns the names of the jobs the other passes must skip."""
    from swarm import enrolment
    from swarm.board.autoinit import store_key
    from swarm.supervisor import launchhome
    launchhome.migrate_home()
    key = store_key(cfg)
    deadline = time.monotonic() + 90 if deadline is None else deadline
    leds, held = _enumerate(board, cfg, state, key, say)
    managed = set(held) | {led.job for led in leds}
    managed |= _orphans(board, cfg, state, key, managed, now, say)
    m = None
    if leds or held:
        try:
            m = _modules()
        except ImportError as exc:                # a half-installed build: loud, and every job holds
            for led in leds:
                _notice(board, state, f"modules|{led.job}", led.job,
                        f"{ev.DRIVER_ERROR} the driver's modules are missing ({exc}); the job holds")
            return managed
        for led in leds:                         # spec 3.2: deadlines of EVERY job first (ledger only, cheap)
            if time.monotonic() >= deadline:
                break
            try:
                m.life.enforce_deadlines(board, _flat_cfg(cfg, led), led, now)
            except Exception as exc:
                log.warning("deadlines of %s failed: %s", led.job, type(exc).__name__)
    for hint in sorted(held):                    # a ledger that does not read: only a repair request can mend it
        if time.monotonic() >= deadline or (job and hint != job):
            continue
        try:
            _control(board, cfg, state, None, hint, now, min(deadline, time.monotonic() + JOB_SECONDS), m)
        except Exception as exc:
            log.warning("control requests of %s failed: %s", hint, type(exc).__name__)
    for led in sorted(leds, key=lambda l: l.last_pass or ""):      # least recently driven first
        if time.monotonic() >= deadline:
            break
        if job and led.job != job:
            continue
        owner = enrolment.find_job_owner(key, led.job)
        if owner is None:
            _notice(board, state, f"orphan|{led.job}", led.job,
                    f"{ev.DRIVER_ORPHAN} this job has a ledger but no owner record on this host; it holds")
            for row in board.launches(led.job, led.generation):
                driver_exec.skip_launch(board, led, row.name, "owner record missing on this host")
            _report_skips(board, led)
            continue
        retry_at = led.watermarks.get("driver-error:retry-at", 0)
        if retry_at > now.timestamp():
            continue
        jdeadline = min(deadline, time.monotonic() + JOB_SECONDS)
        try:
            for row in board.launches(led.job, led.generation):
                if row.state == "queued":
                    board.set_job_data(led.job, f"drive.skip.{led.generation}.{row.name}", None)
            _step(board, cfg, sup, state, led, owner, now, say, start_runner, config_path, jdeadline, m)
            if led.errors:
                def recovered(l):
                    l.errors = 0
                    l.watermarks.pop("driver-error:retry-at", None)
                _mutate(led, recovered)
                _forget(state, f"error|{led.job}|")
        except ledger.LedgerBadError as exc:      # the ledger went bad under us (halt, unhalt, a save): hold
            _bad(board, state, led.job, str(exc)[:200], say)
        except Exception as exc:
            _failed(board, state, led, exc, now, _flat_cfg(cfg, led), m)
            for row in board.launches(led.job, led.generation):
                driver_exec.skip_launch(board, led, row.name, f"driver step failed: {type(exc).__name__}: {str(exc)[:300]}")
        try:
            _report_skips(board, led)
            _mutate(led, lambda l: setattr(l, "last_pass", _iso(now)))
        except Exception as exc:
            log.warning("last_pass of %s not saved: %s", led.job, type(exc).__name__)
    return managed


def drive_jobs(board, cfg: dict) -> set[str]:
    """The names of the jobs the driver owns (a ledger, a bad ledger or a drive claim here), without driving
    anything: what a dry run leaves alone."""
    from swarm.board.autoinit import store_key
    key = store_key(cfg)
    try:
        leds, bad = _scan_files(key, read_only=True)
        held = {job for job, _name, _why in bad if job}
    except Exception:
        leds, held = [], set()
    return set(held) | {led.job for led in leds} | {js.job for js in _claimed_jobs(board)}


def run(board, cfg: dict, sup: dict, state: dict, *, now, say=print, start_runner=None, which=None, scope_ok=None,
        config_path: str = "", deadline: float | None = None, job: str | None = None) -> set[str]:
    """`drive`, guarded for the pass: a failure of the driver itself (an import, the enumeration) is logged and
    the job names that can be named without it are still returned, so the other passes leave them alone."""
    configure_log()
    if now is None:   # the CLI's `swarm supervise` passes none: the board's clock
        now = board.now()
    try:
        return drive(board, cfg, sup, state, now=now, say=say, start_runner=start_runner, which=which,
                     scope_ok=scope_ok, config_path=config_path, deadline=deadline, job=job)
    except Exception as exc:
        log.warning("driver pass failed: %s", type(exc).__name__, exc_info=True)
        return drive_jobs(board, cfg)
