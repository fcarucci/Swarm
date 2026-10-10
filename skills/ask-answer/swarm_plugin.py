"""Structured decisions on generic blockers; full payloads live in per-job plugin data."""
from __future__ import annotations
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
from swarm import addressing, cli, hosts
from swarm.board.base import JOB_DATA_VALUE_MAX, normalize_message


def question_data(ctx, board, blocker):
    data = ctx.job_data(board, blocker.job)
    prefix = f'q{blocker.id}'
    count = int(data.get(prefix + '.count', '0'))
    return json.loads(''.join(data[f'{prefix}.{i}'] for i in range(count))) if count else {}


def save_question(ctx, board, blocker, data):
    # Keep the schema-16 per-value bound without imposing the board's message cap on questions.
    raw = json.dumps(data, ensure_ascii=False)
    old = int(ctx.job_data(board, blocker.job).get(f'q{blocker.id}.count', '0'))
    pieces = [raw[i:i+JOB_DATA_VALUE_MAX] for i in range(0, len(raw), JOB_DATA_VALUE_MAX)]
    for i, piece in enumerate(pieces):
        ctx.set_job_data(board, blocker.job, f'q{blocker.id}.{i}', piece)
    for i in range(len(pieces), old):
        ctx.set_job_data(board, blocker.job, f'q{blocker.id}.{i}', None)
    ctx.set_job_data(board, blocker.job, f'q{blocker.id}.count', str(len(pieces)))


def identity(board, cfg=None, key=None):
    if key:   # a joined agent names itself: hooks bind sessions to agents, the directory edition has none
        name = board.active_agent_name(key)
        if name is None:
            raise ValueError(f'no active agent has the key {key!r}: join the job first '
                             f'(swarm join --job JOB --key KEY --role ROLE)')
        return name   # never 'human'
    sid = hosts.cli_session_id(os.environ)
    if sid:
        for job in board.jobs(True):
            for agent in board.agents(job.job, include_departed=False):
                if board.route(agent.agent_key).session_id == sid:
                    return agent.name
        # An activated root coordinator records answers on the human's behalf.
        # Both the local trusted activation marker and the board session must agree;
        # an unknown child thread has neither identity nor that activation binding.
        if not os.environ.get('CLAUDE_CODE_CHILD_SESSION') and cfg:
            marker_dir = Path((cfg.get('hook') or {}).get('marker_dir', '')).expanduser()
            for path in sorted(marker_dir.glob('*.json')) if marker_dir.is_dir() else ():
                marker = cli._read_marker(path)
                if marker.get('session_id') == sid and marker.get('job'):
                    job = board.job_status(marker['job'])
                    if job and job.status == 'active' and job.session_id == sid:
                        return 'human'
        # An orchestrator is the human's interface, but an unregistered child must not impersonate it.
        if os.environ.get('CLAUDE_CODE_CHILD_SESSION') or os.environ.get('CODEX_THREAD_ID'):
            raise ValueError('agent session has no active board identity; join the job first')
    return 'human'


def authorized(board, blocker, actor):
    if actor == 'human':
        return True
    if blocker.waiting_on == 'human':
        return False
    try:
        return actor in addressing.resolve(board, blocker.job, blocker.waiting_on)
    except ValueError:
        return False


