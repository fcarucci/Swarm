"""The SQLite board backend: one local database file, stdlib `sqlite3` only.

For a swarm whose agents all run on ONE machine and that has no Postgres. Every hook call is its
own short-lived process with its own connection, so the file is the only thing they share.
How that is made safe:

* WAL journal (set once by `setup`, persistent in the file): readers never block the writer and
  never see a half-committed write. Every connection sets a busy timeout ([sqlite]
  busy_timeout_ms) so a writer waits for the lock instead of failing.
* Every read-modify-write runs in `BEGIN IMMEDIATE`, which takes the database's single write
  lock up front: name allocation (and the active-name check), claim_judge, reserve_spawn, route
  claims, message inserts, memory_seen. SQLite has exactly one writer at a time, so within such
  a transaction nothing else can change the rows it looked at. The partial unique indexes (one
  active agent per name, one active judge per job) are kept as a backstop.
* Cursor reads (read_unread) read in a snapshot WITHOUT the write lock and then move the
  cursor by compare-and-set, as Postgres does. (Holding the write lock for the read made
  frequently polling readers starve writers past their busy timeout.)
* Message ids come from INTEGER PRIMARY KEY AUTOINCREMENT (never reused, even after a purge
  deletes the newest). Inserts are serialised by the write lock, so ids commit in id order: a
  reader can never see id N before a smaller id that will still commit (base.py's rule).
* Multi-statement reads (read_unread, rollups) run in one deferred transaction, i.e. one WAL
  snapshot.

Timestamps are the board's clock (this machine's), stored as fixed-width ISO-8601 UTC text
("2026-09-24T08:00:00.123456+00:00"), so text order is time order; they come back tz-aware.

Derived agent/job status is computed in Python with `derive_agent_status`, from the thresholds
of the config the board was opened with (unlike Postgres, nothing is baked in at `setup`: no
re-init after changing them). There are no status views in the file.

Message cap (schema 15): the board's cap is the board_meta row 'message_max_chars', enforced by
the BEFORE INSERT trigger check_messages_cap (length() counts characters, as the cap does), so it
changes with one UPDATE and no table rewrite. Setup seeds it once ([board] message_max_chars for a
new board; the old table CHECK's N for a board upgraded from schema 14 or earlier, whose table is
rebuilt once without that CHECK, keeping every row and id) and never resets it. A lowered cap
leaves stored messages alone: it governs new posts only.

Change notification (watch/tail): no LISTEN/NOTIFY, so triggers bump counters in a one-row-per-
kind table `board_changes` ("messages" on every message insert, "state" on every agent or job
change), and wait_for_change polls them. Each poll first asks `PRAGMA data_version`, which only
changes when another connection committed, so an idle board costs one pragma per poll.

Sandboxed agents: the default path (~/.local/share/swarm-board/board.sqlite3) is outside every
sandbox's writable roots, so it is usually not writable from an agent's sandbox. Construction therefore probes for write access (os.access, then a
no-op UPDATE rolled back) and raises BoardUnavailable if it can't write, so `swarm post` spools the message and
the hooks (which run outside the sandbox) deliver it, exactly as with an unreachable Postgres.

Read-only (`open_read_only`, `swarm supervise --dry-run`): `SqliteBoard(cfg, read_only=True)`
opens the file with `mode=ro` (never created), skips the write probe, migrates nothing (an older
schema raises BoardUnavailable, as on a normal open) and refuses every write method with
ReadOnlyBoard. SQLite itself may still create or update the -wal/-shm side files of a WAL
database for the read: those are the library's, not board data.
"""
from __future__ import annotations

import contextlib
import datetime as _dt
import getpass
import json
import os
import re
import random
import sqlite3
import time
from pathlib import Path
from typing import Iterator, Mapping, Sequence

from .base import (check_job_data, merged_job_data, parse_job_data, LEFT_PAUSED, MOVED_PREFIX, PauseRecord, build_manifest, NAME_MAX, check_images, check_name, decompress_capped, decompress_transcript, MemoryRef, valid_pool, restart_over_limits, STUCK_PREFIX, AUTO_CLOSE_BLOCKING, CloseGuard, AUTO_CLOSED_BY, MEMORY_SEEN_MAX, NAME_SOURCES, RESTART_OUTCOMES, Restart,
                   ROUTE_STATES, TOOL_NAME_MAX, AgentEvent, AgentStatus, Board, BoardError, BoardUnavailable, JobStatus, Member, Message, ReadOnlyBoard, refuse_writes,
                   OwedReply, ReadResult, Route, RosterEntry, SCHEMA_VERSION, SetupResult, SpawnGrant, SyncState,
                   TRANSCRIPT_ROLES, TranscriptImage, TranscriptRow, TranscriptSummary, VERDICTS,
                   CapExceeded, configured_message_cap, derive_agent_status, load_name_pool)
from swarm import compat

_UTC = _dt.timezone.utc
# PRAGMA user_version records base.SCHEMA_VERSION once setup has run. A board older than this
# must be set up again (board.ensure_initialized does it on open) before this code opens it.
DEFAULT_PATH = "~/.local/share/swarm-board/board.sqlite3"
DEFAULT_BUSY_TIMEOUT_MS = 10000
POLL_SECONDS = 0.1           # wait_for_change poll interval

SCHEMA = """
CREATE TABLE IF NOT EXISTS name_pool (
    name   TEXT PRIMARY KEY,
    source TEXT NOT NULL CHECK (source IN ('simpsons', 'english'))
);
CREATE TABLE IF NOT EXISTS jobs (
    job               TEXT PRIMARY KEY,
    description       TEXT,
    created_by        TEXT,
    created_at        TEXT NOT NULL,
    task              TEXT,
    status            TEXT NOT NULL DEFAULT 'active'
                      CHECK (status IN ('active', 'paused', 'completed', 'cancelled', 'failed')),
    outcome           TEXT,
    session_id        TEXT,
    activated_at      TEXT,
    finished_at       TEXT,
    project           TEXT,
    goal              TEXT,
    verdict           TEXT CHECK (verdict IN ('met', 'not_met')),
    verdict_reason    TEXT,
    verdict_next      TEXT,
    verdict_by        TEXT,
    verdict_at        TEXT,
    completion_forced INTEGER NOT NULL DEFAULT 0,
    spawns            INTEGER NOT NULL DEFAULT 0,
    waiting_on        TEXT,
    waiting_since     TEXT,
    waiting_until     TEXT,
    max_hours         REAL,
    plugin_data       TEXT
);
CREATE TABLE IF NOT EXISTS agents (
    agent_key          TEXT PRIMARY KEY,
    name               TEXT NOT NULL CHECK (length(name) > 0),
    job                TEXT NOT NULL REFERENCES jobs(job) ON DELETE CASCADE,
    role               TEXT,
    host               TEXT,
    joined_at          TEXT NOT NULL,
    last_seen          TEXT NOT NULL,
    last_read_id       INTEGER NOT NULL DEFAULT 0,
    left_at            TEXT,
    state              TEXT NOT NULL DEFAULT 'started'
                       CHECK (state IN ('started', 'running', 'completed', 'left', 'dead')),
    tool_calls         INTEGER NOT NULL DEFAULT 0,
    current_tool       TEXT,
    tool_started_at    TEXT,
    last_post_at       TEXT,
    roster_seen        TEXT,
    roster_synced_at   TEXT,
    memory_recalled_at TEXT,
    memory_seen        TEXT NOT NULL DEFAULT '[]',
    remembered_at      TEXT,
    nudged_at          TEXT,
    reply_reminded_id  INTEGER NOT NULL DEFAULT 0,
    calls_at_post      INTEGER NOT NULL DEFAULT 0,
    silence_nudged_at  TEXT,
    judge              INTEGER NOT NULL DEFAULT 0,
    spawns             INTEGER NOT NULL DEFAULT 0,
    verifier           INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS agents_active_name ON agents (name) WHERE left_at IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS agents_one_judge ON agents (job) WHERE judge AND left_at IS NULL;
CREATE INDEX IF NOT EXISTS agents_job ON agents (job);
CREATE TABLE IF NOT EXISTS messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    job        TEXT NOT NULL REFERENCES jobs(job) ON DELETE CASCADE,
    agent_name TEXT NOT NULL CHECK (length(agent_name) > 0),
    created_at TEXT NOT NULL,
    message    TEXT NOT NULL CHECK (length(message) >= 1),
    to_agent   TEXT,
    agent_key  TEXT,
    host       TEXT
);
CREATE INDEX IF NOT EXISTS messages_job_id ON messages (job, id);
CREATE INDEX IF NOT EXISTS messages_created_at ON messages (created_at);
-- job_status's last_activity_at: max(created_at) per job (was a backward scan of the index above, filtered by job)
CREATE INDEX IF NOT EXISTS messages_job_created_at ON messages (job, created_at);
CREATE INDEX IF NOT EXISTS messages_to_agent ON messages (to_agent, id) WHERE to_agent IS NOT NULL;
CREATE TABLE IF NOT EXISTS agent_routes (
    agent_key  TEXT PRIMARY KEY,
    session_id TEXT,
    state      TEXT NOT NULL CHECK (state IN ('pending', 'unverified', 'final')),
    job        TEXT,
    created_at TEXT NOT NULL
);
-- The transcript archive ([transcripts]): one row per (job, agent_key), body = lzma of the
-- redacted JSONL. Added in schema version 3.
CREATE TABLE IF NOT EXISTS transcripts (
    job          TEXT NOT NULL,
    agent_key    TEXT NOT NULL,
    agent_name   TEXT NOT NULL,
    role         TEXT NOT NULL CHECK (role IN ('subagent', 'orchestrator')),
    host         TEXT,
    session_id   TEXT,
    captured_at  TEXT NOT NULL,
    final        INTEGER NOT NULL DEFAULT 0,
    raw_bytes    INTEGER NOT NULL,
    stored_bytes INTEGER NOT NULL,
    redactions   INTEGER NOT NULL DEFAULT 0,
    sha256       TEXT NOT NULL,
    body         BLOB NOT NULL,
    PRIMARY KEY (job, agent_key)
);
CREATE INDEX IF NOT EXISTS transcripts_captured_at ON transcripts (captured_at);
CREATE INDEX IF NOT EXISTS transcripts_agent_name ON transcripts (agent_name);
-- Images taken out of transcripts (schema version 4): once per sha256, raw bytes, and which
-- transcripts refer to them. An image goes when its last reference does.
CREATE TABLE IF NOT EXISTS transcript_images (
    sha256     TEXT PRIMARY KEY,
    mime       TEXT NOT NULL,
    bytes      INTEGER NOT NULL,
    data       BLOB NOT NULL,
    first_seen TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS transcript_image_refs (
    job       TEXT NOT NULL,
    agent_key TEXT NOT NULL,
    sha256    TEXT NOT NULL REFERENCES transcript_images (sha256),
    PRIMARY KEY (job, agent_key, sha256)
);
CREATE INDEX IF NOT EXISTS transcript_image_refs_sha256 ON transcript_image_refs (sha256);
-- Memory provenance (schema version 8): where a memory an agent saved came from, with an excerpt of
-- its transcript (lzma of the redacted JSONL); its images are in transcript_images, referenced
-- below. The first writer of a document_id keeps the row (Board.save_memory_ref).
CREATE TABLE IF NOT EXISTS memory_refs (
    document_id  TEXT PRIMARY KEY,
    bank         TEXT NOT NULL,
    job          TEXT NOT NULL,
    agent_key    TEXT NOT NULL,
    agent_name   TEXT NOT NULL,
    harness      TEXT,
    host         TEXT,
    session_id   TEXT,
    tool_call_id TEXT,
    -- The writer is a name from [provenance] writers, or swarm-remember (checked by save_memory_ref).
    writer       TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    checked_at   TEXT,
    patched      INTEGER NOT NULL DEFAULT 0,
    raw_bytes    INTEGER NOT NULL DEFAULT 0,
    redactions   INTEGER NOT NULL DEFAULT 0,
    excerpt      BLOB
);
CREATE INDEX IF NOT EXISTS memory_refs_job ON memory_refs (job, agent_key);
CREATE TABLE IF NOT EXISTS memory_ref_images (
    document_id TEXT NOT NULL,
    sha256      TEXT NOT NULL REFERENCES transcript_images (sha256),
    PRIMARY KEY (document_id, sha256)
);
CREATE INDEX IF NOT EXISTS memory_ref_images_sha256 ON memory_ref_images (sha256);
-- Supervisor restarts (schema version 6): one row per replacement started (or refused) for a
-- closed agent; old_agent_key is UNIQUE: each closed agent is replaced at most once.
CREATE TABLE IF NOT EXISTS restarts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    job           TEXT NOT NULL,
    agent_key     TEXT NOT NULL,          -- the lineage root: the first agent of the chain
    attempt       INTEGER NOT NULL,
    at            TEXT NOT NULL,
    reason        TEXT NOT NULL,
    old_agent_key TEXT NOT NULL UNIQUE,
    new_agent_key TEXT,
    harness       TEXT NOT NULL,
    host          TEXT NOT NULL,
    os_user       TEXT NOT NULL,
    minutes_cap   REAL NOT NULL,
    ended_at      TEXT,
    outcome       TEXT
);
CREATE INDEX IF NOT EXISTS restarts_job ON restarts (job, agent_key);
CREATE INDEX IF NOT EXISTS restarts_host_at ON restarts (host, os_user, at);
-- Pause/resume (schema version 12): one row per pause of a job, with its resume manifest (JSON).
CREATE TABLE IF NOT EXISTS job_pauses (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    job          TEXT NOT NULL,
    paused_at    TEXT NOT NULL,
    paused_by    TEXT,
    reason       TEXT,
    manifest     TEXT NOT NULL,
    resumed_at   TEXT,
    resumed_by   TEXT,
    resumed_host TEXT,
    outcome      TEXT
);
CREATE INDEX IF NOT EXISTS job_pauses_job ON job_pauses (job, id);
-- Per-board settings (schema 15): 'message_max_chars', the message cap (see the module docstring).
CREATE TABLE IF NOT EXISTS board_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TRIGGER IF NOT EXISTS check_messages_cap BEFORE INSERT ON messages
    WHEN length(NEW.message) > (SELECT CAST(value AS INTEGER) FROM board_meta WHERE key = 'message_max_chars')
    BEGIN SELECT RAISE(ABORT, 'message too long'); END;
-- Change counters for watch/tail (see the module docstring).
CREATE TABLE IF NOT EXISTS board_changes (kind TEXT PRIMARY KEY, n INTEGER NOT NULL DEFAULT 0);
INSERT OR IGNORE INTO board_changes (kind) VALUES ('messages'), ('state');
CREATE TRIGGER IF NOT EXISTS messages_changed AFTER INSERT ON messages BEGIN
    UPDATE board_changes SET n = n + 1 WHERE kind IN ('messages', 'state'); END;
CREATE TRIGGER IF NOT EXISTS agents_inserted AFTER INSERT ON agents BEGIN
    UPDATE board_changes SET n = n + 1 WHERE kind = 'state'; END;
CREATE TRIGGER IF NOT EXISTS agents_updated AFTER UPDATE ON agents BEGIN
    UPDATE board_changes SET n = n + 1 WHERE kind = 'state'; END;
CREATE TRIGGER IF NOT EXISTS agents_deleted AFTER DELETE ON agents BEGIN
    UPDATE board_changes SET n = n + 1 WHERE kind = 'state'; END;
CREATE TRIGGER IF NOT EXISTS jobs_inserted AFTER INSERT ON jobs BEGIN
    UPDATE board_changes SET n = n + 1 WHERE kind = 'state'; END;
CREATE TRIGGER IF NOT EXISTS jobs_updated AFTER UPDATE ON jobs BEGIN
    UPDATE board_changes SET n = n + 1 WHERE kind = 'state'; END;
CREATE TRIGGER IF NOT EXISTS jobs_deleted AFTER DELETE ON jobs BEGIN
    UPDATE board_changes SET n = n + 1 WHERE kind = 'state'; END;
"""

