"""Execution half of the job driver (spec 3.2, 3.4, 4.5; plan Task 8a): turn one `launch` action of
`driver_model.next_actions` into a running seat session, and keep the host's books straight afterwards.

    execute()    steps 1a-4 of the one authoritative protocol (4.5): write-ahead intent (fsynced),
                 `claim_drive_launch` (one transaction: seat CAS, budget, caps, locks, restart row),
                 run file, resume marker, kill-switch recheck, `runner.start`.
    reconcile()  at the start of a pass: ledger <-> board <-> local run evidence (4.5 and [F7]).
    settle_progress() / note_ruling_due()
                 the executor-side state the pure decision reads: `rounds_without_progress`,
                 `last_progress_at` and `ruling_due_at`.

The ledger (host-private) is authoritative for budgets and attempts; a board row is trusted only when
its launch_id matches a ledger intent. The runner's `finish()` concludes an intent with the cost it
read (runner.py); this module concludes the ones the runner could not.

Seat names of driver-owned seats: `judge`, `goal-judge`, `ruling-judge`, `reviewer-<slug>-<hash8>`,
`integrator-<slug>-<hash8>` (a group is a branch name, and a seat name may hold only a-z 0-9 _ - ~, so
`reviewer:<group>` of the plan cannot be stored; seat_name() is the one place that spells them).
"""
from __future__ import annotations

import hashlib
import os
import re
import time
import uuid

from swarm import compat, receipts
from swarm.board import JobPaused
from swarm.supervisor import driver_model as dm
from swarm.supervisor import ledger
from swarm.supervisor.settings import log

LAUNCH_GRACE = 90.0          # seconds a claim may have no run file before it is concluded (runner.LAUNCH_GRACE)
LEDGER_TRIES = 5
AUTH_HOURS = 24 * 7          # a driver-authored seat authorization receipt is valid this long
LOCK_SLACK_MINUTES = 15      # a launch's drive locks outlive its wall clock by this
RULING_RESERVE_SESSIONS = 2  # sessions past max_sessions that only a ruling may use (spec 4.1)
READ_ONLY_ROLES = ("judge", "reviewer", "qa", "engineering_lead")
DRIVER_SEATS = ("judge", "goal-judge", "ruling-judge")
# `[drive.models]` shipped defaults (plan Global Constraints, [P1]); 6a's model table overrides them.
DEFAULT_MODELS = {"engineer": "haiku", "worker": "haiku", "integrator": "haiku", "reviewer": "sonnet",
                  "qa": "sonnet", "judge": "opus", "lead": "opus", "security": "opus"}
_SLUG = re.compile(r"[^a-z0-9_-]+")


# --- small helpers -------------------------------------------------------------------------------

def _iso(now) -> str:
    return now.isoformat()


def _epoch(now) -> float:
    return now.timestamp()


def _dcfg(cfg: dict, led) -> dict:
    """The effective drive settings: shipped defaults, `[drive]`, then the job's budgets."""
    section = (cfg or {}).get("drive")
    return {**dm.DEFAULT_CFG, **(section if isinstance(section, dict) else {}), **(led.budgets or {})}


def mutate(led, fn) -> None:
    """Apply `fn(led)` and save, redoing both on a ledger conflict (another writer saved first): the
    in-memory copy is refreshed from disk, so nothing is overwritten and nothing is lost."""
    for _ in range(LEDGER_TRIES):
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


def seat_name(kind: str, group: str | None = None) -> str:
    """The deterministic seat name of a driver-owned seat: `judge`, `goal-judge`, `ruling-judge`, or
    `reviewer`/`integrator` + a slug of the artifact group + 8 hex digits of its hash."""
    if kind in DRIVER_SEATS:
        return kind
    slug = _SLUG.sub("-", (group or "").lower()).strip("-")[:30] or "group"
    return f"{kind}-{slug}-{hashlib.sha256((group or '').encode()).hexdigest()[:8]}"


def _action_group(action) -> str | None:
    parts = action.action_id.split("|")
    return parts[3] if len(parts) > 3 and parts[3] != "-" else None


