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
from types import SimpleNamespace

from swarm.supervisor import driver_events as ev, ledger

log = logging.getLogger("swarm.supervisor.driver")

SUPERVISOR = "swarm supervisor"
JOB_SECONDS = 30.0            # one job's share of the pass (spec 3.2)
RECONCILE_SECONDS = 20.0
ACTIVATION_GRACE_SECONDS = 120.0
CLOSED = ("completed", "cancelled", "failed")
PERSON_RECEIPT_ACTION = "requester:deactivate"


def _modules():
    """The collaborating modules, imported when a pass runs (tests replace this with fakes)."""
    from swarm.supervisor import driver_exec, driver_life, driver_model, driver_snapshot
    return SimpleNamespace(exec=driver_exec, life=driver_life, model=driver_model, snapshot=driver_snapshot)


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
    try:
        board.post(job, SUPERVISOR, text[:1900])
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

def _enumerate(board, cfg: dict, state: dict, key: str, say):
    """(ledgers, held): every unfinished ledger of this board, and the job names held for a bad ledger.
    A planted lock (or anything else that makes `ledger.unfinished` raise) holds the jobs it cannot read
    and does not stop the pass for the others."""
    held: set = set()
    from swarm.supervisor import settings
    if not os.path.lexists(settings.private_dir() / ledger.SUBDIR):
        return [], held          # no ledger directory yet: nothing to read, and a read creates nothing
    try:
        entries = ledger.unfinished(key)
    except Exception as exc:
        log.warning("ledger enumeration failed: %s", type(exc).__name__)
        entries = []
        for js in _claimed_jobs(board):
            try:
                led = ledger.load(key, js.job)
            except Exception as exc2:
                _bad(board, state, js.job, f"{type(exc2).__name__}", say)
                held.add(js.job)
                continue
            if led is not None and led.terminal is None:
                entries.append(led)
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
        if enrolment.find_job_owner(key, js.job) is None and enrolment.find_job(key, js.job) is None:
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

def _requester_deactivated(led, key: str, now: dt.datetime) -> bool:
    """A receipted `swarm deactivate` by the job's requester session (hook receipt kind person) of this
    incarnation and generation, unexpired."""
    from swarm import enrolment, receipts, safefs
    try:
        with enrolment._dir(create=False) as d:
            found = safefs.scan(d, ".json", limit=receipts.RECORD_MAX,
                                match=lambda n: n.startswith("receipt-"))
    except OSError:
        return False
    want = receipts.content_sha("person", {"action": "deactivate", "target": led.job})
    for _name, data in found:
        r = receipts._parse(data)
        if (r is not None and r.board_key == led.board_key and r.job == led.job and r.kind == "person"
                and r.incarnation == led.incarnation and r.generation == led.generation
                and r.action_id == PERSON_RECEIPT_ACTION and r.content_sha == want
                and r.expires_at > now.timestamp()):
            return True
    return False


def tamper(led, js, owner, data: dict) -> str | None:
    """What the board row says that the ledger and the owner record do not allow, or None."""
    if js is None:
        return "job row missing"
    if js.status in CLOSED:
        return f"job closed ({js.status}) by {js.closed_by or 'someone'}"
    goal = (getattr(owner, "owner", None) or {}).get("goal")
    if goal and (js.goal or "") != goal:
        return "goal changed"
    if not js.supervise:
        return "supervise switched off"
    if (data or {}).get("drive.enabled") != "1":
        return "drive flag cleared"
    return None


def _repair(board, led, owner, js) -> bool:
    """Rewrite the board row from the ledger and the owner record, where the backend allows."""
    goal = (getattr(owner, "owner", None) or {}).get("goal") or None
    try:
        if js is not None and (js.status in CLOSED or (goal and (js.goal or "") != goal)):
            board.open_job(led.job, None, None, None, None, goal=goal)
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