# Schema version 7: the store refuses agent names outside
# base.NAME_RULE and image keys that aren't 64 lowercase hex digits, whoever writes. SQLite can't
# add a CHECK to an existing table, so these are BEFORE INSERT/UPDATE triggers: like Postgres's
# CHECK ... NOT VALID they judge only rows written from now on, so old rows don't block the upgrade.
def _name_ok(col: str) -> str:
    """SQL: `col` is a valid agent name (base.NAME_RULE, in GLOB terms)."""
    return (f"(typeof({col}) = 'text' AND length({col}) BETWEEN 1 AND {NAME_MAX} "
            f"AND {col} NOT GLOB '*[^A-Za-z0-9 ._''-]*' AND {col} NOT GLOB ' *' AND {col} NOT GLOB '* ')")


def _sha_ok(col: str) -> str:
    return f"(typeof({col}) = 'text' AND length({col}) = 64 AND {col} NOT GLOB '*[^0-9a-f]*')"


def _check_trigger(name: str, table: str, cols: str, ok: str, what: str) -> str:
    return "".join(
        f"CREATE TRIGGER IF NOT EXISTS check_{name}_{op[:3].lower()} BEFORE {op} ON {table} "
        f"WHEN NOT ({ok}) BEGIN SELECT RAISE(ABORT, 'invalid {what}'); END;\n"
        for op in ("INSERT", f"UPDATE OF {cols}"))


CHECKS = (
    _check_trigger("messages_names", "messages", "agent_name, to_agent",
                   f"{_name_ok('NEW.agent_name')} AND (NEW.to_agent IS NULL OR {_name_ok('NEW.to_agent')})",
                   "agent name")
    + _check_trigger("agents_name", "agents", "name", _name_ok("NEW.name"), "agent name")
    + _check_trigger("transcript_images_sha256", "transcript_images", "sha256", _sha_ok("NEW.sha256"),
                     "image sha256")
    # schema 8: memory refs are held to the same rules
    + _check_trigger("memory_refs_name", "memory_refs", "agent_name", _name_ok("NEW.agent_name"), "agent name")
    + _check_trigger("memory_ref_images_sha256", "memory_ref_images", "sha256", _sha_ok("NEW.sha256"),
                     "image sha256"))

# Columns added after schema version 1 go here as (table, column, declaration); setup adds the
# missing ones in place (SQLite has no ADD COLUMN IF NOT EXISTS).
MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    ("jobs", "closed_by", "TEXT"),   # who closed the job; 'auto' = the auto-close sweep
    ("agents", "harness", "TEXT"),
    ("agents", "model", "TEXT"),
    ("agents", "turn_ended_at", "TEXT"),
    ("agents", "os_user", "TEXT"),
    ("transcripts", "harness", "TEXT"),
    ("agents", "left_reason", "TEXT"),   # why the supervisor (or a runner) closed it
    ("agents", "resume_of", "TEXT"),     # the agent_key a replacement took over from
    ("jobs", "supervise", "INTEGER NOT NULL DEFAULT 1"),   # 0: activate --no-supervise
    ("transcripts", "capture_failed", "TEXT"),   # schema 9: why the final capture failed (no body)
    ("jobs", "verdict_next", "TEXT"),   # schema 10: the judge's instructions with a not_met verdict
    ("jobs", "waiting_until", "TEXT"),  # schema 11: when a bounded `wait --for` expires
    ("jobs", "max_hours", "REAL"),      # schema 11: the job's own lifetime cap
    ("jobs", "plugin_data", "TEXT"),    # schema 16: per-job settings kept by CLI plugins (JSON)
    ("agents", "title", "TEXT"),        # schema 20: the optional display title (NULL: none)
)

_MESSAGE_COLS = "id, created_at, job, agent_name, to_agent, message"
_RESTART_COLS = ("id, job, agent_key, attempt, at, reason, old_agent_key, new_agent_key, harness, host, "
                 "os_user, minutes_cap, ended_at, outcome")   # Restart field order
_AGENT_COLS = ("agent_key, name, job, role, host, joined_at, last_seen, left_at, state, tool_calls, "
               "current_tool, tool_started_at, last_post_at, judge, verifier, harness, model, os_user, "
               "left_reason, resume_of, title")
# The replies an agent owes (see OwedReply): addressed to it on its job since it joined, already
# shown (id <= cursor), not yet reminded, and not answered by a later post to the sender.
_OWED = ("SELECT m.id, m.agent_name, m.created_at FROM messages m WHERE m.to_agent = :name "
         "AND m.job = :job AND m.id > :reminded AND m.id <= :cursor AND m.created_at >= :joined "
         "AND NOT EXISTS (SELECT 1 FROM messages r WHERE r.job = m.job AND r.agent_name = :name "
         "AND r.to_agent = m.agent_name AND r.id > m.id) ORDER BY m.id")


# --------------------------------------------------------------------------- helpers

def _ts(d: _dt.datetime | None) -> str | None:
    """A datetime as stored: fixed-width ISO-8601 in UTC, so text order is time order."""
    return None if d is None else d.astimezone(_UTC).isoformat(timespec="microseconds")


def _dt_(s: str | None) -> _dt.datetime | None:
    return None if s is None else _dt.datetime.fromisoformat(s)


def db_path(cfg: dict) -> Path:
    return Path(str((cfg.get("sqlite") or {}).get("path") or DEFAULT_PATH)).expanduser()


def _busy_ms(cfg: dict) -> int:
    return int((cfg.get("sqlite") or {}).get("busy_timeout_ms", DEFAULT_BUSY_TIMEOUT_MS))


def _connect(cfg: dict, create: bool = False, read_only: bool = False) -> sqlite3.Connection:
    """An autocommit connection (transactions are explicit) with the busy timeout and foreign
    keys on. create=False opens an existing file only; read_only opens it with mode=ro.
    Failure -> BoardUnavailable."""
    path = db_path(cfg)
    try:
        if create:
            path.parent.mkdir(parents=True, exist_ok=True)
        mode = "ro" if read_only else "rwc" if create else "rw"
        conn = sqlite3.connect(f"file:{path}?mode={mode}", uri=True,
                               timeout=_busy_ms(cfg) / 1000, isolation_level=None,
                               check_same_thread=False)
        conn.execute(f"PRAGMA busy_timeout = {_busy_ms(cfg)}")
        conn.execute("PRAGMA foreign_keys = ON")
        return conn
    except (sqlite3.Error, OSError) as exc:
        raise BoardUnavailable(str(exc)) from exc


def _history_cursor(c: sqlite3.Connection, job: str, keep: int) -> int:
    """The cursor that leaves exactly the job's newest `keep` messages unread."""
    row = c.execute("SELECT id FROM messages WHERE job = ? ORDER BY id DESC LIMIT 1 OFFSET ?",
                    (job, max(0, keep))).fetchone()
    return row[0] if row else 0


def _drop_writer_check(conn: sqlite3.Connection) -> None:
    """A board created before writers became configurable has CHECK (writer IN (...)) on
    memory_refs.writer, which refuses a configured writer's name. SQLite cannot drop a CHECK, so
    the table is rebuilt without it (rows copied as they are); the indexes and triggers of the
    dropped table are recreated by the schema script that runs next. A no-op on a board without
    the CHECK, so setup can run any number of times."""
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'memory_refs'").fetchone()
    if row is None or not re.search(r"CHECK\s*\(\s*writer\s+IN\s*\(", row[0], re.I):
        return
    ddl = re.sub(r",?\s*CHECK\s*\(\s*writer\s+IN\s*\([^)]*\)\s*\)", "", row[0], flags=re.I)
    ddl = re.sub(r"CREATE TABLE (IF NOT EXISTS )?\"?memory_refs\"?", "CREATE TABLE memory_refs_new", ddl, count=1, flags=re.I)
    cols = ", ".join(r[1] for r in conn.execute("PRAGMA table_info(memory_refs)"))
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("DROP TABLE IF EXISTS memory_refs_new")
        conn.execute(ddl)
        conn.execute(f"INSERT INTO memory_refs_new ({cols}) SELECT {cols} FROM memory_refs")
        conn.execute("DROP TABLE memory_refs")
        conn.execute("ALTER TABLE memory_refs_new RENAME TO memory_refs")
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


_OLD_CAP = re.compile(r"CHECK\s*\(\s*length\s*\(\s*message\s*\)\s*BETWEEN\s+1\s+AND\s+(\d+)\s*\)", re.I)


