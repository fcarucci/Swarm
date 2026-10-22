"""`swarm pass`: one stateless sweep over the open jobs. Its job is that no job halts.

It keeps no state of its own. Everything it decides is read from the board: the job rows, their
open questions (blockers of kind "question"), the agents, the DONE hand-offs, the verdicts, the launch
queue (`swarm run`) and the restart rows that started the launches. A pass changes nothing it has
already done, so it can run as often as the hooks call it.

Per open job, in this order:
  1. the job's queued launches start (concurrency and max_cost_usd only);
  2. an open question: the job waits on it (an expired one fails the job);
  3. a DONE hand-off with a verdict: met completes the job, unreachable fails it;
  4. a launch running or queued, or an agent working: nothing more to start;
  5. a DONE hand-off not yet judged: one judge launch for that artifact (never again for it);
     not_met: the judge's next steps are posted and the lead is re-queued once for that artifact;
  6. no hand-off: the lead is re-queued; after LEAD_ATTEMPTS attempts its brief asks it to replan.

A failed job notifies its owner on the board, again every NOTICE_MINUTES, until `swarm ack`.
"""
from __future__ import annotations

import datetime as _dt
import errno
import hashlib
import json
import math
import os
import re
import time
import uuid

from swarm import paths

LEAD_ATTEMPTS = 3                 # after this many attempts, ask the lead to replan
NOTICE_MINUTES = 30               # a failed job's notice is repeated this often until acked
LAUNCH_USD = 2.0                  # what one launch reserves of the job's max_cost_usd
DEFAULT_MAX_COST_USD = 200.0      # the job's max_cost_usd when neither the job nor [pass] sets one
MAX_JOB_RUNNING = 4               # resource capacity; queued work remains pending when full
THROTTLE_SECONDS = 60             # the hooks run a pass at most this often per host
PASS_NAME = "swarm pass"
BRIEF_MAX = 64 * 1024


def _key(artifact: str) -> str:
    """A launch-name-safe key of an artifact (one judge, one re-lead per artifact)."""
    return hashlib.sha256(artifact.encode("utf-8")).hexdigest()[:12]


def handoff(board, job: str):
    """(hand-off, artifact) of the job's latest DONE hand-off, or None. review.latest_handoffs reads every
    form a hand-off takes on the board (`DONE main@sha | summary`, `DONE "artifact"`, `swarm done`)."""
    from swarm.review import latest_handoffs
    from swarm.board.base import run_start
    js = board.job_status(job)
    found = [h for h in latest_handoffs(board, job) if js is None or h.created_at >= run_start(js)]
    if not found:
        return None
    last = found[-1]
    return last, last.artifact


def max_cost_usd(board, cfg: dict, job: str) -> float:
    """The job's max_cost_usd (job data), else [pass] max_cost_usd, else DEFAULT_MAX_COST_USD."""
    raw = board.job_data(job).get("max_cost_usd")
    if raw is None:
        raw = ((cfg or {}).get("pass") or {}).get("max_cost_usd", DEFAULT_MAX_COST_USD)
    value = float(raw)
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"max_cost_usd of {job} must be a positive number of USD, not {raw!r}")
    return value


def spent_usd(board, job: str) -> float:
    """What the job's launches have cost: the reported cost, else what was reserved for them."""
    total = 0.0
    for r in board.restarts(job):
        if r.launch_id is None:
            continue
        amount = r.cost_usd if r.cost_usd is not None else (r.reserved_usd or 0.0)
        total += float(amount)
    return total


def session_usd_cap(cfg: dict) -> float:
    """Owner-configured native launch reservation; job admission still enforces its cap."""
    raw = ((cfg or {}).get("pass") or {}).get("session_usd_cap", LAUNCH_USD)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise ValueError("[pass] session_usd_cap must be a positive finite number of USD") from None
    if isinstance(raw, bool) or not math.isfinite(value) or value <= 0:
        raise ValueError("[pass] session_usd_cap must be a positive finite number of USD")
    return value


STALE_RESTART_GRACE_MINUTES = 30   # past a launch's own time limit: its restart is an orphan (the runner died)


