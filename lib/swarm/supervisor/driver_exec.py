"""Execution half of the job driver (spec 3.2, 3.4, 4.5; plan Task 8a): turn one `launch` action of
`driver_model.next_actions` into a running launch session, and keep the host's books straight afterwards.

    execute()    steps 1a-4 of the one authoritative protocol (4.5): write-ahead intent (fsynced),
                 `claim_drive_launch` (one transaction: launch CAS, budget, caps, locks, restart row),
                 run file, resume marker, kill-switch recheck, `runner.start`.
    reconcile()  at the start of a pass: ledger <-> board <-> local run evidence (4.5 and [F7]).
    settle_progress() / note_ruling_due()
                 the executor-side state the pure decision reads: `rounds_without_progress`,
                 `last_progress_at` and `ruling_due_at`.

The ledger (host-private) is authoritative for budgets and attempts; a board row is trusted only when
its launch_id matches a ledger intent. The runner's `finish()` concludes an intent with the cost it
read (runner.py); this module concludes the ones the runner could not.

Launch names of driver-owned launches: `judge`, `goal-judge`, `ruling-judge`, `reviewer-<slug>-<hash8>`,
`integrator-<slug>-<hash8>` (a group is a branch name, and a launch name may hold only a-z 0-9 _ - ~, so
`reviewer:<group>` of the plan cannot be stored; launch_name() is the one place that spells them).
"""
from __future__ import annotations

import hashlib
import datetime as dt
import importlib
import importlib.util
import inspect
import os
import re
import sys
import time
import uuid

from swarm import compat, driveroles, receipts
from swarm.board import JobPaused
from swarm.supervisor import barrier
from swarm.supervisor import driver_model as dm
from swarm.supervisor import ledger, procwatch
from swarm.supervisor.settings import log

LAUNCH_GRACE = 90.0          # seconds a claim may have no run file before it is concluded (runner.LAUNCH_GRACE)
LEDGER_TRIES = 5
LOCK_SLACK_MINUTES = 15      # a launch's drive locks outlive its wall clock by this
DRIVER_LAUNCHES = ("judge", "goal-judge", "ruling-judge", "learner")
# `[drive.models]` shipped defaults (plan Global Constraints, [P1]); 6a's model table overrides them.
DEFAULT_MODELS = {"engineer": "haiku", "worker": "haiku", "integrator": "haiku", "reviewer": "sonnet",
                  "qa": "sonnet", "judge": "opus", "lead": "opus", "security": "opus"}
_SLUG = re.compile(r"[^a-z0-9_-]+")
TMP_ROOTS = ("/tmp", "/dev/shm")   # launch workdirs under these are refused (host RAM, swept; Global Constraints)
# A resume handle goes into the argv of the launch's CLI: a UUID (Claude sessions and Codex threads), or a
# plain token that cannot start with "-" and holds no "=" or other option syntax.
_RESUME_HANDLE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
                            r"|[A-Za-z0-9][A-Za-z0-9_.]{7,79}")
_ABSENT_LOGGED: set = set()


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


def launch_name(kind: str, group: str | None = None) -> str:
    """The deterministic launch name of a driver-owned launch: `judge`, `goal-judge`, `ruling-judge`, or
    `reviewer`/`integrator` + a slug of the artifact group + 8 hex digits of its hash."""
    if kind in DRIVER_LAUNCHES:
        return kind
    slug = _SLUG.sub("-", (group or "").lower()).strip("-")[:30] or "group"
    return f"{kind}-{slug}-{hashlib.sha256((group or '').encode()).hexdigest()[:8]}"


def _action_group(action) -> str | None:
    parts = action.action_id.split("|")
    return parts[3] if len(parts) > 3 and parts[3] != "-" else None


def _driver_launch(action) -> tuple[str, str] | None:
    """(launch name, role) of the driver-owned launch an action with no launch runs on, or None."""
    kind, group = action.prompt_kind, _action_group(action)
    if kind == "learn":
        return "learner", "learner"
    if action.ruling or group == "ruling":
        return "ruling-judge", "judge"
    if kind == "goal" or group == "goal":
        return "goal-judge", "judge"
    if kind == "judge":
        return "judge", "judge"
    if kind == "review":
        return launch_name("reviewer", group), "reviewer"
    if kind == "integrate":
        return launch_name("integrator", group), "integrator"
    return None


def _module(name: str):
    """The module `name` when it exists, imported unguarded (a broken module raises); None, logged once,
    when it is not part of this build (a task that has not landed)."""
    loaded = sys.modules.get(name)
    if loaded is not None:
        return loaded
    if importlib.util.find_spec(name) is None:
        if name not in _ABSENT_LOGGED:
            _ABSENT_LOGGED.add(name)
            log(f"module {name} is not available; its step is skipped")
        return None
    return importlib.import_module(name)


