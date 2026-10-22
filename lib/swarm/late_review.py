"""One original-root delegated review of an exact historical pass failure.

This capability never registers an owner, reopens a job, or changes supervisor policy.
It uses the upstream root provenance and single-call private grant mechanisms.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import sys
import time

from swarm import enrolment, person, safefs, settlement, task_authority
from swarm.board.autoinit import store_key
from swarm.supervisor import privfs

TTL = 90
LIMIT = 128 * 1024


def configure(sub):
    for command in ('late-review', 'late-finalize'):
        p = sub.add_parser(command, help='authenticated original-root delegated historical-failure review')
        for key in ('job', 'closure', 'candidate', 'req', 'request-id', 'workdir'):
            p.add_argument('--' + key, required=True)
        if command == 'late-finalize':
            p.add_argument('--verdict-id', type=int, required=True)
            p.add_argument('--evidence', required=True, help='digest of verified immutable final operational evidence')


def fields(args):
    result = {key: getattr(args, key) for key in ('job', 'closure', 'candidate', 'req', 'request_id', 'workdir')}
    if (not re.fullmatch('[0-9a-f]{64}', result['closure'])
            or not re.fullmatch('[0-9a-f]{64}', result['req'])
            or any(not isinstance(v, str) or not v.strip() or len(v) > 4096 or '\0' in v for v in result.values())):
        raise ValueError('review requires exact closure, candidate, requirement digest and real request id')
    if not os.path.isabs(result['workdir']) or not os.path.isdir(result['workdir']):
        raise ValueError('review workdir must be an existing absolute native-approved directory')
    result['workdir'] = os.path.realpath(result['workdir'])
    result['cmd'] = args.cmd
    if args.cmd == 'late-finalize':
        if args.verdict_id < 1 or not re.fullmatch('[0-9a-f]{64}', args.evidence):
            raise ValueError('finalization requires accepted verdict id and exact evidence digest')
        result.update(verdict_id=args.verdict_id, evidence=args.evidence)
    return result


def authorize(board, cfg, values, root, *, agent=None, host='codex'):
    from swarm.passcmd import handoff
    if host != 'codex' or agent:
        raise ValueError('only the actual original root can delegate a closed review')
    js = board.job_status(values['job'])
    if not settlement.eligible(js) or settlement.closure(js)['sha'] != values['closure']:
        raise ValueError('review does not match the current historical no-handoff failure')
    if board.native_failure_provenance(js.job) is None:
        raise ValueError('historical failure lacks native no-handoff notice and launch provenance')
    rec = task_authority.original_root(board, cfg, js.job, root)
    found = handoff(board, js.job)
    if not found or found[1] != values['candidate'] or found[0].created_at <= js.finished_at:
        raise ValueError('review requires the fresh exact final handoff after failure')
    if values.get('cmd') == 'late-finalize':
        from swarm import review
        private = settlement.load(cfg, js.job)
        try:
            verdict = review.bound_verdict(review._chunked(board.job_data(js.job), 'pipeline.goal_verdict'))
        except (KeyError, ValueError, TypeError):
            verdict = None
        if (private is None or private.get('root') != root or not private.get('stopped')
                or private.get('workdir') != values['workdir']
                or private.get('candidate') != values['candidate'] or private.get('req') != values['req']
                or not verdict or verdict.get('message_id') != values['verdict_id']
                or verdict.get('verdict') != 'met' or verdict.get('scope') != 'goal'
                or type(verdict.get('open')) is not int or verdict.get('open') != 0
                or verdict.get('candidate') != values['candidate'] or verdict.get('req') != values['req']
                or js.verdict != 'met' or js.verdict_scope != 'goal'
                or js.verdict_candidate != values['candidate'] or js.verdict_req != values['req']
                or (private.get('assignment') or {}).get('agent_key') == root):
            raise ValueError('finalization requires the exact current accepted independent native review')
        assigned = private.get('assignment') or {}
        judge = next((a for a in board.agents(js.job, include_departed=True)
                      if a.agent_key == assigned.get('agent_key')), None)
        report = board.verdict_details(js.job)
        messages = board.messages_after(values['verdict_id'] - 1, js.job)
        message = next((m for m in messages if m.id == values['verdict_id']), None)
        if (judge is None or judge.role != 'judge' or judge.name != verdict.get('judge')
                or not report or not report.details.strip() or report.by != judge.name
                or not message or message.agent_name != judge.name
                or message.message != review.verdict_text('met', verdict.get('reason', ''), scope='goal')
                or message.created_at <= js.finished_at or message.created_at < found[0].created_at):
            raise ValueError('finalization requires bound native judge evidence and full report')
    from swarm.dependencies import describe
    if (any(t['state'] not in ('done', 'superseded') for t in describe(board, js.job))
            or board.blockers(js.job) or js.waiting_on or js.started or js.running
            or any(a.ended_at is None and a.status not in ('completed', 'left')
                   and a.turn_ended_at is None for a in board.agents(js.job, include_departed=False))
            or board.bg_commands(js.job, running=True)
            or any(r.state in ('queued', 'running') for r in board.launches(js.job))
            or any(r.ended_at is None for r in board.restarts(js.job))):
        raise ValueError('review waits for tasks, launches and owner questions to settle')
    background = settlement.background_pending(board, js.job)
    if background:
        raise ValueError(background)
    from swarm.supervisor.command import workdir_problem
    why = workdir_problem(cfg, values['workdir'])
    if why:
        raise ValueError(why)
    return rec, js, found[0]


def _grant_name(cfg, values, root, pid):
    return 'late-review-grant-' + settlement.digest([store_key(cfg), values, root, pid]) + '.json'


def attest(cfg, payload, host):
    from swarm.drivegate import simple_call, parse_swarm
    from swarm.owner_create import root_request
    command = host.shell_command(payload) or ''
    if 'swarm' not in command:
        return False
    argv = simple_call(command)
    args = parse_swarm(argv) if argv is not None else None
    if args is None or args.cmd not in ('late-review', 'late-finalize'):
        return False
    values = fields(args)
    audit = root_request(cfg, payload, host, values['request_id'])
    from swarm.board import open_board
    with open_board(cfg) as b:
        authorize(b, cfg, values, payload.get('session_id'), agent=payload.get('agent_id'), host=host.name)
    pid = person.host_pid()
    call = payload.get('tool_use_id') or payload.get('tool_call_id')
    if not pid or not isinstance(call, str) or not call:
        raise ValueError('trusted native PID and tool-call identity required')
    record = dict(v=1, board=store_key(cfg), fields=values, audit=audit,
                  pid=pid, call=call, expires=time.time() + TTL)
    with enrolment._dir(create=True) as d:
        safefs.write_atomic(d, _grant_name(cfg, values, audit['session'], pid), json.dumps(record), durable=True)
    return True


def _take(board, cfg, args):
    values = fields(args)
    root = os.environ.get('CODEX_THREAD_ID')
    session = os.environ.get('CODEX_SESSION_ID')
    if not root or session != root:
        raise ValueError('late review requires the original Codex thread and session')
    rec, js, handoff = authorize(board, cfg, values, root)
    return values, root, rec, js, handoff


def save(cfg, record):
    with privfs.dir_fd(settlement.SUBDIR) as d:
        safefs.write_atomic(d, settlement._name(cfg, record['job']), json.dumps(record), durable=True)


def run(board, cfg, args):
    try:
        with enrolment.coordinator_lock(store_key(cfg), args.job), board._task_tx(args.job):
            values, root, rec, js, handoff = _take(board, cfg, args)
            if args.cmd == 'late-finalize':
                intent = settlement.load(cfg, args.job)
                intent['finalize_intent'] = dict(values, root=root)
                save(cfg, intent)
                final = board.finalize_late_review(args.job)
                if final is None:
                    raise ValueError('finalization changed concurrently; native proof remains pending')
                from swarm.dependencies import wake
                wake(cfg)
                print('native finalization recorded; historical failure preserved')
                return 0
            old = settlement.load(cfg, args.job)
            identity = dict(job=args.job, closure=settlement.closure(js), candidate=values['candidate'],
                req=values['req'], request_id=values['request_id'], root=root,
                handoff_id=handoff.id, failure_provenance=board.native_failure_provenance(args.job),
                workdir=values['workdir'])
            if old is not None:
                if any(old.get(k) != v for k, v in identity.items()):
                    raise ValueError('one immutable review intent already exists for this failure')
                print('closed review already delegated; no second executor')
                return 0
            name = 'late-review-' + values['closure'][:16]
            if board.get_launch(args.job, 1, name) is not None:
                raise ValueError('conflicting native launch; no review authority inferred')
            record = dict(identity, name=name, workdir=values['workdir'], model='gpt-6-astra',
                assignment=None, stopped=False)
            save(cfg, record)
            board.queue_launch(args.job, 1, name, role='judge', harness='codex', model=record['model'],
                workdir=values['workdir'], brief=brief(js, record), queued_by='native late review', if_absent=True)
        from swarm.dependencies import wake
        why = wake(cfg)
        if why:
            print('review delegated; reconciliation pending: ' + why)
        else:
            print('closed review delegated through one native claim; lifecycle preserved')
        return 0
    except (OSError, ValueError) as exc:
        from swarm.cli import term_safe
        print('swarm late-review: ' + term_safe(str(exc)), file=sys.stderr)
        return 1


def brief(js, rec):
    artifact = shlex.join(['swarm', 'verdict', '--job', js.job, '--scope', 'artifact',
                          '--artifact', rec['candidate'], 'met', '--details', '-'])
    goal = shlex.join(['swarm', 'verdict', '--job', js.job, '--scope', 'goal',
                      '--candidate', rec['candidate'], '--req', rec['req'], '--open', '0',
                      'met', '--details', '-'])
    return (f'Independently judge the entire original goal: {js.goal}\n'
            f'Exact candidate: {rec["candidate"]}\nRequirements digest: {rec["req"]}\n'
            'Historical failure is preserved. Do not activate/reopen/change lifecycle or start other workers. '
            'Inspect the complete final cleanup evidence and every original requirement. '
            'Only if every criterion is met, record BOTH the exact artifact acceptance and full-goal acceptance. '
            f'Use {artifact} with --reason explaining the artifact checks and the full report on stdin; '
            f'then {goal} with --reason explaining whole-goal acceptance and its full report on stdin. '
            'Otherwise record goal-scope not_met with candidate, req, open count, full report, reason and next steps. '
            'Never post FINALIZED: a separate authenticated executor finalizes after you stop. End after the verdict.')


def admitted(board, cfg, job, row, run=None):
    """Only the exact original-root review row may cross the closed-job launch fences."""
    rec = settlement.load(cfg, job)
    js = board.job_status(job)
    if not rec or not settlement.eligible(js) or rec.get('closure') != settlement.closure(js):
        return False
    from swarm.passcmd import handoff
    found = handoff(board, job)
    if not found or found[1] != rec.get('candidate') or found[0].id != rec.get('handoff_id'):
        return False
    try:
        task_authority.original_root(board, cfg, job, rec['root'])
    except (OSError, ValueError):
        return False
    if (row is None or row.job != job or row.name != rec.get('name') or row.generation != 1
            or row.role != 'judge' or row.harness != 'codex' or row.model != 'gpt-6-astra'
            or row.workdir != rec.get('workdir')
            or row.brief != brief(js, rec) or row.queued_by != 'native late review'):
        return False
    from swarm.supervisor.command import workdir_problem
    if workdir_problem(cfg, row.workdir):
        return False
    assigned = rec.get('assignment')
    if row.state == 'queued':
        return assigned is None and row.attempts == 0 and run is None
    if row.state != 'running' or not assigned:
        return False
    if (assigned.get('row_id') != row.id or assigned.get('launch_id') != row.launch_id
            or row.attempts != 1):
        return False
    if run is not None and (run.get('restart_id') != assigned.get('restart_id')
            or run.get('launch_id') != assigned.get('launch_id') or run.get('launch') != row.name
            or run.get('resume_of') != 'launch:' + row.launch_id):
        return False
    return True


def bind_start(board, cfg, row, restart, run):
    rec = settlement.load(cfg, row.job)
    if not rec or rec.get('assignment') is not None:
        raise ValueError('closed review already claimed')
    rec['assignment'] = dict(row_id=row.id, restart_id=restart.id, launch_id=restart.launch_id, agent_key=None)
    run['late_review'] = rec['closure']['sha']
    run['config'] = cfg.get('_config_path')
    save(cfg, rec)
    current = board.get_launch(row.job, row.generation, row.name)
    if not admitted(board, cfg, row.job, current, run):
        raise ValueError('closed-review assignment changed before native start')


def bind_thread(board, cfg, run, key):
    if not run.get('late_review'):
        return
    with enrolment.coordinator_lock(store_key(cfg), run['job']):
        return _bind_thread(board, cfg, run, key)


def _bind_thread(board, cfg, run, key):
    rec = settlement.load(cfg, run['job'])
    row = next((r for r in board.launches(run['job']) if r.launch_id == run.get('launch_id')), None)
    if not admitted(board, cfg, run['job'], row, run):
        raise ValueError('native review thread lost its exact private assignment')
    rec['assignment']['agent_key'] = key
    save(cfg, rec)


def record_stopped(cfg, run, outcome):
    with enrolment.coordinator_lock(store_key(cfg), run['job']):
        return _record_stopped(cfg, run, outcome)


def _record_stopped(cfg, run, outcome):
    """Runner-only positive proof survives runtime cleanup and listener restart."""
    from swarm.supervisor import runner
    from swarm.board import open_board
    if not runner.review_executor_stopped(run):
        return False
    rec = settlement.load(cfg, run['job'])
    assigned = (rec or {}).get('assignment') or {}
    if (not rec or run.get('late_review') != rec['closure']['sha']
            or assigned.get('restart_id') != run.get('restart_id')
            or assigned.get('launch_id') != run.get('launch_id')
            or assigned.get('agent_key') != run.get('agent_key')):
        return False
    with open_board(cfg) as b:
        restart = next((r for r in b.restarts(run['job']) if r.id == run['restart_id']), None)
        if (restart is None or restart.ended_at is None or restart.launch_id != run['launch_id']
                or restart.new_agent_key != run.get('agent_key') or restart.outcome != outcome):
            return False
    rec['stopped'] = True
    rec['stopped_restart_id'] = run['restart_id']
    save(cfg, rec)
    return True


def reconcile(board, cfg, job, starter):
    """Durable listener/pass retry; no second claim, auto lead, or lifecycle transition."""
    if settlement.load(cfg, job) is None:
        return None
    from swarm import passcmd
    with enrolment.coordinator_lock(store_key(cfg), job), board._task_tx(job):
        rec = settlement.load(cfg, job)
        js = board.job_status(job)
        if not rec or not settlement.eligible(js) or rec['closure'] != settlement.closure(js):
            return []
        row = board.get_launch(job, 1, rec['name'])
        if row is None and rec.get('assignment') is None:
            board.queue_launch(job, 1, rec['name'], role='judge', harness='codex', model=rec['model'],
                workdir=rec['workdir'], brief=brief(js, rec), queued_by='native late review', if_absent=True)
            row = board.get_launch(job, 1, rec['name'])
        actions = []
        if rec.get('assignment'):
            from swarm.supervisor import runner
            runner.reap(cfg, board, deadline=time.monotonic() + 5, job=job,
                        restart_id=rec['assignment']['restart_id'])
        if row and row.state == 'queued' and admitted(board, cfg, job, row):
            action = passcmd._start_one(board, cfg, job, row, passcmd.session_usd_cap(cfg), starter)
            if action:
                actions.append(action)
        passcmd._settle(board, job, cfg)
        if rec.get('finalize_intent'):
            board.finalize_late_review(job)
        if board.settle_late_review(job):
            board.set_job_data(job, 'late_review.pending', None)
            actions.append(job + ': audited full-goal completion recorded; historical failure preserved')
        else:
            board.set_job_data(job, 'late_review.pending', settlement.pending_reason(board, cfg, job))
        return actions