def _settle(board, job: str, cfg: dict | None = None, now: _dt.datetime | None = None) -> set[str]:
    """A running launch whose restart has ended takes the restart's outcome: done when completed,
    else failed. A launch whose restart is still open stays running, unless the restart is older
    than the launch time limit plus a grace: then its runner is gone (a crashed or replaced
    supervisor) and the restart is ended failed here, so the job is never stuck on it."""
    from swarm.supervisor.settings import settings
    now = now or board.now()
    default_cap = float(settings(cfg)["max_minutes"])
    departed = {a.agent_key: a for a in board.agents(job, include_departed=True) if a.ended_at is not None}
    for r in board.restarts(job):
        if not r.launch_id or r.ended_at is not None or r.at is None:
            continue
        gone = departed.get(r.new_agent_key) if r.new_agent_key else None
        if gone is not None:   # its session already left the board: the runner never recorded the end
            board.finish_restart(r.id, "completed" if gone.status == "completed" else "failed")
            continue
        cap = float(r.minutes_cap or 0) or default_cap
        if now - r.at > _dt.timedelta(minutes=cap + STALE_RESTART_GRACE_MINUTES):
            board.finish_restart(r.id, "failed")
    ended = {r.launch_id: r for r in board.restarts(job) if r.launch_id and r.ended_at is not None}
    settled_failed = {}
    from swarm.dependencies import (evidence, interrupted_pending_outcome,
                                    interrupted_pending_revision, spec)
    agents = {a.agent_key: a for a in board.agents(job, include_departed=True)}

    def interrupted(row, restart):
        agent = agents.get(restart.new_agent_key or restart.old_agent_key)
        turn_ended = getattr(agent, "turn_ended_at", None) if agent is not None else None
        return (spec(row) is not None and agent is not None
                and (agent.ended_at is not None or turn_ended is not None)
                and not (agent.status in ("started", "running") and turn_ended is None)
                and evidence(board, row) is None)

    # A question can stop _advance after settlement. Keep only exact interrupted task launches
    # eligible across that return; ordinary failures and historical launch rows are not retried.
    for row in board.launches(job):
        restart = ended.get(row.launch_id)
        pending_revision = interrupted_pending_revision(row.last_outcome)
        if (row.state == "failed" and pending_revision == row.revision
                and row.launch_id and restart is not None and restart.outcome == "failed"
                and interrupted(row, restart)):
            settled_failed[row.launch_id] = row.revision
    for row in board.launches(job, include_closed=False):
        if row.state != "running" or row.launch_id not in ended:
            continue
        restart = ended[row.launch_id]
        outcome = restart.outcome or "failed"
        state = "done" if outcome == "completed" else "failed"
        retry_candidate = state == "failed" and outcome == "failed" and interrupted(row, restart)
        last_outcome = interrupted_pending_outcome(row.revision + 1) if retry_candidate else outcome
        updated = board.set_launch_state(row.id, row.revision, state, job=job, last_outcome=last_outcome)
        if updated is not None and retry_candidate:
            settled_failed[row.launch_id] = updated.revision
    return settled_failed


OBSERVER_ROLES = ("owner", "orchestrator", "requester")


def _observer(agent) -> bool:
    """A person's or orchestrator's own board identity: it posts and answers, it never works the job."""
    return (getattr(agent, "role", None) or "") in OBSERVER_ROLES


def managed(board, job: str) -> bool:
    """Whether the pass manages the job: it has a work dir of its own or launches. Any other open job
    (a plain one, never launched) is left alone: the pass would otherwise fail it for lacking a work dir."""
    return bool(board.job_data(job).get("workdir")) or bool(board.launches(job))


def _running(board, job: str) -> bool:
    """Whether a launch of the job has an open restart (its session is still running)."""
    open_ids = {r.launch_id for r in board.restarts(job) if r.launch_id and r.ended_at is None}
    return any(row.state == "running" and row.launch_id in open_ids for row in board.launches(job))


def notice(job: str, reason: str) -> str:
    return f"NOTICE {job} failed: {reason}. Owner: swarm ack --job {job} stops these notices."[:200]


def _fail(board, job: str, reason: str, now: _dt.datetime) -> str:
    board.post(job, PASS_NAME, notice(job, reason))
    board.close_job(job, "failed", reason, closed_by=PASS_NAME)
    board.set_job_data(job, "pass_notified", now.isoformat())
    return f"{job}: failed: {reason}"


def _store_key(cfg: dict) -> str:
    from swarm.board.autoinit import store_key
    return store_key(cfg)