def valid_resume_handle(handle) -> bool:
    return isinstance(handle, str) and _RESUME_HANDLE.fullmatch(handle) is not None


def read_only_role(role) -> bool:
    """Roles are prompt instructions; every drive launch has the same writable workdir."""
    return False


def _in_tmp(path: str) -> bool:
    real = os.path.realpath(path)
    return any(real == r or real.startswith(r + os.sep) for r in TMP_ROOTS)


# --- owner, models, authorization ----------------------------------------------------------------

def _owner(led):
    """The host-private owner record of the job (harness, work dir, brief), or None."""
    from swarm import enrolment
    return enrolment.find_job_owner(led.board_key, led.job)


def _models(cfg: dict, led) -> dict:
    section = ((cfg or {}).get("drive") or {}).get("models")
    return {**DEFAULT_MODELS, **(section if isinstance(section, dict) else {}),
            **(getattr(led, "models", None) or {})}


def _driver_fields(cfg: dict, led, role: str) -> dict | None:
    """The fields of a driver-owned launch (judge, reviewer, integrator): the owner record decides."""
    owner = _owner(led)
    if owner is None:
        return None
    o = getattr(owner, "owner", None) or {}
    workdir = owner.cwd or (o.get("roots") or [None])[0]
    if not workdir:
        return None
    from swarm import paths
    from swarm.cli import safe_job
    brief = o.get("brief") or str(paths.host_dir() / "briefs" / f"lead-brief-{safe_job(led.job)}.md")
    return {"role": role, "harness": owner.harness, "model": _models(cfg, led).get(role), "workdir": workdir,
            "brief_path": brief}


def materialize_launch(board, led, action, cfg: dict, now):
    """Find a queued launch; new pipeline/wake triggers can re-queue successful names."""
    if action.launch is not None:
        row = board.get_launch(led.job, led.generation, action.launch)
        if row is not None and row.state == "done" and action.prompt_kind != "launch":
            return _queue_existing(board, row)
        return row
    spec = _driver_launch(action)
    if spec is None:
        return None
    name, role = spec
    row = board.get_launch(led.job, led.generation, name)
    if row is not None:
        if row.state in ("done", "failed") and not any(i.action_id == action.action_id for i in led.intents):
            return _queue_existing(board, row)
        return row
    fields = _driver_fields(cfg, led, role)
    if fields is None:
        return None
    lead = board.get_launch(led.job, led.generation, "lead")
    keys = {lead.agent_key} if lead else set()
    prior = next((r for r in board.restarts(led.job) if lead and r.launch_id == lead.launch_id), None)
    if prior:
        keys.update((prior.old_agent_key, prior.new_agent_key))
    queued_by = next((a.name for a in board.agents(led.job) if a.agent_key in keys),
                     led.requester_session or "requester")
    return board.queue_launch(led.job, led.generation, name, role=role, harness=fields["harness"],
                              model=fields["model"], workdir=fields["workdir"],
                              brief=fields["brief_path"], queued_by=queued_by)


def _queue_existing(board, row):
    return board.queue_launch(row.job, row.generation, row.name, role=row.role, harness=row.harness,
                              model=row.model, workdir=row.workdir, brief=row.brief,
                              queued_by=row.queued_by)


# --- budget and locks ----------------------------------------------------------------------------

def _reservation(d: dict, led, ruling: bool, now=None) -> tuple[float, float] | None:
    """(usd, minutes) to reserve for one launch, or None when the budget does not allow it (spec 4.1):
    launches leave the ruling reserve alone, a ruling draws on it."""
    if now is not None and led.activated_at:
        activated = dt.datetime.fromisoformat(led.activated_at)
        if now - activated >= dt.timedelta(hours=d["max_wall_hours"]):
            return None
    sessions = sum(1 for i in led.intents if i.outcome not in UNCOUNTED)
    if sessions >= d["max_sessions"]:
        return None
    left = d["max_cost_usd"] - led.charged_usd() - (0 if ruling else d["ruling_reserve_usd"])
    usd = min(float(d["session_usd_cap"]), left)
    if usd < d["min_session_usd"] and not (ruling and usd > 0):
        return None
    return usd, float(d["max_session_minutes"])


def _locks(launch) -> tuple[list[str], dict]:
    """Agents may share a workdir. Git coordination belongs to the agents."""
    return [], {}


# --- execute -------------------------------------------------------------------------------------

UNCOUNTED = dm.UNCOUNTED_OUTCOMES        # attempts that do not count toward max_attempts
MAX_EVENTS_SHOWN = 50                                 # event ids one launch shows (and later acknowledges), oldest first
EVACK = "evack:"                                      # evack:<launch>:<event id> = 1: that event was shown and answered
ACK_OUTCOMES = ("completed", "max_turns", "timeout")   # [F4]: the session completed at least one turn


