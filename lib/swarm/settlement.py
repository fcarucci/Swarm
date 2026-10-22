"""Audited settlement of an exact historical native no-handoff failure.

Neither a posted report nor a stored verdict can admit the review or manufacture a
completion receipt. The one-shot request and runtime binding live in host-private state.
"""
from __future__ import annotations

import hashlib
import json
import os
import re

SUBDIR = 'late-reviews'


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def closure(js):
    data = {key: (getattr(js, key).isoformat() if getattr(js, key) is not None and key.endswith('_at')
                  else getattr(js, key))
            for key in ('job', 'status', 'outcome', 'closed_by', 'finished_at', 'activated_at', 'goal')}
    return dict(data, sha=digest(data))


def eligible(js):
    return bool(js and js.status == 'failed' and js.closed_by == 'swarm pass'
                and js.finished_at and js.goal and not js.completion_forced
                and re.fullmatch(r'[1-9][0-9]* launches with no hand-off \(DONE\)', js.outcome or ''))


def _name(cfg, job):
    from swarm.board.autoinit import store_key
    return digest([store_key(cfg), job]) + '.json'


def load(cfg, job):
    from swarm.supervisor import privfs
    try:
        with privfs.dir_fd(SUBDIR, create=False) as d:
            raw = privfs.read(d, _name(cfg, job), 128 * 1024)
        rec = json.loads(raw) if raw else None
        return rec if isinstance(rec, dict) and rec.get('job') == job else None
    except (OSError, ValueError):
        return None


def background_pending(board, job):
    """An ended label does not prove containment absence of an owned command."""
    from swarm import bg
    from swarm.board.base import run_start
    from swarm.supervisor import runner
    js = board.job_status(job)
    for row in board.bg_commands(job):
        if row.ended_at is not None and row.ended_at < run_start(js):
            continue  # unrelated historical run, preserved as history
        if row.ended_at is None:
            return f'background command {row.id} has not ended'
        place = bg.where(row, bg.this_host(), bg.boot_key())
        if place == 'rebooted':
            continue
        if place != 'local':
            if place == 'foreign' and row.outcome in ('gone', 'reaped', 'killed'):
                continue  # the native originating host recorded its positive reap result
            return f'background command {row.id} stop is {place} on {row.host}'
        members = runner._list_members(row.pgid, row.tag, row.proc_start, bg.TAG_ENV)
        if members is None:
            return f'background command {row.id} containment is unknown'
        if members:
            return f'background command {row.id} still has owned live processes'
        try:
            os.kill(row.pid, 0)
        except ProcessLookupError:
            continue
        except OSError:
            return f'background command {row.id} leader stop is unconfirmed'
        token = bg.proc_start(row.pid)
        if token is None or token == row.proc_start:
            return f'background command {row.id} leader remains or its identity is unknown'
    return None


class Pending(Exception):
    pass


def ready(board, cfg, job):
    try:
        return _ready(board, cfg, job)
    except Pending:
        return None


def pending_reason(board, cfg, job):
    try:
        _ready(board, cfg, job)
        return None
    except Pending as why:
        return str(why)