def _start_one(board, cfg: dict, job: str, row, reserve: float, starter) -> str | None:
    """Claim one queued launch and start its runner. None when the claim is refused (a cap or a
    race): the next pass tries again."""
    from swarm.supervisor import launch, markers
    from swarm.supervisor.settings import settings
    from swarm import late_review
    js = board.job_status(job)
    review = bool(js and js.status != 'active' and late_review.admitted(board, cfg, job, row))
    if js is None or (js.status != 'active' and not review):
        return None
    minutes = float(settings(cfg)["max_minutes"])
    launch_id = f"{row.name}-{uuid.uuid4().hex[:8]}"
    # The session enrols as the replacement of a departed predecessor (hooks._enrol_resumed ->
    # claim_resume): give it one, under the launch's role, closed before the claim.
    old = f"launch:{launch_id}"
    name = board.allocate_name(old, job, row.role)   # the session takes this name over (claim_resume)
    if row.role == "judge":
        board.take_judge_launch(old, job)   # its verdict is recorded under the session's own name
    board.close_agent(old, "drive predecessor")
    r = board.claim_drive_launch(job=job, generation=row.generation, action_id=f"pass|{row.generation}|{row.id}|{row.name}|{row.revision}",
                                 attempt=row.attempts + 1, launch_id=launch_id, launch_row_id=row.id,
                                 queue_version=row.revision, harness=row.harness, reserve_usd=reserve,
                                 reserve_minutes=minutes, locks=[], lock_seconds=3600,
                                 max_job_running=MAX_JOB_RUNNING, max_cost_usd=max_cost_usd(board, cfg, job))
    if r is None:
        return None
    try:
        model = launch.replacement_model(cfg, row.harness, row.role, row.model)
        workdir = os.path.realpath(row.workdir)
        from swarm.dependencies import prompt, spec as task_spec
        from swarm.driveroles import is_read_only_role
        read_only = is_read_only_role(row.role)
        spec = launch.spec_for(cfg, row.harness, prompt=prompt(row), workdir=workdir, model=model,
                               minutes=minutes, budget_usd=reserve, drive=True, read_only=read_only)
        marker = markers.write_resume_marker(cfg, job, r.id, resume_of=old, name=name,
                                             harness=row.harness, session_id=spec.session_id)
        if spec.session_id:
            board.set_restart_agent(r.id, spec.session_id)
        run = {"restart_id": r.id, "job": job, "name": name, "harness": row.harness,
               "resume_of": old, "marker": str(marker), "argv": list(spec.argv), "cwd": spec.cwd,
               "stdin": spec.stdin, "session_id": spec.session_id, "limit_seconds": minutes * 60,
               "enrol_seconds": float(settings(cfg)["enrol_minutes"]) * 60, "config": None,
               "board": _store_key(cfg), "launch_id": launch_id, "launch": row.name, "read_only": read_only,
               "managed_task": task_spec(row) is not None}
        if row.harness == "codex":
            # The same flat proxy rate used by spec_for to size this launch's rollout allowance.
            run["codex_usd_per_mtok"] = float((cfg.get("drive") or {}).get("codex_usd_per_mtok", 10))
        if review:
            late_review.bind_start(board, cfg, row, r, run)
        current_job = board.job_status(job)
        current_row = board.get_launch(job, row.generation, row.name)
        if current_job is None or (current_job.status != "active" and not
                (review and late_review.admitted(board, cfg, job, current_row, run))):
            board.finish_restart(r.id, "cancelled")
            current_row = board.get_launch(job, row.generation, row.name)
            if current_row is not None and current_row.state == "running":
                board.set_launch_state(current_row.id, current_row.revision, "failed", job=job,
                                       last_outcome="job closed before runner start")
            return f"{job}: {row.name} cancelled before runner start"
        starter(cfg, run)
    except Exception as exc:   # the start failed: the restart and the launch end failed; the job goes on
        board.finish_restart(r.id, "failed")
        cur = board.get_launch(job, row.generation, row.name)
        if cur is not None and cur.state == "running":
            board.set_launch_state(cur.id, cur.revision, "failed", job=job, last_outcome=type(exc).__name__)
        return f"{job}: {row.name} failed to start ({type(exc).__name__}: {exc})"
    return f"{job}: started {row.name} as restart {r.id}"


