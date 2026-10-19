"""Host-private exact child spawn receipts. Board route rows are only a cache."""
from __future__ import annotations

import hashlib
import json

from swarm import enrolment, safefs
from swarm.board.autoinit import store_key
from swarm.hosts import codex


_FIELDS = {'v', 'board', 'root', 'parent', 'child', 'job', 'tool_call', 'native'}


def _name(board: str, root: str, child: str) -> str:
    digest = hashlib.sha256('\0'.join((board, root, child)).encode()).hexdigest()
    return f'spawn-{digest}.json'


def _native(child: str, root: str) -> dict | None:
    meta = codex._first_meta(codex.find_rollout(child))
    parent = codex._dig(meta.get('source'), ('subagent', 'thread_spawn', 'parent_thread_id'))
    path = codex._dig(meta.get('source'), codex.CODEX_AGENT_PATH)
    if meta.get('id') != child or not isinstance(parent, str) or not isinstance(path, str):
        return None
    if not codex._root_ancestor(parent, root):
        return None
    return {'id': child, 'parent': parent, 'path': path}


def write(cfg: dict, payload: dict, child: str, job: str) -> bool:
    """Called only by the trusted PostToolUse hook after resolving the actual spawn result."""
    root = payload.get('session_id')
    parent = payload.get('agent_id') or root
    call = payload.get('tool_use_id')
    if not all(isinstance(v, str) and v for v in (root, parent, call, child, job)):
        return False
    native = _native(child, root)
    if native is None or native['parent'] != parent:
        return False
    board = store_key(cfg)
    receipt = dict(v=1, board=board, root=root, parent=parent, child=child,
                   job=job, tool_call=call, native=native)
    with enrolment._dir(create=True) as directory:
        safefs.write_atomic(directory, _name(board, root, child), json.dumps(receipt), durable=True)
    return True


def read(cfg: dict, root: str, child: str) -> dict | None:
    """Validate receipt scope and the current native identity before permitting enrollment."""
    board = store_key(cfg)
    try:
        with enrolment._dir(create=False) as directory:
            body = safefs.read(directory, _name(board, root, child), 8192)
        receipt = json.loads(body) if body else None
        if not isinstance(receipt, dict) or set(receipt) != _FIELDS or receipt['v'] != 1:
            return None
        if (receipt['board'], receipt['root'], receipt['child']) != (board, root, child):
            return None
        if not all(isinstance(receipt[k], str) and receipt[k] and len(receipt[k]) <= 1024
                   for k in ('board', 'root', 'parent', 'child', 'job', 'tool_call')):
            return None
        native = _native(child, root)
        if native is None or native != receipt['native'] or native['parent'] != receipt['parent']:
            return None
        return receipt
    except (OSError, ValueError, TypeError, KeyError):
        return None