def _driver_seat(action) -> tuple[str, str] | None:
    """(seat name, role) of the driver-owned seat an action with no seat runs on, or None."""
    kind, group = action.prompt_kind, _action_group(action)
    if action.ruling or group == "ruling":
        return "ruling-judge", "judge"
    if kind == "goal" or group == "goal":
        return "goal-judge", "judge"
    if kind == "judge":
        return "judge", "judge"
    if kind == "review":
        return seat_name("reviewer", group), "reviewer"
    if kind == "integrate":
        return seat_name("integrator", group), "integrator"
    return None


# --- owner, models, authorization ----------------------------------------------------------------

def _owner(led):
    """The host-private owner record of the job (harness, work dir, brief), or None."""
    from swarm import enrolment
    return enrolment.find_job_owner(led.board_key, led.job)


def _models(cfg: dict, led) -> dict:
    section = ((cfg or {}).get("drive") or {}).get("models")
    return {**DEFAULT_MODELS, **(section if isinstance(section, dict) else {}),
            **(getattr(led, "models", None) or {})}


# STUB until Task 5 lands drive_authz.py: the same canonical argv and decision fields.
def _canonical_seat_args(seat) -> list[str]:
    try:
        from swarm.supervisor import drive_authz
        return drive_authz.canonical_seat_args(seat)
    except ImportError:
        argv = ["seat", "request", "--job", seat.job, "--seat", seat.seat, "--role", seat.role,
                "--harness", seat.harness]
        if seat.model is not None:
            argv += ["--model", seat.model]
        argv += ["--brief", seat.brief_path, "--workdir", seat.workdir]
        if seat.title is not None:
            argv += ["--title", seat.title]
        if seat.task_ref is not None:
            argv += ["--task", seat.task_ref]
        return argv + ["--complexity", seat.complexity]


def _seat_decision(seat) -> dict:
    return {"kind": "seat_request", "id": str(seat.id), "rev": seat.revision, "role": seat.role,
            "model": seat.model, "brief": seat.brief_path, "workdir": seat.workdir}


def authorize_seat(led, seat, now) -> None:
    """Write the driver-authored authorization of a seat row the driver itself created (a driver
    seat, a successor): a seat_request receipt for the row's id and revision, as the hook writes for
    a lead's own request (spec 3.4). The action id names the writer."""
    receipts.write(led.board_key, receipts.Receipt(
        board_key=led.board_key, job=led.job, generation=seat.generation, incarnation=led.incarnation,
        action_id=f"driver|authorize|{seat.seat}", attempt=0, seat=seat.seat, kind="seat_request",
        record_id=str(seat.id), rev=seat.revision,
        content_sha=receipts.content_sha("seat_request", _seat_decision(seat)),
        args_sha=receipts.args_sha(_canonical_seat_args(seat)), expires_at=_epoch(now) + AUTH_HOURS * 3600))


def seat_authorized(led, seat, now) -> bool:
    """Whether the receipt for this seat row, as it is now, exists and matches it (fails closed)."""
    r = receipts.find(led.board_key, led.job, "seat_request", str(seat.id), rev=seat.revision,
                      incarnation=led.incarnation, generation=seat.generation, now=_epoch(now))
    return (r is not None and r.args_sha == receipts.args_sha(_canonical_seat_args(seat))
            and receipts.matches(r, _seat_decision(seat)))


_SAME_FIELDS = ("role", "harness", "model", "workdir", "brief_path", "title", "task_ref", "complexity")


def carry_authorization(led, old, new, now) -> None:
    """A seat's revision changes with every state change; its authorization (bound to a revision) is
    carried to the new revision by the driver when the old one was authorized and no request field
    differs: a row altered meanwhile by another writer is not blessed."""
    if (new is not None and seat_authorized(led, old, now)
            and all(getattr(old, f) == getattr(new, f) for f in _SAME_FIELDS)):
        authorize_seat(led, new, now)


def _successor_of(board, led, name: str):
    """(predecessor row) of a successor seat name `<base>~<n>`: the base seat, or the previous successor."""
    base, _, n = name.partition("~")
    if not n.isdigit():
        return None
    prev = base if int(n) <= 1 else f"{base}~{int(n) - 1}"
    return board.seat(led.job, led.generation, prev) or board.seat(led.job, led.generation, base)