def _start_queued(board, cfg: dict, job: str, starter) -> tuple[list[str], str | None]:
    """Start every eligible queued launch without letting a configured cost estimate strand work."""
    from swarm.supervisor import command
    acts = []
    js = board.job_status(job)
    if js is None or js.status != "active":
        return acts, None
    cap = max_cost_usd(board, cfg, job)
    per_session = session_usd_cap(cfg)
    for row in sorted((x for x in board.launches(job, include_closed=False) if x.state == "queued"),
                      key=lambda x: x.id):
        from swarm.dependencies import block_reason
        if block_reason(board, row, launching=True):
            continue
        spent = spent_usd(board, job)
        reserve = min(per_session, cap - spent)
        if reserve <= 0:
            return acts, f"cost capacity exhausted: ${spent:.2f} of configured max_cost_usd ${cap:g}"
        why = command.workdir_problem(cfg, row.workdir)
        if why:
            board.set_launch_state(row.id, row.revision, "failed", job=job, last_outcome="refused")
            acts.append(f"{job}: {row.name} refused: {why}")
            continue
        done = _start_one(board, cfg, job, row, reserve, starter)
        if done:
            acts.append(done)
    return acts, None


OWNER_INPUT = ("HOST-RESULT", "OWNER", "PUSH-RESULT")   # posts that hand a lead new facts to act on


def _attempts(board, job: str, since: _dt.datetime) -> int:
    """Lead and judge launches that were queued or ended since `since`, or since the newest owner input
    (a HOST-RESULT or OWNER post): a lead that hands work to the owner and exits is progressing, not stuck."""
    from swarm.board.base import run_start
    from swarm.board.launches import SUPERSEDED
    js = board.job_status(job)
    if js is not None:
        since = max(since, run_start(js))
    for m in reversed(board.recent_messages(200, job)):
        if m.message.startswith(OWNER_INPUT):
            since = max(since, m.created_at)
            break
    starts = {r.launch_id: r.at for r in board.restarts(job) if r.launch_id}
    return sum(1 for row in board.launches(job) if row.role in ("lead", "engineering_lead", "judge")
               and row.last_outcome != SUPERSEDED and (starts.get(row.launch_id) or row.updated_at) >= since)


def _run_launch(board, js, name):
    """A historical judge/relead name cannot consume the current run's hand-off."""
    from swarm.board.base import run_start
    from swarm.board.launches import SUPERSEDED
    row = board.get_launch(js.job, 1, name)
    if row is None or row.last_outcome == SUPERSEDED or row.updated_at < run_start(js):
        return None
    if row.launch_id and row.state != "queued":
        previous = next((r for r in board.restarts(js.job) if r.launch_id == row.launch_id), None)
        if previous is not None and previous.at < run_start(js):
            return None
    return row


def _next_relead_name(board, job: str, artifact_key: str) -> str:
    """Return the live re-lead for this artifact, or allocate the next durable name."""
    prefix = f"relead-{artifact_key}"
    matching = [r for r in board.launches(job) if r.role == "lead"
                and (r.name == prefix or r.name.startswith(prefix + "-"))]
    for row in reversed(matching):
        if row.state not in ("done", "failed", "cancelled"):
            return row.name
    names = {r.name for r in matching}
    if prefix not in names:
        return prefix
    suffix = 2
    while f"{prefix}-{suffix}" in names:
        suffix += 1
    return f"{prefix}-{suffix}"


def _queue(board, job: str, name: str, role: str, brief: str, workdir: str, cfg: dict | None = None) -> None:
    from swarm import models
    from swarm.dependencies import spec
    stages = [row for row in board.launches(job) if spec(row) is not None]
    harness = stages[-1].harness if stages else "claude"
    if stages and role == "lead":
        role = "engineering_lead"
    model = models.model_for(cfg or {}, harness, role)
    board.queue_launch(job, 1, name, role=role, harness=harness, model=model, workdir=workdir,
                       brief=brief[:BRIEF_MAX], queued_by=PASS_NAME)


def _workdir(board, job: str) -> str | None:
    """The work dir the job's launches use: the job's own (`swarm run` records it), else the latest launch's."""
    own = board.job_data(job).get("workdir")
    if own:
        return own
    rows = board.launches(job)
    return rows[-1].workdir if rows else None


def _lead_brief(js, steps: str) -> str:
    return (f"Goal of job {js.job}: {js.goal or '(none set)'}\n"
            f"Hand off with: swarm done --job {js.job} --artifact <branch@sha> --summary \"...\"\n"
            f"Last judge notes: {steps or 'none yet'}")


