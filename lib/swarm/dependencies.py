"""Durable engineering stages using existing named launch rows and bounded runners.

A task's immutable brief envelope contains its exact artifact and predecessor names. Evidence is
append-only board event data bound to a task generation and its runner (or declared external agent).
The pass and atomic launch claim both apply the same gate; completed stages are never requeued.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json

PREFIX = 'SWARM_TASK_V1\n'
KIND = 'TASK-EVIDENCE'


def spec(row):
    if row.queued_by != 'swarm task':
        return None
    if not row.brief.startswith(PREFIX):
        raise ValueError(f'task {row.name} has invalid metadata')
    value = json.loads(row.brief[len(PREFIX):])
    if value.get('version') != 1:
        raise ValueError(f'task {row.name} has unsupported metadata')
    return value


def _event_key(row):
    return hashlib.sha256(f'{row.generation}|{row.name}|{row.brief}|{row.launch_id or "external"}'.encode()).hexdigest()


def evidence(board, row):
    key = _event_key(row)
    for event in board.events(row.job):
        if event.kind == KIND and event.key == key:
            return json.loads(event.text)
    return None


def register(board, job, generation, name, *, artifact, depends, role, harness, model,
             workdir, brief, queued_by, reporter=None):
    """Atomically insert a task once; repeated identical registration preserves all lifecycle state.

    reporter names an external agent already in an active turn whose completion must be witnessed,
    e.g. an existing Mac build. Such a task is never launched by the pass. That assigned agent
    attests the exact artifact and the dependency is released only after that turn ends.
    """
    from swarm.board.base import run_start
    from swarm.board.launches import check_launch
    board.recover_auto_close(job)
    js = board.job_status(job)
    if js is None:
        raise ValueError(f'{job} is not an open job')
    if not isinstance(artifact, str) or not artifact.strip():
        raise ValueError('task artifact must be explicit and nonempty')
    if not isinstance(depends, (list, tuple)) or not all(isinstance(d, str) for d in depends):
        raise ValueError('task dependencies must be task names')
    if name in depends or len(set(depends)) != len(depends):
        raise ValueError('task dependencies cannot contain self or duplicates')
    for dep in depends:
        check_launch(dep, generation, role, harness, workdir, brief, queued_by)
    existing = board.get_launch(job, generation, name)
    old_spec = spec(existing) if existing else None
    reporter_agent = next((a for a in board.agents(job, include_departed=True)
                           if a.name == reporter and a.role == role and (
                               (old_spec is not None and a.agent_key == old_spec.get('reporter_key'))
                               or (old_spec is None and a.ended_at is None
                                   and getattr(a, 'turn_ended_at', None) is None
                                   and a.status in ('started', 'running')))), None) if reporter else None
    if reporter is not None and reporter_agent is None:
        raise ValueError('external reporter must be a named agent in the declared role with an active agent turn')
    value = dict(version=1, artifact=artifact, depends=sorted(depends), brief=brief,
                 run=run_start(js).isoformat(), reporter=reporter, reporter_key=reporter_agent.agent_key if reporter_agent else None,
                 registered_by=queued_by)
    encoded = PREFIX + json.dumps(value, sort_keys=True, separators=(',', ':'))
    if len(encoded) > 64 * 1024:
        raise ValueError('task brief exceeds 64 KiB')
    existing = board.get_launch(job, generation, name)
    if existing is None and js.status != 'active':
        raise ValueError(f'{job} is not an open job')
    row = board.queue_launch(job, generation, name, role=role, harness=harness, model=model,
                             workdir=workdir, brief=encoded, queued_by='swarm task', if_absent=True)
    if (row.brief, row.role, row.harness, row.model, row.workdir) != (encoded, role, harness, model, workdir):
        raise ValueError('task generation already registered differently; use a new generation')
    return row


def report(board, job, generation, name, *, artifact, outcome, reason, actor):
    """Accept evidence only from this task's assigned live agent; the first report is immutable."""
    row = board.get_launch(job, generation, name)
    value = spec(row) if row else None
    if value is None:
        raise ValueError('unknown task')
    if artifact != value['artifact']:
        raise ValueError('evidence artifact does not match task artifact')
    if not _current(board, row, value):
        raise ValueError('task belongs to a superseded job run')
    if outcome not in ('passed', 'failed') or not isinstance(reason, str) or not reason.strip():
        raise ValueError('task report needs passed|failed and a reason')
    agents = {a.agent_key: a for a in board.agents(job, include_departed=True)}
    if value.get('reporter'):
        assigned = agents.get(value['reporter_key'])
    else:
        restart = next((r for r in board.restarts(job) if r.launch_id == row.launch_id), None)
        assigned = agents.get(restart.new_agent_key) if restart and restart.ended_at is None else None
    if assigned is None or assigned.name != actor or assigned.role != row.role or assigned.ended_at is not None:
        raise ValueError('only the task assigned live agent may report evidence')
    reason = ' '.join(reason.split())[:250]
    payload = dict(outcome=outcome, reason=reason, actor=actor, at=board.now().isoformat())
    text = json.dumps(payload, ensure_ascii=False, separators=(',', ':'))
    while len(text) > 500:
        payload['reason'] = payload['reason'][:-1]
        text = json.dumps(payload, ensure_ascii=False, separators=(',', ':'))
    return board.post_event(job, KIND, _event_key(row), text, source='swarm-task')