def materialize_seat(board, led, action, cfg: dict, now):
    """The seat row an action runs on, creating (and authorizing) a driver-owned seat or a successor
    that does not exist yet. None when the action names a seat that is not there and is not ours to
    create. A seat that already exists is returned as it is (authorization is checked by the caller)."""
    job, gen = led.job, led.generation
    if action.seat is not None:
        row = board.seat(job, gen, action.seat)
        if row is not None:
            return row
        prev = _successor_of(board, led, action.seat)
        if prev is None:
            return None
        row = board.request_seat(job, gen, action.seat, role=prev.role, harness=prev.harness, model=prev.model,
                                 workdir=prev.workdir, brief_path=prev.brief_path, title=prev.title,
                                 task_ref=prev.task_ref, complexity=prev.complexity, requested_by="driver")
        authorize_seat(led, row, now)
        return row
    spec = _driver_seat(action)
    if spec is None:
        return None
    name, role = spec
    row = board.seat(job, gen, name)
    if row is not None:
        return row
    owner = _owner(led)
    if owner is None:
        return None
    o = getattr(owner, "owner", None) or {}
    workdir = owner.cwd or (o.get("roots") or [None])[0]
    brief = o.get("brief") or os.path.join(workdir, ".swarm", f"lead-brief-{job}.md")
    model = _models(cfg, led).get(role)
    row = board.request_seat(job, gen, name, role=role, harness=owner.harness, model=model, workdir=workdir,
                             brief_path=brief, title=name, complexity="standard", requested_by="driver")
    authorize_seat(led, row, now)
    return row


# --- budget and locks ----------------------------------------------------------------------------

def _reservation(d: dict, led, ruling: bool) -> tuple[float, float] | None:
    """(usd, minutes) to reserve for one launch, or None when the budget does not allow it (spec 4.1):
    seats leave the ruling reserve alone, a ruling draws on it."""
    sessions = sum(1 for i in led.intents if i.outcome != "lost")
    limit = d["max_sessions"] + (RULING_RESERVE_SESSIONS if ruling else 0)
    if (sessions >= limit) if ruling else (sessions >= d["max_sessions"]):
        return None
    left = d["max_cost_usd"] - led.charged_usd() - (0 if ruling else d["ruling_reserve_usd"])
    usd = min(float(d["session_usd_cap"]), left)
    if usd < d["min_session_usd"] and not (ruling and usd > 0):
        return None
    return usd, float(d["max_session_minutes"])


def _locks(seat) -> tuple[list[str], dict]:
    """The drive locks of a launch: a writer seat claims the worktree root of its work dir."""
    if seat.role in READ_ONLY_ROLES:
        return [], {}
    from swarm.supervisor import worktree
    root = worktree.worktree_root(seat.workdir) or os.path.realpath(seat.workdir).replace(os.sep, "/")
    key = "wt:" + hashlib.sha256(root.encode()).hexdigest()
    return [key], {key: root}


# --- execute -------------------------------------------------------------------------------------

ACK_OUTCOMES = ("completed", "max_turns", "timeout")   # [F4]: the session completed at least one turn


def _conclude(led, launch_id: str, outcome: str, success: bool, charged: float) -> None:
    """Conclude an open intent (once) and settle its wake watermarks: the ids the launch was started
    with become the seat's acknowledged `msg:<seat>`/`event:<seat>` marks when its session completed a
    turn ([F4]); either way the pending marks of the launch are dropped."""
    def apply(l):
        for i in l.intents:
            if i.launch_id != launch_id or i.state == "concluded":
                continue
            i.state, i.outcome, i.success, i.charged_usd = "concluded", outcome, success, charged
            for kind in ("msg", "event"):
                pending = l.watermarks.pop(f"launch-{kind}:{launch_id}", None)
                if pending is not None and outcome in ACK_OUTCOMES and i.seat:
                    key = f"{kind}:{i.seat}"
                    l.watermarks[key] = max(l.watermarks.get(key, 0), pending)
    mutate(led, apply)