def open_question(ctx, board, job, actor, to, text, options=None, default=None, expires=None, blocks=None):
    if not text.strip():
        raise ValueError('question must be nonempty')
    if to != 'human':
        addressing.resolve(board, job, to)
    if actor != 'human':
        addressing.check_author(board, job, actor)
    options = options or []
    if options and (any(not x.strip() for x in options) or len(set(options)) != len(options)):
        raise ValueError('options must be distinct nonempty values')
    if options and default is not None and default not in options:
        raise ValueError('default must be one of the options')
    until = None
    if expires:
        epoch = cli.wait_deadline(None, expires, board.now().timestamp())
        until = dt.datetime.fromtimestamp(epoch, dt.timezone.utc)
    # Serialize payload and blocker mutations with the same lock as core resolution/close guards.
    with board._blocker_lock(job):
        b = board.open_blocker(job, 'question', to, text, until=until,
                               default_value=default, created_by=actor)
        save_question(ctx, board, b, {'text': text, 'options': options, 'default': default,
                                     'blocks': blocks, 'asked_by': actor})
    pointer = f' (answer: swarm answer {b.id} ...)'
    head = f'Q{b.id} -> {to}: '
    cap = board.message_cap()
    if len(head) + len(pointer) >= cap:
        head, _ = normalize_message(head, cap - len(pointer))
        summary = ''
    else:
        summary, _ = normalize_message(text, cap - len(head) - len(pointer))
    board.post(job, 'swarm', head + summary + pointer)
    return b


def answer_question(ctx, board, id, actor, text=None, option=None, use_default=False,
                    comment=None, reopen=None):
    b = board.blocker(id)
    if b is None or b.kind != 'question':
        raise ValueError(f'no question Q{id}')
    with board._blocker_lock(b.job):
        b = board.blocker(id)
        data = question_data(ctx, board, b)
        choices = [text is not None, option is not None, use_default, reopen is not None]
        if sum(choices) > 1:
            raise ValueError('give one answer: text, --option, --default or --reopen')
        answering = any(choices)
        if not answering and not comment:
            raise ValueError('give an answer or --comment')
        if answering and not authorized(board, b, actor):
            raise ValueError(f'Q{id} is addressed to {b.waiting_on}; you may only comment')
        value = reopen if reopen is not None else option if option is not None else text
        if use_default:
            value = data.get('default', b.default_value)
            if value is None:
                raise ValueError(f'Q{id} has no default')
        if answering and (not isinstance(value, str) or not value.strip()):
            raise ValueError('answer must be nonempty')
        if option is not None and option not in data.get('options', []):
            raise ValueError('option must be one of: ' + ', '.join(data.get('options', [])))
        if answering:
            if b.state != 'open':
                if reopen is None:
                    raise ValueError(f'Q{id} is already {b.state}; use --reopen TEXT to correct it')
                board.reopen_blocker(id, value, actor=actor)
            elif reopen is not None:
                raise ValueError('only an answered or expired question can be reopened')
        if comment:
            board.comment_blocker(id, comment, actor=actor)
        if answering:
            board.resolve_blocker(id, value, actor=actor)
    return board.blocker(id)


def bookkeeping(board, b, text):
    # Expiry continues while paused: these are core bookkeeping, not agent work.
    message, _ = normalize_message(text, board.message_cap())
    board._insert_message(b.job, 'swarm', message, b.created_by if b.created_by != 'human' else None, None)


def event_hook(ctx, event):
    with ctx.open_board() as board:
        b = board.blocker(event.blocker)
        if b is None or b.kind != 'question':
            return
        if event.event in ('resolved', 'expired'):
            data = question_data(ctx, board, b)
            if data:
                data.update(answer=event.detail, answered_by=event.actor, answer_event=event.id,
                            assumed=event.event == 'expired')
                save_question(ctx, board, b, data)
            label = 'default applied' if event.event == 'expired' else f'answered by {event.actor}'
            bookkeeping(board, b, f'Q{b.id} {label}: {event.detail} (full answer: swarm questions --job {b.job} --all)')
        mapping = {'opened':'opened', 'resolved':'answered', 'expired':'expired', 'overdue':'overdue'}
        if event.event in mapping:
            notify(ctx, b, mapping[event.event])


