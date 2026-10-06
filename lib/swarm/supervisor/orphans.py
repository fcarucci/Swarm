"""Recover locally-enrolled jobs whose last coordinator and workers disappeared."""
from __future__ import annotations
import datetime as dt
from swarm.supervisor.stuck import SUPERVISOR_NAME, WAITING_PREFIX

REASON = "orphan-coordinator:"


def local_record(cfg, js):
    from swarm import enrolment
    from swarm.board.autoinit import store_key
    key = store_key(cfg)
    # The unsandboxed activation hook writes job records in this machine/user's
    # private enrolment directory. Existing records predate separate owner records
    # and already prove local ownership; board rows alone never authorize a launch.
    return enrolment.find_job(key, js.job)


def human_question(board, js):
    """An unanswered question addressed to the owner/user is an external wait."""
    owner = (getattr(js, "created_by", None) or "").casefold()
    humans = {"user", "human", "owner", "francesco", owner} - {""}
    answered = set()
    for m in reversed(board.recent_messages(100, job=js.job)):
        target = (getattr(m, "to_agent", None) or "").casefold()
        if target in humans and target not in answered and m.message.rstrip().endswith("?"):
            return True
        answered.add(m.agent_name.casefold())
    return False


def eligible(board, cfg, js, sup, now):
    from swarm.cli import OrchestratorWatch
    if js.status != "active" or not js.supervise:
        return False
    if js.waiting_on and not js.waiting_on.startswith(WAITING_PREFIX):
        return False  # all explicit external/human waits stay under owner control
    if human_question(board, js):
        return False
    cutoff = now - dt.timedelta(minutes=sup["orphan_minutes"])
    activity = js.last_activity_at or js.activated_at or js.created_at
    if activity > cutoff or js.started or js.running or js.idle:
        return False
    rows = board.agents(js.job)
    if any(a.ended_at is not None and a.ended_at > cutoff for a in rows):
        return False
    # Derivation changes running/idle into dead only after dead_minutes: wait a further
    # orphan window after that transition, rather than restarting immediately at dead.
    dead = float(cfg.get("board", {}).get("dead_minutes", 30))
    if any(a.ended_at is None and a.last_contact_at
           and a.last_contact_at + dt.timedelta(minutes=dead) > cutoff for a in rows):
        return False
    if OrchestratorWatch(cfg, sup["orphan_minutes"], js.job).active():
        return False
    return True


def restart_note(board, js, sup, now):
    from swarm.hosts.resume import _clean
    lines = [f'[swarm job: {js.job}]',
             f"The supervisor is restarting this orphaned job at {now.isoformat()}: no live agents "
             f"or coordinator contact for at least {sup['orphan_minutes']} minutes.",
             "You are the replacement coordinator. Read the board and inspect existing work first. "
             "Continue from the last state; do not redo completed work. You may spawn workers.",
             f"Original task/brief: {_clean(js.task or js.description, 12000)}",
             f"Goal: {_clean(js.goal, 2000)}",
             f"Last activity: {js.last_activity_at}",
             f"Latest verdict: {_clean(js.verdict)} {_clean(getattr(js, 'verdict_reason', None), 2000)}"]
    if js.verdict_next:
        lines.append(f"Judge's next steps: {_clean(js.verdict_next, 2000)}")
    lines.append("Last board messages:")
    for m in board.recent_messages(sup["brief_posts"], job=js.job):
        lines.append(f"#{m.id} {_clean(m.agent_name)}: {_clean(m.message, 1000)}")
    return "\n".join(lines)


def run(board, cfg, sup, state, *, job=None, now=None, dry_run=False, say=print,
        start_runner=None, which=None, scope_ok=lambda: True, config_path="", skip_jobs=()):
    from swarm.supervisor import command
    from swarm.pause import resume
    now = now or board.now()
    histories = state.setdefault("orphan_restarts", {}) if not dry_run else state.get("orphan_restarts", {})
    for js in board.jobs(False):
        if (job and js.job != job) or js.job in skip_jobs:
            continue
        rec = local_record(cfg, js)
        if rec is None or not eligible(board, cfg, js, sup, now):
            continue
        if js.verdict == "met":
            if dry_run:
                say(f"would remind {js.job}: goal met; close the job")
            else:
                command._post_once(board, state, f"orphan-met|{js.job}|{js.activated_at}", js.job,
                                   "Goal is met and no agents remain: close this job; no restart needed.")
            continue
        history = [t for t in histories.get(js.job, []) if now.timestamp() - t < 86400]
        if len(history) >= sup["orphan_max_restarts"]:
            if dry_run:
                say(f"GAVE UP {js.job}: orphan restart limit reached")
            else:
                command._post_once(board, state, f"orphan-gave-up|{js.job}|{history[0] if history else 0}",
                                   js.job, "GAVE UP: orphan coordinator restart limit reached (24 hours).")
            continue
        delay = 15 * 2 ** max(0, len(history) - 1)
        if history and now.timestamp() - history[-1] < delay * 60:
            say(f"not restarting orphan {js.job}: backoff ({delay} minutes)")
            continue
        wd = command.workdir_for(cfg, rec)
        binary = sup.get(f"{rec.harness}_bin")
        import shutil
        why = (None if wd else "coordinator work directory is gone") or \
              (command.workdir_problem(cfg, wd) if wd else None) or \
              (command._workdir_hold(cfg, wd) if wd else None) or \
              (None if binary and (which or shutil.which)(binary) else f"host executable unavailable for {rec.harness}") or \
              (None if scope_ok() else command.NO_MANAGER_NOTE)
        if why:
            say(f"not restarting orphan {js.job}: {why}")
            if not dry_run:
                command._post_once(board, state, f"orphan-hold|{js.job}|{why}", js.job,
                                   f"orphan coordinator held: {why}")
            continue
        say(f"{'would restart' if dry_run else 'restarting'} orphan {js.job} coordinator "
            f"on {rec.harness} (attempt {len(history)+1}/{sup['orphan_max_restarts']})")
        if dry_run:
            continue
        fresh = board.job_status(js.job)
        if not fresh or fresh.verdict == "met" or not eligible(board, cfg, fresh, sup, now):
            continue
        if command._switched_off_now(cfg, board, js.job):
            continue
        # Save before launch: failed launches also count, and a dying pass never loops launches.
        histories[js.job] = history + [now.timestamp()]
        from swarm.supervisor.settings import save_state, log
        save_state(state)
        log(f"restarting orphan coordinator on {js.job}: attempt {len(history)+1}")
        try:
            resume(board, cfg, js.job, orphan=rec, restart_note=restart_note(board, fresh, sup, now),
                   start_runner=start_runner, config_path=config_path)
        except Exception as exc:
            board.post(js.job, SUPERVISOR_NAME, f"orphan coordinator restart failed ({type(exc).__name__})")