def conclude_run(cfg: dict, run: dict, outcome: str, cost: float | None, outage: bool = False) -> bool:
    """Called by the runner's finish() for a drive run: conclude the ledger intent of the run's launch
    with the cost the runner read (the reservation when it read none, nothing for a refused launch).
    False when the ledger kept changing (finish is retried); True when done or there is nothing to do."""
    try:
        led = ledger.load(run["board"], run["job"])
    except ledger.LedgerBadError as exc:
        log(f"job {run['job']}: ledger unreadable at finish of restart {run['restart_id']}: {exc}")
        return True
    intent = next((i for i in led.intents if i.launch_id == run["launch_id"]), None) if led else None
    if intent is None or intent.state == "concluded":
        return True
    if cost is None:
        cost = 0.0 if outcome == "refused" else intent.reserved_usd
    try:
        _conclude(led, run["launch_id"], "outage" if outage and outcome == "failed" else outcome,
                  outcome == "completed", float(cost))
    except ledger.LedgerConflict:
        return False
    return True


def _set_state(led, launch_id: str, state: str) -> None:
    def apply(l):
        for i in l.intents:
            if i.launch_id == launch_id and i.state == "claiming":
                i.state = state
    mutate(led, apply)


def _discard(run: dict, *, marker=None) -> None:
    """Remove what a launch that did not start left on this host: its marker and its run file."""
    from swarm.supervisor import markers
    for step in (lambda: markers.remove_resume_marker(marker) if marker else None,
                 lambda: _unlink_run(run)):
        try:
            step()
        except Exception as exc:
            log(f"launch {run['launch_id']}: cleanup step failed: {type(exc).__name__}")


def _unlink_run(run: dict) -> None:
    from swarm.supervisor import privfs, runner
    with runner._runs_fd(run["board"]) as d:
        privfs.unlink(d, runner.run_file(run["restart_id"], run["board"]).name)


def _idle_seat(board, led, name: str, outcome: str, now, **fields) -> None:
    """Move a seat whose launch ended from running to idle (keeping its authorization)."""
    seat = board.seat(led.job, led.generation, name)
    if seat is None or seat.state != "running":
        return
    try:
        moved = board.set_seat_state(seat.id, seat.revision, "idle", job=led.job, last_outcome=outcome, **fields)
    except ValueError:
        return                           # its launch is still open: reconcile moves it later
    carry_authorization(led, seat, moved, now)


def _check_resumable(seat) -> str | None:
    """The session handle to resume, for a seat that has run before and is not driver-owned."""
    return seat.session_id if seat.state == "idle" and seat.session_id else None


