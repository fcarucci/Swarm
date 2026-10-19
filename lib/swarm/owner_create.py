"""Root-requester CREATE-NEW capability; existing lifecycle operations remain forbidden.

A real user message is retained as provenance, not interpreted as an NLP approval.
The trusted root coordinator has the delegated creation capability. Workers never do.
"""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import sys
import time

from swarm import enrolment, hosts, person, safefs
from swarm.board import open_board
from swarm.board.autoinit import store_key
from swarm.jobmarkers import JOB_NAME

TTL = 120
LIMIT = 128 * 1024


def configure(sub):
    p = sub.add_parser('create', help='root requester creates a NEW job; never changes an existing job')
    p.add_argument('--job', required=True)
    p.add_argument('--goal', required=True)
    p.add_argument('--task', default='')
    p.add_argument('--description', default='')
    p.add_argument('--workdir', required=True)
    p.add_argument('--request-id', required=True, help='actual root user message id, for provenance (not NLP authorization)')


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _fields(args):
    if not JOB_NAME.fullmatch(args.job or ''):
        raise ValueError('invalid new job name')
    for name, maximum in [('goal',12000),('task',12000),('description',1000),('request_id',256)]:
        value = getattr(args,name)
        if not isinstance(value,str) or len(value)>maximum or '\x00' in value or value=='-':
            raise ValueError('invalid '+name+'; inline text is required')
    if not args.goal.strip() or not args.request_id.strip():
        raise ValueError('goal and request id are required')
    path = Path(args.workdir)
    if not path.is_absolute() or not path.is_dir():
        raise ValueError('workdir must be an existing absolute directory')
    return dict(job=args.job,goal=args.goal,task=args.task,description=args.description,
                workdir=os.path.realpath(path),request_id=args.request_id)


def root_request(cfg, payload, host, request_id):
    """Read actual root provenance; reject nested/replacement sessions and synthetic context."""
    from swarm.session_cli import is_enrolled_session
    sid=payload.get('session_id')
    if host.name!='codex' or payload.get('agent_id') or not isinstance(sid,str):
        raise ValueError('only a trusted Codex root requester can create a job')
    if is_enrolled_session(cfg,sid):
        raise ValueError('a worker cannot create a job')
    from swarm.hosts.codex import find_rollout, read_rollout, CONTEXT_PREFIXES
    path=find_rollout(sid)
    if path is None:
        raise ValueError('root rollout unavailable')
    entries=[json.loads(line) for line in read_rollout(path).splitlines() if line.strip()]
    meta=entries[0].get('payload',{})
    if meta.get('id')!=sid or meta.get('source')!='cli' or meta.get('forked_from_id'):
        raise ValueError('creation requires the original interactive root')
    for item in reversed(entries):
        p=item.get('payload',{})
        if item.get('type')!='response_item' or p.get('id')!=request_id:
            continue
        if p.get('type')!='message' or p.get('role')!='user':
            break
        text='\n'.join(c.get('text','') for c in p.get('content',[]) if c.get('type')=='input_text')
        if not text.strip() or text.lstrip().startswith((*CONTEXT_PREFIXES,'<codex_internal_context','<send_user_message_question_reply>')):
            break
        return {'session':sid,'request_id':request_id,'text_sha':hashlib.sha256(text.encode()).hexdigest()}
    raise ValueError('request id must reference an actual root user message')


def _grant_key(cfg, fields, session, pid):
    return 'create-grant-'+_digest([store_key(cfg),fields,session,pid])+'.json'


def _command(payload,host):
    from swarm.drivegate import simple_call,parse_swarm
    command=host.shell_command(payload) or ''
    argv=simple_call(command)
    if argv is None:return None
    parsed=parse_swarm(argv)
    return parsed if parsed is not None and parsed.cmd=='create' else None


def attest(cfg,payload,host):
    """Called only by the real PreToolUse hook, before any marker fast path."""
    args=_command(payload,host)
    if args is None:return False
    if payload.get('agent_id'):
        raise ValueError('workers cannot create jobs')
    fields=_fields(args)
    audit=root_request(cfg,payload,host,args.request_id)
    pid=person.host_pid()
    call=payload.get('tool_use_id') or payload.get('tool_call_id')
    if not pid or not isinstance(call,str) or not call:
        raise ValueError('trusted host process and tool-call identity are required')
    name=_grant_key(cfg,fields,audit['session'],pid)
    record={'v':1,'board':store_key(cfg),'fields':fields,'audit':audit,'pid':pid,
            'call':call,'expires':time.time()+TTL}
    with enrolment._dir(create=True) as directory:
        safefs.write_atomic(directory,name,json.dumps(record),durable=True)
    return True


def _take(cfg,args):
    fields=_fields(args)
    sid=os.environ.get('CODEX_THREAD_ID')
    pid=person.host_pid()
    if not sid or not pid:
        raise ValueError('a trusted root hook grant is required')
    name=_grant_key(cfg,fields,sid,pid)
    with enrolment._dir(create=True) as directory:
        raw=safefs.read(directory,name,LIMIT)
        if raw is None:raise ValueError('a trusted root hook grant is required')
        record=json.loads(raw)
        if (record.get('v')!=1 or record.get('fields')!=fields or record.get('board')!=store_key(cfg)
                or record.get('audit',{}).get('session')!=sid or record.get('pid')!=pid
                or not time.time()<record.get('expires',0)<=time.time()+TTL):
            raise ValueError('invalid or expired creation grant')
        safefs.unlink(directory,name)
    return fields,record['audit']


def run(cfg,args):
    from swarm import cli
    try:
        with enrolment.coordinator_lock(store_key(cfg),args.job):
            fields,audit=_take(cfg,args)
            request=_digest([audit['session'],audit['request_id']])
            request_hash=_digest([fields,audit])
            with open_board(cfg) as board:
                receipt=board.create_job_once(args.job,args.description,args.task,audit['session'],'owner requester',
                    goal=args.goal,request_id=request,request_hash=request_hash)
                current=board.job_status(args.job)
                # A retry after completion or purge only returns the immutable receipt.
                if current is not None and current.status=='active' and current.created_at.isoformat()==receipt['created_at']:
                    bk=store_key(cfg)
                    owner=enrolment.find_job_owner(bk,args.job)
                    if owner is None:
                        enrolment.write_job_owner(bk,job=args.job,harness='codex',session_id=audit['session'],cwd=fields['workdir'])
                    enrolment.write_job(bk,job=args.job,harness='codex',session_id=audit['session'],cwd=fields['workdir'])
                    marker=cli._marker(cfg,args.job)
                    data={'job':args.job,'session_id':audit['session'],'cwd':fields['workdir'],'goal':True,'create_request':request}
                    if marker.exists() and cli._read_marker(marker)!=data:
                        raise ValueError('existing marker conflicts with creation receipt')
                    cli._write_marker(marker,data)
        print(f'{args.job}: creation receipt {request[:12]}; existing lifecycle preserved')
        return 0
    except (OSError,ValueError,BlockingIOError) as exc:
        print('swarm create: '+cli.term_safe(str(exc)),file=sys.stderr)
        return 1