def _judge_brief(js, artifact: str, *, goal: bool = False) -> str:
    scope = f" --scope goal --candidate {artifact}" if goal else ""
    return (f"You are the judge of job {js.job}. Goal: {js.goal or '(none set)'}\n"
            f"Judge the hand-off {artifact}. Record it with swarm verdict --job {js.job} "
            f"met|not_met|unreachable{scope}, with --candidate and --reason (and --next for not_met). "
            f"A not_met verdict starts the next lead run itself, with your --next as its brief: make --next "
            f"the exact list of what to change and how it will be re-checked. Record the verdict, then stop.")


EMPTY_MINUTES = 30   # an open job nobody works on, with nothing queued and no question, ends failed after this


def _empty(board, js, now: _dt.datetime) -> str | None:
    """Why an open job the pass does not launch for (an orchestrator's own job) must end: no agent started,
    running or idle, no open question, and no board activity for EMPTY_MINUTES. None while it is in use."""
    from swarm.board.base import quiet_start
    if not js.goal:   # a job without a goal has the board's own auto-close and orphan rules
        return None
    if js.started or js.running or js.idle:
        return None
    if any(b.kind == "question" for b in board.blockers(js.job)):
        return None
    quiet = now - quiet_start(js, now=now)
    if quiet < _dt.timedelta(minutes=EMPTY_MINUTES):
        return None
    return f"no agent working and nothing queued for {int(quiet.total_seconds() // 60)} min"


def _advance(board, cfg: dict, js, now: _dt.datetime, starter) -> list[str]:
    job = js.job
    if not managed(board, job):
        why = _empty(board, js, now)   # a job is active only while someone works on it
        return [_fail(board, job, why, now)] if why else []
    # A task report plus its assigned actor's ended turn is sufficient to settle the task.
    # Reconcile it before starting dependents; runner/session completion remains audit data.
    from swarm.dependencies import reconcile_external
    reconcile_external(board, job)
    settled_failed = _settle(board, job, cfg, now)
    acts, cost_reason = _start_queued(board, cfg, job, starter)
    if cost_reason:
        board.post(job, PASS_NAME, notice(job, cost_reason))
        return acts
    questions = [b for b in board.blockers(job) if b.kind == "question"]
    expired = [b for b in questions if b.until is not None and b.until <= now]
    if expired:
        return acts + [_fail(board, job, f"Q{expired[0].id} expired unanswered: {expired[0].reason}", now)]
    if questions:
        return acts   # waits on a person's answer: the only wait there is
    from swarm.dependencies import advance, describe, reconcile_interrupted_turns, spec, supersession
    reconcile_interrupted_turns(board, job, settled_launch_revisions=settled_failed)
    task_rows = describe(board, job)
    task_acts = advance(board, job)
    if task_acts is not None:
        return acts + task_acts
    found = handoff(board, job)
    msg = artifact = judged = None
    if found is not None:
        msg, artifact = found
        if js.verdict is not None and (js.verdict_artifact == artifact or (
                js.verdict_artifact is None and js.verdict_at is not None and js.verdict_at >= msg.created_at)):
            judged = js.verdict
    if task_rows and (js.verdict_scope != "goal" or js.verdict_candidate != artifact
                      or msg is None or js.verdict_at is None or js.verdict_at < msg.created_at):
        judged = None
    if judged == "met":   # a verdict settles the job, whatever is still running
        board.close_job(job, "completed", f"met: {artifact}", closed_by=PASS_NAME)
        return acts + [f"{job}: completed ({artifact})"]
    if judged == "unreachable":
        return acts + [_fail(board, job, f"judge: unreachable ({js.verdict_reason or artifact})", now)]
    if _running(board, job) or any(r.state == "queued" and not (spec(r) is not None and supersession(board, r))
                                  for r in board.launches(job, include_closed=False)):
        return acts
    if any(a.status in ("started", "running") and not _observer(a) for a in board.agents(job, include_departed=False)):
        return acts
    workdir = _workdir(board, job)
    if workdir is None:
        return acts + [_fail(board, job, "no work dir is known for its launches (swarm run --workdir)", now)]
    if found is not None:
        key = _key(artifact)
        if judged == "not_met":
            steps = js.verdict_next or js.verdict_reason or "see the verdict"
            attempts = _attempts(board, job, msg.created_at)
            if attempts >= LEAD_ATTEMPTS:
                steps = (f"Replan after {attempts} lead attempts; do not repeat the same approach. "
                         f"Address the latest verdict: {steps}")
            name = _next_relead_name(board, job, key)
            if _run_launch(board, js, name) is not None:
                return acts
            board.post(job, PASS_NAME, f"NOT MET {artifact}: {steps}"[:200])
            _queue(board, job, name, "lead", _lead_brief(js, steps), workdir, cfg)
            return acts + [f"{job}: re-queued the lead for {artifact} (not met)"]
        if judged is None:   # one judge per artifact, never again
            judge_name = f"goal-judge-{key}" if task_rows else f"judge-{key}"
            if _run_launch(board, js, judge_name) is None:
                _queue(board, job, judge_name, "judge", _judge_brief(js, artifact, goal=bool(task_rows)), workdir, cfg)
                return acts + [f"{job}: judge queued for {artifact}"]
            return acts + [_fail(board, job, f"the judge of {artifact} ended with no verdict", now)]
        since = msg.created_at
    else:
        since = js.activated_at or js.created_at
    attempts = _attempts(board, job, since)
    name = f"lead-{len([r for r in board.launches(job) if r.role == 'lead']) + 1}"
    extra = js.verdict_next or js.verdict_reason or ""
    if attempts >= LEAD_ATTEMPTS:
        extra = (f"Replan after {attempts} lead attempts; do not repeat the same approach. "
                 f"Inspect current work and choose a concrete next step. {extra}")
        board.post(job, PASS_NAME, f"No hand-off after {attempts} lead attempts; re-planning the lead.")
    _queue(board, job, name, "lead", _lead_brief(js, extra), workdir, cfg)
    return acts + [f"{job}: queued {name}"]