def _conclude(led, launch_id: str, outcome: str, success: bool, charged: float) -> None:
    """Conclude an open intent (once) and settle its wake watermarks: the ids the launch was started
    with become the launch's acknowledged `msg:<launch>`/`event:<launch>` marks when its session completed a
    turn ([F4]); either way the pending marks of the launch are dropped."""
    def apply(l):
        for i in l.intents:
            if i.launch_id != launch_id or i.state == "concluded":
                continue
            i.state, i.outcome, i.success, i.charged_usd = "concluded", outcome, success, charged
            pending = l.watermarks.pop(f"launch-msg:{launch_id}", None)
            if pending is not None and outcome in ACK_OUTCOMES and i.launch:
                key = f"msg:{i.launch}"
                l.watermarks[key] = max(l.watermarks.get(key, 0), pending)
            # events are acknowledged one by one (`evack:<launch>:<id>`), never as a high-water mark: one event
            # with a far id would otherwise hide every later one
            shown = [k for k in l.watermarks if k.startswith(f"launch-ev:{launch_id}:")]
            for k in shown:
                eid = l.watermarks.pop(k)
                if outcome in ACK_OUTCOMES and i.launch:
                    l.watermarks[f"{EVACK}{i.launch}:{eid}"] = 1
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
    if run.get("driver_interrupted") or (outcome != "completed" and
            led.watermarks.get(f"driver-restart:{run['launch_id']}")):
        outcome = "interrupted by driver restart"
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


def _idle_launch(board, led, name: str, outcome: str, now, **fields) -> None:
    """Settle once. A re-queue supersedes a prior attempt's settlement."""
    launch = board.get_launch(led.job, led.generation, name)
    if launch is None or (launch.state != "running" and not (
            driveroles.is_judge_role(launch.role) and launch.state in ("done", "failed")
            and outcome == "no hand-off")):
        return
    if outcome in ("halted", "interrupted by driver restart"):
        board.set_launch_state(launch.id, launch.revision, "queued", job=led.job, last_outcome=outcome)
        return
    missing = driveroles.is_judge_role(launch.role) and outcome == "no hand-off"
    recovery = launch.role == driveroles.LEAD_ROLE and outcome == "no hand-off"
    if missing or (recovery and not led.watermarks.get(f"lead-recovery:{name}")):
        key = f"verdict-missing:{name}" if missing else f"lead-recovery:{name}"
        def note(l):
            once = f"recovery:{launch.launch_id}"
            if once in l.watermarks:
                return
            l.watermarks[once] = 1
            for i in l.intents:
                if i.launch_id == launch.launch_id:
                    i.outcome, i.success = "no hand-off", False
            l.watermarks[key] = l.watermarks.get(key, 0) + 1
            if missing and l.watermarks[key] >= 3:
                l.watermarks[f"retry-at:{name}"] = int(now.timestamp()) + 300
        mutate(led, note)
        if missing and led.watermarks[key] >= 3:
            _escalate_verdict(board, led, launch, now, "three or more runs ended without the mandatory verdict")
        _queue_existing(board, launch)
        return
    if not missing and driveroles.is_judge_role(launch.role):
        mutate(led, lambda l: (l.watermarks.pop(f"verdict-missing:{name}", None),
                               l.watermarks.pop(f"retry-at:{name}", None)))
    if launch.role == driveroles.LEAD_ROLE and outcome == "completed" and led.watermarks.get(f"lead-recovery:{name}"):
        def recovered(l):
            l.watermarks.pop(f"lead-recovery:{name}", None)
            l.rounds_without_progress = 0
        mutate(led, recovered)
    failed = outcome != "completed"
    try:
        moved = board.set_launch_state(launch.id, launch.revision, "failed" if failed else "done", job=led.job,
                                       last_outcome=outcome, **fields)
    except ValueError:
        return
    if moved is not None and failed:
        board.post(led.job, "swarm driver", f"launch {name} failed: {outcome}", to=launch.queued_by)
        board.post_event(led.job, "launch-failed", f"{launch.launch_id}:{launch.attempts}",
                         f"launch {name} failed: {outcome}", to=launch.queued_by)


