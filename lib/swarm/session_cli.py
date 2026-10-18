"""Explicit coordinator reassignment, independent of activation and job lifecycle."""
from __future__ import annotations

import os
import sys

from swarm import cli, enrolment, hosts
from swarm.board import open_board
from swarm.board.autoinit import store_key
from swarm.jobmarkers import JOB_NAME


def is_enrolled_session(cfg: dict, session_id: str | None) -> bool:
    """Host-private identity check shared with the hook, whose payload survives shell env wrappers."""
    if not session_id:
        return False
    bk = store_key(cfg)
    return session_id in enrolment.agent_sessions(bk) or enrolment.find(bk, session_id) is not None


def run(cfg: dict, args) -> int:
    try:
        return _run(cfg, args)
    except BlockingIOError:
        print('swarm session: another coordinator update is in progress; retry', file=sys.stderr)
        return 1
    except (OSError, ValueError) as exc:
        print(f'swarm session: {cli.term_safe(str(exc))}', file=sys.stderr)
        return 1


def _run(cfg: dict, args) -> int:
    def refuse(reason, rc=1):
        print(f'swarm session: {cli.term_safe(reason)}', file=sys.stderr)
        return rc

    if not JOB_NAME.fullmatch(args.job or ''):
        return refuse('invalid --job', 2)
    if args.session is not None and not cli.valid_session_id(args.session):
        return refuse('invalid --session: use a plain session id, at most 128 characters', 2)
    if args.clear and args.no_routing:
        return refuse('--clear already preserves ownership and routing', 2)
    bk = store_key(cfg)
    callers = {hosts.cli_session_id(os.environ), os.environ.get('CODEX_THREAD_ID'),
               os.environ.get('CODEX_SESSION_ID'), os.environ.get('CLAUDE_CODE_CHILD_SESSION')} - {None, ''}
    if any(is_enrolled_session(cfg, caller) for caller in callers):
        return refuse('an enrolled agent cannot reassign its coordinator')

    if args.session and is_enrolled_session(cfg, args.session):
        return refuse('an enrolled agent session cannot become the coordinator')

    with enrolment.coordinator_lock(bk, args.job), open_board(cfg) as board:
        if args.session and board.route(args.session).member_job is not None:
            return refuse('a known worker session cannot become the coordinator')
        if any(board.route(caller).member_job is not None for caller in callers):
            return refuse('a known worker cannot reassign its coordinator')
        before = board.job_status(args.job)
        if before is None:
            return refuse(f'job {args.job} does not exist')
        old = before.session_id
        records, markers = [], []
        try:
            if not args.clear:
                records = enrolment.coordinator_records(bk, args.job)
                if not args.no_routing:
                    marker = cli._marker(cfg, args.job)
                    others = [cli._read_marker(p) for p in marker.parent.glob('*.json')]
                    if any(m.get('session_id') == args.session and m.get('job') != args.job for m in others):
                        return refuse('target session already routes another job; use --no-routing for a shared pane')
                    # Child/worker attachments retain their sessions. Move only the primary
                    # coordinator marker, including recovery after a binding-only update.
                    data = cli._read_marker(marker)
                    if data.get('job') == args.job and 'resume' not in data and not data.get('attached'):
                        markers.append((marker, data))
            if not board.reassign_job_session(args.job, args.session, expected_session=old):
                return refuse('coordinator binding changed concurrently; retry after inspecting it')
            try:
                if not args.clear:
                    enrolment.reassign_coordinator_records(records, args.session)
                    for path, data in markers:
                        with cli.locked_marker(path, cli.MARKER_REMOVE_WAIT) as fh:
                            if fh is None or cli._read_marker(path) != data:
                                raise ValueError('coordinator marker changed concurrently')
                            cli._write_marker(path, {**data, 'session_id': args.session})
            except (OSError, ValueError):
                # Compensate ordinary local write failures. Files and the remote board cannot
                # form one crash-atomic transaction; rerunning the command repairs a crash.
                if board.reassign_job_session(args.job, old, expected_session=args.session):
                    for name, rec in records:
                        enrolment._store(rec, name)
                raise
        except (OSError, ValueError) as exc:
            return refuse(str(exc))
    if args.clear:
        print(f'{args.job}: pane binding cleared; ownership and routing unchanged')
    else:
        owner = 'updated existing local owner' if any(rec.kind == 'owner' for _, rec in records) else 'local owner absent; execution authority was not created'
        routing = 'hook routing unchanged' if args.no_routing else f'coordinator markers updated: {len(markers)}'
        print(f'{args.job}: session {old or "unbound"} -> {args.session}; {owner}; {routing}')
        print('workers remain in their original sessions; lifecycle and history preserved')
    return 0
