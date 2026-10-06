"""Owner-only hand-off -> judge -> fix/finalize transitions on the supervisor timer.

Artifacts are opaque. Evidence and execution recipes belong to plugins; this module
never interprets a repository, deployment or report reference.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import shutil
import uuid
from dataclasses import dataclass

from swarm.supervisor import budget

DEFAULTS = {"enabled": True, "judge_host": "claude", "judge_model": "",
            "evidence_wait_command": "", "finalize": "", "finalize_enabled": True}
LIVE = ("started", "running", "idle")
EXECUTORS = ("finalizer", "integrator")


def settings(cfg):
    result = dict(DEFAULTS)
    result.update(cfg.get("pipeline") or {})
    if not isinstance(result["enabled"], bool) or not isinstance(result["finalize_enabled"], bool):
        raise ValueError("[pipeline] enabled and finalize_enabled must be booleans")
    if result["judge_host"] not in ("claude", "codex"):
        raise ValueError("[pipeline] judge_host must be claude or codex")
    for key in ("judge_model", "evidence_wait_command", "finalize"):
        if not isinstance(result[key], str):
            raise ValueError(f"[pipeline] {key} must be text")
    return result


def _stamp(value):
    if isinstance(value, dt.datetime):
        return value
    if isinstance(value, str):
        try:
            parsed = dt.datetime.fromisoformat(value)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)
        except ValueError:
            pass
    return None


def _digest(value):
    return hashlib.sha256(value.encode()).hexdigest()[:24]


@dataclass(frozen=True)
class Transition:
    job: object
    handoff: object
    role: str
    verdict: dict | None = None

    @property
    def token(self):
        # A verdict revision gets a new fix/finalize trigger. An artifact's attempt budget
        # survives revisions so repeated not_met decisions cannot evade the cap.
        event = self.handoff.id if self.role == "judge" else self.verdict.get("at")
        return f"{self.role}|{self.handoff.artifact}|{event}"

    @property
    def lineage(self):
        return "pipeline-" + _digest(self.job.job + "|" + self.role + "|" + self.handoff.artifact)


def _worker_consumed(board, action, handoffs):
    trigger = "pipeline:worker:" + _digest(action.token)
    agents = {a.agent_key: a for a in board.agents(action.job.job)}
    for r in board.restarts(job=action.job.job):
        if r.reason != trigger or r.outcome != "completed":
            continue
        worker = agents.get(r.new_agent_key) or agents.get(r.old_agent_key)
        if worker and any(m.agent_name == worker.name and m.message.startswith("DONE ")
                          and m.created_at >= r.at and (r.ended_at is None or m.created_at <= r.ended_at)
                          for m in board.messages_after(0, action.job.job)):
            return True
    return False


def transitions(board, js, cfg=None, config_path=None):
    """One pending action: review all hand-offs before fixing/finalizing their results."""
    from swarm import review
    if any(r.ended_at is None and r.reason.startswith("pipeline:") for r in board.restarts(job=js.job)):
        return []  # runner owns this transition even before its first hook enrolls
    active = [a for a in board.agents(js.job) if a.status in LIVE and a.ended_at is None]
    busy_worker = any(a.role not in ("judge", "verifier", *EXECUTORS) for a in active)
    busy_judge = any(a.role == "judge" for a in active)
    busy_executor = any(a.role in EXECUTORS for a in active)
    verdicts = review.artifact_verdicts(board, js.job)
    handoffs, _ = effective_handoffs(board, cfg or {}, js, config_path=config_path)
    unjudged = [h for h in handoffs if not review.covered(h, verdicts.get(h.artifact))]
    if unjudged:
        if busy_judge or busy_executor:
            return []
        h = unjudged[0]
        previous = verdicts.get(h.artifact)
        if previous is None:
            # Carry the fix brief into the review of its new reference, without borrowing
            # another parallel artifact's latest verdict.
            agents = {a.agent_key: a for a in board.agents(js.job)}
            for r in reversed(board.restarts(job=js.job)):
                worker = agents.get(r.new_agent_key) or agents.get(r.old_agent_key)
                if (r.reason.startswith("pipeline:worker:") and worker and worker.name == h.agent_name
                        and r.at <= h.created_at and (r.ended_at is None or h.created_at <= r.ended_at)):
                    previous = next((v for ref, v in verdicts.items()
                                     if r.agent_key == "pipeline-" + _digest(js.job + "|worker|" + ref)
                                     and v.get("verdict") == "not_met"), None)
                    if previous:
                        break
        return [Transition(js, h, "judge", previous)]
    if busy_worker or busy_judge or busy_executor:
        return []
    for h in handoffs:
        v = verdicts[h.artifact]
        if _finalized(board, js.job, h.artifact, max(h.created_at, _stamp(v.get("at")))):
            continue
        if v.get("verdict") == "not_met":
            action = Transition(js, h, "worker", v)
            if not _worker_consumed(board, action, handoffs):
                return [action]
        elif v.get("verdict") == "met":
            at = _stamp(v.get("at"))
            blocked = [m for m in board.recent_messages(500, job=js.job)
                       if m.created_at >= at and m.message.startswith("FINALIZE_BLOCKED " + h.artifact + " ")]
            if blocked:
                m = max(blocked, key=lambda m: m.id)
                instructions = m.message[len("FINALIZE_BLOCKED " + h.artifact + " "):]
                fix = dict(v, at=m.created_at.isoformat(), reason="Finalization blocked",
                           next_steps=instructions)
                action = Transition(js, h, "worker", fix)
                if not _worker_consumed(board, action, handoffs):
                    return [action]
            else:
                return [Transition(js, h, "finalizer", v)]
    return []


def effective_handoffs(board, cfg, js, config_path=None):
    """Recipes may group revisions; newest ref wins within that opaque group."""
    from swarm import review
    found, superseded = {}, {}
    for h in review.latest_handoffs(board, js.job):
        group = recipe_for(cfg, board, js, h.artifact, config_path=config_path).get("artifact_group")
        key = ("recipe", group) if isinstance(group, str) and group else ("artifact", h.artifact)
        if key in found:
            superseded[found[key].artifact] = h.artifact
        found[key] = h
    return sorted(found.values(), key=lambda h: h.id), superseded


def recipe_for(cfg, board, js, artifact, config_path=None):
    from swarm import plugins
    resolver = getattr(plugins, "pipeline_recipe", None)
    recipe = dict(resolver(cfg, board, js.job, artifact, config_path=config_path) or {}) if resolver else {}
    p = settings(cfg)
    data = board.job_data(js.job)
    for key, default_key in (("evidence_command", "evidence_wait_command"), ("finalize", "finalize")):
        stored = "pipeline." + key
        if stored in data:
            recipe[key] = data[stored]
        elif getattr(js, key, None) is not None:
            recipe[key] = getattr(js, key)
        else:
            recipe[key] = recipe.get(key) or p[default_key]
    return recipe


def prompt_for(action, recipe):
    import shlex
    from swarm.hosts.resume import _clean
    js, h = action.job, action.handoff
    role = recipe.get("finalizer_role", "finalizer") if action.role == "finalizer" else action.role
    ref_arg = shlex.quote(h.artifact)
    lines = [f"[swarm job: {js.job}]", f"[swarm role: {role}]",
             f"Goal: {_clean(js.goal, 12000)}", f"Task: {_clean(js.task or js.description, 12000)}",
             f"Hand-off artifact: {_clean(h.artifact, 2000)}",
             f"Hand-off #{h.id} from {_clean(h.agent_name)}: {_clean(h.summary, 8000)}"]
    previous = action.verdict or {}
    if action.role == "judge":
        lines.extend(["You are the independent judge. Inspect only; do not fix, merge, push, or finalize.",
                      f"You MUST conclude with swarm verdict --job {shlex.quote(js.job)} --as YOUR_JOINED_NAME "
                      f"met|not_met --artifact {ref_arg} "
                      "--reason REASON (not_met also requires --next NEXT). Do not stop without recording it."])
        if recipe.get("evidence_command"):
            lines.extend(["Wait for external evidence using this command:", recipe["evidence_command"],
                          "If evidence remains pending or fails, record not_met with reason evidence pending/red "
                          "and actionable --next. Never leave the job without a verdict."])
    elif action.role == "worker":
        lines.extend(["You are a fix worker. Continue existing work from the independent verdict.",
                      f"Verdict reason: {_clean(previous.get('reason'), 8000)}",
                      f"Your brief: {_clean(previous.get('next_steps') or previous.get('next'), 12000)}",
                      "When ready, publish a new hand-off with swarm done --artifact REF --summary TEXT."])
    else:
        lines.extend(["You are a FINALIZER, a separate executing role. The judge only judged.",
                      "Before execution, read current hand-offs and artifact verdicts. This met verdict "
                      "covers only this exact artifact. If a newer hand-off supersedes it, stop execution."])
        if recipe.get("evidence_command"):
            lines.extend(["Recheck external evidence before execution:", recipe["evidence_command"],
                          "If evidence is pending/red, do not finalize. Publish FINALIZE_BLOCKED "
                          "with this artifact and actionable instructions for a fix worker."])
        lines.extend([recipe.get("finalize") or "Record the outcome and durable learnings.",
                      "If execution needs substantial fixes or non-trivial conflicts, publish "
                      f"FINALIZE_BLOCKED {h.artifact} <actionable fix instructions> and stop.",
                      f"On success post FINALIZED {h.artifact} (a coding integrator posts INTEGRATED).",
                      "Run swarm learn with durable outcome/learnings. Only deactivate the job completed "
                      "once every current handed-off artifact is finalized and no other work remains. "
                      "References recorded as pipeline.superseded in job data are older revisions and do not count."])
    if action.role == "judge" and previous.get("verdict") == "not_met":
        lines.append(f"Previous not_met next steps: {_clean(previous.get('next_steps'), 12000)}")
    return "\n".join(lines)


def launch_action(board, cfg, sup, action, owner, recipe, decision, *, start_runner=None, config_path=""):
    """Use replacement seats, markers and bounded runner, just like orphan recovery."""
    from swarm.board.autoinit import store_key
    from swarm.supervisor import command, launch, markers, runner, settings as ss
    role = recipe.get("finalizer_role", "finalizer") if action.role == "finalizer" else action.role
    if role not in (*EXECUTORS, "judge", "worker"):
        raise ValueError("invalid pipeline executing role")
    harness = settings(cfg)["judge_host"] if role == "judge" else owner.harness
    old = "pipeline-seat-" + str(uuid.uuid4())
    name = board.allocate_name(old, action.job.job, role)
    if role == "judge" and not board.claim_judge(old, action.job.job):
        board.close_agent(old, "pipeline judge seat busy")
        return None
    board.close_agent(old, "pipeline predecessor")
    row = board.record_restart(action.job.job, action.lineage, old, "pipeline:" + role + ":" + _digest(action.token), harness,
                               decision.minutes, max_per_job=sup["max_restarts_per_job"],
                               max_job_minutes=sup["max_restart_minutes"],
                               max_host_running=sup["max_concurrent_replacements"],
                               max_host_minutes=sup["daily_restart_minutes"], day_start=ss.today_start())
    if row is None:
        return None
    marker = None
    try:
        model = settings(cfg)["judge_model"] if role == "judge" else ""
        if role == "judge":
            from swarm import models
            model = model or models.model_for(cfg, harness, "judge")
        else:
            model = model or launch.replacement_model(cfg, harness, role, None)
        spec = launch.spec_for(cfg, harness, prompt=prompt_for(action, recipe), workdir=owner.cwd,
                               model=model, minutes=decision.minutes)
        board.set_job_data(action.job.job, "pipeline.judge." + hashlib.sha256(old.encode()).hexdigest()[:32], action.handoff.artifact)
        marker = markers.write_resume_marker(cfg, action.job.job, row.id, resume_of=old, name=name,
                                             harness=harness, session_id=spec.session_id)
        if spec.session_id:
            board.set_restart_agent(row.id, spec.session_id)
        run = {"restart_id": row.id, "job": action.job.job, "name": name, "harness": harness,
               "resume_of": old, "marker": str(marker), "argv": list(spec.argv), "cwd": spec.cwd,
               "stdin": spec.stdin, "session_id": spec.session_id,
               "limit_seconds": decision.minutes * 60, "enrol_seconds": sup["enrol_minutes"] * 60,
               "config": config_path, "board": store_key(cfg)}
        fresh = board.job_status(action.job.job)
        if not fresh or fresh.status != "active" or command._switched_off_now(cfg, board, fresh.job):
            board.finish_restart(row.id, "cancelled")
            markers.remove_resume_marker(marker)
            return None
        (start_runner or runner.start)(cfg, run)
        return run
    except Exception:
        board.finish_restart(row.id, "failed")
        if marker:
            markers.remove_resume_marker(marker)
        raise


def _finalized(board, job, artifact, since):
    for m in board.recent_messages(500, job=job):
        if m.created_at < since:
            continue
        for prefix in ("FINALIZED ", "INTEGRATED "):
            if m.message.startswith(prefix) and (m.message[len(prefix):].strip() == artifact or m.message[len(prefix):].startswith(artifact + " ")):
                return True
    return False


def run(board, cfg, sup, state, *, job=None, now=None, dry_run=False, say=print,
        start_runner=None, which=None, scope_ok=lambda: True, config_path=""):
    """Return owned pipeline jobs, so orphan/replacement passes do not duplicate their work."""
    from swarm.supervisor import command, orphans
    from swarm.supervisor.settings import save_state
    p = settings(cfg)
    if not p["enabled"]:
        return set()
    managed = set()
    now = now or board.now()
    for js in board.jobs(False):
        if (job and job != js.job) or not js.goal or not js.supervise or js.status != "active":
            continue
        owner = orphans.local_record(cfg, js)
        if owner is None:
            continue
        from swarm import review
        if not review.latest_handoffs(board, js.job):
            continue
        managed.add(js.job)
        if (js.waiting_on and not js.waiting_on.startswith("supervisor:")) or orphans.human_question(board, js):
            continue
        try:
            actions = transitions(board, js, cfg, config_path=config_path)
            _, superseded = effective_handoffs(board, cfg, js, config_path=config_path)
        except Exception as exc:
            say(f"pipeline held {js.job}: recipe failed ({type(exc).__name__})")
            continue
        if not dry_run:
            desired = {"pipeline.superseded." + hashlib.sha256(ref.encode()).hexdigest()[:32]: replacement
                       for ref, replacement in superseded.items()}
            for key in board.job_data(js.job):
                if key.startswith("pipeline.superseded.") and key not in desired:
                    board.set_job_data(js.job, key, None)
            for old_ref, new_ref in superseded.items():
                board.set_job_data(js.job, "pipeline.superseded." + hashlib.sha256(old_ref.encode()).hexdigest()[:32], new_ref)
        for action in actions:
            try:
                recipe = recipe_for(cfg, board, js, action.handoff.artifact, config_path=config_path)
            except Exception as exc:
                say(f"pipeline held {js.job}: recipe failed ({type(exc).__name__})")
                continue
            if action.role == "finalizer":
                if not p["finalize_enabled"] or recipe.get("enabled", True) is False:
                    continue
                if _finalized(board, js.job, action.handoff.artifact, max(action.handoff.created_at, _stamp(action.verdict.get("at")))):
                    continue
            rec = owner
            if action.role == "worker":
                rows = [a for a in board.agents(js.job) if a.role not in ("judge", "verifier", "coordinator", *EXECUTORS)]
                if rows:
                    last = max(rows, key=lambda a: a.joined_at)
                    rec = command.enrolment_of(cfg, last, js.job) or owner
            wd = command.workdir_for(cfg, rec)
            harness = p["judge_host"] if action.role == "judge" else rec.harness
            why = (None if wd else "owner work directory is gone") or \
                  (command.workdir_problem(cfg, wd) if wd else None) or \
                  (command._workdir_hold(cfg, wd) if wd else None) or \
                  (None if (which or shutil.which)(sup.get(harness + "_bin", harness)) else "host executable unavailable") or \
                  (None if scope_ok() else command.NO_MANAGER_NOTE)
            if why:
                say(f"pipeline held {js.job}: {why}")
                continue
            check = recipe.get("evidence_check")
            if action.role == "finalizer" and check:
                try:
                    green = bool(check(wd))
                except Exception:
                    green = False
                if not green:
                    say(f"pipeline held {js.job}: artifact evidence pending/red")
                    if not dry_run:
                        command._post_once(board, state, "pipeline-evidence|" + js.job + "|" + action.token,
                                           js.job, "Artifact met; finalization held: evidence pending/red.")
                    continue
            prior = board.restarts(job=js.job)
            mine = [r for r in prior if r.agent_key == action.lineage]
            # First judge/finalizer hand-off should launch promptly. Fix rounds and retries
            # use the existing replacement backoff and all existing minute/concurrency caps.
            closed = _stamp(action.verdict.get("at")) if action.verdict else action.handoff.created_at
            decision = budget.decide(sup, lineage=action.lineage, job_restarts=prior,
                                     host_restarts=command._host_restarts(board), closed_at=closed,
                                     now=now, running=command._host_running(board),
                                     outage_started=now if not mine and action.role != "worker" else None)
            if not decision.go:
                say(f"pipeline held {js.job} {action.role}: {decision.why}")
                if decision.cap and not dry_run:
                    command._post_once(board, state, "pipeline-gave-up|" + js.job + "|" + action.lineage,
                                       js.job, f"GAVE UP: pipeline {action.role} limit reached: {decision.why}")
                continue
            # An existing open launch has authority until the bounded runner reaps it.
            if any(r.ended_at is None for r in mine):
                continue
            if dry_run:
                say(f"would launch pipeline {action.role} on {js.job}: {action.handoff.artifact}")
                continue
            # Check all transition inputs again, immediately before claiming a seat.
            fresh = board.job_status(js.job)
            try:
                valid = fresh and any(t.token == action.token for t in transitions(board, fresh, cfg, config_path=config_path))
            except Exception as exc:
                say(f"pipeline held {js.job}: recipe recheck failed ({type(exc).__name__})")
                continue
            if not valid:
                continue
            if command._switched_off_now(cfg, board, js.job):
                continue
            try:
                from types import SimpleNamespace
                verified = SimpleNamespace(cwd=wd, harness=rec.harness)
                launched = launch_action(board, cfg, sup, action, verified, recipe, decision,
                                         start_runner=start_runner, config_path=config_path)
                if launched:
                    command._post_once(board, state, "pipeline-start|" + str(launched["restart_id"]), js.job,
                                       f"Pipeline launched {action.role} for {action.handoff.artifact}")
                    save_state(state)
            except Exception as exc:
                say(f"pipeline launch failed {js.job}: {type(exc).__name__}")
    return managed