def _ready(board, cfg, job):
    """Native transaction gate. Missing private provenance is a refusal, never inference."""
    from swarm import review, task_authority
    from swarm.passcmd import handoff
    rec = load(cfg, job)
    js = board.job_status(job)
    if not rec or not eligible(js) or rec.get('closure') != closure(js):
        raise Pending('authenticated review is absent or its historical closure/goal changed')
    if rec.get('failure_provenance') != board.native_failure_provenance(job):
        raise Pending('native no-handoff failure provenance changed')
    try:
        task_authority.original_root(board, cfg, job, rec.get('root'))
    except (OSError, ValueError):
        raise Pending('original-root coordinator proof changed or is unavailable')
    found = handoff(board, job)
    if not found or found[1] != rec.get('candidate') or found[0].id != rec.get('handoff_id'):
        raise Pending('latest handoff no longer matches the delegated exact candidate')
    assignment = rec.get('assignment') or {}
    rows = board.launches(job)
    task = next((r for r in rows if r.id == assignment.get('row_id')), None)
    restart = next((r for r in board.restarts(job) if r.id == assignment.get('restart_id')), None)
    judge = next((a for a in board.agents(job, include_departed=True)
                  if a.agent_key == assignment.get('agent_key')), None)
    if task is None or restart is None:
        raise Pending('native judge launch is queued or has not acquired its one owned claim')
    if restart.ended_at is None:
        raise Pending('owned native judge executor is still running')
    if not rec.get('stopped') or rec.get('stopped_restart_id') != restart.id:
        raise Pending('owned native judge executor stop remains unconfirmed')
    if task.state != 'done' or restart.outcome != 'completed':
        raise Pending('native judge launch ended without a completed review: ' + str(restart.outcome or task.state))
    try:
        verdict = review.bound_verdict(review._chunked(board.job_data(job), 'pipeline.goal_verdict'))
    except (KeyError, ValueError, TypeError):
        raise Pending('fresh bound full-goal MET with exact candidate/requirements and open zero is missing')
    if (not verdict or verdict.get('scope') != 'goal' or verdict.get('verdict') != 'met'
            or type(verdict.get('open')) is not int or verdict.get('open') != 0 or verdict.get('candidate') != rec.get('candidate')
            or verdict.get('req') != rec.get('req') or task is None or task.role != 'judge'
            or task.state != 'done' or task.launch_id != assignment.get('launch_id')
            or restart is None or restart.outcome != 'completed' or restart.ended_at is None
            or restart.launch_id != task.launch_id or restart.new_agent_key != assignment.get('agent_key')
            or judge is None or judge.role != 'judge' or judge.name != verdict.get('judge')
            or not rec.get('stopped') or rec.get('stopped_restart_id') != restart.id):
        raise Pending('full-goal MET or its owned native judge assignment does not match the delegated review')
    details = board.verdict_details(job)
    if not details or not details.details.strip() or details.by != judge.name:
        raise Pending('full independent native judge report is missing')
    messages = board.pipeline_messages(job)
    msg = next((m for m in board.messages_after(verdict['message_id'] - 1, job)
                if m.id == verdict['message_id']), None)
    expected = review.verdict_text('met', verdict.get('reason', ''), scope='goal',
                                  candidate=rec['candidate'], req=rec['req']) if verdict else None
    if (msg is None or msg.agent_name != judge.name or msg.message != expected or msg.created_at <= js.finished_at
            or msg.created_at < found[0].created_at or js.verdict_at != details.at
            or js.verdict != 'met' or js.verdict_scope != 'goal'
            or js.verdict_candidate != rec['candidate'] or js.verdict_req != rec['req']):
        raise Pending('current goal verdict/message no longer matches the accepted native evidence')
    native_final = board.finalization_receipt(job)
    finals = [m for m in messages if native_final and m.id == native_final.get('message_id')
              and m.message == 'FINALIZED ' + rec['candidate'] and m.agent_name == native_final.get('executor')
              and m.agent_name != judge.name and m.created_at >= msg.created_at]
    if (not native_final or native_final.get('closure') != rec['closure']
            or native_final.get('candidate') != rec['candidate'] or native_final.get('req') != rec['req']
            or native_final.get('verdict_id') != msg.id):
        raise Pending('authenticated separate-executor finalization for this accepted evidence is pending')
    if not finals:
        raise Pending('native finalization visibility message is absent or changed')
    if review.completion_pending(board, job, js.goal, js.verdict):
        from swarm.dependencies import describe
        tasks = [t for t in describe(board, job) if t['state'] != 'done']
        if tasks:
            raise Pending('; '.join(f"task {t['name']}: {t['reason'] or t['state']}" for t in tasks))
        raise Pending('artifact acceptance/finalization pending: ' + ', '.join(review.unfinalized_artifacts(board, job)))
    if js.started or js.running:
        raise Pending(f'{js.started + js.running} agents are still working')
    if js.waiting_on:
        raise Pending('registered wait: ' + js.waiting_on)
    blockers = board.blockers(job)
    if blockers:
        raise Pending('; '.join(f'{b.kind} {b.id} -> {b.waiting_on}: {b.reason}' for b in blockers))
    pending = [r.name for r in rows if r.state in ('queued', 'running')]
    if pending:
        raise Pending('pending native launches: ' + ', '.join(pending))
    if any(r.ended_at is None for r in board.restarts(job)):
        raise Pending('owned native restart has not ended')
    unknown = [a.name for a in board.agents(job, include_departed=False)
               if a.ended_at is None and a.status not in ('completed', 'left') and a.turn_ended_at is None]
    if unknown:
        raise Pending('current turn completion is unconfirmed: ' + ', '.join(unknown))
    # Native background work carries its own stop proof, independent of agent turn ends.
    background = background_pending(board, job)
    if background:
        raise Pending(background)
    return dict(job=job, closure=rec['closure'], candidate=rec['candidate'], req=rec['req'],
                review_restart_id=restart.id, review_launch_id=task.launch_id,
                judge_key=judge.agent_key, verdict_id=msg.id, verdict_at=js.verdict_at.isoformat(),
                verdict_chunks={k: v for k, v in board.job_data(job).items()
                                if k == 'pipeline.goal_verdict' or k.startswith('pipeline.goal_verdict.')},
                finalization_id=finals[-1].id, projection_fence=projection_fence(board, job))