def _escalate_verdict(board, led, launch, now, cause: str) -> None:
    """Use the launch failure notification path, keeping the job active and the retry scheduled."""
    from swarm.supervisor import driver_events as ev
    mutate(led, lambda l: l.watermarks.__setitem__(f"retry-at:{launch.name}", int(now.timestamp()) + 300))
    history = "; ".join(f"{i.launch_id} attempt {i.attempt}: {i.outcome}"
                        for i in led.intents if i.launch == launch.name)
    text = f"{ev.DRIVER_ESCALATE} {launch.name}: {cause}; retried with the mandatory verdict format; " \
           f"retry in 300s. Tried: {history}"
    board.post_event(led.job, "launch-failed", f"verdict:{launch.launch_id}:{int(now.timestamp())}",
                     text, to=launch.queued_by)
    board.post(led.job, "swarm driver", text, to=launch.queued_by)


def _check_resumable(launch) -> str | None:
    """The session handle to resume, for a launch that has run before and is not driver-owned. The handle
    came through the board, so it must look like a session id (never an option for the CLI): otherwise
    the launch starts a fresh session."""
    if not launch.session_id:
        return None
    if not valid_resume_handle(launch.session_id):
        log(f"launch {launch.name}: the session handle is not a session id; starting fresh")
        return None
    return launch.session_id


def execute(board, cfg, sup, led, action, *, start_runner=None, now, prompt: str | None = None,
            config_path: str = ""):
    """Run one `launch` action (spec 4.5 steps 1a-4). Returns the run dict handed to the runner, or None
    when nothing was started (not a launch, the action is not allowed now, the claim lost its race, a
    kill switch went off). An exception from the runner's start propagates after the attempt is
    concluded `failed`."""
    from swarm.board.autoinit import store_key
    from swarm.supervisor import command, launch as launcher, markers, runner
    if action.kind != "launch" or not action.action_id.startswith(f"gen{led.generation}|"):
        return None
    if led.state != "active":
        return None                      # an activation not finished is never driven ([RR1])
    if action.ruling and action.reason != "missing-verdict":
        mutate(led, lambda l: l.watermarks.setdefault(f"ruling-trigger:{action.reason}", 1))
    d = _dcfg(cfg, led)
    mine = [i for i in led.intents if i.action_id == action.action_id]
    if any(i.state != "concluded" for i in mine):
        return None                      # an open attempt of this action blocks a second one
    limit = d["ruling_attempts"] if action.ruling else d["max_attempts"]
    if sum(1 for i in mine if i.outcome not in UNCOUNTED) >= limit:
        return None
    launch = materialize_launch(board, led, action, cfg, now)
    if launch is None or launch.state not in ("queued",):
        return None
    reserve = _reservation(d, led, action.ruling, now)
    if reserve is None:
        if led.watermarks.get(f"verdict-missing:{launch.name}"):
            _escalate_verdict(board, led, launch, now, "retry blocked by the session, wall-clock or cost budget")
        return None
    reserve_usd, reserve_minutes = reserve
    owned = action.launch is None
    resume = None if owned else _check_resumable(launch)
    model = launch.model or _models(cfg, led).get(launch.role) or launcher.replacement_model(cfg, launch.harness, launch.role, None)
    text = prompt if prompt is not None else _render_prompt(board, cfg, led, action, launch)
    spec = launcher.spec_for(cfg, launch.harness, prompt=text, workdir=launch.workdir, model=model,
                           minutes=reserve_minutes, budget_usd=reserve_usd, read_only=read_only_role(launch.role),
                           resume=resume, drive=True)
    lock_keys, lock_paths = _locks(launch)
    marks = _board_marks(board, led, launch, now)
    launch_id, attempt = new_launch_id(now), max((i.attempt for i in mine), default=0) + 1
    barrier.hit("before_claim")
    # 1a. write-ahead intent, fsynced (ledger.save), before anything reaches the board
    mutate(led, lambda l: l.intents.append(ledger.Intent(
        launch_id=launch_id, action_id=action.action_id, attempt=attempt, launch=launch.name,
        reserved_usd=reserve_usd, reserved_minutes=reserve_minutes, locks=list(lock_keys), state="claiming")))
    barrier.hit("after_intent_before_claim")
    old = f"launch:{launch_id}"
    try:
        try:
            name = board.allocate_name(old, led.job, launch.role)
        except JobPaused:                # nobody joins a paused job: nothing was claimed
            _conclude(led, launch_id, "lost", False, 0.0)
            return None
        if launch.role == "judge":
            # the session takes over this row (claim_resume copies its judge flag), so the job's judge launch goes
            # to it as for a pipeline judge (pipeline.py): its verdict is recorded under its own name
            board.take_judge_launch(old, led.job)
        board.close_agent(old, "drive predecessor")
        row = board.claim_drive_launch(
            job=led.job, generation=led.generation, action_id=action.action_id, attempt=attempt,
            launch_id=launch_id, launch_row_id=launch.id, queue_version=launch.revision, harness=launch.harness,
            reserve_usd=reserve_usd, reserve_minutes=reserve_minutes, locks=lock_keys,
            lock_seconds=min((reserve_minutes + LOCK_SLACK_MINUTES) * 60, 24 * 3600),
            max_host_running=d.get("max_host_running"), max_job_running=int(d["max_concurrent_sessions"]),
            ruling=action.ruling, lock_paths=lock_paths)
    except Exception:
        _conclude(led, launch_id, "lost", False, 0.0)
        raise
    if row is None:                      # lost the race: not an attempt, nothing counted (F7)
        _conclude(led, launch_id, "lost", False, 0.0)
        return None
    barrier.hit("after_claim")
    # the claim is recorded; a launch of a no-progress kind advances the progress counter, and the
    # board position the session starts from is kept until its turn is acknowledged
    advance = dm.counts_as_no_progress(action)

    def claimed(l):
        for i in l.intents:
            if i.launch_id == launch_id and i.state == "claiming":
                i.state = "claimed"
                if advance:
                    l.rounds_without_progress += 1
                l.watermarks[f"rid:{launch_id}"] = row.id      # the one restart row this launch owns
                l.watermarks[f"launch-msg:{launch_id}"] = marks[0]
                for eid in marks[1]:
                    l.watermarks[f"launch-ev:{launch_id}:{eid}"] = eid
    mutate(led, claimed)
    run = {"restart_id": row.id, "job": led.job, "name": name, "harness": launch.harness, "resume_of": old,
           "marker": "", "argv": list(spec.argv), "cwd": spec.cwd, "stdin": spec.stdin,
           "session_id": spec.session_id, "limit_seconds": reserve_minutes * 60,
           "enrol_seconds": sup["enrol_minutes"] * 60, "config": config_path, "board": store_key(cfg),
           "launch_id": launch_id, "action_id": action.action_id, "launch": launch.name,
           "generation": led.generation,
           "driver_pid": os.getpid(), "driver_token": procwatch.start_token(os.getpid()),
           # Task 20: a reviewer's or judge's work tree is bound read-only; the lead writes its briefs and an
           # artifact job's report in the tree, so its tree stays writable (it still has no write rights elsewhere)
           "read_only": read_only_role(launch.role) and launch.role != driveroles.LEAD_ROLE}
    marker = None
    try:
        # 3. run file, then the resume marker
        marker = markers.write_resume_marker(cfg, led.job, row.id, resume_of=old, name=name,
                                             harness=launch.harness, session_id=spec.session_id)
        run["marker"] = str(marker)
        barrier.hit("after_marker")
        if spec.session_id:
            board.set_restart_agent(row.id, spec.session_id)
        runner.write_run(run)
        barrier.hit("after_run_file")
        # 4. recheck the kill switches, then start
        try:
            fresh = ledger.load(led.board_key, led.job)
            held = "the job is halted" if fresh is not None and fresh.halted else None
        except ledger.LedgerBadError as exc:      # an unreadable ledger holds the job: nothing starts
            log(f"job {led.job}: ledger unreadable at launch: {exc}")
            held = "the ledger is unreadable"
        off = command._switched_off_now(cfg, board, led.job, drive=True) or held
        if off:
            board.finish_restart(row.id, "cancelled")
            _discard(run, marker=marker)
            board.release_drive_locks(launch_id, job=led.job, holder=action.action_id)
            _idle_launch(board, led, launch.name, "cancelled", now)
            _conclude(led, launch_id, "cancelled-before-start", False, 0.0)
            return None
        (start_runner or runner.start)(cfg, run)
        barrier.hit("after_runner_start")
        return run
    except Exception:
        board.finish_restart(row.id, "failed")
        _discard(run, marker=marker)
        board.release_drive_locks(launch_id, job=led.job, holder=action.action_id)
        _idle_launch(board, led, launch.name, "failed", now)
        _conclude(led, launch_id, "failed", False, 0.0)
        raise