def execute(board, cfg, sup, led, action, *, start_runner=None, now, prompt: str | None = None,
            config_path: str = ""):
    """Run one `launch` action (spec 4.5 steps 1a-4). Returns the run dict handed to the runner, or None
    when nothing was started (not a launch, the action is not allowed now, the claim lost its race, a
    kill switch went off). An exception from the runner's start propagates after the attempt is
    concluded `failed`."""
    from swarm.board.autoinit import store_key
    from swarm.supervisor import command, launch, markers, runner
    if action.kind != "launch" or not action.action_id.startswith(f"gen{led.generation}|"):
        return None
    d = _dcfg(cfg, led)
    mine = [i for i in led.intents if i.action_id == action.action_id]
    if any(i.state != "concluded" for i in mine):
        return None                      # an open attempt of this action blocks a second one
    limit = d["ruling_attempts"] if action.ruling else d["max_attempts"]
    if sum(1 for i in mine if i.outcome not in ("outage", "lost")) >= limit:
        return None
    seat = materialize_seat(board, led, action, cfg, now)
    if seat is None or seat.state not in ("requested", "idle"):
        return None
    if not seat_authorized(led, seat, now):
        log(f"job {led.job}: seat {seat.seat} is not authorized; not launched")
        return None
    reserve = _reservation(d, led, action.ruling)
    if reserve is None:
        return None
    reserve_usd, reserve_minutes = reserve
    owned = action.seat is None
    resume = None if owned else _check_resumable(seat)
    model = seat.model or _models(cfg, led).get(seat.role) or launch.replacement_model(cfg, seat.harness, seat.role, None)
    text = prompt if prompt is not None else _render_prompt(board, cfg, led, action, seat)
    spec = launch.spec_for(cfg, seat.harness, prompt=text, workdir=seat.workdir, model=model,
                           minutes=reserve_minutes, budget_usd=reserve_usd, read_only=seat.role in READ_ONLY_ROLES,
                           resume=resume, drive=True)
    lock_keys, lock_paths = _locks(seat)
    marks = _board_marks(board, led.job)
    launch_id, attempt = new_launch_id(now), max((i.attempt for i in mine), default=0) + 1
    # 1a. write-ahead intent, fsynced (ledger.save), before anything reaches the board
    mutate(led, lambda l: l.intents.append(ledger.Intent(
        launch_id=launch_id, action_id=action.action_id, attempt=attempt, seat=seat.seat,
        reserved_usd=reserve_usd, reserved_minutes=reserve_minutes, locks=list(lock_keys), state="claiming")))
    old = f"launch:{launch_id}"
    try:
        try:
            name = board.allocate_name(old, led.job, seat.role)
        except JobPaused:                # nobody joins a paused job: nothing was claimed
            _conclude(led, launch_id, "lost", False, 0.0)
            return None
        board.close_agent(old, "drive predecessor")
        row = board.claim_drive_launch(
            job=led.job, generation=led.generation, action_id=action.action_id, attempt=attempt,
            launch_id=launch_id, seat_id=seat.id, seat_revision=seat.revision, harness=seat.harness,
            reserve_usd=reserve_usd, reserve_minutes=reserve_minutes, locks=lock_keys,
            lock_seconds=min((reserve_minutes + LOCK_SLACK_MINUTES) * 60, 24 * 3600),
            max_host_running=int(d["max_concurrent_sessions"]), ruling=action.ruling, lock_paths=lock_paths)
    except Exception:
        _conclude(led, launch_id, "lost", False, 0.0)
        raise
    if row is None:                      # lost the race: not an attempt, nothing counted (F7)
        _conclude(led, launch_id, "lost", False, 0.0)
        return None
    carry_authorization(led, seat, board.seat(led.job, led.generation, seat.seat), now)
    # the claim is recorded; a launch of a no-progress kind advances the progress counter, and the
    # board position the session starts from is kept until its turn is acknowledged
    advance = dm.counts_as_no_progress(action)

    def claimed(l):
        for i in l.intents:
            if i.launch_id == launch_id and i.state == "claiming":
                i.state = "claimed"
                if advance:
                    l.rounds_without_progress += 1
                l.watermarks[f"launch-msg:{launch_id}"] = marks[0]
                l.watermarks[f"launch-event:{launch_id}"] = marks[1]
    mutate(led, claimed)
    run = {"restart_id": row.id, "job": led.job, "name": name, "harness": seat.harness, "resume_of": old,
           "marker": "", "argv": list(spec.argv), "cwd": spec.cwd, "stdin": spec.stdin,
           "session_id": spec.session_id, "limit_seconds": reserve_minutes * 60,
           "enrol_seconds": sup["enrol_minutes"] * 60, "config": config_path, "board": store_key(cfg),
           "launch_id": launch_id, "action_id": action.action_id, "seat": seat.seat,
           "generation": led.generation}
    marker = None
    try:
        # 3. run file, then the resume marker
        marker = markers.write_resume_marker(cfg, led.job, row.id, resume_of=old, name=name,
                                             harness=seat.harness, session_id=spec.session_id)
        run["marker"] = str(marker)
        if spec.session_id:
            board.set_restart_agent(row.id, spec.session_id)
        runner.write_run(run)
        # 4. recheck the kill switches, then start
        fresh = ledger.load(led.board_key, led.job)
        off = (command._switched_off_now(cfg, board, led.job)
               or ("the job is halted" if fresh is not None and fresh.halted else None))
        if off:
            board.finish_restart(row.id, "cancelled")
            _discard(run, marker=marker)
            board.release_drive_locks(launch_id, job=led.job, holder=action.action_id)
            _idle_seat(board, led, seat.seat, "cancelled", now)
            _conclude(led, launch_id, "cancelled", False, 0.0)
            return None
        (start_runner or runner.start)(cfg, run)
        return run
    except Exception:
        board.finish_restart(row.id, "failed")
        _discard(run, marker=marker)
        board.release_drive_locks(launch_id, job=led.job, holder=action.action_id)
        _idle_seat(board, led, seat.seat, "failed", now)
        _conclude(led, launch_id, "failed", False, 0.0)
        raise