def _drop_message_check(conn: sqlite3.Connection) -> int | None:
    """Schema 15: the cap is no longer a table CHECK. A messages table that still has
    CHECK (length(message) BETWEEN 1 AND N) is rebuilt without it (documented procedure: foreign
    keys OFF, one transaction: new table, copy every row with its id, drop, rename; the table's
    indexes and triggers are recreated by the schema script that runs next) and N is returned
    (the cap that board really enforced, which seeds board_meta). None when there is no such
    CHECK, so setup can run any number of times. Message ids are preserved, and so is the
    AUTOINCREMENT counter (an id is never reused). Must run outside a transaction."""
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'messages'").fetchone()
    if row is None:
        return None
    m = _OLD_CAP.search(row[0])
    if m is None:
        return None
    old = int(m.group(1))
    ddl = _OLD_CAP.sub("CHECK (length(message) >= 1)", row[0], count=1)
    ddl = re.sub(r'CREATE TABLE (IF NOT EXISTS )?"?messages"?', "CREATE TABLE messages_new", ddl, count=1, flags=re.I)
    cols = ", ".join(r[1] for r in conn.execute("PRAGMA table_info(messages)"))
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute("DROP TABLE IF EXISTS messages_new")
            conn.execute(ddl)
            seq = conn.execute("SELECT seq FROM sqlite_sequence WHERE name = 'messages'").fetchone()
            conn.execute(f"INSERT INTO messages_new ({cols}) SELECT {cols} FROM messages ORDER BY id")
            conn.execute("DROP TABLE messages")
            conn.execute("ALTER TABLE messages_new RENAME TO messages")
            top = conn.execute("SELECT max(id) FROM messages").fetchone()[0] or 0
            keep = max(top, seq[0] if seq else 0)
            if conn.execute("UPDATE sqlite_sequence SET seq = ? WHERE name = 'messages'", (keep,)).rowcount == 0:
                conn.execute("INSERT INTO sqlite_sequence (name, seq) VALUES ('messages', ?)", (keep,))
            if conn.execute("PRAGMA foreign_key_check").fetchall():
                raise BoardUnavailable("board database: foreign key violations after rebuilding messages")
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.execute("PRAGMA foreign_keys = ON")
    return old


def _allow_paused(conn: sqlite3.Connection) -> None:
    """Schema 12: jobs.status may be 'paused'. SQLite can't alter a CHECK, so an older jobs table is
    rebuilt by the documented procedure: foreign keys OFF (dropping the table with them on would
    cascade-delete every agent and message), then in one transaction a new table with the widened
    CHECK, copy, drop, rename, and the table's triggers again. Only when the stored CREATE statement
    lacks 'paused'. Must run outside a transaction."""
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'jobs'").fetchone()
    if row is None or "'paused'" in row[0]:
        return
    old = "'cancelled', 'failed')"
    if old not in row[0]:
        raise BoardUnavailable("board database: the jobs table has an unexpected status check; "
                               "cannot add the 'paused' status")
    new_sql = re.sub(r'CREATE TABLE "?jobs"?', "CREATE TABLE jobs_new", row[0], count=1).replace(
        old, "'cancelled', 'failed', 'paused')", 1)
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute("DROP TABLE IF EXISTS jobs_new")
            conn.execute(new_sql)
            cols = ", ".join(r[1] for r in conn.execute("PRAGMA table_info(jobs)"))
            conn.execute(f"INSERT INTO jobs_new ({cols}) SELECT {cols} FROM jobs")
            conn.execute("DROP TABLE jobs")
            conn.execute("ALTER TABLE jobs_new RENAME TO jobs")
            for name, op in (("inserted", "INSERT"), ("updated", "UPDATE"), ("deleted", "DELETE")):
                conn.execute(f"CREATE TRIGGER IF NOT EXISTS jobs_{name} AFTER {op} ON jobs BEGIN "
                             f"UPDATE board_changes SET n = n + 1 WHERE kind = 'state'; END")
            if conn.execute("PRAGMA foreign_key_check").fetchall():
                raise BoardUnavailable("board database: foreign key violations after rebuilding jobs")
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.execute("PRAGMA foreign_keys = ON")


from .blockers import SqlBlockers, sql_schema, migrate_sql, rollup, protects