def _check_tamper(board, cfg, state, led, js, owner, data, now, m, say) -> str | None:
    """None when the row agrees; else "cancelled" (the requester's deactivate, job finished) or "held"."""
    if getattr(led, "state", "active") == "activating":
        return None                                    # the activation is still being completed
    intent = led.terminal_intent
    if js is not None and js.status in CLOSED and intent is not None and js.closed_by == m.life.CLOSED_BY:
        return None                                    # the driver's own close; terminalize commits it
    if js is not None and js.status == "paused":
        return None
    what = tamper(led, js, owner, data)
    if what is None:
        _forget(state, f"tamper|{led.job}|")
        return None
    if js is not None and js.status in CLOSED and _requester_deactivated(led, led.board_key, now):
        _cancel(board, cfg, led, now, m)
        say(f"drive: {led.job} cancelled by its requester")
        return "cancelled"
    repaired = _repair(board, led, owner, js)
    if not led.halted:
        m.life.halt(board, cfg, led, "driver (tamper)", now, reason=f"tamper: {what}"[:200])
    if _notice(board, state, f"tamper|{led.job}|{what}", led.job,
               f"{ev.DRIVER_TAMPER} {what}: " + ("the row was restored from the ledger; " if repaired
                                           else "the row could not be restored; ")
               + "the job is halted until a person unhalts it"):
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
        life.begin_terminal(board, led, gv.verdict, gv.scope, gv.by or "judge", action.reason or gv.verdict,
                            now, candidate=gv.candidate, req=gv.req)
    return True


def _stop_seat(board, cfg, led, seat: str, m) -> None:
    life = m.life
    launches = {i.launch_id for i in led.intents if i.seat == seat and i.state != "concluded"}
    rows = [r for r in life._open_rows(board, led) if r.launch_id in launches]
    if rows:
        life.Hooks().stop_rows(board, cfg, rows)


def _act(board, cfg, sup, led, snap, actions, now, start_runner, config_path, jdeadline, m) -> None:
    for a in actions:
        if _over(jdeadline):
            return
        if a.kind == "launch":
            m.exec.execute(board, cfg, sup, led, a, start_runner=start_runner, now=now, config_path=config_path)
        elif a.kind in ("record_unreachable", "close"):
            if _terminal_from(snap, led, board, now, m, a):
                m.life.terminalize(board, cfg, led, now)
        elif a.kind == "rollover":
            m.life.rollover(board, cfg, led, now)
        elif a.kind == "stop_seat" and a.seat:
            _stop_seat(board, cfg, led, a.seat, m)
        # hold, nothing and complete_land (terminalize step 0) need no action here


def _plan(board, cfg, led, dcfg, now, m):
    """Snapshot, progress rule and the model's actions. A ruling that has just become due is recorded in
    the ledger first, so its action ids carry the due time as their trigger from the start."""
    snap = m.snapshot.read(board, led, led.job, cfg=dcfg, now=now)
    m.exec.settle_progress(led, snap, now)
    actions = m.model.next_actions(snap, dcfg)
    if led.ruling_due_at is None and any(a.ruling for a in actions) and m.exec.note_ruling_due(led, now):
        actions = m.model.next_actions(snap, dcfg)
    return snap, actions


def _watch_stalled(board, state, led, actions, now, sup) -> None:
    """DRIVER-STALLED (spec 3.13): the model names no responsible actor (nothing live, no launch, no record) for
    more than two timer periods. The first such pass is remembered in the ledger; any other pass forgets it."""
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
                f"{ev.DRIVER_STALLED} no seat, launch or record is responsible for this job since "
                f"{dt.datetime.fromtimestamp(since, dt.timezone.utc).isoformat()}")


def _step(board, cfg, sup, state, led, owner, now, say, start_runner, config_path, jdeadline, m) -> None:
    life, exe = m.life, m.exec
    flat = _flat_cfg(cfg, led)
    life.enforce_deadlines(board, flat, led, now)                  # cheap; ledger and job row only
    exe.reconcile(board, cfg, led, now, min(jdeadline, time.monotonic() + RECONCILE_SECONDS))
    if led.terminal is not None:
        return
    js = board.job_status(led.job)
    verdict = _check_tamper(board, cfg, state, led, js, owner, board.job_data(led.job), now, m, say)
    if verdict is not None:
        return
    if led.halted is not None:
        life.settle_halt(board, flat, led, now)
        return
    if led.terminal_intent is not None:
        life.terminalize(board, flat, led, now)
        return
    if life.rollover(board, flat, led, now):
        return
    try:
        dcfg = m.snapshot.drive_cfg(led.board_key, led.job, flat)
    except m.snapshot.DriveConfigError as exc:
        _notice(board, state, f"config|{led.job}", led.job,
                f"{ev.DRIVER_CONFIG} the owner record of this job lacks what the driver needs ({exc}); the job holds")
        return
    snap, actions = _plan(board, cfg, led, dcfg, now, m)
    _watch_stalled(board, state, led, actions, now, sup)
    _act(board, cfg, sup, led, snap, actions, now, start_runner, config_path, jdeadline, m)