def finalization_ready(board, cfg, job):
    from swarm.late_review import authorize
    rec = load(cfg, job)
    intent = (rec or {}).get('finalize_intent')
    if not intent:
        return None
    try:
        authorize(board, cfg, intent, intent.get('root'))
    except (OSError, ValueError):
        return None
    return dict(job=job, closure=rec['closure'], candidate=rec['candidate'], req=rec['req'],
        verdict_id=intent['verdict_id'], evidence=intent['evidence'], executor=intent['root'],
        request_id=intent['request_id'], user_text_sha=intent.get('user_text_sha'),
        grant_id=intent.get('grant_id'), grant_call=intent.get('grant_call'))


def same_finalization(old, proof):
    return all(old.get(k) == proof.get(k) for k in ('job', 'closure', 'candidate', 'req',
                                                    'verdict_id', 'evidence', 'executor', 'request_id'))


def projection_fence(board, job):
    """Native obligation high-water marks; new work cannot inherit old acceptance.

    Messages unrelated to the pipeline, activity heartbeats and completed context
    turns do not invalidate a receipt. A new handoff or native obligation does.
    """
    latest = lambda rows: max((r.id for r in rows), default=0)
    launches = board.launches(job)
    return dict(done=latest(m for m in board.pipeline_messages(job) if m.message.startswith('DONE ')),
                launches=latest(launches), launch_revisions=sum(r.revision for r in launches),
                restarts=latest(board.restarts(job)),
                background=latest(board.bg_commands(job)), blockers=latest(board.blockers(job, include_closed=True)),
                task_evidence=latest(e for e in board.events(job) if e.kind in ('TASK-EVIDENCE', 'TASK-SUPERSEDE')))


def projected(js, receipt, data=None, fence=None):
    """A native receipt projects completion only while the exact accepted identity remains."""
    evidence = (bool(js.native_completed_at) if data is None else
                bool(receipt and fence == receipt.get('projection_fence') and receipt.get('verdict_chunks') and
                     all(data.get(k) == v for k, v in receipt['verdict_chunks'].items())))
    return bool(evidence and receipt and eligible(js) and not js.waiting_on and receipt.get('closure') == closure(js)
                and js.verdict == 'met' and js.verdict_scope == 'goal'
                and receipt.get('candidate') == js.verdict_candidate
                and receipt.get('req') == js.verdict_req and js.verdict_at
                and receipt.get('verdict_at') == js.verdict_at.isoformat())