def _board_marks(board, job: str) -> tuple[int, int]:
    """The newest message id and event id of the job: what a session is launched knowing about."""
    msgs = board.recent_messages(1, job=job)
    events = board.pending_events(job)
    return max((m.id for m in msgs), default=0), max((e.id for e in events), default=0)


def _render_prompt(board, cfg, led, action, seat) -> str:
    """The seat prompt from Task 8c's `prompts.render` (STUB until it lands: callers pass `prompt=`)."""
    from swarm.supervisor import prompts
    return prompts.render(board, cfg, led, action, seat)


# --- reconcile -----------------------------------------------------------------------------------

def _launch_age(row, now) -> float:
    return (now - row.at).total_seconds()


def new_launch_id(now) -> str:
    """`t<epoch>-<32 hex>`: unique, and it carries the time of the write-ahead intent, because an
    Intent has no time field of its own."""
    return f"t{int(_epoch(now))}-{uuid.uuid4().hex}"


_LAUNCH_ID = re.compile(r"t(\d{9,11})-[0-9a-f]{32}")


def _intent_age(launch_id: str, now) -> float:
    """Seconds since an intent was written, from the epoch its launch_id carries; an id that does
    not carry one (not made by new_launch_id) is as old as the grace period."""
    m = _LAUNCH_ID.fullmatch(launch_id)
    return _epoch(now) - int(m.group(1)) if m else LAUNCH_GRACE + 1


def _usage_charge(row, intent) -> float:
    if row.cost_usd is not None:
        return float(row.cost_usd)
    return 0.0 if row.outcome in ("refused",) else float(intent.reserved_usd)


def _reap(cfg, board) -> None:
    from swarm.supervisor import runner
    try:
        runner.reap(cfg, board)
    except Exception as exc:
        log(f"reap failed: {type(exc).__name__}")


def _rows_by_launch(board, job: str) -> dict:
    return {r.launch_id: r for r in board.restarts(job=job) if r.launch_id}


def _live_evidence(board_key: str, row_id: int | None, run: dict | None) -> bool:
    """Local evidence that a launch is alive, whatever the board says (F7): its run lock is held, or
    its scope unit is present or cannot be told to be gone."""
    from swarm.supervisor import runner
    if row_id is not None and runner._held(row_id, board_key):
        return True
    unit = (run or {}).get("unit")
    return bool(unit) and runner.unit_presence(unit, (run or {}).get("slice")) != "absent"


def _local_run(cfg, board_key: str, launch_id: str):
    """(restart id, run dict) of the local run file that carries `launch_id`, or None."""
    from swarm.supervisor import runner
    for rid, p in runner._run_paths(board_key):
        run = runner._read_run(p.name, board_key)
        if isinstance(run, dict) and run.get("launch_id") == launch_id:
            return rid, run
    return None


def reconcile(board, cfg, led, now, deadline: float) -> None:
    """Spec 4.5 and [F7], bounded by `deadline` (a time.monotonic() value): reap this board's runs,
    then settle every intent against the board row that carries its launch_id and the local run
    evidence; a row whose launch_id is in no intent is foreign and ignored."""
    from swarm.board.autoinit import store_key
    from swarm.supervisor import markers, runner
    here = store_key(cfg)
    _finish_activation(board, cfg, led)
    if any(i.state != "concluded" for i in led.intents):
        _reap(cfg, board)
    rows = _rows_by_launch(board, led.job)
    known = {i.launch_id for i in led.intents}
    for lid in sorted(set(rows) - known):
        log(f"job {led.job}: restart row {rows[lid].id} has launch_id {lid[:12]} of no intent; ignored")
    for intent in list(led.intents):
        if time.monotonic() >= deadline:
            return
        row = rows.get(intent.launch_id)
        if intent.state == "concluded":
            if row is not None and row.ended_at is None and row.host == compat.node():
                board.finish_restart(row.id, "cancelled")        # a claim that committed late: nobody runs it
                board.release_drive_locks(intent.launch_id, job=led.job, holder=intent.action_id)
            continue
        if row is None:
            local = _local_run(cfg, here, intent.launch_id)       # a deleted board row never restarts a live run
            if local is not None and _live_evidence(here, local[0], local[1]):
                continue
            if _intent_age(intent.launch_id, now) >= LAUNCH_GRACE:
                _conclude(led, intent.launch_id, "failed", False, 0.0)
            continue
        if intent.state == "claiming":
            _set_state(led, intent.launch_id, "claimed")
        if row.ended_at is not None:
            if runner.has_run(cfg, row.id):
                continue                                          # its finish is still under way: it concludes
            _conclude(led, intent.launch_id, row.outcome or "failed", row.outcome == "completed",
                      _usage_charge(row, intent))
            continue
        if row.host != compat.node():
            continue
        if runner.has_run(cfg, row.id) or _live_evidence(here, row.id, None):
            continue                                              # live, or the runner's reap finishes it
        if _launch_age(row, now) < LAUNCH_GRACE:
            continue
        board.finish_restart(row.id, "failed")                    # claimed, never started
        try:
            markers.remove_resume_marker(markers.resume_marker_path(cfg, led.job, row.id))
        except Exception as exc:
            log(f"launch {intent.launch_id}: marker cleanup failed: {type(exc).__name__}")
        board.release_drive_locks(intent.launch_id, job=led.job, holder=intent.action_id)
        _conclude(led, intent.launch_id, "failed", False, 0.0)
    _settle_seats(board, cfg, led, _rows_by_launch(board, led.job), now)
    sweep_unlaunched_seats(board, led.job)
    _sweep_locks(board, cfg, now)