class SqliteBoard(SqlBlockers, Board):
    _blocker_pg = False
    """A Board over one SQLite connection to the shared database file."""

    def __init__(self, cfg: dict, read_only: bool = False):
        super().__init__(cfg)
        self._conn: sqlite3.Connection | None = None
        conn = _connect(cfg, read_only=read_only)
        try:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version < SCHEMA_VERSION:
                raise sqlite3.OperationalError(
                    f"board database {db_path(cfg)} is not initialised: run `swarm init`")
            if read_only:   # no write probe: this connection can't write, by design
                self._conn = conn
                refuse_writes(self, f"board database {db_path(cfg)}")
                self._watch, self._seen = None, (None, None)
                return
            # Write-access probe: a sandboxed agent can often read the file but not write it
            # (or not even create the WAL index); it must spool rather than fail mid-way.
            # access() answers for the macOS sandbox and read-only mounts without a lock; the
            # write statement (matching no row) catches the rest: it fails on a read-only
            # handle, where BEGIN alone doesn't.
            if not os.access(db_path(cfg), os.R_OK | os.W_OK):
                raise sqlite3.OperationalError(f"attempt to write a readonly database ({db_path(cfg)})")
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute("UPDATE board_changes SET n = n WHERE kind = ''")
            finally:
                conn.execute("ROLLBACK")
        except sqlite3.Error as exc:
            conn.close()
            raise BoardUnavailable(str(exc)) from exc
        self._conn = conn
        self._watch: str | None = None     # counter kind subscribed to
        self._seen = (None, None)          # (data_version, counter) at the last wait

    # ---- lifecycle -------------------------------------------------------------

    @contextlib.contextmanager
    def op_timeout(self, seconds: float):
        """The busy timeout lowered to `seconds` (never raised) within the block."""
        conn = self._conn
        if conn is None:
            yield
            return
        ms = max(1, min(int(seconds * 1000), _busy_ms(self.cfg)))
        conn.execute(f"PRAGMA busy_timeout = {ms}")
        try:
            yield
        finally:
            if self._conn is conn:
                conn.execute(f"PRAGMA busy_timeout = {_busy_ms(self.cfg)}")

    @classmethod
    def setup(cls, cfg: dict, names: Mapping[str, Sequence[str]]) -> SetupResult:
        names = valid_pool(names)   # a name that could forge context is never handed out
        existed = db_path(cfg).exists()
        conn = _connect(cfg, create=True)
        try:
            notes = () if existed else (f"created board database {db_path(cfg)}",)
            if conn.execute("PRAGMA journal_mode").fetchone()[0].lower() != "wal":
                conn.execute("PRAGMA journal_mode = WAL")     # persistent in the file
            _drop_writer_check(conn)
            seed = configured_message_cap(cfg)   # ValueError: refused before anything changes
            old_cap = _drop_message_check(conn)
            conn.executescript("BEGIN IMMEDIATE;\n" + SCHEMA + CHECKS + "\nCOMMIT;")
            # the cap is set once: an upgraded board keeps the one it enforced, a new one starts
            # with the config's; setup never resets it
            conn.execute("INSERT OR IGNORE INTO board_meta (key, value) VALUES ('message_max_chars', ?)",
                         (str(old_cap or seed),))
            _allow_paused(conn)
            conn.execute("BEGIN IMMEDIATE")
            try:
                for table, column, decl in MIGRATIONS:
                    have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
                    if column not in have:
                        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
                for statement in sql_schema().split(";"):
                    if statement.strip(): conn.execute(statement)
                migrate_sql(conn)
                for source in NAME_SOURCES:
                    conn.executemany("INSERT OR IGNORE INTO name_pool (name, source) VALUES (?, ?)",
                                     [(n, source) for n in names.get(source, ())])
                if conn.execute("PRAGMA user_version").fetchone()[0] < SCHEMA_VERSION:   # never downgrade
                    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            pool = {s: conn.execute("SELECT count(*) FROM name_pool WHERE source = ?", (s,)).fetchone()[0]
                    for s in NAME_SOURCES}
            return SetupResult(notes, pool)
        except sqlite3.Error as exc:
            raise BoardUnavailable(str(exc)) from exc
        finally:
            conn.close()

    def _read_message_cap(self) -> int | None:
        row = self._conn.execute("SELECT value FROM board_meta WHERE key = 'message_max_chars'").fetchone()
        return int(row[0]) if row else None

    def _write_message_cap(self, cap: int) -> None:
        self._conn.execute("INSERT INTO board_meta (key, value) VALUES ('message_max_chars', ?) "
                           "ON CONFLICT (key) DO UPDATE SET value = excluded.value", (str(cap),))

    @classmethod
    def schema_version(cls, cfg: dict) -> int | None:
        if not db_path(cfg).exists():
            return None
        conn = _connect(cfg)
        try:
            return conn.execute("PRAGMA user_version").fetchone()[0] or None
        except sqlite3.Error as exc:
            raise BoardUnavailable(str(exc)) from exc
        finally:
            conn.close()

    @classmethod
    def store_missing(cls, cfg: dict, cheap: bool = True) -> bool:
        return not db_path(cfg).exists()

    @classmethod
    def identity(cls, cfg: dict) -> str:
        return str(db_path(cfg).resolve())

    def close(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            conn.close()

    def _c(self) -> sqlite3.Connection:
        if self._conn is None:
            raise BoardError("board is closed")
        return self._conn

    @contextlib.contextmanager
    def _tx(self, write: bool = True) -> Iterator[sqlite3.Connection]:
        """One transaction: BEGIN IMMEDIATE (the write lock, taken up front) or a deferred
        read transaction (one consistent snapshot)."""
        c = self._c()
        if write and self.read_only:
            raise ReadOnlyBoard(f"board database {db_path(self.cfg)} is open read-only")
        if c.in_transaction:
            yield c
            return
        c.execute("BEGIN IMMEDIATE" if write else "BEGIN")
        try:
            yield c
        except BaseException:
            c.execute("ROLLBACK")
            raise
        c.execute("COMMIT")

    def now(self) -> _dt.datetime:
        return _dt.datetime.now(_UTC)

    def _now(self) -> str:
        return _ts(self.now())

    # ---- retention ---------------------------------------------------------------

    def purge(self) -> None:
        b = self.board_cfg
        now = self.now()
        keep = _ts(now - _dt.timedelta(days=int(b["retention_days"])))
        stale = _ts(now - _dt.timedelta(hours=int(b["agent_stale_hours"])))
        c = self._c()
        # Each statement is its own (autocommit) transaction: independently atomic.
        paused = "(SELECT job FROM jobs WHERE status = 'paused')"   # a paused job keeps its history
        c.execute(f"DELETE FROM messages WHERE created_at < ? AND job NOT IN {paused}", (keep,))
        c.execute("UPDATE agents SET left_at = ?, state = 'dead', current_tool = NULL "
                  "WHERE left_at IS NULL AND last_seen < ?", (_ts(now), stale))
        c.execute(f"DELETE FROM agents WHERE left_at < ? AND job NOT IN {paused}", (keep,))
        c.execute("DELETE FROM jobs WHERE COALESCE(finished_at, activated_at, created_at) < ? "
                  "AND NOT EXISTS (SELECT 1 FROM messages m WHERE m.job = jobs.job) "
                  "AND NOT EXISTS (SELECT 1 FROM agents a WHERE a.job = jobs.job AND a.left_at IS NULL) "
                  "AND status <> 'paused'",
                  (keep,))
        c.execute("DELETE FROM agent_routes WHERE created_at < ?", (keep,))
        c.execute("DELETE FROM restarts WHERE at < ?", (keep,))

    # ---- jobs --------------------------------------------------------------------

    def _ensure_job(self, c: sqlite3.Connection, job: str, description: str | None = None,
                    created_by: str | None = None) -> None:
        c.execute("INSERT INTO jobs (job, description, created_by, created_at) VALUES (?, ?, ?, ?) "
                  "ON CONFLICT (job) DO UPDATE SET description = COALESCE(excluded.description, "
                  "jobs.description) WHERE excluded.description IS NOT NULL",
                  (job, description, created_by, self._now()))

    def ensure_job(self, job: str, description: str | None = None,
                   created_by: str | None = None) -> None:
        self._ensure_job(self._c(), job, description, created_by)

    def open_job(self, job: str, description: str | None, task: str | None,
                 session_id: str | None, created_by: str | None, project: str | None = None,
                 goal: str | None = None) -> None:
        now = self._now()
        self._c().execute(
            "INSERT INTO jobs (job, description, task, session_id, created_by, project, goal, status, "
            "created_at, activated_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, ?) "
            "ON CONFLICT (job) DO UPDATE SET status = 'active', activated_at = excluded.activated_at, "
            "finished_at = NULL, outcome = NULL, "
            "description = COALESCE(excluded.description, jobs.description), "
            "task = COALESCE(excluded.task, jobs.task), "
            "session_id = COALESCE(excluded.session_id, jobs.session_id), "
            "project = COALESCE(excluded.project, jobs.project), goal = COALESCE(excluded.goal, jobs.goal), "
            "verdict = NULL, verdict_reason = NULL, verdict_next = NULL, verdict_by = NULL, verdict_at = NULL, "
            "completion_forced = 0, spawns = 0, waiting_on = NULL, waiting_since = NULL, "
            "waiting_until = NULL, max_hours = NULL, closed_by = NULL",
            (job, description, task, session_id, created_by, project, goal, now, now))

        self.set_waiting(job, None)

    def close_job(self, job: str, status: str, outcome: str | None, forced: bool = False,
                  closed_by: str | None = None, guard: CloseGuard | None = None) -> bool:
        with self._tx() as c:   # (the write lock: nothing changes between the guard and the close)
            if guard:
                row = c.execute("SELECT goal, verdict, max_hours FROM jobs WHERE job = ?", (job,)).fetchone()
                if not row or not guard.allows(*row) or any(protects(b, self.now()) for b in self.blockers(job)):
                    return False
                if guard.settled and not guard.pending_ok and row[0] and row[2] is None:
                    from swarm.review import auto_close_pending
                    if auto_close_pending(self, job):
                        return False
            return self._close(c, job, status, outcome, forced, closed_by)

    def _close(self, c: sqlite3.Connection, job: str, status: str, outcome: str | None,
               forced: bool, closed_by: str | None) -> bool:
        for blocker in self.blockers(job):
            if blocker.kind == 'wait':
                self.resolve_blocker(blocker.id, 'job closed', actor=closed_by or 'swarm')
        now = self._now()
        c.execute("UPDATE agents SET left_at = ?, state = 'left', current_tool = NULL, "
                  "tool_started_at = NULL WHERE job = ? AND left_at IS NULL", (now, job))
        return c.execute(
            "UPDATE jobs SET status = ?, outcome = COALESCE(?, outcome), "
            "finished_at = COALESCE(finished_at, ?), completion_forced = ?, waiting_on = NULL, "
            "waiting_since = NULL, waiting_until = NULL, closed_by = ? WHERE job = ?",
            (status, outcome, now, int(bool(forced)), closed_by, job)).rowcount > 0

    def auto_close_job(self, job: str, before: _dt.datetime, outcome: str) -> _dt.datetime | None:
        # Under the write lock (BEGIN IMMEDIATE): nothing can change between the checks and
        # the close, and a concurrent sweep sees the job closed once it gets the lock.
        with self._tx() as c:
            row = c.execute("SELECT COALESCE(activated_at, created_at), goal, verdict FROM jobs WHERE job = ? "
                            "AND status = 'active' AND waiting_on IS NULL", (job,)).fetchone()
            if not row or _dt_(row[0]) >= before:
                return None
            from swarm.review import completion_pending
            if completion_pending(self, job, row[1], row[2]):
                return None
            start, now = _dt_(row[0]), self.now()
            rows = c.execute("SELECT state, current_tool, tool_started_at, joined_at, last_seen, "
                             "left_at FROM agents WHERE job = ?", (job,)).fetchall()
            if not any(state in ("completed", "left") and left and _dt_(left) >= start
                       for state, _, _, _, _, left in rows):
                return None
            for state, tool, tool_at, joined, seen, left in rows:
                if self._derive(state, tool, _dt_(tool_at), _dt_(seen), now) in AUTO_CLOSE_BLOCKING:
                    return None
                if any(t is not None and _dt_(t) >= before for t in (joined, seen, left)):
                    return None
            if c.execute("SELECT 1 FROM messages WHERE job = ? AND created_at >= ? LIMIT 1",
                         (job, _ts(before))).fetchone():
                return None
            if not self._close(c, job, "completed", outcome, False, AUTO_CLOSED_BY):
                return None
            # the timestamp this close wrote, read back inside the same write transaction
            return _dt_(c.execute("SELECT finished_at FROM jobs WHERE job = ?", (job,)).fetchone()[0])

    def undo_auto_close(self, job: str, closed_at: _dt.datetime) -> bool:
        return self._c().execute(
            "UPDATE jobs SET status = 'active', finished_at = NULL, outcome = NULL, closed_by = NULL "
            "WHERE job = ? AND status = 'completed' AND closed_by = ? AND finished_at = ?",
            (job, AUTO_CLOSED_BY, _ts(closed_at))).rowcount > 0

    def set_job_max_hours(self, job: str, hours: float | None) -> bool:
        return self._c().execute("UPDATE jobs SET max_hours = ? WHERE job = ?", (hours, job)).rowcount > 0

    def bind_job_session(self, job: str, session_id: str) -> None:
        self._c().execute("UPDATE jobs SET session_id = ? WHERE job = ? AND session_id IS NULL",
                          (session_id, job))

    # ---- agents --------------------------------------------------------------------

    @staticmethod
    def _name_held(c: sqlite3.Connection, name: str) -> bool:
        return c.execute("SELECT 1 FROM agents WHERE name = ? AND left_at IS NULL",
                         (name,)).fetchone() is not None

    def _free_name(self, c: sqlite3.Connection) -> str:
        """A random free pool name, sources in NAME_SOURCES order; else "<english name> NNN".
        Called under the write lock, so a name found free stays free until commit."""
        for source in NAME_SOURCES:
            free = [r[0] for r in c.execute(
                "SELECT name FROM name_pool p WHERE source = ? AND NOT EXISTS "
                "(SELECT 1 FROM agents a WHERE a.name = p.name AND a.left_at IS NULL)", (source,))]
            if free:
                return random.choice(free)
        english = load_name_pool()["english"]
        while True:
            candidate = f"{random.choice(english)} {random.randint(100, 999)}"
            if not self._name_held(c, candidate):
                return candidate

    def _allocate_name(self, agent_key: str, job: str, role: str | None = None) -> str:
        self.purge()
        with self._tx() as c:
            self._ensure_job(c, job)
            now = self._now()
            row = c.execute("SELECT name, left_at, job, left_reason FROM agents WHERE agent_key = ?",
                            (agent_key,)).fetchone()
            if row is not None and row[1] is None:      # active: moves (a move drops the seats)
                same = int(row[2] == job)
                c.execute("UPDATE agents SET last_seen = ?, job = ?, judge = judge AND ?, "
                          "verifier = verifier AND ? WHERE agent_key = ?", (now, job, same, same, agent_key))
                return row[0]
            if row is not None and ((row[3] or "").startswith(STUCK_PREFIX) or c.execute(
                    "SELECT 1 FROM restarts WHERE old_agent_key = ?", (agent_key,)).fetchone()):
                return row[0]      # supervisor-closed or replaced: stays departed, never revived
            if row is not None and not self._name_held(c, row[0]):   # departed: revive, same name
                c.execute("UPDATE agents SET left_at = NULL, state = 'started', job = ?, last_seen = ?, "
                          "current_tool = NULL, tool_started_at = NULL, turn_ended_at = NULL, judge = 0, verifier = 0, "
                          "left_reason = NULL WHERE agent_key = ?", (job, now, agent_key))
                return row[0]
            name = self._free_name(c)
            c.execute("DELETE FROM agents WHERE agent_key = ?", (agent_key,))   # full reset
            c.execute("INSERT INTO agents (agent_key, name, job, role, host, os_user, joined_at, last_seen, "
                      "last_read_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                      (agent_key, name, job, role, compat.node(), getpass.getuser(), now, now,
                       _history_cursor(c, job, int(self.board_cfg.get("join_history", 30)))))
            return name

    def claim_judge(self, agent_key: str, job: str) -> bool:
        with self._tx() as c:
            if c.execute("SELECT 1 FROM agents WHERE agent_key = ? AND job = ? AND left_at IS NULL",
                         (agent_key, job)).fetchone() is None:
                return False
            if c.execute("SELECT 1 FROM agents WHERE job = ? AND judge AND left_at IS NULL "
                         "AND agent_key <> ?", (job, agent_key)).fetchone():
                return False
            c.execute("UPDATE agents SET judge = 1 WHERE agent_key = ?", (agent_key,))
            return True

    def _move_agent_row(self, agent_key: str, job: str, keep: int) -> str | None:
        with self._tx() as c:
            row = c.execute("SELECT job FROM agents WHERE agent_key = ? AND left_at IS NULL",
                            (agent_key,)).fetchone()
            if row is None or c.execute("SELECT 1 FROM jobs WHERE job = ? AND status = 'active'",
                                        (job,)).fetchone() is None:
                return None
            if row[0] == job:
                return job
            c.execute("UPDATE agents SET job = ?, judge = 0, verifier = 0, last_seen = ?, last_read_id = ?, "
                      "reply_reminded_id = (SELECT COALESCE(MAX(id), 0) FROM messages), "
                      "calls_at_post = tool_calls, silence_nudged_at = NULL, roster_seen = ?, "
                      "roster_synced_at = NULL WHERE agent_key = ?",
                      (job, self._now(), _history_cursor(c, job, keep), MOVED_PREFIX + row[0], agent_key))
            return row[0]

    def set_job_goal(self, job: str, goal: str) -> bool:
        with self._tx() as c:
            row = c.execute("SELECT goal, plugin_data FROM jobs WHERE job = ? AND status = 'active'", (job,)).fetchone()
            if row is None:
                return False
            if row[0] != goal:
                from swarm.review import clear_verdict_data
                c.execute("UPDATE jobs SET goal = ?, verdict = NULL, verdict_reason = NULL, "
                          "verdict_next = NULL, verdict_by = NULL, verdict_at = NULL, plugin_data = ? WHERE job = ?",
                          (goal, clear_verdict_data(row[1]), job))
            return True

    def claim_verifier(self, agent_key: str, job: str) -> bool:
        return self._c().execute(
            "UPDATE agents SET verifier = 1 WHERE agent_key = ? AND job = ? AND left_at IS NULL "
            "AND NOT judge", (agent_key, job)).rowcount > 0

    def verification_counts(self, job: str) -> tuple[int, int]:
        row = self._c().execute(
            "SELECT COALESCE(sum(substr(m.message, 1, 8) = 'VERIFIED'), 0), "
            "COALESCE(sum(substr(m.message, 1, 6) = 'FAILED'), 0) FROM messages m "
            "WHERE m.job = ? AND m.agent_name IN (SELECT name FROM agents WHERE job = ? AND verifier)",
            (job, job)).fetchone()
        return int(row[0]), int(row[1])

    def reserve_spawn(self, agent_key: str, job: str, per_agent: int, per_job: int) -> SpawnGrant:
        with self._tx() as c:
            job_row = c.execute("SELECT spawns FROM jobs WHERE job = ?", (job,)).fetchone()
            agent_row = c.execute("SELECT spawns FROM agents WHERE agent_key = ? AND job = ? "
                                  "AND left_at IS NULL", (agent_key, job)).fetchone()
            if job_row is None or agent_row is None:
                return SpawnGrant(False, 0, 0, "member")
            mine, total = agent_row[0], job_row[0]
            refused = "agent" if mine >= per_agent else "job" if total >= per_job else None
            if refused:
                return SpawnGrant(False, mine, total, refused)
            c.execute("UPDATE agents SET spawns = spawns + 1 WHERE agent_key = ?", (agent_key,))
            c.execute("UPDATE jobs SET spawns = spawns + 1 WHERE job = ?", (job,))
            return SpawnGrant(True, mine + 1, total + 1)

    def record_verdict(self, job: str, judge_name: str, verdict: str, reason: str,
                       next_steps: str | None = None, artifact: str | None = None) -> bool:
        check_name(judge_name, "judge name")
        if verdict not in VERDICTS:
            raise BoardError(f"unknown verdict {verdict!r}")
        from swarm.review import verdict_data
        with self._tx() as c:
            row = c.execute("SELECT plugin_data FROM jobs WHERE job = ? AND EXISTS "
                "(SELECT 1 FROM agents WHERE job = ? AND name = ? AND judge AND left_at IS NULL)",
                (job, job, judge_name)).fetchone()
            if row is None:
                return False
            at = self.now()
            c.execute("UPDATE jobs SET verdict = ?, verdict_reason = ?, verdict_next = ?, verdict_by = ?, "
                "verdict_at = ?, plugin_data = ? WHERE job = ?",
                (verdict, reason, next_steps, judge_name, at.isoformat(),
                 verdict_data(row[0], artifact, verdict, reason, next_steps, judge_name, at), job))
            return True

    def active_agent_name(self, agent_key: str) -> str | None:
        row = self._c().execute("SELECT name FROM agents WHERE agent_key = ? AND left_at IS NULL",
                                (agent_key,)).fetchone()
        return row[0] if row else None

    def was_member(self, agent_key: str, job: str) -> bool:
        return self._c().execute("SELECT 1 FROM agents WHERE agent_key = ? AND job = ?",
                                 (agent_key, job)).fetchone() is not None

    def tool_started(self, agent_key: str, tool_name: str | None) -> Member | None:
        now = self._now()
        row = self._c().execute(
            "UPDATE agents SET state = 'running', current_tool = ?, tool_started_at = ?, turn_ended_at = NULL, "
            "tool_calls = tool_calls + 1, last_seen = ? WHERE agent_key = ? AND left_at IS NULL "
            "RETURNING name, job, EXISTS (SELECT 1 FROM agent_routes r "
            "WHERE r.agent_key = agents.agent_key AND r.state = 'unverified'), verifier, model",
            ((tool_name or "?")[:TOOL_NAME_MAX], now, now, agent_key)).fetchone()
        return Member(row[0], row[1], bool(row[2]), bool(row[3]), row[4]) if row else None

    def tool_contact(self, agent_key: str, tool_name: str | None) -> Member | None:
        now = self._now()
        row = self._c().execute(
            "UPDATE agents SET state = 'running', current_tool = ?, tool_started_at = NULL, turn_ended_at = NULL, "
            "tool_calls = tool_calls + 1, last_seen = ? WHERE agent_key = ? AND left_at IS NULL "
            "RETURNING name, job, EXISTS (SELECT 1 FROM agent_routes r "
            "WHERE r.agent_key = agents.agent_key AND r.state = 'unverified'), verifier, model",
            ((tool_name or "?")[:TOOL_NAME_MAX], now, agent_key)).fetchone()
        return Member(row[0], row[1], bool(row[2]), bool(row[3]), row[4]) if row else None

    # ---- routes ---------------------------------------------------------------------

    def record_route(self, agent_key: str, session_id: str | None, state: str,
                     job: str | None = None) -> None:
        if state not in ROUTE_STATES:
            raise BoardError(f"unknown route state {state!r}")
        self._c().execute(
            "INSERT INTO agent_routes (agent_key, session_id, state, job, created_at) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT (agent_key) DO UPDATE SET "
            "session_id = excluded.session_id, state = excluded.state, job = excluded.job, "
            "created_at = excluded.created_at", (agent_key, session_id, state, job, self._now()))

    def claim_route(self, agent_key: str, session_id: str | None, from_state: str | None,
                    state: str, job: str | None = None) -> bool:
        if state not in ROUTE_STATES:
            raise BoardError(f"unknown route state {state!r}")
        with self._tx() as c:
            row = c.execute("SELECT state FROM agent_routes WHERE agent_key = ?", (agent_key,)).fetchone()
            if (row[0] if row else None) != from_state:
                return False
            self.record_route(agent_key, session_id, state, job)
            return True

    def route(self, agent_key: str) -> Route:
        row = self._c().execute(
            "SELECT r.state, r.job, r.session_id, a.job FROM (SELECT ? AS k) x "
            "LEFT JOIN agent_routes r ON r.agent_key = x.k LEFT JOIN agents a ON a.agent_key = x.k",
            (agent_key,)).fetchone()
        return Route(*row)

    def tool_finished(self, agent_key: str) -> None:
        self._c().execute("UPDATE agents SET current_tool = NULL, tool_started_at = NULL, last_seen = ? "
                          "WHERE agent_key = ? AND left_at IS NULL", (self._now(), agent_key))

    def agent_stopped(self, agent_key: str) -> None:
        self._c().execute("UPDATE agents SET left_at = ?, state = 'completed', current_tool = NULL, "
                          "tool_started_at = NULL WHERE agent_key = ? AND left_at IS NULL",
                          (self._now(), agent_key))

    def set_agent_role(self, agent_key: str, role: str) -> None:
        from swarm.roles import custom_role
        if custom_role(role) is None:
            raise ValueError("role must be a custom role identifier, not judge/verifier")
        self._c().execute("UPDATE agents SET role = ? WHERE agent_key = ? AND left_at IS NULL",
                          (role, agent_key))

    def set_agent_title(self, agent_key: str, title: str | None) -> bool:
        from .base import clean_title
        return self._c().execute("UPDATE agents SET title = ? WHERE agent_key = ? AND left_at IS NULL",
                                 (clean_title(title), agent_key)).rowcount > 0

    def set_agent_runtime(self, agent_key: str, harness: str | None, model: str | None) -> None:
        self._c().execute("UPDATE agents SET harness = COALESCE(?, harness), model = COALESCE(?, model) "
                          "WHERE agent_key = ? AND left_at IS NULL", (harness, model, agent_key))

    def agent_turn_ended(self, agent_key: str) -> None:
        now = self._now()
        self._c().execute("UPDATE agents SET turn_ended_at = ?, current_tool = NULL, tool_started_at = NULL, "
                          "last_seen = ? WHERE agent_key = ? AND left_at IS NULL", (now, now, agent_key))

    def turns_resumed(self, job: str) -> list[str]:
        rows = self._c().execute(
            "UPDATE agents SET turn_ended_at = ?, last_seen = ? "
            "WHERE job = ? AND left_at IS NULL AND turn_ended_at IS NOT NULL RETURNING agent_key",
            (self._now(), self._now(), job)).fetchall()
        return [r[0] for r in rows]

    def finish_quiet_agents(self, quiet_seconds: float) -> list[str]:
        cutoff = _ts(self.now() - _dt.timedelta(seconds=quiet_seconds))
        rows = self._c().execute(
            "UPDATE agents SET left_at = ?, state = 'completed', current_tool = NULL, tool_started_at = NULL "
            "WHERE left_at IS NULL AND turn_ended_at IS NOT NULL AND turn_ended_at < ? RETURNING agent_key",
            (self._now(), cutoff)).fetchall()
        return [r[0] for r in rows]

    def leave(self, agent_key: str | None = None, name: str | None = None) -> bool:
        match, param = ("agent_key = ?", agent_key) if agent_key else ("name = ?", name)
        return self._c().execute("UPDATE agents SET left_at = ?, state = 'left', current_tool = NULL "
                                 "WHERE left_at IS NULL AND " + match, (self._now(), param)).rowcount > 0

    def close_agent(self, agent_key: str, reason: str, seen_before=None) -> bool:
        cond, args = ("", ()) if seen_before is None else (" AND last_seen <= ?", (_ts(seen_before),))
        return self._c().execute(
            "UPDATE agents SET left_at = ?, state = 'left', left_reason = ?, current_tool = NULL, "
            "tool_started_at = NULL WHERE agent_key = ? AND left_at IS NULL" + cond,
            (self._now(), reason, agent_key, *args)).rowcount > 0

    def claim_resume(self, agent_key: str, resume_of: str, job: str) -> str | None:
        # Under the write lock (BEGIN IMMEDIATE): the checks hold until the insert commits.
        with self._tx() as c:
            cur = c.execute("SELECT name, left_at, job FROM agents WHERE agent_key = ?",
                            (agent_key,)).fetchone()
            if cur is not None and cur[1] is None and cur[2] == job:
                return cur[0]
            old = c.execute("SELECT name, role, job, left_at, last_read_id, judge, verifier, title FROM agents "
                            "WHERE agent_key = ?", (resume_of,)).fetchone()
            if old is None or old[2] != job or old[3] is None or self._name_held(c, old[0]):
                return None
            name, role, _, _, cursor, judge, verifier, title = old
            judge = bool(judge) and c.execute(
                "SELECT 1 FROM agents WHERE job = ? AND judge AND left_at IS NULL", (job,)).fetchone() is None
            now = self._now()
            # A full reset, as allocate_name's: every other column (sync state, counters) defaults.
            c.execute("DELETE FROM agents WHERE agent_key = ?", (agent_key,))
            c.execute("INSERT INTO agents (agent_key, name, job, role, host, os_user, joined_at, last_seen, "
                      "last_read_id, state, judge, verifier, resume_of, title) "
                      "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'started', ?, ?, ?, ?)",
                      (agent_key, name, job, role, compat.node(), getpass.getuser(), now, now,
                       cursor, int(judge), int(bool(verifier)), resume_of, title))
            return name

    def job_state(self, job: str) -> str | None:
        r = self._c().execute("SELECT status FROM jobs WHERE job = ?", (job,)).fetchone()
        return r[0] if r else None

    # ---- pause / resume ------------------------------------------------------------------

    _PAUSE_COLS = "id, job, paused_at, paused_by, reason, manifest, resumed_at, resumed_by, resumed_host, outcome"

    @staticmethod
    def _pause_record(r) -> PauseRecord:
        (pid, job, at, by, reason, manifest, rat, rby, rhost, outcome) = r
        return PauseRecord(id=pid, job=job, paused_at=_dt_(at), paused_by=by, reason=reason,
                           manifest=json.loads(manifest), resumed_at=_dt_(rat), resumed_by=rby,
                           resumed_host=rhost, outcome=None if outcome is None else json.loads(outcome))

    def pause_job(self, job: str, by: str | None, reason: str | None,
                  cwds: Mapping[str, str] | None = None) -> PauseRecord | None:
        with self._tx() as c:
            row = c.execute("SELECT status, goal, task, description, project, verdict, waiting_on, max_hours "
                            "FROM jobs WHERE job = ?", (job,)).fetchone()
            if row is None:
                return None
            if row[0] == "paused":
                return self._open(c, job)
            if row[0] != "active":
                return None
            now = self.now()
            job_row = dict(zip(("status", "goal", "task", "description", "project", "verdict",
                                "waiting_on", "max_hours"), row))
            cols = ("agent_key, name, role, host, os_user, harness, model, last_read_id, tool_calls, "
                    "current_tool, tool_started_at, state, last_seen, judge, verifier, resume_of, turn_ended_at")
            names = [x.strip() for x in cols.split(",")]
            rows = []
            for r in c.execute(f"SELECT {cols} FROM agents WHERE job = ? AND left_at IS NULL "
                               "ORDER BY joined_at, agent_key", (job,)).fetchall():
                a = dict(zip(names, r))
                a["status"] = self._derive(a["state"], a["current_tool"], _dt_(a["tool_started_at"]),
                                           _dt_(a["last_seen"]), now)
                a["last_seen"], a["turn_ended_at"] = _dt_(a["last_seen"]), _dt_(a["turn_ended_at"])
                sess = c.execute("SELECT session_id FROM agent_routes WHERE agent_key = ?",
                                 (a["agent_key"],)).fetchone()
                a["session_id"] = sess[0] if sess else None
                a["orchestrator"] = c.execute("SELECT 1 FROM transcripts WHERE job = ? AND agent_key = ? "
                                              "AND role = 'orchestrator'", (job, a["agent_key"])).fetchone() is not None
                rows.append(a)
            last = c.execute("SELECT COALESCE(MAX(id), 0) FROM messages WHERE job = ?", (job,)).fetchone()[0]
            manifest = build_manifest(job, now, by, reason, job_row, rows, last, cwds)
            c.execute("UPDATE agents SET left_at = ?, state = 'left', left_reason = ?, current_tool = NULL, "
                      "tool_started_at = NULL WHERE job = ? AND left_at IS NULL",
                      (_ts(now), LEFT_PAUSED, job))
            c.execute("UPDATE jobs SET status = 'paused' WHERE job = ?", (job,))
            c.execute("INSERT INTO job_pauses (job, paused_at, paused_by, reason, manifest) "
                      "VALUES (?, ?, ?, ?, ?)", (job, _ts(now), by, reason, json.dumps(manifest)))
            return self._open(c, job)

    def _open(self, c: sqlite3.Connection, job: str) -> PauseRecord | None:
        r = c.execute(f"SELECT {self._PAUSE_COLS} FROM job_pauses WHERE job = ? AND resumed_at IS NULL "
                      "ORDER BY id DESC LIMIT 1", (job,)).fetchone()
        return self._pause_record(r) if r else None

    def open_pause(self, job: str) -> PauseRecord | None:
        return self._open(self._c(), job)

    def pauses(self, job: str) -> list[PauseRecord]:
        return [self._pause_record(r) for r in self._c().execute(
            f"SELECT {self._PAUSE_COLS} FROM job_pauses WHERE job = ? ORDER BY id", (job,))]

    def begin_resume(self, job: str, pause_id: int, by: str | None, host: str | None) -> bool:
        with self._tx() as c:
            if c.execute("SELECT 1 FROM job_pauses WHERE id = ? AND job = ? AND resumed_at IS NULL",
                         (pause_id, job)).fetchone() is None:
                return False
            if c.execute("UPDATE jobs SET status = 'active' WHERE job = ? AND status = 'paused'",
                         (job,)).rowcount == 0:
                return False
            c.execute("UPDATE job_pauses SET resumed_at = ?, resumed_by = ?, resumed_host = ? WHERE id = ?",
                      (self._now(), by, host, pause_id))
            return True

    def record_resume_outcome(self, pause_id: int, outcome: dict) -> bool:
        return self._c().execute("UPDATE job_pauses SET outcome = ? WHERE id = ?",
                                 (json.dumps(outcome), pause_id)).rowcount > 0

    def job_data(self, job: str) -> dict[str, str]:
        row = self._c().execute("SELECT plugin_data FROM jobs WHERE job = ?", (job,)).fetchone()
        return parse_job_data(row[0] if row else None)

    def set_job_data(self, job: str, key: str, value: str | None) -> bool:
        check_job_data(key, value)
        with self._tx() as c:
            row = c.execute("SELECT plugin_data FROM jobs WHERE job = ?", (job,)).fetchone()
            if row is None:
                return False
            c.execute("UPDATE jobs SET plugin_data = ? WHERE job = ?",
                      (merged_job_data(row[0], key, value), job))
            return True

    def set_job_supervise(self, job: str, on: bool) -> bool:
        return self._c().execute("UPDATE jobs SET supervise = ? WHERE job = ?",
                                 (1 if on else 0, job)).rowcount > 0

    # ---- supervisor restarts ------------------------------------------------------------

    @staticmethod
    def _restart(r) -> Restart:
        (rid, job, key, attempt, at, reason, old, new, harness, host, os_user, cap, ended, outcome) = r
        return Restart(id=rid, job=job, agent_key=key, attempt=attempt, at=_dt_(at), reason=reason,
                       old_agent_key=old, new_agent_key=new, harness=harness, host=host, os_user=os_user,
                       minutes_cap=float(cap), ended_at=_dt_(ended), outcome=outcome)

    def record_restart(self, job: str, agent_key: str, old_agent_key: str, reason: str, harness: str,
                       minutes_cap: float, outcome: str | None = None,
                       max_per_job: int | None = None, max_job_minutes: float | None = None,
                       max_host_running: int | None = None, max_host_minutes: float | None = None,
                       day_start: _dt.datetime | None = None) -> Restart | None:
        if outcome is not None and outcome not in RESTART_OUTCOMES:
            raise ValueError(outcome)
        with self._tx() as c:   # the write lock: no count can change before the insert
            if max_per_job is not None and c.execute(
                    "SELECT count(*) FROM restarts WHERE job = ?", (job,)).fetchone()[0] >= int(max_per_job):
                return None
            if outcome is None and any(v is not None for v in (max_job_minutes, max_host_running,
                                                                max_host_minutes)):
                rows = lambda col, v: [self._restart(r) for r in c.execute(
                    f"SELECT {_RESTART_COLS} FROM restarts WHERE {col} = ?", (v,))]
                if restart_over_limits(rows("job", job), rows("host", compat.node()), float(minutes_cap),
                                       max_job_minutes=max_job_minutes, max_host_running=max_host_running,
                                       max_host_minutes=max_host_minutes, day_start=day_start):
                    return None
            now = self._now()
            row = c.execute(
                "INSERT INTO restarts (job, agent_key, attempt, at, reason, old_agent_key, harness, host, "
                "os_user, minutes_cap, ended_at, outcome) VALUES (?, ?, 1 + (SELECT count(*) FROM restarts "
                "WHERE job = ? AND agent_key = ?), ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                f"ON CONFLICT (old_agent_key) DO NOTHING RETURNING {_RESTART_COLS}",
                (job, agent_key, job, agent_key, now, reason, old_agent_key, harness, compat.node(),
                 getpass.getuser(), float(minutes_cap), now if outcome else None, outcome)).fetchone()
            return self._restart(row) if row else None

    def set_restart_agent(self, restart_id: int, new_agent_key: str) -> bool:
        return self._c().execute("UPDATE restarts SET new_agent_key = ? WHERE id = ?",
                                 (new_agent_key, restart_id)).rowcount > 0

    def finish_restart(self, restart_id: int, outcome: str) -> bool:
        if outcome not in RESTART_OUTCOMES:
            raise ValueError(outcome)
        return self._c().execute("UPDATE restarts SET ended_at = ?, outcome = ? WHERE id = ? AND ended_at IS NULL",
                                 (self._now(), outcome, restart_id)).rowcount > 0

    def was_replaced(self, agent_key: str) -> bool:
        return self._c().execute("SELECT 1 FROM restarts WHERE old_agent_key = ?",
                                 (agent_key,)).fetchone() is not None

    def restarts(self, job: str | None = None, agent_key: str | None = None, host: str | None = None,
                 os_user: str | None = None, since: _dt.datetime | None = None) -> list[Restart]:
        conds, args = [], []
        for col, v in (("job", job), ("agent_key", agent_key), ("host", host), ("os_user", os_user)):
            if v is not None:
                conds.append(f"{col} = ?")
                args.append(v)
        if since is not None:
            conds.append("at >= ?")
            args.append(_ts(since))
        where = (" WHERE " + " AND ".join(conds)) if conds else ""
        return [self._restart(r) for r in self._c().execute(
            f"SELECT {_RESTART_COLS} FROM restarts{where} ORDER BY id", args)]

    # ---- derived status ---------------------------------------------------------------

    def _derive(self, state: str, current_tool, tool_started_at, last_seen: _dt.datetime,
                now: _dt.datetime) -> str:
        b = self.board_cfg
        return derive_agent_status(state, current_tool, tool_started_at, last_seen, now,
                                   idle_minutes=int(b["idle_minutes"]), dead_minutes=int(b["dead_minutes"]),
                                   tool_timeout_minutes=int(b["tool_timeout_minutes"]))

    def _agent_statuses(self, c: sqlite3.Connection, job: str, now: _dt.datetime,
                        include_departed: bool = True, with_messages: bool = True) -> list[AgentStatus]:
        """The job's agents with derived status, in agents() order."""
        msgs = ("(SELECT count(*) FROM messages m WHERE m.job = a.job AND m.agent_name = a.name "
                "AND m.created_at >= a.joined_at)") if with_messages else "0"
        active = "" if include_departed else " AND left_at IS NULL"
        out = []
        for r in c.execute(f"SELECT {_AGENT_COLS}, {msgs} FROM agents a WHERE job = ?{active} "
                           "ORDER BY left_at IS NOT NULL, joined_at", (job,)):
            (key, name, ajob, role, host, joined, seen, left, state, calls, tool, tool_at, post_at,
             judge, verifier, harness, model, os_user, left_reason, resume_of, title, messages) = r
            last_seen = _dt_(seen)
            out.append(AgentStatus(
                job=ajob, name=name, role="judge" if judge else "verifier" if verifier else role,
                status=self._derive(state, tool, _dt_(tool_at), last_seen, now), current_tool=tool,
                tool_calls=calls, messages=messages, joined_at=_dt_(joined), last_contact_at=last_seen,
                last_post_at=_dt_(post_at), ended_at=_dt_(left), host=host, agent_key=key,
                harness=harness, model=model, os_user=os_user, left_reason=left_reason,
                resume_of=resume_of, title=title))
        return out

    def agents(self, job: str, include_departed: bool = True) -> list[AgentStatus]:
        with self._tx(write=False) as c:
            return self._agent_statuses(c, job, self.now(), include_departed)

    def roster(self, job: str) -> list[RosterEntry]:
        with self._tx(write=False) as c:
            return [RosterEntry(a.agent_key, a.name, a.role, a.status, a.current_tool, a.ended_at is None)
                    for a in self._agent_statuses(c, job, self.now(), with_messages=False)]

    # ---- per-agent sync state ----------------------------------------------------------

    def sync_state(self, agent_key: str) -> SyncState | None:
        with self._tx(write=False) as c:
            r = c.execute(
                "SELECT name, joined_at, roster_seen, roster_synced_at, memory_recalled_at, memory_seen, "
                "remembered_at, nudged_at, tool_calls, calls_at_post, last_post_at, silence_nudged_at, "
                "reply_reminded_id, job, last_read_id FROM agents WHERE agent_key = ?",
                (agent_key,)).fetchone()
            if r is None:
                return None
            owed = tuple(OwedReply(i, sender, _dt_(at)) for i, sender, at in c.execute(
                _OWED, {"name": r[0], "job": r[13], "reminded": r[12], "cursor": r[14], "joined": r[1]}))
        return SyncState(name=r[0], joined_at=_dt_(r[1]), now=self.now(), roster_seen=r[2],
                         roster_synced_at=_dt_(r[3]), memory_recalled_at=_dt_(r[4]),
                         memory_seen=tuple(json.loads(r[5])), remembered_at=_dt_(r[6]),
                         nudged_at=_dt_(r[7]), tool_calls=r[8], calls_at_post=r[9],
                         last_post_at=_dt_(r[10]), silence_nudged_at=_dt_(r[11]),
                         reply_reminded_id=r[12], replies_owed=owed)

    def record_roster_sync(self, agent_key: str, snapshot: str, full: bool) -> None:
        if full:
            self._c().execute("UPDATE agents SET roster_seen = ?, roster_synced_at = ? WHERE agent_key = ?",
                              (snapshot, self._now(), agent_key))
        else:
            self._c().execute("UPDATE agents SET roster_seen = ? WHERE agent_key = ?", (snapshot, agent_key))

    def record_memory_recall(self, agent_key: str, shown_ids) -> None:
        with self._tx() as c:
            row = c.execute("SELECT memory_seen FROM agents WHERE agent_key = ?", (agent_key,)).fetchone()
            if row is None:
                return
            seen = json.loads(row[0])
            seen += [i for i in dict.fromkeys(shown_ids) if i not in seen]
            c.execute("UPDATE agents SET memory_recalled_at = ?, memory_seen = ? WHERE agent_key = ?",
                      (self._now(), json.dumps(seen[-MEMORY_SEEN_MAX:]), agent_key))

    def record_remembered(self, name: str) -> None:
        self._c().execute("UPDATE agents SET remembered_at = ? WHERE name = ? AND left_at IS NULL",
                          (self._now(), name))

    def record_nudge(self, agent_key: str) -> None:
        self._c().execute("UPDATE agents SET nudged_at = ? WHERE agent_key = ?", (self._now(), agent_key))

    def record_silence_nudge(self, agent_key: str) -> None:
        self._c().execute("UPDATE agents SET silence_nudged_at = ? WHERE agent_key = ?",
                          (self._now(), agent_key))

    def record_reply_reminder(self, agent_key: str, upto_id: int) -> None:
        self._c().execute("UPDATE agents SET reply_reminded_id = max(reply_reminded_id, ?) "
                          "WHERE agent_key = ?", (upto_id, agent_key))

    def agent_events(self, since: _dt.datetime, job: str | None = None) -> list[AgentEvent]:
        jf, jp = (" AND job = ?", (job,)) if job else ("", ())
        return [AgentEvent(name=n, job=j, role=r, joined_at=_dt_(ja), left_at=_dt_(la), state=s)
                for n, j, r, ja, la, s in self._c().execute(
                    "SELECT name, job, role, joined_at, left_at, state FROM agents "
                    f"WHERE (joined_at > ? OR left_at > ?){jf} ORDER BY COALESCE(left_at, joined_at)",
                    (_ts(since), _ts(since), *jp))]

    # ---- messages ------------------------------------------------------------------

    @staticmethod
    def _msg(r) -> Message:
        return Message(id=r[0], created_at=_dt_(r[1]), job=r[2], agent_name=r[3], to_agent=r[4],
                       message=r[5])

    def _insert_message(self, job: str, name: str, text: str, to: str | None,
                        agent_key: str | None) -> int:
        # One write transaction: the write lock serialises inserts, so ids commit in id order.
        try:
            with self._tx() as c:
                self._ensure_job(c, job)
                now = self._now()
                msg_id = c.execute("INSERT INTO messages (job, agent_name, created_at, message, to_agent, "
                                   "agent_key, host) VALUES (?, ?, ?, ?, ?, ?, ?)",
                                   (job, name, now, text, to, agent_key, compat.node())).lastrowid
                c.execute("UPDATE agents SET last_seen = ?, last_post_at = ?, calls_at_post = tool_calls "
                          "WHERE name = ? AND left_at IS NULL", (now, now, name))
                return msg_id
        except sqlite3.IntegrityError as exc:
            if "message too long" not in str(exc):
                raise
            raise CapExceeded(self.message_cap()) from exc   # the cap was lowered since it was read

    def read_unread(self, agent_key: str | None = None, name: str | None = None,
                    job: str | None = None, advance: bool = True, *, touch: bool = True) -> ReadResult:
        match, param = ("agent_key = ?", agent_key) if agent_key else ("name = ?", name)
        # The page, the count and the job's top id come from one snapshot (a read transaction,
        # which takes no lock); the cursor then moves by compare-and-set, as in Postgres. Reading
        # under the write lock instead would make the frequent reads starve the writers.
        with self._tx(write=False) as c:
            row = c.execute(f"SELECT agent_key, name, job, last_read_id FROM agents WHERE {match} "
                            "AND left_at IS NULL", (param,)).fetchone()
            if not row:
                return ReadResult([], 0)
            key, me, my_job, last = row
            of_job = job or my_job
            top = c.execute("SELECT COALESCE(max(id), 0) FROM messages WHERE job = ?", (of_job,)).fetchone()[0]
            unread = c.execute("SELECT count(*) FROM messages WHERE job = ? AND id > ? AND agent_name <> ?",
                               (of_job, last, me)).fetchone()[0]
            messages = [self._msg(r) for r in c.execute(
                f"SELECT {_MESSAGE_COLS} FROM messages WHERE job = ? AND id > ? AND agent_name <> ? "
                "ORDER BY id LIMIT ?", (of_job, last, me, int(self.board_cfg["read_limit"])))]
            remaining = unread - len(messages)
        if not advance:
            return ReadResult(messages, remaining)
        new = messages[-1].id if remaining else max(last, top)
        moved = self._c().execute("UPDATE agents SET last_read_id = ?, last_seen = CASE WHEN ? THEN ? ELSE last_seen END WHERE agent_key = ? "
                                  "AND last_read_id = ?", (new, touch, self._now(), key, last)).rowcount
        # A parallel read of this agent moved the cursor first and owns these messages.
        return ReadResult(messages, remaining) if moved else ReadResult([], 0)

    def recent_messages(self, limit: int, job: str | None = None,
                        active_jobs_only: bool = False) -> list[Message]:
        if limit <= 0:
            return []
        if job:
            jf, jp = " WHERE job = ?", (job,)
        elif active_jobs_only:
            jf, jp = " WHERE job IN (SELECT job FROM jobs WHERE status = 'active')", ()
        else:
            jf, jp = "", ()
        rows = self._c().execute(f"SELECT {_MESSAGE_COLS} FROM messages{jf} ORDER BY id DESC LIMIT ?",
                                 (*jp, limit)).fetchall()
        return [self._msg(r) for r in reversed(rows)]

    def last_post(self, job: str, agent_name: str) -> Message | None:
        row = self._c().execute(f"SELECT {_MESSAGE_COLS} FROM messages WHERE job = ? AND agent_name = ? "
                                "ORDER BY id DESC LIMIT 1", (job, agent_name)).fetchone()
        return self._msg(row) if row else None

    def messages_after(self, after_id: int, job: str | None = None) -> list[Message]:
        jf, jp = (" AND job = ?", (job,)) if job else ("", ())
        return [self._msg(r) for r in self._c().execute(
            f"SELECT {_MESSAGE_COLS} FROM messages WHERE id > ?{jf} ORDER BY id", (after_id, *jp))]

    def last_message_id(self, job: str | None = None) -> int:
        jf, jp = (" WHERE job = ?", (job,)) if job else ("", ())
        return self._c().execute(f"SELECT COALESCE(max(id), 0) FROM messages{jf}", jp).fetchone()[0]

    # ---- status ----------------------------------------------------------------------

    _JOB_COLS = ("job, status, description, task, outcome, created_by, session_id, created_at, "
                 "activated_at, finished_at, project, goal, verdict, verdict_reason, verdict_by, "
                 "verdict_at, completion_forced, waiting_on, waiting_since, closed_by, supervise, "
                 "(SELECT count(*) FROM messages m WHERE m.job = jobs.job), "
                 "(SELECT max(created_at) FROM messages m WHERE m.job = jobs.job AND NOT (m.agent_name = 'swarm' AND m.message LIKE 'Blocker % expired:%')), "
                 "(SELECT a.name FROM agents a WHERE a.job = jobs.job AND a.judge AND a.left_at IS NULL), "
                 "verdict_next, max_hours, waiting_until, plugin_data")

    def _job_status(self, c: sqlite3.Connection, r, now: _dt.datetime) -> JobStatus:
        (job, status, desc, task, outcome, by, session, created, activated, finished, project, goal,
         verdict, reason, verdict_by, verdict_at, forced, waiting_on, waiting_since, closed_by,
         supervise, n_messages, last_message, judge, verdict_next, max_hours, waiting_until, plugin_data) = r
        sts = self._agent_statuses(c, job, now, with_messages=False)
        stamps = [a.last_contact_at for a in sts] + ([_dt_(last_message)] if last_message else [])
        count = lambda *st: sum(1 for a in sts if a.status in st)  # noqa: E731
        from swarm.review import pipeline_status
        js = JobStatus(
            job=job, status=status, description=desc, task=task, outcome=outcome, created_by=by,
            session_id=session, created_at=_dt_(created), activated_at=_dt_(activated),
            finished_at=_dt_(finished), agents=len(sts), started=count("started"),
            running=count("running"), idle=count("idle"), completed=count("completed"),
            dead_or_left=count("dead", "left"), messages=n_messages,
            last_activity_at=max(stamps) if stamps else None, project=project, goal=goal,
            verdict=verdict, verdict_reason=reason, verdict_by=verdict_by, verdict_at=_dt_(verdict_at),
            completion_forced=bool(forced), judge=judge, waiting_on=waiting_on,
            waiting_since=_dt_(waiting_since), closed_by=closed_by, supervise=bool(supervise),
            verdict_next=verdict_next, max_hours=max_hours, waiting_until=_dt_(waiting_until),
            **pipeline_status(plugin_data))

        return rollup(js, self.blockers(job), now)

    def job_status(self, job: str) -> JobStatus | None:
        with self._tx(write=False) as c:
            r = c.execute(f"SELECT {self._JOB_COLS} FROM jobs WHERE job = ?", (job,)).fetchone()
            return self._job_status(c, r, self.now()) if r else None

    def jobs(self, include_closed: bool = False) -> list[JobStatus]:
        where = "" if include_closed else " WHERE status = 'active'"
        with self._tx(write=False) as c:
            now = self.now()
            rows = c.execute(f"SELECT {self._JOB_COLS} FROM jobs{where} ORDER BY status <> 'active', "
                             "COALESCE(activated_at, created_at), job").fetchall()
            return [self._job_status(c, r, now) for r in rows]

    def session_jobs(self, session: str) -> list[JobStatus]:
        with self._tx(write=False) as c:
            now = self.now()
            rows = c.execute(f"SELECT {self._JOB_COLS} FROM jobs WHERE session_id = ? "
                             "ORDER BY status <> 'active', COALESCE(activated_at, created_at), job",
                             (session,)).fetchall()
            return [self._job_status(c, r, now) for r in rows]

    def session_shown_jobs(self, session: str) -> list[JobStatus]:
        with self._tx(write=False) as c:
            rows = c.execute(f"SELECT {self._JOB_COLS} FROM jobs WHERE session_id = ? "
                             "AND (status IN ('active', 'paused') OR job = (SELECT job FROM jobs WHERE session_id = ? "
                             "AND NOT EXISTS (SELECT 1 FROM jobs WHERE session_id = ? AND status IN ('active', 'paused')) "
                             "ORDER BY COALESCE(finished_at, created_at) DESC, job DESC LIMIT 1)) "
                             "ORDER BY COALESCE(activated_at, created_at), job",
                             (session, session, session)).fetchall()
            now = self.now()
            return [self._job_status(c, r, now) for r in rows]

    # ---- change notification -----------------------------------------------------------

    def _counter(self) -> tuple[int, int]:
        c = self._c()
        version = c.execute("PRAGMA data_version").fetchone()[0]
        if self._seen[0] == version:        # nobody else committed since the last look
            return version, self._seen[1]
        return version, c.execute("SELECT n FROM board_changes WHERE kind = ?", (self._watch,)).fetchone()[0]

    def subscribe(self, messages_only: bool = False) -> None:
        self._watch = "messages" if messages_only else "state"
        self._seen = (None, None)
        self._seen = self._counter()

    def wait_for_change(self, timeout: float) -> bool:
        if self._watch is None:
            self.subscribe(False)
        deadline = time.monotonic() + timeout
        while True:
            now = self._counter()
            if now[1] != self._seen[1]:
                self._seen = now            # everything pending so far counts once
                return True
            self._seen = now
            left = deadline - time.monotonic()
            if left <= 0:
                return False
            time.sleep(min(POLL_SECONDS, left))

    # ---- transcripts ---------------------------------------------------------------------

    _TRANSCRIPT_COLS = ("job, agent_key, agent_name, role, host, captured_at, final, raw_bytes, "
                        "stored_bytes, redactions, session_id, sha256, harness, capture_failed")

    def save_transcript(self, row: TranscriptRow) -> bool:
        if row.role not in TRANSCRIPT_ROLES:
            raise ValueError(f"unknown transcript role {row.role!r}")
        check_images(row.images)   # recomputed: a sha256 names a file
        with self._tx() as c:
            c.execute(
                "INSERT INTO transcripts (job, agent_key, agent_name, role, host, session_id, captured_at, "
                "final, raw_bytes, stored_bytes, redactions, sha256, body, harness, capture_failed) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (job, agent_key) DO UPDATE SET agent_name = excluded.agent_name, "
                "role = excluded.role, host = excluded.host, session_id = excluded.session_id, "
                "captured_at = excluded.captured_at, final = excluded.final, raw_bytes = excluded.raw_bytes, "
                "stored_bytes = excluded.stored_bytes, redactions = excluded.redactions, "
                "sha256 = excluded.sha256, body = excluded.body, harness = excluded.harness, "
                "capture_failed = excluded.capture_failed "
                "WHERE (transcripts.sha256 <> excluded.sha256 OR (excluded.final AND NOT transcripts.final)) "
                "AND NOT (excluded.capture_failed IS NOT NULL AND transcripts.final "
                "AND transcripts.capture_failed IS NULL)",
                (row.job, row.agent_key, row.agent_name, row.role, row.host, row.session_id,
                 _ts(row.captured_at or self.now()), int(bool(row.final)), int(row.raw_bytes),
                 len(row.body), int(row.redactions), row.sha256, sqlite3.Binary(row.body), row.harness,
                 row.failed))
            if c.execute("SELECT changes()").fetchone()[0] == 0:
                return False
            now = _ts(self.now())
            c.execute("DELETE FROM transcript_image_refs WHERE job = ? AND agent_key = ?",
                      (row.job, row.agent_key))
            for img in dict((i.sha256, i) for i in row.images).values():
                c.execute("INSERT OR IGNORE INTO transcript_images (sha256, mime, bytes, data, first_seen) "
                          "VALUES (?, ?, ?, ?, ?)",
                          (img.sha256, img.mime, int(img.size), sqlite3.Binary(img.data), now))
                c.execute("INSERT OR IGNORE INTO transcript_image_refs (job, agent_key, sha256) VALUES (?, ?, ?)",
                          (row.job, row.agent_key, img.sha256))
            self._drop_orphan_images(c)
            return True

    def refresh_transcript(self, job: str, agent_key: str, final: bool) -> bool:
        return self._c().execute("UPDATE transcripts SET captured_at = ?, final = MAX(final, ?), "
                                 "capture_failed = CASE WHEN ? THEN NULL ELSE capture_failed END "
                                 "WHERE job = ? AND agent_key = ?",
                                 (self._now(), int(bool(final)), int(bool(final)), job, agent_key)).rowcount > 0

    def mark_capture_failed(self, job: str, agent_key: str, reason: str, marker: TranscriptRow) -> str:
        with self._tx() as c:
            if c.execute("UPDATE transcripts SET final = 1, capture_failed = ? "
                         "WHERE job = ? AND agent_key = ? AND final = 0", (reason, job, agent_key)).rowcount:
                return "marked"
            c.execute(
                "INSERT INTO transcripts (job, agent_key, agent_name, role, host, session_id, captured_at, "
                "final, raw_bytes, stored_bytes, redactions, sha256, body, harness, capture_failed) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 1, 0, ?, 0, ?, ?, ?, ?) ON CONFLICT (job, agent_key) DO NOTHING",
                (job, agent_key, marker.agent_name, marker.role, marker.host, marker.session_id,
                 _ts(marker.captured_at or self.now()), len(marker.body), marker.sha256,
                 sqlite3.Binary(marker.body), marker.harness, reason))
            return "stored" if c.execute("SELECT changes()").fetchone()[0] else "kept"

    def pending_final_transcripts(self, host: str, os_user: str, harness: str, since) -> list[tuple[str, str]]:
        rows = self._c().execute(
            "SELECT a.job, a.agent_key FROM agents a LEFT JOIN transcripts t "
            "ON t.job = a.job AND t.agent_key = a.agent_key WHERE a.left_at IS NOT NULL AND a.left_at >= ? "
            "AND a.host = ? AND a.os_user = ? AND a.harness = ? AND (t.agent_key IS NULL OR t.final = 0) "
            "ORDER BY a.left_at", (_ts(since), host, os_user, harness)).fetchall()
        return [(r[0], r[1]) for r in rows]

    @staticmethod
    def _drop_orphan_images(c) -> None:
        c.execute("DELETE FROM transcript_images WHERE NOT EXISTS "
                  "(SELECT 1 FROM transcript_image_refs r WHERE r.sha256 = transcript_images.sha256) "
                  "AND NOT EXISTS (SELECT 1 FROM memory_ref_images m WHERE m.sha256 = transcript_images.sha256)")

    def _image_refs(self, where: str, params) -> dict:
        """(job, agent_key) -> [(sha256, bytes)] for the transcripts matching `where`."""
        out: dict = {}
        for job, key, sha, size in self._c().execute(
                "SELECT r.job, r.agent_key, r.sha256, i.bytes FROM transcript_image_refs r "
                "JOIN transcript_images i USING (sha256) JOIN transcripts t USING (job, agent_key)"
                f"{where} ORDER BY r.sha256", params):
            out.setdefault((job, key), []).append((sha, int(size)))
        return out

    def transcript_image(self, sha256: str) -> TranscriptImage | None:
        r = self._c().execute("SELECT sha256, mime, bytes, data, first_seen FROM transcript_images "
                              "WHERE sha256 = ?", (sha256,)).fetchone()
        return None if r is None else TranscriptImage(r[0], r[1], int(r[2]), bytes(r[3]), _dt_(r[4]))

    def transcript_images(self, job: str | None = None, agent_key: str | None = None) -> list[TranscriptImage]:
        conds, params = [], []
        for col, val in (("r.job", job), ("r.agent_key", agent_key)):
            if val is not None:
                conds.append(f"{col} = ?")
                params.append(val)
        where = (" WHERE " + " AND ".join(conds)) if conds else ""
        rows = self._c().execute(
            "SELECT DISTINCT i.sha256, i.mime, i.bytes, i.first_seen FROM transcript_images i "
            f"JOIN transcript_image_refs r USING (sha256){where} ORDER BY i.sha256", params).fetchall()
        return [TranscriptImage(r[0], r[1], int(r[2]), None, _dt_(r[3])) for r in rows]

    def transcripts(self, job: str | None = None, agent_name: str | None = None,
                    agent_key: str | None = None, role: str | None = None) -> list[TranscriptSummary]:
        conds, params = [], []
        for col, val in (("job", job), ("agent_name", agent_name), ("agent_key", agent_key), ("role", role)):
            if val is not None:
                conds.append(f"t.{col} = ?")
                params.append(val)
        where = (" WHERE " + " AND ".join(conds)) if conds else ""
        with self._tx(write=False) as c:   # one snapshot for the rows and their images
            rows = c.execute(f"SELECT {self._TRANSCRIPT_COLS} FROM transcripts t{where} "
                             "ORDER BY captured_at, job, agent_key", params).fetchall()
            refs = self._image_refs(where, params)
        out = []
        for r in rows:
            imgs = tuple(refs.get((r[0], r[1]), ()))
            ib = sum(size for _, size in imgs)
            out.append(TranscriptSummary(r[0], r[1], r[2], r[3], r[4], _dt_(r[5]), bool(r[6]), r[7] + ib,
                                         r[8] + ib, r[9], r[10], r[11], ib, imgs, harness=r[12], failed=r[13]))
        return out

    def transcript_body(self, job: str, agent_key: str) -> bytes | None:
        row = self._c().execute("SELECT body FROM transcripts WHERE job = ? AND agent_key = ? "
                                "AND NOT (capture_failed IS NOT NULL AND raw_bytes = 0)",
                                (job, agent_key)).fetchone()
        return None if row is None else decompress_transcript(bytes(row[0]))

    def _delete_transcripts(self, rows, before: _dt.datetime, jobs) -> int:
        n = 0
        with self._tx() as c:
            for job in jobs:
                n += c.execute("DELETE FROM transcripts WHERE job = ?", (job,)).rowcount
            for job, key in rows:
                if job not in jobs:
                    n += c.execute("DELETE FROM transcripts WHERE job = ? AND agent_key = ? "
                                   "AND captured_at < ?", (job, key, _ts(before))).rowcount
            c.execute("DELETE FROM transcript_image_refs WHERE NOT EXISTS (SELECT 1 FROM transcripts t "
                      "WHERE t.job = transcript_image_refs.job AND t.agent_key = transcript_image_refs.agent_key)")
            self._drop_orphan_images(c)
        return n

    # ---- memory provenance (schema 8) ------------------------------------------------------

    _MREF_COLS = ("document_id, bank, job, agent_key, agent_name, harness, host, session_id, tool_call_id, "
                  "writer, raw_bytes, redactions, created_at, checked_at, patched, length(excerpt)")

    def _save_memory_ref(self, ref: MemoryRef) -> str:
        with self._tx() as c:
            old = c.execute("SELECT agent_key FROM memory_refs WHERE document_id = ?", (ref.document_id,)).fetchone()
            if old is not None and old[0] != ref.agent_key:
                return "kept"
            c.execute(
                "INSERT INTO memory_refs (document_id, bank, job, agent_key, agent_name, harness, host, session_id, "
                "tool_call_id, writer, created_at, checked_at, patched, raw_bytes, redactions, excerpt) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?) "
                "ON CONFLICT (document_id) DO UPDATE SET bank = excluded.bank, job = excluded.job, "
                "agent_name = excluded.agent_name, harness = excluded.harness, host = excluded.host, "
                "session_id = excluded.session_id, tool_call_id = excluded.tool_call_id, writer = excluded.writer, "
                "created_at = excluded.created_at, checked_at = NULL, patched = excluded.patched, "
                "raw_bytes = excluded.raw_bytes, redactions = excluded.redactions, excerpt = excluded.excerpt",
                (ref.document_id, ref.bank, ref.job, ref.agent_key, ref.agent_name, ref.harness, ref.host,
                 ref.session_id, ref.tool_call_id, ref.writer, _ts(ref.created_at or self.now()),
                 int(bool(ref.patched)), int(ref.raw_bytes), int(ref.redactions),
                 None if ref.excerpt is None else sqlite3.Binary(ref.excerpt)))
            c.execute("DELETE FROM memory_ref_images WHERE document_id = ?", (ref.document_id,))
            now = _ts(self.now())
            for img in dict((i.sha256, i) for i in ref.images).values():
                c.execute("INSERT OR IGNORE INTO transcript_images (sha256, mime, bytes, data, first_seen) "
                          "VALUES (?, ?, ?, ?, ?)", (img.sha256, img.mime, int(img.size), sqlite3.Binary(img.data), now))
                c.execute("INSERT OR IGNORE INTO memory_ref_images (document_id, sha256) VALUES (?, ?)",
                          (ref.document_id, img.sha256))
            self._drop_orphan_images(c)
            return "inserted" if old is None else "updated"

    def memory_refs(self, job=None, agent_name=None, agent_key=None, document_id=None) -> list[MemoryRef]:
        conds, params = [], []
        for col, val in (("job", job), ("agent_name", agent_name), ("agent_key", agent_key),
                         ("document_id", document_id)):
            if val is not None:
                conds.append(f"m.{col} = ?")
                params.append(val)
        where = (" WHERE " + " AND ".join(conds)) if conds else ""
        with self._tx(write=False) as c:   # one snapshot for the rows and their images
            rows = c.execute(f"SELECT {self._MREF_COLS} FROM memory_refs m{where} "
                             "ORDER BY m.created_at, m.document_id", params).fetchall()
            imgs: dict = {}
            for doc, sha, mime, size, first in c.execute(
                    "SELECT r.document_id, i.sha256, i.mime, i.bytes, i.first_seen FROM memory_ref_images r "
                    "JOIN transcript_images i ON i.sha256 = r.sha256 JOIN memory_refs m ON m.document_id = r.document_id"
                    f"{where} ORDER BY i.sha256", params):
                imgs.setdefault(doc, []).append(TranscriptImage(sha, mime, int(size), None, _dt_(first)))
        return [MemoryRef(document_id=r[0], bank=r[1], job=r[2], agent_key=r[3], agent_name=r[4], harness=r[5],
                          host=r[6], session_id=r[7], tool_call_id=r[8], writer=r[9], raw_bytes=int(r[10]),
                          redactions=int(r[11]), created_at=_dt_(r[12]), checked_at=_dt_(r[13]),
                          patched=bool(r[14]), stored_bytes=int(r[15] or 0), images=tuple(imgs.get(r[0], ())))
                for r in rows]

    def memory_ref_excerpt(self, document_id: str) -> bytes | None:
        row = self._c().execute("SELECT excerpt FROM memory_refs WHERE document_id = ?", (document_id,)).fetchone()
        return None if row is None or row[0] is None else decompress_capped(bytes(row[0]))

    def mark_memory_refs_checked(self, document_ids) -> None:
        with self._tx() as c:
            now = self._now()
            c.executemany("UPDATE memory_refs SET checked_at = ? WHERE document_id = ?",
                          [(now, d) for d in document_ids])

    def delete_memory_refs(self, document_ids, expected=None) -> int:
        n = 0
        with self._tx() as c:
            for d in dict.fromkeys(document_ids):
                if expected is not None:
                    row = c.execute("SELECT created_at, checked_at FROM memory_refs WHERE document_id = ?",
                                    (d,)).fetchone()
                    if row is None or d not in expected \
                            or tuple(expected[d]) != (_dt_(row[0]), _dt_(row[1])):
                        continue
                c.execute("DELETE FROM memory_ref_images WHERE document_id = ?", (d,))
                n += c.execute("DELETE FROM memory_refs WHERE document_id = ?", (d,)).rowcount
            self._drop_orphan_images(c)
        return n