def agent_lines(ctx, job, agent_key):
    with ctx.open_board() as board, board._blocker_lock(job):
        name = board.active_agent_name(agent_key)
        out = []
        delivery = 'delivered.' + hashlib.sha256(agent_key.encode()).hexdigest()[:32]
        last = int(ctx.job_data(board, job).get(delivery, '0'))
        newest = last
        for b in board.blockers(job, include_closed=True):
            if b.kind != 'question' or b.created_by != name:
                continue
            data = question_data(ctx, board, b)
            event_id = int(data.get('answer_event', 0))
            if event_id > last:
                label = 'default applied' if data.get('assumed') else f'answered by {data.get("answered_by")}'
                out.append(f'[swarm question] Q{b.id} {label}: {data.get("answer")}')
                newest = max(newest, event_id)
        if newest != last:
            ctx.set_job_data(board, job, delivery, str(newest))
        return out


def question_rows(ctx, board, job=None, all_=False, to=None):
    rows = [b for b in board.blockers(job, include_closed=all_) if b.kind == 'question']
    return [b for b in rows if to is None or (b.waiting_on == 'human' if to == 'human' else authorized(board, b, to))]


def age(now, at):
    return f'{max(0, int((now-at).total_seconds()/60))}m'


def orchestrator_lines(ctx, job):
    with ctx.open_board() as board:
        rows = [b for b in question_rows(ctx, board, job) if b.waiting_on == 'human']
        if not rows:
            return []
        return [f'{len(rows)} open question{"s" if len(rows)!=1 else ""} for you '
                f'(oldest {age(board.now(), rows[0].created_at)}): ' + ' '.join(f'Q{b.id}' for b in rows)]


def watch_pane(ctx, job):
    with ctx.open_board() as board:
        rows = question_rows(ctx, board, job)
        if not rows:
            return []
        now = board.now()
        out = ['QUESTIONS  id  to  age  default  time left']
        for b in rows:
            left = '-' if b.until is None else 'OVERDUE' if b.until <= now else age(b.until, now)
            out.append(f'Q{b.id} {b.waiting_on} {age(now,b.created_at)} '
                       f'{"yes" if b.default_value is not None else "no"} {left}')
        return out


def notify_error_path(ctx):
    return ctx.config_dir / 'question-notify-errors.log'


def record_error(path, text):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('a', encoding='utf-8') as f:
            f.write(text.replace('\n', ' ') + '\n')
    except OSError:
        print('swarm question: notification failed; error log is unwritable', file=sys.stderr)


def notification_argv(command, values):
    # Split before substitution: one template token stays one argv entry even for shell syntax.
    tokens = shlex.split(command, posix=os.name != 'nt')
    if os.name == 'nt':
        tokens = [token[1:-1] if len(token) >= 2 and token[0] == token[-1] == '"' else token
                  for token in tokens]
    return [token.format_map(values) for token in tokens]


def notify(ctx, b, event):
    command = (ctx.cfg.get('notify') or {}).get('on_question')
    if not command:
        return
    values = {'event':event, 'job':b.job, 'id':str(b.id), 'to':b.waiting_on,
              'summary': ' '.join(b.reason.split())[:160]}
    try:
        argv = notification_argv(command, values)
        if not argv:
            raise ValueError('empty notification command')
        env = os.environ.copy()
        env.update({f'SWARM_{key.upper()}':value for key,value in values.items()})
        # Pass configured command content through the environment, never command-line arguments.
        env['SWARM_QUESTION_NOTIFY_PAYLOAD'] = json.dumps({'argv':argv, 'timeout':5})
        kwargs = {'start_new_session':True} if os.name != 'nt' else {
            'creationflags':subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS}
        subprocess.Popen([sys.executable, '-B', str(Path(__file__).with_name('notify_runner.py')),
                          str(notify_error_path(ctx))], env=env, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True, **kwargs)
    except Exception as exc:
        record_error(notify_error_path(ctx), f'Q{b.id} {event}: notification launch failed ({type(exc).__name__})')


def doctor(ctx, args):
    path = notify_error_path(ctx)
    if path.is_file():
        print('question notifications: failures recorded in ' + str(path))
        for line in path.read_text(encoding='utf-8').splitlines()[-3:]:
            print('  ' + cli.term_safe(line))