def _current(board, row, value):
    from swarm.board.base import run_start
    js = board.job_status(row.job)
    return js is not None and dt.datetime.fromisoformat(value['run']) >= run_start(js)


def block_reason(board, row, *, launching=False):
    """None means a queued task may start. Every other return is a precise internal blocker."""
    value = spec(row)
    if value is None:
        return None
    if not _current(board, row, value):
        return 'superseded by a newer job run'
    if any(other.name == row.name and other.generation > row.generation for other in board.launches(row.job)
           if other.queued_by == 'swarm task'):
        return 'superseded by a newer task generation'
    data = board.job_data(row.job)
    artifact = value['artifact']
    if data.get('pipeline.superseded.' + hashlib.sha256(artifact.encode()).hexdigest()[:32]):
        return 'artifact superseded by a newer handoff'
    if value.get('reporter') and launching:
        return 'waiting for declared external reporter ' + value['reporter']
    for name in value['depends']:
        dep = board.get_launch(row.job, row.generation, name)
        dv = spec(dep) if dep else None
        if dv is None:
            return f'missing dependency {name}'
        if dv['artifact'] != artifact or dv['run'] != value['run']:
            return f'dependency {name} artifact/run mismatch'
        newer = [other for other in board.launches(row.job) if other.name == name
                 and other.generation > dep.generation and other.queued_by == 'swarm task']
        if newer:
            return f'dependency {name} superseded by task generation {max(other.generation for other in newer)}'
        if dep.state == 'failed':
            return f'dependency {name} failed: {dep.last_outcome or "runner failed"}'
        ev = evidence(board, dep)
        if ev and ev['outcome'] != 'passed':
            return f'dependency {name} failed evidence: {ev["reason"]}'
        if dep.state != 'done':
            return f'dependency {name} {dep.state}; waiting for its turn to end'
        if ev is None:
            return f'dependency {name} missing exact-artifact evidence'
    return None


def describe(board, job):
    rows = []
    data = board.job_data(job)
    launches = board.launches(job)
    latest = {}
    for row in launches:
        if row.queued_by == 'swarm task':
            latest[row.name] = max(latest.get(row.name, 0), row.generation)
    for row in launches:
        value = spec(row)
        if row.generation < latest.get(row.name, 0):
            continue
        if value is None or not _current(board, row, value):
            continue
        if data.get('pipeline.superseded.' + hashlib.sha256(value['artifact'].encode()).hexdigest()[:32]):
            continue
        reason = block_reason(board, row)
        ev = evidence(board, row)
        state = row.state
        if reason:
            state = 'blocked'
        elif state == 'queued' and value.get('reporter'):
            state, reason = 'blocked', 'waiting for external evidence/turn end: ' + value['reporter']
        elif state == 'done' and (ev is None or ev['outcome'] != 'passed'):
            state, reason = 'blocked', ('missing exact-artifact evidence' if ev is None else 'failed evidence: ' + ev['reason'])
        elif state == 'queued':
            state, reason = 'ready', 'ready for bounded runner; subject to job concurrency/cost limits'
        rows.append(dict(name=row.name, generation=row.generation, artifact=value['artifact'],
                         state=state, reason=reason or '', depends=value['depends']))
    return rows


