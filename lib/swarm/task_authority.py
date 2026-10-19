"""Host-private, exact-command authority for root/EL task supersession.

Only the trusted pre-tool hook mints grants from its actual agent context. Shell environment
and mutable board role labels alone cannot authorize this obligation-changing command.
"""
import hashlib
import json
import os
import time

from swarm import enrolment, person, safefs
from swarm.board.autoinit import store_key
from swarm.board.base import run_start

TTL = 90
LIMIT = 65536
FIELDS = ('job', 'name', 'generation', 'artifact', 'replacement', 'replacement_generation',
          'replacement_artifact', 'reason', 'actor')


def _fields(args):
    return {key:getattr(args, key, None) for key in FIELDS}


def _name(cfg, fields, caller, pid):
    digest = hashlib.sha256(json.dumps([store_key(cfg), fields, caller, pid], sort_keys=True).encode()).hexdigest()
    return 'task-supersede-grant-' + digest + '.json'


def _authorize(board, cfg, args, host, root, agent_key):
    from swarm import codex_bindings
    from swarm.hosts import codex
    from swarm.session_cli import is_enrolled_session
    if host.name != 'codex' or not isinstance(root, str) or not root:
        raise ValueError('task supersession requires an actual trusted Codex root or engineering lead')
    js = board.job_status(args.job)
    if js is None or js.status != 'active':
        raise ValueError('task supersession requires the existing active job')
    bk, start = store_key(cfg), run_start(js)
    if agent_key:
        rec = enrolment.find(bk, agent_key)
        member = next((a for a in board.agents(args.job) if a.agent_key == agent_key and a.ended_at is None), None)
        proof = codex_bindings.read(cfg, root, agent_key)
        path = proof.get('native', {}).get('path', '') if proof else ''
        if (rec is None or rec.job != args.job or rec.harness != 'codex' or rec.session_id != root or rec.created_at < start.timestamp()
                or member is None or member.joined_at < start or proof is None or proof.get('job') != args.job
                or codex._role_of_name(path.rsplit('/', 1)[-1]) != 'engineering_lead'):
            raise ValueError('task supersession requires the actual enrolled native engineering lead')
        caller, actor = agent_key, member.name
    else:
        rec = enrolment.find_job(bk, args.job) or enrolment.find_job_owner(bk, args.job)
        meta = codex._first_meta(codex.find_rollout(root))
        if (rec is None or rec.harness != 'codex' or rec.session_id != root
                or rec.created_at < start.timestamp() or enrolment.job_routing_disabled(rec)
                or is_enrolled_session(cfg, root) or meta.get('id') != root
                or meta.get('source') != 'cli' or meta.get('forked_from_id')):
            raise ValueError('task supersession requires the actual original root for this job run')
        caller, actor = root, root
    if args.actor is not None and args.actor != actor:
        raise ValueError('--as cannot impersonate a different supersession actor')
    return caller, actor, start.isoformat()


def attest(cfg, payload, host):
    from swarm.drivegate import simple_call, parse_swarm
    command = host.shell_command(payload) or ''
    if 'swarm' not in command:
        return False
    argv = simple_call(command)
    args = parse_swarm(argv) if argv is not None else None
    if args is None or args.cmd != 'task' or args.task_cmd != 'supersede':
        return False
    from swarm.board import open_board
    with open_board(cfg) as board:
        caller, actor, run = _authorize(board, cfg, args, host, payload.get('session_id'), payload.get('agent_id'))
    pid = person.host_pid()
    call = payload.get('tool_use_id') or payload.get('tool_call_id')
    if not pid or not isinstance(call, str) or not call:
        raise ValueError('trusted host process and tool-call identity required')
    fields = _fields(args)
    record = dict(v=1, board=store_key(cfg), fields=fields, caller=caller, actor=actor,
                  run=run, root=payload.get('session_id'), agent_key=payload.get('agent_id'),
                  pid=pid, call=call, expires=time.time()+TTL)
    with enrolment._dir(create=True) as directory:
        safefs.write_atomic(directory, _name(cfg, fields, caller, pid), json.dumps(record), durable=True)
    return True


def take(board, cfg, args):
    caller, pid = os.environ.get('CODEX_THREAD_ID'), person.host_pid()
    if not caller or not pid:
        raise ValueError('a trusted task supersession hook grant is required')
    fields = _fields(args)
    name = _name(cfg, fields, caller, pid)
    with enrolment._dir(create=True) as directory:
        raw = safefs.read(directory, name, LIMIT)
        if raw is None:
            raise ValueError('a trusted task supersession hook grant is required')
        value = json.loads(raw)
        now = time.time()
        current = board.job_status(args.job)
        if (value.get('v') != 1 or value.get('board') != store_key(cfg) or value.get('fields') != fields
                or value.get('caller') != caller or value.get('pid') != pid
                or not now < value.get('expires', 0) <= now+TTL or current is None
                or value.get('run') != run_start(current).isoformat()):
            raise ValueError('invalid or expired task supersession grant')
        from swarm import hosts
        authorized = _authorize(board, cfg, args, hosts.get('codex'), value.get('root'), value.get('agent_key'))
        if authorized != (caller, value.get('actor'), value.get('run')):
            raise ValueError('task supersession authority changed before command execution')
        safefs.unlink(directory, name)
    return value['actor']