def _sweep_locks(board, cfg: dict, now) -> None:
    """The host's drive-lock sweep with writer fencing (L5) is Task 8b's `driver_life.sweep_locks_by_host`
    (STUB until it lands: skipped when the module is not there)."""
    try:
        from swarm.supervisor import driver_life
        sweep = driver_life.sweep_locks_by_host
    except (ImportError, AttributeError):
        return
    try:
        sweep(board, cfg, now)
    except Exception as exc:
        log(f"drive lock sweep failed: {type(exc).__name__}: {exc}")


def sweep_unlaunched_seats(board, job: str) -> int:
    """End any seat left `running` with no launch (a dev board from before launch ids): it becomes
    `idle` with last_outcome "no launch". The number ended."""
    n = 0
    for seat in board.seats(job):
        if seat.state != "running" or seat.launch_id is not None:
            continue
        try:
            if board.set_seat_state(seat.id, seat.revision, "idle", job=job, last_outcome="no launch") is not None:
                n += 1
        except ValueError as exc:
            log(f"seat {seat.seat} of {job}: not ended: {exc}")
    return n


def _finish_activation(board, cfg: dict, led) -> None:
    """Recover a crashed activation: a ledger still "activating" gets Task 6a's idempotent
    finish_activation (board row, lead seat request, ledger "active"). A failure (no owner record:
    DRIVER-ORPHAN) is logged; the pass's own checks report it."""
    if getattr(led, "state", "active") != "activating":
        return
    try:
        from swarm import drive_activate
        done = drive_activate.finish_activation(cfg, board, led.job)
    except ImportError:
        return                           # STUB until Task 6a lands
    except Exception as exc:
        log(f"job {led.job}: activation not finished: {type(exc).__name__}: {exc}")
        return
    if done is not None:
        led.__dict__.update(done.__dict__)


def _settle_seats(board, cfg: dict, led, rows: dict, now) -> None:
    """A seat `running` whose launch row has ended goes to `idle` (the session ended), or to `failed`
    when its action has run out of attempts without success; it keeps the session handle."""
    d = _dcfg(cfg, led)
    for seat in board.seats(led.job, led.generation):
        if seat.state != "running" or seat.launch_id is None:
            continue
        row = rows.get(seat.launch_id)
        if row is None or row.ended_at is None:
            continue
        intent = next((i for i in led.intents if i.launch_id == seat.launch_id), None)
        spent = (sum(1 for i in led.intents if i.action_id == intent.action_id
                     and i.outcome not in ("outage", "lost")) if intent else 0)
        failed = (intent is not None and row.outcome != "completed" and intent.state == "concluded"
                  and spent >= d["max_attempts"])
        fields = {"last_outcome": row.outcome}
        if row.new_agent_key:
            fields.update(agent_key=row.new_agent_key, session_id=row.new_agent_key)
        try:
            moved = board.set_seat_state(seat.id, seat.revision, "failed" if failed else "idle", job=led.job,
                                         **fields)
        except ValueError as exc:
            log(f"seat {seat.seat}: not settled yet: {exc}")
            continue
        carry_authorization(led, seat, moved, now)


