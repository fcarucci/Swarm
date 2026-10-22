"""Resolve task supersession to the exact live actor and current job run."""
import os

from swarm.board.base import run_start

def original_root(board, cfg, job, root):
    """Match the original coordinator session recorded on the job."""
    js = board.job_status(job)
    if (js is None or not isinstance(root, str) or not root
            or js.session_id != root):
        raise ValueError('an actual original root coordinator is required')
    return root


def _authorize(board, cfg, args, host, root, agent_key):
    js = board.job_status(args.job)
    if js is None or js.status != 'active':
        raise ValueError('task supersession requires the active job')
    start = run_start(js)
    if agent_key:
        member = next((a for a in board.agents(args.job) if a.agent_key == agent_key and a.ended_at is None), None)
        route = board.route(agent_key)
        if (member is None or member.joined_at < start or route.member_job != args.job
                or route.session_id != root):
            raise ValueError('task supersession requires the exact active job actor')
        actor = member.name
    else:
        original_root(board, cfg, args.job, root)
        actor = root
    if args.actor is not None and args.actor != actor:
        raise ValueError('--as cannot impersonate a different supersession actor')
    return actor


def take(board, cfg, args):
    from swarm import hosts
    host = hosts.get(hosts.detect_cli_host(os.environ))
    if host.name == 'codex':
        caller = os.environ.get('CODEX_THREAD_ID')
        root = os.environ.get('CODEX_SESSION_ID')
        if not caller or not root:
            raise ValueError('task supersession requires the current Codex thread and session')
        return _authorize(board, cfg, args, host, root, None if caller == root else caller)
    session = hosts.cli_session_id(os.environ)
    if not session:
        raise ValueError('task supersession requires the current host session')
    return _authorize(board, cfg, args, host, session, None)