def _board_marks(board, led, launch, now) -> tuple[int, list]:
    """The message id and event id a session is launched knowing about: only the ids the snapshot would list
    as this launch's pending wakes (driver_snapshot._wakes): messages to the launch's agent that carry the
    ledger's own `post` receipt (host-private, so a database writer cannot fake one), and events addressed
    to that agent, returned as the list of ids shown (each acknowledged on its own). Nothing else moves a mark."""
    prev_msg = led.watermarks.get(f"msg:{launch.name}", 0)
    seen = f"{EVACK}{launch.name}:"
    from swarm.supervisor import driver_snapshot
    names = {a.agent_key: a.name for a in board.agents(led.job, include_departed=True)}
    name = names.get(launch.agent_key) if launch.agent_key else None
    if name is None:
        return prev_msg, []
    rc = driver_snapshot._Receipts(led, led.job, led.generation, now)
    msgs = [m.id for m in board.messages_after(prev_msg, led.job) if m.to_agent == name and m.id > prev_msg
            and rc.get("post", m.id, 1, {"kind": "post", "id": str(m.id), "rev": 1, "to": m.to_agent,
                                         "body": m.message}) is not None]
    events = [e.id for e in board.pending_events(led.job) if e.to and e.to.lower() == name.lower()
              and f"{seen}{e.id}" not in led.watermarks]
    return max(msgs, default=prev_msg), sorted(events)[:MAX_EVENTS_SHOWN]