def reconcile_external(board, job):
    for row in board.launches(job, include_closed=False):
        value = spec(row)
        if value is None or row.state != 'queued' or not value.get('reporter') or block_reason(board, row):
            continue
        ev = evidence(board, row)
        if not ev:
            continue
        agent = next((a for a in board.agents(job, include_departed=True) if a.agent_key == value['reporter_key']), None)
        # A stop hook persists turn_ended_at. Mere heartbeat/contact never ends a turn.
        reported_at = dt.datetime.fromisoformat(ev['at'])
        if agent is None:
            continue
        terminal_at = agent.ended_at or getattr(agent, 'turn_ended_at', None)
        if terminal_at is None or terminal_at < reported_at:
            continue
        board.set_launch_state(row.id, row.revision, 'done' if ev['outcome'] == 'passed' else 'failed',
                               job=job, last_outcome='external evidence: ' + ev['outcome'])


def advance(board, job):
    """Task jobs own their lifecycle until every stage is successfully evidenced and quiescent."""
    for row in board.launches(job, include_closed=False):
        if row.state == 'queued' and spec(row) is not None:
            why = block_reason(board, row)
            if why and (why.startswith('superseded') or why.startswith('artifact superseded')):
                board.set_launch_state(row.id, row.revision, 'failed', job=job, last_outcome=why)
    reconcile_external(board, job)
    rows = describe(board, job)
    if not rows:
        return None
    blocked = [r for r in rows if r['state'] == 'blocked']
    board.set_job_data(job, 'task.blocker', '; '.join(f'{r["name"]}: {r["reason"]}' for r in blocked)[:4000] or None)
    complete = all(r['state'] == 'done' for r in rows)
    busy = any(a.status in ('started', 'running') and a.role not in ('owner', 'orchestrator', 'requester')
               for a in board.agents(job, include_departed=False))
    js = board.job_status(job)
    # Declared engineering stages can be only a subset of the job's broader goal. Successful
    # stages permit goal acceptance; they never manufacture a met verdict or completion contract.
    if complete:
        remaining = js.verdict_next or js.verdict_reason or 'awaiting full job goal acceptance'
        board.set_job_data(job, 'task.blocker', 'declared tasks passed; remaining job goal: ' + remaining)
        from swarm.passcmd import handoff
        found = handoff(board, job)
        if (not busy and not any(r.ended_at is None for r in board.restarts(job))
                and found and found[1] in {r['artifact'] for r in rows}):
            return None
    return []


def prompt(row):
    import shlex
    value = spec(row)
    if value is None:
        return row.brief
    return (value['brief'] + '\n\nExact artifact: ' + value['artifact'] +
            '\nBefore ending, record verification: swarm task report --job ' + shlex.quote(row.job) +
            ' --name ' + shlex.quote(row.name) + ' --generation ' + str(row.generation) +
            ' --artifact ' + shlex.quote(value['artifact']) + ' --as YOUR_JOINED_NAME --outcome passed|failed --reason REASON.\n'
            'Include checks and evidence in the reason. Register required downstream tasks before ending your turn. '
            'Never dispatch an already registered successor. End your turn normally; Swarm derives job status.')


def wake(cfg):
    """Ensure recovery survives the final hook; deliberately independent of external HTTP events."""
    from swarm import events_listener
    return events_listener.ensure_reconciler(cfg)