def on_not_met(board, cfg: dict, job: str, *, now: _dt.datetime | None = None, starter=None,
               start: bool = True) -> str | None:
    """The judge's not_met starts the next run at once: called by `swarm verdict ... not_met` while the
    judge's own session is still open (the pass would wait for it to end). Queues the lead with the
    verdict's next steps (the same relead-<key> name the pass uses, so it is never queued twice) and
    starts it. Returns what it did, or None when the job is not one the pass launches for."""
    now = now or board.now()
    if not managed(board, job):
        return None
    js = board.job_status(job)
    if js is None or js.status != "active":
        return None
    found = handoff(board, job)
    artifact = js.verdict_artifact or (found[1] if found else "the job")
    key = _key(artifact)
    since = found[0].created_at if found else (js.activated_at or js.created_at)
    workdir = _workdir(board, job)
    if workdir is None:
        return _fail(board, job, "no work dir is known for its launches (swarm run --workdir)", now)
    name = _next_relead_name(board, job, key)
    row = _run_launch(board, js, name)
    if row is None or row.state in ("done", "failed", "cancelled"):
        steps = js.verdict_next or js.verdict_reason or "see the verdict"
        board.post(job, PASS_NAME, f"NOT MET {artifact}: {steps}"[:200])
        _queue(board, job, name, "lead", _lead_brief(js, steps), workdir, cfg)
    if not start:   # inside a launch: the next hook pass (within a minute) starts queued launches first
        return f"{job}: {name} queued"
    if starter is None:
        from swarm.supervisor import runner
        starter = runner.start
    acts, cost_reason = _start_queued(board, cfg, job, starter)
    if cost_reason:
        board.post(job, PASS_NAME, notice(job, cost_reason))
        return f"{job}: queued work is waiting for cost capacity"
    return "; ".join(acts) or f"{job}: {name} queued"


def _notices(board, now: _dt.datetime) -> list[str]:
    """Repeat the notice of every failed job not yet acked, once per NOTICE_MINUTES."""
    acts = []
    for js in board.jobs(include_closed=True):
        if js.status != "failed" or js.native_completed_at or js.started or js.running or not managed(board, js.job):
            continue
        data = board.job_data(js.job)
        if data.get("pass_acked"):
            continue
        last = data.get("pass_notified")
        if last and (now - _dt.datetime.fromisoformat(last)).total_seconds() < NOTICE_MINUTES * 60:
            continue
        board.post(js.job, PASS_NAME, notice(js.job, js.outcome or "failed"))
        board.set_job_data(js.job, "pass_notified", now.isoformat())
        acts.append(f"{js.job}: notice repeated")
    return acts