def _render_prompt(board, cfg, led, action, launch) -> str:
    """The launch prompt from Task 8c's `prompts.render` (callers pass `prompt=` until that lands)."""
    prompts = _module("swarm.supervisor.prompts")
    if prompts is None:
        raise RuntimeError("no prompt renderer: pass prompt=")
    return prompts.render(board, cfg, led, action, launch)


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


BOARD_OUTCOMES = ("completed", "timeout", "max_turns", "failed", "cancelled")


def _board_outcome(row) -> str:
    """What the driver takes from an ended board row it has no local record of: a plain end label.
    `lost` and `outage` are the driver's own words (and what a launch costs is never read from the board:
    the intent keeps its reservation), so anything else becomes `failed`."""
    return row.outcome if row.outcome in BOARD_OUTCOMES else "failed"


def _reap(cfg, board) -> None:
    from swarm.supervisor import runner
    try:
        runner.reap(cfg, board)
    except Exception as exc:
        log(f"reap failed: {type(exc).__name__}")


def _rows_by_launch(board, job: str) -> dict:
    """launch_id -> every restart row that carries it (one, unless the board was tampered with)."""
    out: dict = {}
    for r in board.restarts(job=job):
        if r.launch_id:
            out.setdefault(r.launch_id, []).append(r)
    return out


_AMBIGUOUS = object()


def _row_for(led, intent, rows: dict, cfg, here: str):
    """The restart row an intent owns: the one whose id the driver recorded when it claimed (or whose id
    the local run file carries); a row that only repeats the launch_id is a forgery and is ignored. With
    no id recorded anywhere (a crash right after the claim) a sole row is adopted; several are ambiguous."""
    cands = rows.get(intent.launch_id, [])
    rid = led.watermarks.get(f"rid:{intent.launch_id}")
    if rid is None:
        local = _local_run(cfg, here, intent.launch_id)
        rid = local[0] if local else None
    if rid is None:
        if len(cands) > 1:
            log(f"job {led.job}: {len(cands)} restart rows carry launch_id {intent.launch_id[:12]}; held")
            return _AMBIGUOUS
        return cands[0] if cands else None
    for r in cands:
        if r.id != rid:
            log(f"job {led.job}: restart row {r.id} repeats launch_id {intent.launch_id[:12]} of row {rid}; ignored")
    return next((r for r in cands if r.id == rid), None)


def _matched_rows(led, rows: dict, cfg, here: str) -> dict:
    out = {}
    for i in led.intents:
        r = _row_for(led, i, rows, cfg, here)
        if r is not None and r is not _AMBIGUOUS:
            out[i.launch_id] = r
    return out