def run_ask(ctx, args):
    with ctx.open_board() as board:
        b = open_question(ctx, board, args.job, identity(board, ctx.cfg, getattr(args, 'key', None)), args.to, ' '.join(args.text),
                          options=args.options.split(',') if args.options else None,
                          default=args.default, expires=args.expires, blocks=args.blocks)
        print(f'Q{b.id}')


def run_answer(ctx, args):
    with ctx.open_board() as board:
        b = answer_question(ctx, board, args.id, identity(board, ctx.cfg, getattr(args, 'key', None)),
                            text=' '.join(args.text) if args.text else None, option=args.option,
                            use_default=args.default, comment=args.comment, reopen=args.reopen)
        print(f'Q{b.id} {b.state}')


def run_questions(ctx, args):
    with ctx.open_board() as board:
        to = identity(board, ctx.cfg, getattr(args, 'key', None)) if args.to == 'me' else args.to
        for b in question_rows(ctx, board, args.job, args.all, to):
            data = question_data(ctx, board, b)
            print(cli.term_safe(f'Q{b.id} [{b.state}] -> {b.waiting_on}: {data.get("text", b.reason)}'))
            if data.get('blocks'):
                print(cli.term_safe('  blocks: ' + data['blocks']))
            if data.get('options'):
                print(cli.term_safe('  options: ' + ', '.join(data['options'])))
            if b.default_value is not None:
                print(cli.term_safe('  default: ' + b.default_value))
            if b.state != 'open':
                print(cli.term_safe(f'  answer by {b.resolved_by}: {b.resolved_how}'))
            elif b.until and b.until <= board.now():
                print('  OVERDUE')
            for event in board.blocker_events(b.id):
                if event.event == 'commented':
                    print(cli.term_safe(f'  comment by {event.actor}: {event.detail}'))


def setup_ask(p):
    p.add_argument('--job', required=True)
    p.add_argument('--key', help='your agent key, when you joined with `swarm join --key`')
    p.add_argument('--to', required=True)
    p.add_argument('text', nargs='+')
    for name in ('options','default','expires','blocks'):
        p.add_argument('--' + name)


def setup_answer(p):
    p.add_argument('id', type=int)
    p.add_argument('--key', help='your agent key, when you joined with `swarm join --key`')
    p.add_argument('text', nargs='*')
    p.add_argument('--option')
    p.add_argument('--default', action='store_true')
    p.add_argument('--comment')
    p.add_argument('--reopen', metavar='TEXT')


def setup_questions(p):
    p.add_argument('--job')
    p.add_argument('--key', help='your agent key, when you joined with `swarm join --key`')
    group = p.add_mutually_exclusive_group()
    group.add_argument('--open', action='store_true')
    group.add_argument('--all', action='store_true')
    p.add_argument('--to')


def guard_core_resolution(ctx, args):
    if args.bcmd != 'resolve':
        return
    with ctx.open_board() as board:
        b = board.blocker(args.id)
        if b and b.kind == 'question' and not authorized(board, b, identity(board, ctx.cfg)):
            print(f'Q{b.id} is addressed to {b.waiting_on}; you may only comment', file=sys.stderr)
            return 1


def register(api):
    api.add_command('ask', run_ask, setup_ask, 'ask a person or role for a decision')
    api.add_command('answer', run_answer, setup_answer, 'answer or comment on a question')
    api.add_command('questions', run_questions, setup_questions, 'list structured questions')
    api.register_blocker_kind('question', protection='always', display=lambda ctx,b: f'Q{b.id} -> {b.waiting_on}: {b.reason}')
    api.add_blocker_event_hook(event_hook)
    api.add_watch_pane(watch_pane)
    api.add_orchestrator_lines(orchestrator_lines)
    api.add_agent_lines(agent_lines)
    api.extend_command('doctor', before=doctor)
    api.extend_command('blocker', before=guard_core_resolution)