def _restore_hook_binding(board, cfg, job, js):
    """Restore a missing coordinator binding from this host's current private proof."""
    from swarm import enrolment, cli, safefs
    from swarm.board.autoinit import store_key
    rec = enrolment.find_job(store_key(cfg), job)
    if (rec is None or not rec.session_id or not cli.valid_session_id(rec.session_id)
            or rec.created_at < (js.activated_at or js.created_at).timestamp()
            or enrolment.job_routing_disabled(rec)):
        return None
    bindings = [cli._read_marker(p) for p in cli._job_markers(cfg, job)]
    if any(m.get('session_id') == rec.session_id and 'resume' not in m for m in bindings):
        return None
    marker = cli._marker(cfg, job).with_name(f'{cli.safe_job(job)}--{cli.safe_job(rec.session_id)}.json')
    data = {'job': job, 'session_id': rec.session_id, 'cwd': rec.cwd,
            'adopt_running': True, 'attached': True, 'recovered_run': (js.activated_at or js.created_at).isoformat(),
            **({'goal': True} if js.goal else {})}
    # Activation, teardown and reassignment share the coordinator lock. Publish complete
    # JSON atomically so an interrupted write leaves recovery retryable.
    with safefs.dir_fd(marker.parent, create=True) as directory:
        if safefs.exists(directory, marker.name):
            raise FileExistsError('conflicting recovery hook binding')
        safefs.write_atomic(directory, marker.name, json.dumps(data), durable=True)
    return marker, data


def _retire_recovered_routing(cfg, job, js):
    from swarm import cli
    run = (js.activated_at or js.created_at).isoformat() if js and js.status == 'active' else None
    for marker in cli._job_markers(cfg, job):
        data = cli._read_marker(marker)
        if 'recovered_run' in data and data['recovered_run'] != run:
            cli.remove_marker(marker, keep=lambda p: cli._read_marker(p) != data)


def repair_open_routing(board, cfg, job):
    """Normal reconciliation repairs lost local markers; never changes job/agent state."""
    from swarm import enrolment
    from swarm.board.autoinit import store_key
    if not cfg or 'hook' not in cfg:
        return False
    key = store_key(cfg)
    if enrolment.find_job(key, job) is None:
        return False
    with enrolment.coordinator_lock(key, job):
        js = board.job_status(job)
        _retire_recovered_routing(cfg, job, js)
        if js is None or js.status != 'active':
            return False
        created = _restore_hook_binding(board, cfg, job, js)
        current = board.job_status(job)
        if (current is None or current.status != 'active'
                or (current.activated_at or current.created_at) != (js.activated_at or js.created_at)):
            if created is not None:
                from swarm import cli
                marker, data = created
                cli.remove_marker(marker, keep=lambda p: cli._read_marker(p) != data)
            return False
        return created is not None


def recover_auto_close(board, cfg: dict, job: str) -> bool:
    """Recover the same run and routing without reviving agents or owner-closed jobs."""
    from swarm import enrolment, cli
    from swarm.board.autoinit import store_key
    from swarm.board.base import AUTO_CLOSED_BY
    from swarm.review import erroneous_auto_close
    if not cfg or 'hook' not in cfg:
        return board.recover_auto_close(job)
    observed = board.job_status(job)
    if (observed is None or observed.status not in ('completed', 'cancelled')
            or observed.closed_by != AUTO_CLOSED_BY):
        return False
    with enrolment.coordinator_lock(store_key(cfg), job):
        if not erroneous_auto_close(board, job):
            return False
        # Prepare before reopening: an I/O failure leaves recovery retryable.
        created = _restore_hook_binding(board, cfg, job, board.job_status(job))
        if board.recover_auto_close(job):
            return True
        if created is not None:
            marker, data = created
            cli.remove_marker(marker, keep=lambda p: cli._read_marker(p) != data)
        return False