def _live_evidence(board_key: str, row_id: int | None, run: dict | None) -> bool:
    """Local evidence that a launch is alive, whatever the board says (F7): its run lock is held, or
    its scope unit (else its process group) is present or cannot be told to be gone."""
    from swarm.supervisor import runner
    if row_id is not None and runner._held(row_id, board_key):
        return True
    if not isinstance(run, dict):
        return False
    if run.get("unit"):
        return runner.unit_presence(run["unit"], run.get("slice")) != "absent"
    # no scope ([T21], no user manager): its process group, while the run has not recorded its end
    return not run.get("exited") and runner.group_presence(run) != "absent"


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
        log(f"job {led.job}: restart row {rows[lid][0].id} has launch_id {lid[:12]} of no intent; ignored")
    for intent in list(led.intents):
        if time.monotonic() >= deadline:
            return
        row = _row_for(led, intent, rows, cfg, here)
        if row is _AMBIGUOUS:
            continue
        if intent.state == "concluded":
            if (row is not None and row.ended_at is None and row.host == compat.node()
                    and not runner.has_run(cfg, row.id) and not _live_evidence(here, row.id, None)):
                board.finish_restart(row.id, "cancelled")        # a claim that committed late: nobody runs it
                board.release_drive_locks(intent.launch_id, job=led.job, holder=intent.action_id)
            continue
        if row is None:
            local = _local_run(cfg, here, intent.launch_id)       # a deleted board row never restarts a live run
            if local is not None and _live_evidence(here, local[0], local[1]):
                continue
            if _intent_age(intent.launch_id, now) >= LAUNCH_GRACE:
                _conclude(led, intent.launch_id, "interrupted by driver restart", False, 0.0)
                if intent.launch:
                    _idle_launch(board, led, intent.launch, "interrupted by driver restart", now)
            continue
        if intent.state == "claiming":
            _set_state(led, intent.launch_id, "claimed")
        if row.ended_at is not None:
            if runner.has_run(cfg, row.id) or _live_evidence(here, row.id, None):
                continue                                          # alive, or its finish is under way: it concludes
            outcome = _board_outcome(row)
            if led.watermarks.get(f"driver-restart:{intent.launch_id}") and outcome != "completed":
                outcome = "interrupted by driver restart"
            _conclude(led, intent.launch_id, outcome, outcome == "completed", float(intent.reserved_usd))
            continue
        if row.host != compat.node():
            continue
        if runner.has_run(cfg, row.id) or _live_evidence(here, row.id, None):
            continue                                              # live, or the runner's reap finishes it
        if _launch_age(row, now) < LAUNCH_GRACE:
            continue
        board.finish_restart(row.id, "cancelled")                 # driver died before starting
        try:
            markers.remove_resume_marker(markers.resume_marker_path(cfg, led.job, row.id))
        except Exception as exc:
            log(f"launch {intent.launch_id}: marker cleanup failed: {type(exc).__name__}")
        board.release_drive_locks(intent.launch_id, job=led.job, holder=intent.action_id)
        _conclude(led, intent.launch_id, "interrupted by driver restart", False, 0.0)
    _settle_launches(board, cfg, led, _matched_rows(led, _rows_by_launch(board, led.job), cfg, here), now)
    sweep_unlaunched_launches(board, led.job)
    _settle_event_acks(board, led)
    _sweep_locks(board, cfg, now)


def _settle_event_acks(board, led) -> None:
    """Keep the ledger's `evack:` keys bounded: acknowledge on the board the events a session answered (so
    they stop being pending), and drop the key of every event that is no longer pending (acknowledged,
    expired or gone): nothing can wake a launch on it any more."""
    keys = [k for k in led.watermarks if k.startswith(EVACK)]
    if not keys:
        return
    pending = {e.id for e in board.pending_events(led.job)}
    answered = sorted({int(k.rsplit(":", 1)[1]) for k in keys} & pending)
    if answered:
        board.ack_events(led.job, answered, "swarm driver")
    stale = [k for k in keys if int(k.rsplit(":", 1)[1]) not in pending or int(k.rsplit(":", 1)[1]) in answered]
    if stale:
        mutate(led, lambda l: [l.watermarks.pop(k, None) for k in stale])


def _sweep_locks(board, cfg: dict, now) -> None:
    """The host's drive-lock sweep with writer fencing (L5): Task 8b's `driver_life.sweep_locks_by_host`.
    Called unguarded when the module is part of the build: a failure is the pass's to report."""
    life = _module("swarm.supervisor.driver_life")
    if life is not None:
        from swarm.board.autoinit import store_key
        extra = {"board_key": store_key(cfg)} if "board_key" in inspect.signature(life.sweep_locks_by_host).parameters else {}
        life.sweep_locks_by_host(board, cfg, now, **extra)      # 8b's sweep matches runs by local evidence in this board's key


def sweep_unlaunched_launches(board, job: str) -> int:
    """End any launch left `running` with no launch (a dev board from before launch ids): it becomes
    `idle` with last_outcome "no launch". The number ended."""
    n = 0
    for launch in board.launches(job):
        if launch.state != "running" or launch.launch_id is not None:
            continue
        try:
            if board.set_launch_state(launch.id, launch.revision, "done", job=job, last_outcome="no launch") is not None:
                n += 1
        except ValueError as exc:
            log(f"launch {launch.name} of {job}: not ended: {exc}")
    return n


def _finish_activation(board, cfg: dict, led) -> None:
    """Recover a crashed activation: a ledger still "activating" gets Task 6a's idempotent
    finish_activation (board row, lead launch, ledger "active"). Unguarded: an error (no owner
    record: DRIVER-ORPHAN) is the pass's to report, and the job stays undriven (execute holds an
    "activating" ledger)."""
    if led.state != "activating":
        return
    activate = _module("swarm.drive_activate")
    if activate is None:
        return
    done = activate.finish_activation(cfg, board, led.job)
    if done is not None:
        led.__dict__.update(done.__dict__)