# --- executor-side state: progress and rulings ---------------------------------------------------

def note_ruling_due(led, now) -> bool:
    """Record the moment a ruling became due (the ruling action's trigger and the start of its
    deadline). Once: a later call keeps the first time. True when this call recorded it."""
    if led.ruling_due_at is not None:
        return False
    mutate(led, lambda l: setattr(l, "ruling_due_at", _iso(now)) if l.ruling_due_at is None else None)
    return True


def _hash_int(text: str) -> int:
    return int(hashlib.sha256(text.encode()).hexdigest()[:12], 16)


def _prev_view(group_key: str, scope: str, generation: int, wm: dict):
    """The previous judged round of a group as a VerdictView, from the ledger's watermarks."""
    if f"p.open.{group_key}" not in wm:
        return None
    art = wm.get(f"p.art.{group_key}")
    return dm.VerdictView(verdict="not_met", scope=scope, artifact=None if art is None else str(art),
                          candidate=None, req=None, generation=generation, open=wm[f"p.open.{group_key}"],
                          progress=None, reason="", next="", by=None, at=None, receipt_id=None)


def settle_progress(led, snap, now) -> bool:
    """The progress rule (spec 3.3 and 3.6) over what the snapshot shows that the ledger has not seen:
    a new judged `not_met` round either shows progress (`driver_model.progress`: the counter resets and
    `last_progress_at` moves) or advances `rounds_without_progress`; a new hand-off, an integration or a
    verdict with progress moves `last_progress_at`; an integration or progress also resets the counter.
    Idempotent: what was seen is kept in the ledger's watermarks. True when the ledger changed."""
    wm = led.watermarks
    handoff_id = max([h.receipt_id for h in snap.handoffs if h.receipt_id is not None] or [0])
    landed = sum(1 for e in snap.integrated.values() if (e or {}).get("landed"))
    judged = [v for v in list(snap.verdicts.values()) + ([snap.goal_verdict] if snap.goal_verdict else [])
              if v.receipt_id is not None and v.receipt_id > wm.get("p.verdict", 0)]
    judged.sort(key=lambda v: v.receipt_id)
    if handoff_id <= wm.get("p.handoff", 0) and landed <= wm.get("p.landed", 0) and not judged:
        return False
    activity = dm._Ctx(snap, {}).last_activity()   # the one definition of "newest activity"
    stamp = _iso(activity or now)
    plan = {"moved": False, "reset": False, "rounds": 0, "wm": {}}
    if handoff_id > wm.get("p.handoff", 0):
        plan["moved"] = True
        plan["wm"]["p.handoff"] = handoff_id
    if landed > wm.get("p.landed", 0):
        plan["moved"] = plan["reset"] = True
    plan["wm"]["p.landed"] = max(landed, wm.get("p.landed", 0))
    view_wm = dict(wm)
    for v in judged:
        plan["wm"]["p.verdict"] = v.receipt_id
        if v.verdict != "not_met":
            continue
        group = (v.artifact or "goal").partition("@")[0] if v.scope != "goal" else "goal"
        key = hashlib.sha256(group.encode()).hexdigest()[:16]
        if dm.progress(_prev_view(key, v.scope, v.generation, view_wm), v):
            plan["moved"] = plan["reset"] = True
            plan["rounds"] = 0
        else:
            plan["rounds"] += 1
        if v.open is not None:
            view_wm[f"p.open.{key}"] = plan["wm"][f"p.open.{key}"] = v.open
        if v.scope != "goal" and v.artifact:
            view_wm[f"p.art.{key}"] = plan["wm"][f"p.art.{key}"] = _hash_int(v.artifact)

    def apply(l):
        l.watermarks.update(plan["wm"])
        if plan["reset"]:
            l.rounds_without_progress = 0
        l.rounds_without_progress += plan["rounds"]
        if plan["moved"]:
            l.last_progress_at = stamp
    mutate(led, apply)
    return True
