"""Storage for the swarm message board. See base.py for the interface contract.

    from swarm.board import ensure_initialized, open_board, setup_board
    ensure_initialized(cfg)             # sets the storage up if needed (cheap once done)
    with open_board(cfg) as board:      # raises BoardUnavailable if storage is unreachable
        board.post(job, name, text)

The backend is cfg["board"]["backend"] (default "file"; see board_backend). Backend modules are imported
lazily: the hooks run on every tool call, and psycopg must only be imported when the Postgres
backend is actually chosen.
"""
from __future__ import annotations

import importlib
from typing import Mapping, Sequence

from .base import (AUTO_CLOSED_BY, RESTART_OUTCOMES, STUCK_PREFIX, STUCK_REASONS, AutoClosed, Restart, AGENT_STATES, AGENT_STATUSES, CLOSED_JOB_STATUSES, DATA_DIR,  # noqa: F401
                   EXCERPT_MAX_RAW, TRANSCRIPT_MAX_RAW, JOB_STATUSES, MEMORY_SEEN_MAX, MEMORY_REF_IMAGE_BYTES_MAX, MEMORY_REF_IMAGES_MAX, MemoryRef, NAME_SOURCES, TOOL_NAME_MAX, AgentEvent,
                   AgentStatus, Board, BoardError, JobPaused, PauseRecord, PAUSE_WRITER, LEFT_PAUSED, MANIFEST_VERSION, build_manifest, BoardUnavailable, CapExceeded, check_message_cap, configured_message_cap, MESSAGE_CAP_DEFAULT, MESSAGE_CAP_MIN, MESSAGE_CAP_MAX, IncompatibleStorage, ReadOnlyBoard,
                   Blocker, BlockerEvent, CloseGuard, Event, JobStatus, Member, Message, OwedReply, PostResult, ReadResult, ROUTE_STATES, SCHEMA_VERSION,
                   Route, RosterEntry, database_hosts, SetupResult, SpawnGrant, SyncState, TRANSCRIPT_ROLES, TranscriptImage, TranscriptRow, TranscriptSummary, TranscriptTotals,
                   VERDICTS, derive_agent_status,
                   decompress_capped, decompress_transcript, derive_job_status, WAITING_GOAL, goal_unmet, load_name_pool, normalize_message)

# backend name -> (module under this package, class name)
BACKENDS = {
    "postgres": ("postgres", "PostgresBoard"),
    "memory": ("memory", "MemoryBoard"),
    "sqlite": ("sqlite", "SqliteBoard"),
    "file": ("file", "FileBoard"),
}


DEFAULT_BACKEND = "file"


def board_backend(cfg: dict) -> str:
    """The configured backend name: [board] backend, "file" when unset. (cli.load_config
    resolves an old config with a [database] section and no backend to "postgres" first.)"""
    return str((cfg.get("board") or {}).get("backend") or DEFAULT_BACKEND)


def backend_class(cfg: dict) -> type[Board]:
    """The Board subclass selected by cfg["board"]["backend"]; imports only that module."""
    name = board_backend(cfg)
    try:
        module, cls = BACKENDS[name]
    except KeyError:
        raise BoardError(f"unknown board backend {name!r} (known: {', '.join(BACKENDS)})") from None
    return getattr(importlib.import_module(f".{module}", __name__), cls)


def open_board(cfg: dict, init_timeout: float = 60.0, readers: bool = False) -> Board:
    """Connect to the configured board. Raises BoardUnavailable if it cannot be reached.

    If opening fails because the store itself is gone although a stamp said it was set up
    (a dropped database, a deleted file), it is set up again (autoinit.recover_missing, waiting
    at most init_timeout for another setup) and the open is retried, once.

    readers: for a command that only reads. A Postgres board with several hosts then falls back
    to whichever standby answers when no primary is reachable (board.degraded names it; writes
    and LISTEN are unavailable there). Other backends and single hosts: no difference."""
    cls = backend_class(cfg)
    kw = {"readers": True} if readers and board_backend(cfg) == "postgres" else {}
    try:
        return cls(cfg, **kw)
    except BoardUnavailable:
        from . import autoinit
        if not autoinit.recover_missing(cfg, init_timeout):
            raise
    return cls(cfg, **kw)


def open_read_only(cfg: dict) -> Board:
    """Open the configured board for reading only (`swarm supervise --dry-run`): no recovery
    setup, and the file and SQLite backends create, write, truncate and rename nothing (a
    missing store raises BoardUnavailable; every write raises ReadOnlyBoard). Postgres and
    memory open plainly: opening them writes nothing."""
    cls = backend_class(cfg)
    if board_backend(cfg) in ("file", "sqlite"):
        return cls(cfg, read_only=True)
    return cls(cfg)


def setup_board(cfg: dict, names: Mapping[str, Sequence[str]] | None = None) -> SetupResult:
    """Create/upgrade the configured board's storage (`swarm init`); names default to the
    shipped pool (load_name_pool())."""
    return backend_class(cfg).setup(cfg, load_name_pool() if names is None else names)


def ensure_initialized(cfg: dict, timeout: float = 60.0,
                       names: Mapping[str, Sequence[str]] | None = None):
    """Set the configured board up if it isn't yet for this code (see board/autoinit.py);
    returns an autoinit.InitResult. Cheap once done: a stamp file check, no query."""
    from .autoinit import ensure_initialized as ensure
    return ensure(cfg, timeout, names)


__all__ = [
    "ensure_initialized", "SCHEMA_VERSION",
    "open_board", "open_read_only", "setup_board", "backend_class", "board_backend", "DEFAULT_BACKEND", "BACKENDS",
    "Board", "BoardError", "JobPaused", "PauseRecord", "PAUSE_WRITER", "LEFT_PAUSED", "MANIFEST_VERSION", "build_manifest", "BoardUnavailable", "IncompatibleStorage", "ReadOnlyBoard",
    "Message", "PostResult", "SetupResult", "AgentStatus", "JobStatus", "AgentEvent",
    "ReadResult", "RosterEntry", "OwedReply", "SyncState", "Member", "Route", "MEMORY_SEEN_MAX",
    "derive_agent_status", "derive_job_status", "WAITING_GOAL", "goal_unmet", "normalize_message", "load_name_pool", "SpawnGrant",
    "AGENT_STATES", "AGENT_STATUSES", "JOB_STATUSES", "CLOSED_JOB_STATUSES", "NAME_SOURCES",
    "TOOL_NAME_MAX", "DATA_DIR", "ROUTE_STATES", "VERDICTS",
    "TRANSCRIPT_ROLES", "TranscriptImage", "TranscriptRow", "TranscriptSummary", "TranscriptTotals",
    "MemoryRef", "EXCERPT_MAX_RAW", "TRANSCRIPT_MAX_RAW", "decompress_capped", "decompress_transcript",
    "MEMORY_REF_IMAGES_MAX", "MEMORY_REF_IMAGE_BYTES_MAX",
]