def run_job_pass(board, cfg: dict, job: str, *, now: _dt.datetime | None = None, starter=None) -> list[str]:
    """Advance one active job under a stable per-job lock.

    Terminal hooks can race the ordinary host pass, so both paths use this lock
    around the check-and-queue transition. Different jobs retain independent locks.
    """
    import hashlib

    d = paths.state_dir()
    d.mkdir(parents=True, exist_ok=True)
    lock_path = d / f"pass-job-{hashlib.sha256(job.encode('utf-8')).hexdigest()}.lock"
    try:
        import fcntl
    except ImportError:  # Windows
        fcntl = None
    if fcntl is None:
        try:
            import msvcrt
        except ImportError as exc:
            raise RuntimeError("no supported per-job pass lock on this platform") from exc

    with open(lock_path, "a+b") as lock:
        if fcntl is not None:
            fcntl.flock(lock, fcntl.LOCK_EX)
        else:
            lock.seek(0, 2)
            if lock.tell() == 0:
                lock.write(b"\\0")
                lock.flush()
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
        try:
            js = board.job_status(job)
            if js is None or js.status != "active":
                return []
            if starter is None:
                from swarm.supervisor import runner
                starter = runner.start
            return _advance(board, cfg, js, now or board.now(), starter)
        finally:
            if fcntl is not None:
                fcntl.flock(lock, fcntl.LOCK_UN)
            else:
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)


def run_pass(board, cfg: dict, *, now: _dt.datetime | None = None, starter=None) -> list[str]:
    """One pass over every open job. Returns what it did (empty when nothing needed doing)."""
    if starter is None:
        from swarm.supervisor import runner
        starter = runner.start
    now = now or board.now()
    board.expire_blockers(now)
    acts = []
    for js in board.jobs(include_closed=True):
        try:
            if recover_auto_close(board, cfg, js.job):
                acts.append(f"{js.job}: restored unfinished automatically closed run")
        except Exception as exc:
            # A nonblocking coordinator lock held by another operation is retried next pass.
            if not isinstance(exc, BlockingIOError) or exc.errno not in (errno.EAGAIN, errno.EWOULDBLOCK):
                acts.append(f"{js.job}: pass error Recovery {type(exc).__name__}: {exc}")
    for job, failed_at, launch_id, revision in board.failed_launch_retries():
        if board.reopen_failed_job(job, failed_at, launch_id, revision):
            acts.append(f"{job}: new run for fresh queued work")
    for js in board.jobs(include_closed=True):
        try:
            from swarm import late_review
            late = late_review.reconcile(board, cfg, js.job, starter)
            if late is not None:
                acts += late
                continue
            if repair_open_routing(board, cfg, js.job):
                acts.append(f"{js.job}: restored trusted coordinator hook routing")
        except Exception as exc:
            # Routing can defer while independent board-native work still advances below.
            if not isinstance(exc, BlockingIOError) or exc.errno not in (errno.EAGAIN, errno.EWOULDBLOCK):
                acts.append(f"{js.job}: pass error Routing {type(exc).__name__}: {exc}")
        if js.status != 'active':
            continue
        try:
            acts += run_job_pass(board, cfg, js.job, now=now, starter=starter)
        except Exception as exc:   # one job's trouble never stops the others
            acts.append(f"{js.job}: pass error {type(exc).__name__}: {exc}")
    return acts + _notices(board, now)


def ack(board, job: str, now: _dt.datetime | None = None) -> bool:
    """`swarm ack`: the owner has seen a failed job's notice; its notices stop."""
    return board.set_job_data(job, "pass_acked", (now or board.now()).isoformat())


def due() -> bool:
    """Whether a pass is due on this host: the last one started THROTTLE_SECONDS ago or more. Cheap: the hooks
    check it before they connect to the board."""
    try:
        return time.time() - (paths.state_dir() / "pass.stamp").stat().st_mtime >= THROTTLE_SECONDS
    except FileNotFoundError:
        return True


def throttled(board, cfg: dict) -> list[str] | None:
    """The hooks' pass: at most one per THROTTLE_SECONDS per host, never two at once (a lock file).
    None when skipped."""
    if not due():
        return None
    d = paths.state_dir()
    d.mkdir(parents=True, exist_ok=True)
    stamp = d / "pass.stamp"
    try:
        import fcntl
    except ImportError:   # Windows: no flock; the stamp alone throttles
        fcntl = None
    with open(d / "pass.lock", "a+") as lock:
        if fcntl is not None:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return None
        try:
            stamp.touch()
            return run_pass(board, cfg)
        finally:
            if fcntl is not None:
                fcntl.flock(lock, fcntl.LOCK_UN)
