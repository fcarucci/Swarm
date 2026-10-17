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
  6. no hand-off: the lead is re-queued; after LEAD_ATTEMPTS launches with no new hand-off the job fails.

A failed job notifies its owner on the board, again every NOTICE_MINUTES, until `swarm ack`.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import math
import os
import re
import time
import uuid

from swarm import paths

LEAD_ATTEMPTS = 3                 # launches with no new hand-off before the job fails
NOTICE_MINUTES = 30               # a failed job's notice is repeated this often until acked
LAUNCH_USD = 2.0                  # what one launch reserves of the job's max_cost_usd
DEFAULT_MAX_COST_USD = 200.0      # the job's max_cost_usd when neither the job nor [pass] sets one
MAX_JOB_RUNNING = 4               # launches running at once per job
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
    found = latest_handoffs(board, job)
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


STALE_RESTART_GRACE_MINUTES = 30   # past a launch's own time limit: its restart is an orphan (the runner died)


def _settle(board, job: str, cfg: dict | None = None, now: _dt.datetime | None = None) -> None:
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
    for row in board.launches(job, include_closed=False):
        if row.state != "running" or row.launch_id not in ended:
            continue
        outcome = ended[row.launch_id].outcome or "failed"
        board.set_launch_state(row.id, row.revision, "done" if outcome == "completed" else "failed",
                               job=job, last_outcome=outcome)


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
    minutes = float(settings(cfg)["max_minutes"])
    launch_id = f"{row.name}-{uuid.uuid4().hex[:8]}"
    # The session enrols as the replacement of a departed predecessor (hooks._enrol_resumed ->
    # claim_resume): give it one, under the launch's role, closed before the claim.
    old = f"launch:{launch_id}"
    name = board.allocate_name(old, job, row.role)   # the session takes this name over (claim_resume)
    if row.role == "judge":
        board.take_judge_launch(old, job)   # its verdict is recorded under the session's own name
    board.close_agent(old, "drive predecessor")
    r = board.claim_drive_launch(job=job, generation=row.generation, action_id=f"pass|{row.name}|{row.revision}",
                                 attempt=row.attempts + 1, launch_id=launch_id, launch_row_id=row.id,
                                 queue_version=row.revision, harness=row.harness, reserve_usd=reserve,
                                 reserve_minutes=minutes, locks=[], lock_seconds=3600,
                                 max_job_running=MAX_JOB_RUNNING)
    if r is None:
        return None
    try:
        model = launch.replacement_model(cfg, row.harness, row.role, row.model)
        workdir = os.path.realpath(row.workdir)
        spec = launch.spec_for(cfg, row.harness, prompt=row.brief, workdir=workdir, model=model,
                               minutes=minutes, budget_usd=reserve, drive=True)
        marker = markers.write_resume_marker(cfg, job, r.id, resume_of=old, name=name,
                                             harness=row.harness, session_id=spec.session_id)
        if spec.session_id:
            board.set_restart_agent(r.id, spec.session_id)
        run = {"restart_id": r.id, "job": job, "name": name, "harness": row.harness,
               "resume_of": old, "marker": str(marker), "argv": list(spec.argv), "cwd": spec.cwd,
               "stdin": spec.stdin, "session_id": spec.session_id, "limit_seconds": minutes * 60,
               "enrol_seconds": float(settings(cfg)["enrol_minutes"]) * 60, "config": None,
               "board": _store_key(cfg), "launch_id": launch_id, "launch": row.name}
        starter(cfg, run)
    except Exception as exc:   # the start failed: the restart and the launch end failed; the job goes on
        board.finish_restart(r.id, "failed")
        cur = board.get_launch(job, row.generation, row.name)
        if cur is not None and cur.state == "running":
            board.set_launch_state(cur.id, cur.revision, "failed", job=job, last_outcome=type(exc).__name__)
        return f"{job}: {row.name} failed to start ({type(exc).__name__}: {exc})"
    return f"{job}: started {row.name} as restart {r.id}"


def _start_queued(board, cfg: dict, job: str, starter) -> tuple[list[str], str | None]:
    """Start the job's queued launches. (actions, the reason the job must fail: the cost cap is
    reached)."""
    from swarm.supervisor import command
    acts = []
    cap = max_cost_usd(board, cfg, job)
    for row in sorted((x for x in board.launches(job, include_closed=False) if x.state == "queued"),
                      key=lambda x: x.id):
        spent = spent_usd(board, job)
        reserve = min(LAUNCH_USD, cap - spent)
        if reserve <= 0:
            return acts, f"cost cap reached: ${spent:.2f} of max_cost_usd ${cap:g}"
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
    for m in reversed(board.recent_messages(200, job)):
        if m.message.startswith(OWNER_INPUT):
            since = max(since, m.created_at)
            break
    return sum(1 for row in board.launches(job) if row.role in ("lead", "judge") and row.updated_at >= since)