def _failed(board, state, led, exc, now, flat: dict, m) -> None:
    """A driver step raised: count it; after `driver_error_passes` in a row post DRIVER-ERROR once and make a
    ruling due (spec 3.2), so the deadline can still end the job."""
    log.warning("driver step for %s failed: %s", led.job, type(exc).__name__, exc_info=True)
    try:
        _mutate(led, lambda l: setattr(l, "errors", l.errors + 1))
        if led.errors >= int(flat["driver_error_passes"]):
            if _notice(board, state, f"error|{led.job}", led.job, f"{ev.DRIVER_ERROR} {type(exc).__name__}"):
                m.exec.note_ruling_due(led, now)
    except Exception as exc2:
        log.warning("error count for %s not saved: %s", led.job, type(exc2).__name__)


def drive(board, cfg: dict, sup: dict, state: dict, *, now: dt.datetime, say=print, start_runner=None, which=None,
          scope_ok=None, config_path: str = "", deadline: float | None = None, job: str | None = None) -> set[str]:
    """One drive step of the pass over every unfinished ledger of this board; `deadline` is a
    time.monotonic() value. Returns the names of the jobs the other passes must skip."""
    from swarm import enrolment
    from swarm.board.autoinit import store_key
    key = store_key(cfg)
    deadline = time.monotonic() + 90 if deadline is None else deadline
    leds, held = _enumerate(board, cfg, state, key, say)
    managed = set(held) | {led.job for led in leds}
    managed |= _orphans(board, cfg, state, key, managed, now, say)
    m = _modules() if leds else None
    for led in sorted(leds, key=lambda l: l.last_pass or ""):      # least recently driven first
        if time.monotonic() >= deadline:
            break
        if job and led.job != job:
            continue
        owner = enrolment.find_job_owner(key, led.job)
        if owner is None:
            _notice(board, state, f"orphan|{led.job}", led.job,
                    f"{ev.DRIVER_ORPHAN} this job has a ledger but no owner record on this host; it holds")
            continue
        jdeadline = min(deadline, time.monotonic() + JOB_SECONDS)
        try:
            _step(board, cfg, sup, state, led, owner, now, say, start_runner, config_path, jdeadline, m)
            if led.errors:
                _mutate(led, lambda l: setattr(l, "errors", 0))
        except ledger.LedgerBadError as exc:      # the ledger went bad under us (halt, unhalt, a save): hold
            _bad(board, state, led.job, str(exc)[:200], say)
        except Exception as exc:
            _failed(board, state, led, exc, now, _flat_cfg(cfg, led), m)
        try:
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
        leds, held = _enumerate(board, cfg, {}, key, lambda _m: None)
    except Exception:
        leds, held = [], set()
    return set(held) | {led.job for led in leds} | {js.job for js in _claimed_jobs(board)}


def run(board, cfg: dict, sup: dict, state: dict, *, now, say=print, start_runner=None, which=None, scope_ok=None,
        config_path: str = "", deadline: float | None = None, job: str | None = None) -> set[str]:
    """`drive`, guarded for the pass: a failure of the driver itself (an import, the enumeration) is logged and
    the job names that can be named without it are still returned, so the other passes leave them alone."""
    try:
        return drive(board, cfg, sup, state, now=now, say=say, start_runner=start_runner, which=which,
                     scope_ok=scope_ok, config_path=config_path, deadline=deadline, job=job)
    except Exception as exc:
        log.warning("driver pass failed: %s", type(exc).__name__, exc_info=True)
        return drive_jobs(board, cfg)