def _settle_launches(board, cfg: dict, led, rows: dict, now) -> None:
    """Settle ended launches; driver interruptions and missing verdicts are recoverable."""
    for launch in board.launches(led.job, led.generation):
        if launch.launch_id is None or (launch.state != "running" and not (
                driveroles.is_judge_role(launch.role) and launch.state in ("done", "failed"))):
            continue
        if launch.state != "running" and led.watermarks.get(f"verdict-checked:{launch.launch_id}"):
            continue
        row = rows.get(launch.launch_id)
        if row is None or row.ended_at is None:
            continue
        fields = {}
        if row.new_agent_key and valid_resume_handle(row.new_agent_key):
            fields.update(agent_key=row.new_agent_key, session_id=row.new_agent_key)
        intent = next((i for i in led.intents if i.launch_id == launch.launch_id), None)
        outcome = intent.outcome if intent and intent.outcome in UNCOUNTED else row.outcome or "failed"
        if led.halted is not None and led.watermarks.get(f"hopen:{launch.launch_id}"):
            outcome = "halted"
        local = _local_run(cfg, led.board_key, launch.launch_id)
        reason = local[1].get("refused_reason") if local else None
        if reason and outcome != "completed":
            outcome = f"{outcome}: {reason}"
        # A successful process exit still needs the role's hand-off. Roles control expectations,
        # not filesystem rights. The learner's completion is recorded by the learning pipeline.
        if outcome == "completed" and launch.role != "learner":
            names = {a.name for a in board.agents(led.job)
                     if a.agent_key in (row.new_agent_key, launch.agent_key, row.old_agent_key)}
            prefix = ("VERDICT" if driveroles.is_judge_role(launch.role) else
                      "REVIEW " if driveroles.is_read_only_role(launch.role) else
                      "GOAL-READY " if launch.role == driveroles.LEAD_ROLE else ("DONE ", "LAND-REQUEST ") if launch.role == "integrator" else "DONE ")
            handed_off = any(m.agent_name in names and m.message.startswith(prefix)
                             and row.at <= m.created_at <= row.ended_at
                             for m in board.recent_messages(200, job=led.job))
            if launch.role == driveroles.LEAD_ROLE:
                handed_off |= any(s.queued_by in names and s.updated_at >= row.at
                                  for s in board.launches(led.job, led.generation))
            if not handed_off:
                outcome = "no hand-off"
        if driveroles.is_judge_role(launch.role) and outcome not in UNCOUNTED and outcome != "no hand-off":
            names = {a.name for a in board.agents(led.job)
                     if a.agent_key in (row.new_agent_key, launch.agent_key, row.old_agent_key)}
            if not any(m.agent_name in names and m.message.startswith("VERDICT")
                       and row.at <= m.created_at <= row.ended_at
                       for m in board.recent_messages(200, job=led.job)):
                outcome = "no hand-off"
        if outcome == "no hand-off":
            run = local[1] if local else {}
            owner, token = run.get("driver_pid"), run.get("driver_token")
            interrupted = led.watermarks.get(f"driver-restart:{launch.launch_id}") or (
                owner is not None and token is not None and procwatch.start_token(owner) != token)
            if interrupted:
                outcome = "interrupted by driver restart"
                def mark_interrupted(l):
                    for i in l.intents:
                        if i.launch_id == launch.launch_id:
                            i.outcome, i.success = outcome, False
                mutate(led, mark_interrupted)
        if driveroles.is_judge_role(launch.role) and outcome == "completed":
            mutate(led, lambda l: l.watermarks.__setitem__(f"verdict-checked:{launch.launch_id}", 1))
        _idle_launch(board, led, launch.name, outcome, now, **fields)


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
    # Only this generation's verdicts count: a rollover (or a reopen) clears the watermarks, and replaying every
    # earlier run's not_met / unreachable verdicts as new rounds made "no-progress" fire on the new run's first
    # pass, so a ruling judge ended it before any launch started (2026-10-15).
    judged = [v for v in list(snap.verdicts.values()) + ([snap.goal_verdict] if snap.goal_verdict else [])
              if v.receipt_id is not None and v.receipt_id > wm.get("p.verdict", 0)
              and getattr(v, "generation", led.generation) == led.generation]
    judged.sort(key=lambda v: v.receipt_id)
    if handoff_id <= wm.get("p.handoff", 0) and landed <= wm.get("p.landed", 0) and not judged:
        return False
    # the time of the progress events themselves (new hand-offs, judged rounds), never later than now: launch
    # row churn is not progress, and a forged future time cannot hold the stall wake off
    fired = [h.at for h in snap.handoffs if h.receipt_id is not None and h.receipt_id > wm.get("p.handoff", 0)]
    fired += [v.at for v in judged]
    fired = [t for t in fired if t is not None]
    stamp = _iso(min(max(fired), now) if fired else now)
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
        if plan["moved"] and (l.last_progress_at is None or stamp > l.last_progress_at):
            l.last_progress_at = stamp          # never backwards: a late-receipted older event moves nothing
    mutate(led, apply)
    return True