def _queue(board, job: str, name: str, role: str, brief: str, workdir: str) -> None:
    board.queue_launch(job, 1, name, role=role, harness="claude", model=None, workdir=workdir,
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


def _judge_brief(js, artifact: str) -> str:
    return (f"You are the judge of job {js.job}. Goal: {js.goal or '(none set)'}\n"
            f"Judge the hand-off {artifact}. Record it with swarm verdict --job {js.job} "
            f"met|not_met|unreachable, with --candidate and --reason (and --next for not_met). "
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
    _settle(board, job, cfg, now)
    acts, cost_reason = _start_queued(board, cfg, job, starter)
    if cost_reason:
        return acts + [_fail(board, job, cost_reason, now)]
    questions = [b for b in board.blockers(job) if b.kind == "question"]
    expired = [b for b in questions if b.until is not None and b.until <= now]
    if expired:
        return acts + [_fail(board, job, f"Q{expired[0].id} expired unanswered: {expired[0].reason}", now)]
    if questions:
        return acts   # waits on a person's answer: the only wait there is
    found = handoff(board, job)
    msg = artifact = judged = None
    if found is not None:
        msg, artifact = found
        if js.verdict is not None and (js.verdict_artifact == artifact or (
                js.verdict_artifact is None and js.verdict_at is not None and js.verdict_at >= msg.created_at)):
            judged = js.verdict
    if judged == "met":   # a verdict settles the job, whatever is still running
        board.close_job(job, "completed", f"met: {artifact}", closed_by=PASS_NAME)
        return acts + [f"{job}: completed ({artifact})"]
    if judged == "unreachable":
        return acts + [_fail(board, job, f"judge: unreachable ({js.verdict_reason or artifact})", now)]
    if _running(board, job) or any(r.state == "queued" for r in board.launches(job, include_closed=False)):
        return acts
    if any(a.status in ("started", "running") and not _observer(a) for a in board.agents(job, include_departed=False)):
        return acts
    workdir = _workdir(board, job)
    if workdir is None:
        return acts + [_fail(board, job, "no work dir is known for its launches (swarm run --workdir)", now)]
    if found is not None:
        key = _key(artifact)
        if judged == "not_met" and board.get_launch(job, 1, f"relead-{key}") is None:   # once per artifact
            if _attempts(board, job, msg.created_at) >= LEAD_ATTEMPTS:
                return acts + [_fail(board, job, f"no met verdict after {LEAD_ATTEMPTS} launches", now)]
            steps = js.verdict_next or js.verdict_reason or "see the verdict"
            board.post(job, PASS_NAME, f"NOT MET {artifact}: {steps}"[:200])
            _queue(board, job, f"relead-{key}", "lead", _lead_brief(js, steps), workdir)
            return acts + [f"{job}: re-queued the lead for {artifact} (not met)"]
        if judged is None:   # one judge per artifact, never again
            if board.get_launch(job, 1, f"judge-{key}") is None:
                _queue(board, job, f"judge-{key}", "judge", _judge_brief(js, artifact), workdir)
                return acts + [f"{job}: judge queued for {artifact}"]
            return acts + [_fail(board, job, f"the judge of {artifact} ended with no verdict", now)]
        since = msg.created_at
    else:
        since = js.activated_at or js.created_at
    if _attempts(board, job, since) >= LEAD_ATTEMPTS:
        return acts + [_fail(board, job, f"{LEAD_ATTEMPTS} launches with no hand-off (DONE)", now)]
    name = f"lead-{len([r for r in board.launches(job) if r.role == 'lead']) + 1}"
    _queue(board, job, name, "lead", _lead_brief(js, js.verdict_next or js.verdict_reason or ""), workdir)
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
    if _attempts(board, job, since) >= LEAD_ATTEMPTS:
        return _fail(board, job, f"no met verdict after {LEAD_ATTEMPTS} launches", now)
    workdir = _workdir(board, job)
    if workdir is None:
        return _fail(board, job, "no work dir is known for its launches (swarm run --workdir)", now)
    name = f"relead-{key}"
    row = board.get_launch(job, 1, name)
    if row is None or row.state in ("done", "failed", "cancelled"):
        steps = js.verdict_next or js.verdict_reason or "see the verdict"
        board.post(job, PASS_NAME, f"NOT MET {artifact}: {steps}"[:200])
        _queue(board, job, name, "lead", _lead_brief(js, steps), workdir)
    if not start:   # inside a launch: the next hook pass (within a minute) starts queued launches first
        return f"{job}: {name} queued"
    if starter is None:
        from swarm.supervisor import runner
        starter = runner.start
    acts, cost_reason = _start_queued(board, cfg, job, starter)
    if cost_reason:
        return _fail(board, job, cost_reason, now)
    return "; ".join(acts) or f"{job}: {name} queued"


def _notices(board, now: _dt.datetime) -> list[str]:
    """Repeat the notice of every failed job not yet acked, once per NOTICE_MINUTES."""
    acts = []
    for js in board.jobs(include_closed=True):
        if js.status != "failed" or not managed(board, js.job):
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


def run_pass(board, cfg: dict, *, now: _dt.datetime | None = None, starter=None) -> list[str]:
    """One pass over every open job. Returns what it did (empty when nothing needed doing)."""
    if starter is None:
        from swarm.supervisor import runner
        starter = runner.start
    now = now or board.now()
    board.expire_blockers(now)
    acts = []
    for js in board.jobs():
        try:
            acts += _advance(board, cfg, js, now, starter)
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
