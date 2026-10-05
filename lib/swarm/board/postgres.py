"""The Postgres board backend (production).

The SQL here is the SQL swarm.py and swarm_hooks.py ran before the board package existed,
moved over verbatim: a live database already carries this schema, its views and triggers, and
running `swarm tail`/`swarm watch` processes LISTEN on the swarm_board/swarm_state channels.
Do not change a statement here without a migration story for existing databases.

psycopg is imported only by this module; board/__init__.py imports it lazily, so hooks and
CLI paths that never reach Postgres never load psycopg.
"""
from __future__ import annotations

import contextlib
import datetime as _dt
import getpass
import json
import os
import random
import socket
import threading
import time
from pathlib import Path
from typing import Mapping, Sequence

import psycopg
from psycopg import sql

from .base import (LEFT_PAUSED, PauseRecord, build_manifest, database_hosts, MOVED_PREFIX, NAME_PATTERN, check_images, check_name, decompress_capped, decompress_transcript, MemoryRef, valid_pool, restart_over_limits, STUCK_PREFIX, CloseGuard, AUTO_CLOSED_BY, MEMORY_SEEN_MAX, NAME_SOURCES, RESTART_OUTCOMES, ROUTE_STATES, TOOL_NAME_MAX,
                   AgentEvent, Restart,
                   AgentStatus, Board, BoardError, BoardUnavailable, IncompatibleStorage, JobStatus,
                   Member, Message, OwedReply, ReadResult, Route, RosterEntry, SCHEMA_VERSION, SetupResult,
                   SpawnGrant, SyncState, TRANSCRIPT_ROLES, TranscriptImage, TranscriptRow, TranscriptSummary,
                   VERDICTS, load_name_pool)
from swarm import compat

SCHEMA = """
CREATE TABLE IF NOT EXISTS name_pool (
    name   text PRIMARY KEY,
    source text NOT NULL CHECK (source IN ('simpsons', 'english'))
);
CREATE TABLE IF NOT EXISTS jobs (
    job         text PRIMARY KEY,
    description text,
    created_by  text,
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS agents (
    agent_key    text PRIMARY KEY,
    name         text NOT NULL CHECK (length(name) > 0),
    job          text NOT NULL REFERENCES jobs(job) ON DELETE CASCADE,
    role         text,
    host         text,
    joined_at    timestamptz NOT NULL DEFAULT now(),
    last_seen    timestamptz NOT NULL DEFAULT now(),
    last_read_id bigint NOT NULL DEFAULT 0,
    left_at      timestamptz
);
CREATE UNIQUE INDEX IF NOT EXISTS agents_active_name ON agents (name) WHERE left_at IS NULL;
CREATE TABLE IF NOT EXISTS messages (
    id         bigserial PRIMARY KEY,
    job        text NOT NULL REFERENCES jobs(job) ON DELETE CASCADE,
    agent_name text NOT NULL CHECK (length(agent_name) > 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    message    varchar({max_chars}) NOT NULL CHECK (length(message) > 0),
    to_agent   text,
    agent_key  text,
    host       text
);
CREATE INDEX IF NOT EXISTS messages_job_id ON messages (job, id);
CREATE INDEX IF NOT EXISTS messages_created_at ON messages (created_at);
-- job_status's last_activity_at: max(created_at) per job (was a backward scan of the index above, filtered by job)
CREATE INDEX IF NOT EXISTS messages_job_created_at ON messages (job, created_at);
-- `swarm tail` LISTENs on this channel so new messages show up instantly.
CREATE OR REPLACE FUNCTION swarm_notify() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN PERFORM pg_notify('swarm_board', NEW.id::text); RETURN NEW; END $$;
DROP TRIGGER IF EXISTS messages_notify ON messages;
CREATE TRIGGER messages_notify AFTER INSERT ON messages FOR EACH ROW EXECUTE FUNCTION swarm_notify();
-- Lifecycle, written by the hooks. `state` holds the last recorded event; idle and dead are
-- derived from silence in the agent_status view, since an agent that stops talking cannot
-- report it. Added after the first release, hence ALTER ... IF NOT EXISTS (init upgrades in place).
ALTER TABLE agents ADD COLUMN IF NOT EXISTS state text NOT NULL DEFAULT 'started';
ALTER TABLE agents ADD COLUMN IF NOT EXISTS tool_calls integer NOT NULL DEFAULT 0;
ALTER TABLE agents ADD COLUMN IF NOT EXISTS current_tool text;
ALTER TABLE agents ADD COLUMN IF NOT EXISTS tool_started_at timestamptz;
ALTER TABLE agents ADD COLUMN IF NOT EXISTS last_post_at timestamptz;
DO $$ BEGIN
    ALTER TABLE agents ADD CONSTRAINT agents_state_check
        CHECK (state IN ('started', 'running', 'completed', 'left', 'dead'));
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
-- Job lifecycle: `activate` sets active, `deactivate --status` closes it with an outcome.
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS task text;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS status text NOT NULL DEFAULT 'active';
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS outcome text;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS session_id text;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS activated_at timestamptz;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS finished_at timestamptz;
-- `swarm watch` LISTENs here to redraw the moment an agent or job changes. Statement-level, so a
-- hook's UPDATE costs one notification; Postgres also folds duplicates within a transaction.
CREATE OR REPLACE FUNCTION swarm_state_notify() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN PERFORM pg_notify('swarm_state', TG_TABLE_NAME); RETURN NULL; END $$;
DROP TRIGGER IF EXISTS agents_state_notify ON agents;
CREATE TRIGGER agents_state_notify AFTER INSERT OR UPDATE OR DELETE ON agents
    FOR EACH STATEMENT EXECUTE FUNCTION swarm_state_notify();
DROP TRIGGER IF EXISTS jobs_state_notify ON jobs;
CREATE TRIGGER jobs_state_notify AFTER INSERT OR UPDATE OR DELETE ON jobs
    FOR EACH STATEMENT EXECUTE FUNCTION swarm_state_notify();
DO $$ BEGIN
    ALTER TABLE jobs ADD CONSTRAINT jobs_status_check
        CHECK (status IN ('active', 'paused', 'completed', 'cancelled', 'failed'));
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
-- Schema version 12: a job can be 'paused' (swarm pause). A board set up before has the narrower check:
-- replaced in place, once (the definition is looked at, so re-running init takes no table lock).
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = 'jobs'::regclass AND conname = 'jobs_status_check'
               AND pg_get_constraintdef(oid) NOT LIKE '%paused%') THEN
        ALTER TABLE jobs DROP CONSTRAINT jobs_status_check;
        ALTER TABLE jobs ADD CONSTRAINT jobs_status_check
            CHECK (status IN ('active', 'paused', 'completed', 'cancelled', 'failed'));
    END IF;
END $$;
-- Per-agent sync state: what the hooks last told the agent (roster, memory recalls, reminders).
ALTER TABLE agents ADD COLUMN IF NOT EXISTS roster_seen text;
ALTER TABLE agents ADD COLUMN IF NOT EXISTS roster_synced_at timestamptz;
ALTER TABLE agents ADD COLUMN IF NOT EXISTS memory_recalled_at timestamptz;
ALTER TABLE agents ADD COLUMN IF NOT EXISTS memory_seen text[] NOT NULL DEFAULT '{}';
ALTER TABLE agents ADD COLUMN IF NOT EXISTS remembered_at timestamptz;
ALTER TABLE agents ADD COLUMN IF NOT EXISTS nudged_at timestamptz;
-- The memory project (Hindsight bank) of a job; NULL means the job name.
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS project text;
-- Talking on the board: replies owed (reminded once) and the "post a status" nudge.
ALTER TABLE agents ADD COLUMN IF NOT EXISTS reply_reminded_id bigint NOT NULL DEFAULT 0;
ALTER TABLE agents ADD COLUMN IF NOT EXISTS calls_at_post integer NOT NULL DEFAULT 0;
ALTER TABLE agents ADD COLUMN IF NOT EXISTS silence_nudged_at timestamptz;
CREATE INDEX IF NOT EXISTS messages_to_agent ON messages (to_agent, id) WHERE to_agent IS NOT NULL;
-- Which of a Claude session's swarm jobs each subagent was routed to (several jobs can share one
-- session): cached so later hooks never re-read the subagent's transcript. No reference to
-- jobs or agents on purpose: a route may name no job, or one closed meanwhile.
CREATE TABLE IF NOT EXISTS agent_routes (
    agent_key  text PRIMARY KEY,
    session_id text,
    state      text NOT NULL CHECK (state IN ('pending', 'unverified', 'final')),
    job        text,
    created_at timestamptz NOT NULL DEFAULT now()
);
-- Goals and the judge: what "done" means for a job, and the verdict of its one judge agent.
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS goal text;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS verdict text;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS verdict_reason text;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS verdict_next text;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS verdict_by text;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS verdict_at timestamptz;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS completion_forced boolean NOT NULL DEFAULT false;
DO $$ BEGIN
    ALTER TABLE jobs ADD CONSTRAINT jobs_verdict_check CHECK (verdict IN ('met', 'not_met'));
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
ALTER TABLE agents ADD COLUMN IF NOT EXISTS judge boolean NOT NULL DEFAULT false;
CREATE UNIQUE INDEX IF NOT EXISTS agents_one_judge ON agents (job) WHERE judge AND left_at IS NULL;
-- Subagents spawned by swarm agents (the hook enforces the [spawn] caps): per agent and per job.
ALTER TABLE agents ADD COLUMN IF NOT EXISTS spawns integer NOT NULL DEFAULT 0;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS spawns integer NOT NULL DEFAULT 0;
-- Verifiers: any number per job, read-only checkers of the other agents' claims.
ALTER TABLE agents ADD COLUMN IF NOT EXISTS verifier boolean NOT NULL DEFAULT false;
-- What an open job is waiting for (`swarm wait --on`), and since when; NULL = not waiting.
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS waiting_on text;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS waiting_since timestamptz;
-- Schema version 11: when a bounded wait (`swarm wait --for`) expires, and the job's own
-- lifetime cap in hours (`activate --max-hours`; NULL = the [job] max_hours default).
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS waiting_until timestamptz;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS max_hours double precision;
-- Who closed the job: 'auto' for the auto-close sweep, else who ran `swarm deactivate`.
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS closed_by text;
-- The transcript archive ([transcripts], bin/transcripts.py): one row per (job, agent_key), the
-- redacted JSONL lzma-compressed in body. No reference to jobs: transcripts outlive them.
CREATE TABLE IF NOT EXISTS transcripts (
    job          text NOT NULL,
    agent_key    text NOT NULL,
    agent_name   text NOT NULL,
    role         text NOT NULL CHECK (role IN ('subagent', 'orchestrator')),
    host         text,
    session_id   text,
    captured_at  timestamptz NOT NULL DEFAULT now(),
    final        boolean NOT NULL DEFAULT false,
    raw_bytes    bigint NOT NULL,
    stored_bytes bigint NOT NULL,
    redactions   integer NOT NULL DEFAULT 0,
    sha256       text NOT NULL,
    body         bytea NOT NULL,
    PRIMARY KEY (job, agent_key)
);
CREATE INDEX IF NOT EXISTS transcripts_captured_at ON transcripts (captured_at);
CREATE INDEX IF NOT EXISTS transcripts_agent_name ON transcripts (agent_name);

-- Images taken out of transcripts (schema version 4): once per sha256, raw bytes (not
-- recompressed), and which transcripts refer to them. An image goes when its last reference
-- does; image writes and deletes take the TRANSCRIPT_IMAGE_LOCK advisory lock.
CREATE TABLE IF NOT EXISTS transcript_images (
    sha256     text PRIMARY KEY,
    mime       text NOT NULL,
    bytes      bigint NOT NULL,
    data       bytea NOT NULL,
    first_seen timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS transcript_image_refs (
    job       text NOT NULL,
    agent_key text NOT NULL,
    sha256    text NOT NULL REFERENCES transcript_images (sha256),
    PRIMARY KEY (job, agent_key, sha256)
);
CREATE INDEX IF NOT EXISTS transcript_image_refs_sha256 ON transcript_image_refs (sha256);

-- Memory provenance (schema version 8): where a memory an agent saved came from, with an excerpt of
-- its transcript (lzma of the redacted JSONL); its images are in transcript_images, referenced
-- below (so writes take the same advisory lock). The first writer of a document_id keeps the
-- row (Board.save_memory_ref). No reference to jobs: refs follow the memory, not the job.
CREATE TABLE IF NOT EXISTS memory_refs (
    document_id  text PRIMARY KEY,
    bank         text NOT NULL,
    job          text NOT NULL,
    agent_key    text NOT NULL,
    agent_name   text NOT NULL,
    harness      text,
    host         text,
    session_id   text,
    tool_call_id text,
    -- The writer is a name from [provenance] writers, or swarm-remember (checked by save_memory_ref).
    writer       text NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now(),
    checked_at   timestamptz,
    patched      boolean NOT NULL DEFAULT false,
    raw_bytes    bigint NOT NULL DEFAULT 0,
    redactions   integer NOT NULL DEFAULT 0,
    excerpt      bytea
);
CREATE INDEX IF NOT EXISTS memory_refs_job ON memory_refs (job, agent_key);
CREATE TABLE IF NOT EXISTS memory_ref_images (
    document_id text NOT NULL REFERENCES memory_refs (document_id) ON DELETE CASCADE,
    sha256      text NOT NULL REFERENCES transcript_images (sha256),
    PRIMARY KEY (document_id, sha256)
);
CREATE INDEX IF NOT EXISTS memory_ref_images_sha256 ON memory_ref_images (sha256);

-- Board metadata: key 'schema_version' is the base.SCHEMA_VERSION setup last installed (boards
-- set up before it existed have no row: version 0). board.ensure_initialized reads it.
CREATE TABLE IF NOT EXISTS board_meta (
    key   text PRIMARY KEY,
    value text NOT NULL
);
ALTER TABLE agents ADD COLUMN IF NOT EXISTS harness text;
ALTER TABLE agents ADD COLUMN IF NOT EXISTS model text;
ALTER TABLE agents ADD COLUMN IF NOT EXISTS turn_ended_at timestamptz;
ALTER TABLE agents ADD COLUMN IF NOT EXISTS os_user text;
ALTER TABLE transcripts ADD COLUMN IF NOT EXISTS harness text;
-- The supervisor (schema version 6): why an agent was closed, whom a replacement took over
-- from, the per-job kill switch, and one row per replacement started (or refused) for a closed
-- agent; old_agent_key is UNIQUE: each closed agent is replaced at most once.
ALTER TABLE agents ADD COLUMN IF NOT EXISTS left_reason text;
ALTER TABLE agents ADD COLUMN IF NOT EXISTS resume_of text;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS supervise boolean NOT NULL DEFAULT true;
-- Schema version 9: a final capture that kept failing is stored as a marker row without a body,
-- holding why (transcripts.capture_failed_row).
ALTER TABLE transcripts ADD COLUMN IF NOT EXISTS capture_failed text;
CREATE TABLE IF NOT EXISTS restarts (
    id            bigserial PRIMARY KEY,
    job           text NOT NULL,
    agent_key     text NOT NULL,
    attempt       integer NOT NULL,
    at            timestamptz NOT NULL DEFAULT now(),
    reason        text NOT NULL,
    old_agent_key text NOT NULL UNIQUE,
    new_agent_key text,
    harness       text NOT NULL,
    host          text NOT NULL,
    os_user       text NOT NULL,
    minutes_cap   double precision NOT NULL,
    ended_at      timestamptz,
    outcome       text
);
CREATE INDEX IF NOT EXISTS restarts_job ON restarts (job, agent_key);
CREATE INDEX IF NOT EXISTS restarts_host_at ON restarts (host, os_user, at);
-- Pause/resume (schema version 12): one row per pause of a job, with its resume manifest (JSON text).
-- No reference to jobs: the history outlives the job row's purge.
CREATE TABLE IF NOT EXISTS job_pauses (
    id           bigserial PRIMARY KEY,
    job          text NOT NULL,
    paused_at    timestamptz NOT NULL DEFAULT now(),
    paused_by    text,
    reason       text,
    manifest     text NOT NULL,
    resumed_at   timestamptz,
    resumed_by   text,
    resumed_host text,
    outcome      text
);
CREATE INDEX IF NOT EXISTS job_pauses_job ON job_pauses (job, id);
"""

# Thresholds are baked in from config each time `init` runs (re-run it after changing them).
STATUS_VIEW = """
DROP VIEW IF EXISTS job_status;
DROP VIEW IF EXISTS agent_status;
CREATE VIEW agent_status AS
SELECT a.job, a.name, CASE WHEN a.judge THEN 'judge' WHEN a.verifier THEN 'verifier' ELSE a.role END AS role,
       CASE
         WHEN a.state IN ('completed', 'left', 'dead') THEN a.state
         WHEN a.current_tool IS NOT NULL
              AND a.tool_started_at > now() - make_interval(mins => {tool_timeout}) THEN 'running'
         WHEN a.last_seen < now() - make_interval(mins => {dead}) THEN 'dead'
         WHEN a.last_seen < now() - make_interval(mins => {idle}) THEN 'idle'
         ELSE a.state
       END AS status,
       a.current_tool, a.tool_calls,
       (SELECT count(*) FROM messages m
         WHERE m.job = a.job AND m.agent_name = a.name AND m.created_at >= a.joined_at) AS messages,
       a.joined_at, a.last_seen AS last_contact_at, a.last_post_at, a.left_at AS ended_at,
       a.host, a.agent_key, a.harness, a.model, a.os_user, a.left_reason, a.resume_of
  FROM agents a;

DROP VIEW IF EXISTS job_status;
CREATE VIEW job_status AS
SELECT j.job, j.status, j.description, j.task, j.outcome, j.created_by, j.session_id,
       j.created_at, j.activated_at, j.finished_at,
       count(s.agent_key)                                  AS agents,
       count(*) FILTER (WHERE s.status = 'started')        AS started,
       count(*) FILTER (WHERE s.status = 'running')        AS running,
       count(*) FILTER (WHERE s.status = 'idle')           AS idle,
       count(*) FILTER (WHERE s.status = 'completed')      AS completed,
       count(*) FILTER (WHERE s.status IN ('dead', 'left')) AS dead_or_left,
       (SELECT count(*) FROM messages m WHERE m.job = j.job) AS messages,
       greatest(max(s.last_contact_at),
                (SELECT max(created_at) FROM messages m WHERE m.job = j.job)) AS last_activity_at,
       j.project, j.goal, j.verdict, j.verdict_reason, j.verdict_by, j.verdict_at, j.completion_forced,
       (SELECT a.name FROM agents a WHERE a.job = j.job AND a.judge AND a.left_at IS NULL) AS judge,
       j.waiting_on, j.waiting_since, j.closed_by, j.supervise, j.verdict_next,
       j.max_hours, j.waiting_until,
       -- what `status` shows (base.derive_job_status): closed jobs their status, open ones waiting /
       -- active / waiting (goal not met: a goal without a met verdict and nobody started, running
       -- or idle; no sweep closes it) / idle
       CASE WHEN j.status <> 'active' THEN j.status
            WHEN COALESCE(j.waiting_on, '') <> '' THEN 'waiting'
            WHEN count(*) FILTER (WHERE s.status IN ('started', 'running')) > 0 THEN 'active'
            WHEN COALESCE(j.goal, '') <> '' AND j.verdict IS DISTINCT FROM 'met'
                 AND count(*) FILTER (WHERE s.status = 'idle') = 0 THEN 'waiting (goal not met)'
            WHEN COALESCE(greatest(max(s.last_contact_at),
                                   (SELECT max(created_at) FROM messages m WHERE m.job = j.job)),
                          j.activated_at, j.created_at) > now() - make_interval(mins => {idle}) THEN 'active'
            ELSE 'idle'
       END AS shown_status
  FROM jobs j LEFT JOIN agent_status s ON s.job = j.job
 GROUP BY j.job;
"""

# NOTIFY channels, fixed by the triggers above (and by any tail/watch already running).
CHANNEL_MESSAGES = "swarm_board"
CHANNEL_STATE = "swarm_state"

# Column lists in dataclass field order, so rows map positionally.
_AGENT_STATUS_COLS = ("job, name, role, status, current_tool, tool_calls, messages, joined_at, "
                      "last_contact_at, last_post_at, ended_at, host, agent_key, harness, model, os_user, "
                      "left_reason, resume_of")
_JOB_STATUS_COLS = ("job, status, description, task, outcome, created_by, session_id, created_at, "
                    "activated_at, finished_at, agents, started, running, idle, completed, "
                    "dead_or_left, messages, last_activity_at, project, goal, verdict, verdict_reason, "
                    "verdict_by, verdict_at, completion_forced, judge, waiting_on, waiting_since, closed_by, "
                    "supervise, verdict_next, max_hours, waiting_until")
_MESSAGE_COLS = "id, created_at, job, agent_name, to_agent, message"
_RESTART_COLS = ("id, job, agent_key, attempt, at, reason, old_agent_key, new_agent_key, harness, host, "
                 "os_user, minutes_cap, ended_at, outcome")   # Restart field order

# The debounce drain after a wake-up: collects what is already on the wire (as watch did).
_DRAIN_TIMEOUT = 0.01
LIVE_RETRY_SECONDS = 15.0   # how often a degraded or polling watcher tries the primary / LISTEN again

# Claim a name for an agent key: a new row, or a full reset of the key's departed row (fresh
# cursor, counters, sync state and join time). The partial unique index rejects a name an
# active agent holds. The cursor leaves the job's newest join_history messages unread (the id of
# the (join_history+1)-th newest message, or 0), so a newcomer's first read is recent history.
_CLAIM_NAME = ("INSERT INTO agents (agent_key, name, job, role, host, os_user, last_read_id) "
               "VALUES (%s, %s, %s, %s, %s, %s, COALESCE((SELECT id FROM messages WHERE job = %s "
               "ORDER BY id DESC OFFSET %s LIMIT 1), 0)) "
               "ON CONFLICT (agent_key) DO UPDATE SET name = EXCLUDED.name, job = EXCLUDED.job, "
               "role = EXCLUDED.role, host = EXCLUDED.host, left_at = NULL, joined_at = now(), last_seen = now(), "
               "last_read_id = EXCLUDED.last_read_id, state = 'started', tool_calls = 0, "
               "current_tool = NULL, tool_started_at = NULL, last_post_at = NULL, "
               "roster_seen = NULL, roster_synced_at = NULL, memory_recalled_at = NULL, "
               "memory_seen = '{}', remembered_at = NULL, nudged_at = NULL, "
               "reply_reminded_id = 0, calls_at_post = 0, silence_nudged_at = NULL, judge = false, "
               "verifier = false, harness = NULL, model = NULL, turn_ended_at = NULL, "
               "os_user = EXCLUDED.os_user, left_reason = NULL, resume_of = NULL")
# A supervisor replacement (claim_resume) takes its predecessor's name, role, read cursor and
# judge/verifier marks: a new row, or a full reset of the key's row like _CLAIM_NAME's.
_CLAIM_RESUME = ("INSERT INTO agents (agent_key, name, job, role, host, os_user, last_read_id, judge, "
                 "verifier, resume_of) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                 "ON CONFLICT (agent_key) DO UPDATE SET name = EXCLUDED.name, job = EXCLUDED.job, "
                 "role = EXCLUDED.role, host = EXCLUDED.host, left_at = NULL, joined_at = now(), "
                 "last_seen = now(), last_read_id = EXCLUDED.last_read_id, state = 'started', tool_calls = 0, "
                 "current_tool = NULL, tool_started_at = NULL, last_post_at = NULL, "
                 "roster_seen = NULL, roster_synced_at = NULL, memory_recalled_at = NULL, "
                 "memory_seen = '{}', remembered_at = NULL, nudged_at = NULL, "
                 "reply_reminded_id = 0, calls_at_post = 0, silence_nudged_at = NULL, "
                 "judge = EXCLUDED.judge, verifier = EXCLUDED.verifier, harness = NULL, model = NULL, "
                 "turn_ended_at = NULL, os_user = EXCLUDED.os_user, spawns = 0, left_reason = NULL, "
                 "resume_of = EXCLUDED.resume_of")
# Held from before an INSERT INTO messages until its commit, so ids commit (and become visible)
# in id order: a reader's cursor can then never pass an id that commits later. ('SWRM')
POST_LOCK = 0x5357524D
# The agent's sync state (SyncState field order) plus the replies it owes, from `agents me`.
# The lateral subquery runs once per call (it depends on `me` only): messages addressed to the
# agent on its job since it joined, already shown (id <= cursor), not yet reminded, and with no
# later post by the agent addressed to that sender. The partial index keeps it a short scan.
_SYNC_FROM = (
    "SELECT me.name, me.joined_at, now(), me.roster_seen, me.roster_synced_at, "
    "me.memory_recalled_at, me.memory_seen, me.remembered_at, me.nudged_at, me.tool_calls, "
    "me.calls_at_post, me.last_post_at, me.silence_nudged_at, me.reply_reminded_id, owed.list{extra} "
    "FROM agents me LEFT JOIN LATERAL (SELECT json_agg(json_build_object('id', m.id, "
    "'sender', m.agent_name, 'at', m.created_at) ORDER BY m.id) AS list FROM messages m "
    "WHERE m.to_agent = me.name AND m.job = me.job AND m.id > me.reply_reminded_id "
    "AND m.id <= me.last_read_id AND m.created_at >= me.joined_at AND NOT EXISTS ("
    "SELECT 1 FROM messages r WHERE r.job = m.job AND r.agent_name = me.name "
    "AND r.to_agent = m.agent_name AND r.id > m.id)) owed ON true")
_SYNC_WIDTH = 15
_ROSTER_COLS = "agent_key, name, role, status, current_tool, ended_at IS NULL"
# One snapshot: the job's top id, how many are unread, and the first page of them.
_READ = ("SELECT s.top, s.unread, m.id, m.created_at, m.job, m.agent_name, m.to_agent, m.message "
         "FROM (SELECT (SELECT max(id) FROM messages WHERE job = %(job)s) AS top, "
         "(SELECT count(*) FROM messages WHERE job = %(job)s AND id > %(last)s "
         "AND agent_name <> %(me)s) AS unread) s "
         "LEFT JOIN LATERAL (SELECT id, created_at, job, agent_name, to_agent, message FROM messages "
         "WHERE job = %(job)s AND id > %(last)s AND agent_name <> %(me)s ORDER BY id LIMIT %(lim)s) m "
         "ON true ORDER BY m.id")
# "<english name> NNN" draws before allocate_name gives up once the pool is exhausted.
_FALLBACK_ATTEMPTS = 5


def _password(db: dict) -> str | None:
    if os.environ.get("PGPASSWORD"):
        return os.environ["PGPASSWORD"]
    env_file = db.get("password_env_file")
    if env_file:
        p = Path(env_file).expanduser()
        if p.exists():
            for line in p.read_text(encoding="utf-8").splitlines():
                if line.strip().startswith("PGPASSWORD="):
                    return line.split("=", 1)[1].strip().strip("'\"")
    return None


# Client-side deadline for every round trip to the server, unless [database]
# query_timeout_seconds says otherwise (0 = none). Below the hooks' 10 s timeout on purpose:
# a hook gives up and logs why before Claude Code kills it.
DEFAULT_QUERY_TIMEOUT = 8.0


class _Watchdog:
    """Shuts a connection's socket down if the round trip it guards outlives `seconds`.

    Runs in a timer thread; stop() (called by the guarded thread as soon as the round trip ends)
    and the timer's firing are serialised by a lock, so the socket is only ever shut while the
    guarded thread is still inside that round trip. shutdown() (not close()) leaves the file
    descriptor to libpq: the blocked poll() wakes, libpq reads EOF and marks the connection bad.
    """

    def __init__(self, fd: int, seconds: float):
        self.fd, self.fired, self._done = fd, False, False
        self._lock = threading.Lock()
        self._timer = threading.Timer(seconds, self._fire)
        self._timer.daemon = True
        self._timer.start()

    def _fire(self) -> None:
        with self._lock:
            if self._done:
                return
            self.fired = True
            try:
                with socket.socket(fileno=os.dup(self.fd)) as s:
                    s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def stop(self) -> None:
        with self._lock:
            self._done = True
        self._timer.cancel()


class _DeadlineConnection(psycopg.Connection):
    """A psycopg connection whose every round trip has a client-side deadline.

    psycopg 3 has no query timeout, and all of a connection's I/O (execute and fetch, COMMIT,
    LISTEN, the notifies() of wait_for_change) goes through Connection.wait(), so the deadline
    is enforced there: a watchdog shuts the socket down when it passes, and the stuck wait
    raises BoardUnavailable. A server-side statement_timeout would NOT help: in the stall this
    guards against the server is not running the query, it is waiting on the client (the
    session sits in state=active, wait_event=ClientRead while the result never arrives), so
    there is nothing for the server to time out. A cancel request (cancel_safe) would not help
    either, for the same reason, and it travels a new connection through the same pooler.
    Closing our end is the one thing that reliably releases the client.

    After a deadline the connection is closed, so the board's later calls fail at once: a hook
    pays the deadline once, not once per remaining query."""

    query_timeout: float = 0.0

    def wait(self, gen, *args, **kwargs):
        if self.closed:
            raise _closed_error()
        if not self.query_timeout:
            return super().wait(gen, *args, **kwargs)
        # notifies() waits `timeout` on purpose (no notification is not a stall); allow for it
        waiting = kwargs.get("timeout", args[1] if len(args) > 1 else None) or 0.0
        dog = _Watchdog(self.pgconn.socket, self.query_timeout + waiting)
        try:
            return super().wait(gen, *args, **kwargs)
        except psycopg.OperationalError as exc:
            if dog.fired:
                self.pgconn.finish()
                raise BoardUnavailable(f"no reply from the database within the "
                                       f"{self.query_timeout:g}s deadline "
                                       f"([database] query_timeout_seconds)") from exc
            if self.closed:  # the connection itself is gone, not just this statement
                raise BoardUnavailable(str(exc)) from exc
            raise
        finally:
            dog.stop()

    def cursor(self, *args, **kwargs):
        if self.closed:  # execute() on a closed connection fails here, before any wait()
            raise _closed_error()
        return super().cursor(*args, **kwargs)


def _closed_error() -> BoardUnavailable:
    """After a deadline or a lost connection the board is unavailable at once, not per query."""
    cause = psycopg.OperationalError("the connection is closed")
    err = BoardUnavailable(str(cause))
    err.__cause__ = cause
    return err


def _query_timeout(db: dict) -> float:
    value = db.get("query_timeout_seconds", DEFAULT_QUERY_TIMEOUT)
    return max(0.0, float(value if value is not None else 0))


def _server_args(db: dict, any_host: bool = False) -> dict:
    """The libpq host/port arguments. One host: exactly host and port, as ever. Several: libpq's
    multi-host form, tried in order, and target_session_attrs=read-write so that only the primary
    is accepted (a connection follows a switchover); any_host accepts a standby too."""
    hosts = database_hosts(db)
    if len(hosts) == 1:
        return {"host": hosts[0][0], "port": hosts[0][1]}
    return {"host": ",".join(h for h, _ in hosts), "port": ",".join(str(p) for _, p in hosts),
            "target_session_attrs": "any" if any_host else "read-write"}


MULTI_HOST_CONNECT_TIMEOUT = 3   # psycopg applies connect_timeout to each host in turn

def _connect_timeout(db: dict, hosts: int) -> int:
    """[database] connect_timeout, but at most MULTI_HOST_CONNECT_TIMEOUT per host when there are
    several: a dead host costs a few seconds, not the whole budget, and the next host is tried."""
    timeout = db["connect_timeout"]
    return min(timeout, MULTI_HOST_CONNECT_TIMEOUT) if hosts > 1 and timeout else timeout


def _connect(cfg: dict, admin: bool = False, any_host: bool = False) -> psycopg.Connection:
    """One autocommit connection, as swarm.connect() made it, with the query deadline.
    Failure -> BoardUnavailable."""
    db = cfg["database"]
    # No named prepared statements unless [database] prepared_statements = true. psycopg
    # prepares a query after its 5th execution; through a connection pooler, a LISTENing
    # connection that receives NOTIFYs while running prepared statements can deadlock: the
    # backend waits on the client (ClientRead), the pooler waits on the backend, and the query never
    # returns. prepare_threshold=None avoids that; the board's queries are cheap, so re-planning
    # them costs nothing that matters. The query deadline above is the second line of defence.
    extra = {} if db.get("prepared_statements") else {"prepare_threshold": None}
    if db.get("application_name"):
        extra["application_name"] = db["application_name"]
    hosts = database_hosts(db)   # a bad host or port list: BoardError, saying which
    try:
        conn = _DeadlineConnection.connect(
            **_server_args(db, any_host), user=db["user"], password=_password(db),
            dbname=db["admin_dbname"] if admin else db["dbname"],
            connect_timeout=_connect_timeout(db, len(hosts)), sslmode=db["sslmode"], autocommit=True, **extra)
    except Exception as exc:
        if len(hosts) > 1 and not any_host and isinstance(exc, psycopg.OperationalError):
            raise BoardUnavailable(f"no primary reachable among {', '.join(h for h, _ in hosts)}: "
                                   f"{exc}") from exc
        raise BoardUnavailable(str(exc)) from exc
    conn.query_timeout = _query_timeout(db)
    return conn


def _ensure_database(cfg: dict) -> list[str]:
    """Create the board database if missing (notes say so); refuse a non-UTF8 one."""
    dbname = cfg["database"]["dbname"]
    with _connect(cfg, admin=True) as conn:
        row = conn.execute("SELECT pg_encoding_to_char(encoding) FROM pg_database WHERE datname = %s",
                           (dbname,)).fetchone()
        if not row:
            # Explicit UTF8 from template0: a cluster default of SQL_ASCII would return names as
            # bytes and count the message cap in bytes, not characters.
            conn.execute(sql.SQL("CREATE DATABASE {} ENCODING 'UTF8' TEMPLATE template0")
                         .format(sql.Identifier(dbname)))
            return [f"created database {dbname} (UTF8)"]
    # (an SQL_ASCII admin database returns text as bytes, hence the decode)
    enc = row[0].decode() if isinstance(row[0], bytes) else row[0]
    if enc != "UTF8":
        raise IncompatibleStorage(
            f"database {dbname} has encoding {enc}; the board needs UTF8. "
            f"Drop it (it only holds board data) and re-run init.")
    return []


def _database_missing(cfg: dict) -> bool | None:
    """Whether the board database doesn't exist, asked on admin_dbname (the connection error
    doesn't say reliably: through a connection pooler it is "unable to get session context"). None when
    the server can't be asked."""
    try:
        with _connect(cfg, admin=True) as admin:
            return bool(admin.execute("SELECT NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = %s)",
                                      (cfg["database"]["dbname"],)).fetchone()[0])
    except Exception:
        return None


def _connect_new(cfg: dict, wait: float = 10.0) -> psycopg.Connection:
    """_connect to a database just created: through a connection pooler the first connections can fail
    ("kind does not match between main and slot") until every node has it. Retries until `wait`."""
    deadline = time.monotonic() + wait
    while True:
        try:
            return _connect(cfg)
        except BoardUnavailable:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.5)


# Schema version 7: agent names must match base.NAME_RULE
# and image keys be 64 lowercase hex digits, whoever writes (the role is shared). Added NOT VALID:
# enforced on every row written from now on, while rows already stored don't block the upgrade.
# Postgres checks a NOT VALID constraint on every UPDATE too, so an agents row with an invalid
# name could never be touched again (last_seen, left_at...): those rows (forgeries, or names from
# a custom pool that NAME_RULE refuses) are deleted when the check is added; such an agent gets a
# fresh name on its next join. Messages and images are never updated, so theirs stay readable.
_NAME_RE = "'^" + NAME_PATTERN.replace("'", "''") + "$'"
CHECKS = (
    ("messages", "check_messages_agent_name", f"agent_name ~ {_NAME_RE}", ""),
    ("messages", "check_messages_to_agent", f"to_agent IS NULL OR to_agent ~ {_NAME_RE}", ""),
    ("agents", "check_agents_name", f"name ~ {_NAME_RE}", f"DELETE FROM agents WHERE name !~ {_NAME_RE};"),
    ("transcript_images", "check_transcript_images_sha256", "sha256 ~ '^[0-9a-f]{64}$'", ""),
    # schema 8 (memory_ref_images.sha256 references transcript_images, checked above)
    ("memory_refs", "check_memory_refs_agent_name", f"agent_name ~ {_NAME_RE}", ""),
)


def _install_checks(conn: psycopg.Connection) -> None:
    for table, name, expr, before in CHECKS:
        conn.execute(
            "DO $$ BEGIN\n"
            f"  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = '{name}' "
            f"AND conrelid = '{table}'::regclass) THEN\n"
            f"    {before}\n"
            f"    ALTER TABLE {table} ADD CONSTRAINT {name} CHECK ({expr}) NOT VALID;\n"
            "  END IF;\n"
            "EXCEPTION WHEN duplicate_object THEN NULL;\n"
            "END $$")


def _install_schema(conn: psycopg.Connection, b: dict) -> None:
    conn.execute(SCHEMA.replace("{max_chars}", str(int(b["message_max_chars"]))))
    # boards created before writers became configurable restrict memory_refs.writer to three names
    conn.execute("ALTER TABLE memory_refs DROP CONSTRAINT IF EXISTS memory_refs_writer_check")
    _install_checks(conn)
    conn.execute(STATUS_VIEW.format(idle=int(b["idle_minutes"]), dead=int(b["dead_minutes"]),
                                    tool_timeout=int(b["tool_timeout_minutes"])))


def _add_names(conn: psycopg.Connection, source: str, names: Sequence[str]) -> int:
    """Add a source's names to the pool (existing ones are skipped); returns its new size."""
    with conn.cursor() as cur:
        cur.executemany("INSERT INTO name_pool (name, source) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                        [(n, source) for n in names])
    return conn.execute("SELECT count(*) FROM name_pool WHERE source = %s", (source,)).fetchone()[0]


def _agent_match(agent_key: str | None, name: str | None) -> tuple[str, tuple]:
    """WHERE condition and parameter picking an agent by key if given, else by name."""
    return ("agent_key = %s", (agent_key,)) if agent_key else ("name = %s", (name,))


class PostgresBoard(Board):
    """A Board over one autocommit psycopg connection. Every statement commits on its own,
    which is what the pre-refactor code relied on (a failed INSERT in allocate_name's race
    loop does not poison the connection)."""

    def __init__(self, cfg: dict, readers: bool = False):
        super().__init__(cfg)
        self._listening: tuple[str, ...] = ()   # the channels subscribe() was asked for
        self._polling = False                   # LISTEN is unavailable: wait_for_change polls
        self._retry_at = 0.0
        try:
            self._conn = _connect(cfg)
        except BoardUnavailable:
            if not (readers and len(database_hosts(cfg["database"])) > 1):
                raise
            # no primary: a read-only command is served by whichever standby answers
            self._conn = _connect(cfg, any_host=True)
            self.degraded = self._conn.info.host

    @contextlib.contextmanager
    def op_timeout(self, seconds: float):
        """The client-side query deadline lowered to `seconds` (never raised) within the block.
        A deadline that fires closes the connection (see _DeadlineConnection)."""
        conn = self._conn
        old = conn.query_timeout
        conn.query_timeout = max(0.001, min(seconds, old) if old else seconds)
        try:
            yield
        finally:
            conn.query_timeout = old

    # ---- lifecycle -------------------------------------------------------------

    @classmethod
    def setup(cls, cfg: dict, names: Mapping[str, Sequence[str]]) -> SetupResult:
        names = valid_pool(names)   # a name that could forge context is never handed out
        notes = _ensure_database(cfg)
        with _connect_new(cfg) if notes else _connect(cfg) as conn:
            _install_schema(conn, cfg["board"])
            pool = {source: _add_names(conn, source, source_names) for source, source_names in names.items()}
            # last, so a setup that failed half-way is retried; never lowered
            conn.execute("INSERT INTO board_meta (key, value) VALUES ('schema_version', %s) "
                         "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value "
                         "WHERE board_meta.value::int < EXCLUDED.value::int", (str(SCHEMA_VERSION),))
        return SetupResult(tuple(notes), pool)

    @classmethod
    def schema_version(cls, cfg: dict) -> int | None:
        try:
            try:
                conn = _connect(cfg)
            except BoardUnavailable:
                if len(database_hosts(cfg["database"])) < 2:
                    raise
                conn = _connect(cfg, any_host=True)   # a question, so a standby will do
        except BoardUnavailable as exc:
            missing = _database_missing(cfg)
            if missing is None:
                raise exc   # the server itself is unreachable
            if missing:
                return None
            conn = _connect_new(cfg, wait=5.0)   # it exists: perhaps just created (see _connect_new)
        with conn:
            try:
                if conn.execute("SELECT to_regclass('board_meta')").fetchone()[0] is None:
                    return 0 if conn.execute("SELECT to_regclass('messages')").fetchone()[0] else None
                row = conn.execute("SELECT value FROM board_meta WHERE key = 'schema_version'").fetchone()
            except psycopg.Error as exc:
                raise BoardUnavailable(str(exc)) from exc
            return int(row[0]) if row else 0

    @classmethod
    def store_missing(cls, cfg: dict, cheap: bool = True) -> bool:
        return False if cheap else bool(_database_missing(cfg))

    @classmethod
    def identity(cls, cfg: dict) -> str:
        db = cfg["database"]
        return f"{','.join(f'{h}:{p}' for h, p in database_hosts(db))}/{db['dbname']}"

    @classmethod
    @contextlib.contextmanager
    def setup_lock(cls, cfg: dict, timeout: float):
        """Serialise setups across machines: a session advisory lock, taken on the admin
        database (the board database may not exist yet) and held until the block ends.
        TimeoutError if another setup holds it for longer than `timeout` seconds."""
        key = f"swarm-board-setup:{cfg['database']['dbname']}"
        deadline = time.monotonic() + timeout
        with _connect(cfg, admin=True) as conn:
            while not conn.execute("SELECT pg_try_advisory_lock(hashtext(%s))", (key,)).fetchone()[0]:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"another swarm setup of {cfg['database']['dbname']} is running")
                time.sleep(0.1)
            try:
                yield
            finally:
                try:
                    conn.execute("SELECT pg_advisory_unlock(hashtext(%s))", (key,))
                except Exception:
                    pass   # closing the connection releases it too

    def close(self) -> None:
        conn, self._conn = getattr(self, "_conn", None), None
        if conn is not None:
            conn.close()

    def now(self) -> _dt.datetime:
        return self._conn.execute("SELECT now()").fetchone()[0]

    # ---- retention ---------------------------------------------------------------

    def purge(self) -> None:
        days = int(self.board_cfg["retention_days"])
        stale = int(self.board_cfg["agent_stale_hours"])
        conn = self._conn
        paused = "(SELECT job FROM jobs WHERE status = 'paused')"   # a paused job keeps its history
        conn.execute(f"DELETE FROM messages WHERE created_at < now() - make_interval(days => %s) "
                     f"AND job NOT IN {paused}", (days,))
        conn.execute("UPDATE agents SET left_at = now(), state = 'dead', current_tool = NULL "
                     "WHERE left_at IS NULL AND last_seen < now() - make_interval(hours => %s)", (stale,))
        conn.execute(f"DELETE FROM agents WHERE left_at < now() - make_interval(days => %s) "
                     f"AND job NOT IN {paused}", (days,))
        conn.execute("DELETE FROM jobs j WHERE COALESCE(finished_at, activated_at, created_at) "
                     "< now() - make_interval(days => %s) "
                     "AND NOT EXISTS (SELECT 1 FROM messages m WHERE m.job = j.job) "
                     "AND NOT EXISTS (SELECT 1 FROM agents a WHERE a.job = j.job AND a.left_at IS NULL) "
                     "AND j.status <> 'paused'",
                     (days,))
        conn.execute("DELETE FROM agent_routes WHERE created_at < now() - make_interval(days => %s)", (days,))
        conn.execute("DELETE FROM restarts WHERE at < now() - make_interval(days => %s)", (days,))

    # ---- jobs --------------------------------------------------------------------

    def ensure_job(self, job: str, description: str | None = None,
                   created_by: str | None = None) -> None:
        self._conn.execute(
            "INSERT INTO jobs (job, description, created_by) VALUES (%s, %s, %s) "
            "ON CONFLICT (job) DO UPDATE SET description = COALESCE(EXCLUDED.description, jobs.description)",
            (job, description, created_by))

    def open_job(self, job: str, description: str | None, task: str | None,
                 session_id: str | None, created_by: str | None, project: str | None = None,
                 goal: str | None = None) -> None:
        self._conn.execute(
            "INSERT INTO jobs (job, description, task, session_id, created_by, project, goal, status, "
            "activated_at) VALUES (%s, %s, %s, %s, %s, %s, %s, 'active', now()) "
            "ON CONFLICT (job) DO UPDATE SET status = 'active', activated_at = now(), finished_at = NULL, "
            "outcome = NULL, description = COALESCE(EXCLUDED.description, jobs.description), "
            "task = COALESCE(EXCLUDED.task, jobs.task), session_id = COALESCE(EXCLUDED.session_id, jobs.session_id), "
            "project = COALESCE(EXCLUDED.project, jobs.project), goal = COALESCE(EXCLUDED.goal, jobs.goal), "
            "verdict = NULL, verdict_reason = NULL, verdict_next = NULL, verdict_by = NULL, verdict_at = NULL, "
            "completion_forced = false, spawns = 0, waiting_on = NULL, waiting_since = NULL, "
            "waiting_until = NULL, max_hours = NULL, closed_by = NULL",
            (job, description, task, session_id, created_by, project, goal))

    def close_job(self, job: str, status: str, outcome: str | None, forced: bool = False,
                  closed_by: str | None = None, guard: CloseGuard | None = None) -> bool:
        if guard:   # the guard and the close in one transaction, the job row locked
            with self._conn.transaction():
                row = self._conn.execute("SELECT goal, verdict, max_hours FROM jobs WHERE job = %s FOR UPDATE",
                                         (job,)).fetchone()
                if not row or not guard.allows(*row):
                    return False
                return self.close_job(job, status, outcome, forced, closed_by)
        self._leave_job(job)
        return self._conn.execute(
            "UPDATE jobs SET status = %s, outcome = COALESCE(%s, outcome), "
            "finished_at = COALESCE(finished_at, now()), completion_forced = %s, waiting_on = NULL, "
            "waiting_since = NULL, waiting_until = NULL, closed_by = %s WHERE job = %s RETURNING job",
            (status, outcome, bool(forced), closed_by, job)).fetchone() is not None

    def _leave_job(self, job: str) -> None:
        self._conn.execute("UPDATE agents SET left_at = now(), state = 'left', current_tool = NULL, "
                           "tool_started_at = NULL WHERE job = %s AND left_at IS NULL", (job,))

    def auto_close_job(self, job: str, before: _dt.datetime, outcome: str) -> _dt.datetime | None:
        # One UPDATE decides and closes: a concurrent sweep's UPDATE waits for this row's lock,
        # then finds status no longer 'active' and changes nothing. The derived statuses come
        # from the agent_status view, so the thresholds are the ones `status` shows.
        with self._conn.transaction():
            closed = self._conn.execute(
                "UPDATE jobs j SET status = 'completed', outcome = %(outcome)s, "
                "finished_at = now(), completion_forced = false, waiting_on = NULL, "
                "waiting_since = NULL, waiting_until = NULL, closed_by = %(by)s "
                "WHERE j.job = %(job)s AND j.status = 'active' AND j.waiting_on IS NULL "
                "AND (j.goal IS NULL OR j.verdict = 'met') "
                "AND COALESCE(j.activated_at, j.created_at) < %(before)s "
                "AND EXISTS (SELECT 1 FROM agents a WHERE a.job = j.job "
                "AND a.state IN ('completed', 'left') "
                "AND a.left_at >= COALESCE(j.activated_at, j.created_at)) "
                "AND NOT EXISTS (SELECT 1 FROM agent_status s WHERE s.job = j.job "
                "AND s.status IN ('started', 'running', 'idle')) "
                "AND NOT EXISTS (SELECT 1 FROM agents a WHERE a.job = j.job AND (a.joined_at >= %(before)s "
                "OR a.last_seen >= %(before)s OR a.left_at >= %(before)s)) "
                "AND NOT EXISTS (SELECT 1 FROM messages m WHERE m.job = j.job "
                "AND m.created_at >= %(before)s) RETURNING j.finished_at",
                {"job": job, "before": before, "outcome": outcome, "by": AUTO_CLOSED_BY}).fetchone()
            if closed is None:
                return None
            self._leave_job(job)
            return closed[0]   # the timestamp this UPDATE wrote

    def undo_auto_close(self, job: str, closed_at: _dt.datetime) -> bool:
        return self._conn.execute(
            "UPDATE jobs SET status = 'active', finished_at = NULL, outcome = NULL, closed_by = NULL "
            "WHERE job = %s AND status = 'completed' AND closed_by = %s AND finished_at = %s "
            "RETURNING job", (job, AUTO_CLOSED_BY, closed_at)).fetchone() is not None

    def set_waiting(self, job: str, on: str | None, until: _dt.datetime | None = None) -> bool:
        return self._conn.execute(
            "UPDATE jobs SET waiting_on = %s, waiting_since = CASE WHEN %s::text IS NULL THEN NULL "
            "ELSE now() END, waiting_until = %s::timestamptz "
            "WHERE job = %s AND status = 'active' RETURNING job",
            (on, on, until if on is not None else None, job)).fetchone() is not None

    def set_job_max_hours(self, job: str, hours: float | None) -> bool:
        return self._conn.execute("UPDATE jobs SET max_hours = %s WHERE job = %s RETURNING job",
                                  (hours, job)).fetchone() is not None

    def bind_job_session(self, job: str, session_id: str) -> None:
        self._conn.execute("UPDATE jobs SET session_id = %s WHERE job = %s AND session_id IS NULL",
                           (session_id, job))

    # ---- agents --------------------------------------------------------------------

    def _allocate_name(self, agent_key: str, job: str, role: str | None = None) -> str:
        self.purge()
        self.ensure_job(job)
        known = self._existing_name(agent_key, job)
        if known:
            return known
        host = compat.node()
        return (self._claim_pool_name(agent_key, job, role, host)
                or self._claim_fallback_name(agent_key, job, role, host))

    def _existing_name(self, agent_key: str, job: str) -> str | None:
        """The key's name if it is active (moved to `job`) or could be revived; else None."""
        conn = self._conn
        row = conn.execute("SELECT name, left_at, left_reason FROM agents WHERE agent_key = %s",
                           (agent_key,)).fetchone()
        if not row:
            return None
        if row[1] is not None and ((row[2] or "").startswith(STUCK_PREFIX) or self.was_replaced(agent_key)):
            return row[0]      # supervisor-closed or replaced: stays departed, never revived
        if row[1] is None:
            # a move to another job gives up the judge seat (judge/job on the right are the old values)
            conn.execute("UPDATE agents SET last_seen = now(), job = %s, judge = (judge AND job = %s), "
                         "verifier = (verifier AND job = %s) "
                         "WHERE agent_key = %s", (job, job, job, agent_key))
            return row[0]
        # A finished agent resumed (e.g. continued via SendMessage): give it back the name it
        # already knows, with its read cursor, unless someone else has taken that name since.
        try:
            revived = conn.execute(
                "UPDATE agents SET left_at = NULL, state = 'started', job = %s, last_seen = now(), "
                "current_tool = NULL, tool_started_at = NULL, turn_ended_at = NULL, "
                "judge = false, verifier = false, left_reason = NULL "
                "WHERE agent_key = %s "
                "RETURNING name",
                (job, agent_key)).fetchone()
        except Exception:  # unique index: the name is held by an active agent now
            return None
        return revived[0] if revived else None

    def _claim_pool_name(self, agent_key: str, job: str, role: str | None, host: str) -> str | None:
        """Claim a random free pool name, every simpsons one before any english; None if none."""
        conn = self._conn
        for source in NAME_SOURCES:
            free = [r[0] for r in conn.execute(
                "SELECT name FROM name_pool p WHERE source = %s AND NOT EXISTS "
                "(SELECT 1 FROM agents a WHERE a.name = p.name AND a.left_at IS NULL)", (source,))]
            random.shuffle(free)
            for name in free:
                try:
                    conn.execute(_CLAIM_NAME, (agent_key, name, job, role, host, getpass.getuser(), job, self._history()))
                    return name
                except Exception:  # lost a race for this name; try the next one
                    continue
        return None

    def _claim_fallback_name(self, agent_key: str, job: str, role: str | None, host: str) -> str:
        """Pool exhausted: "<english name> NNN". Upserts like the pool names: a departed agent
        whose old name was taken still has its row, and a plain INSERT would collide on the key."""
        english = load_name_pool()["english"]
        attempts_left = _FALLBACK_ATTEMPTS
        while True:
            name = f"{random.choice(english)} {random.randint(100, 999)}"
            try:
                self._conn.execute(_CLAIM_NAME, (agent_key, name, job, role, host, getpass.getuser(), job, self._history()))
                return name
            except psycopg.errors.UniqueViolation:  # an active agent holds this one; draw again
                attempts_left -= 1
                if not attempts_left:
                    raise

    def _history(self) -> int:
        return max(0, int(self.board_cfg.get("join_history", 30)))

    def claim_judge(self, agent_key: str, job: str) -> bool:
        try:
            row = self._conn.execute("UPDATE agents SET judge = true WHERE agent_key = %s AND job = %s "
                                     "AND left_at IS NULL RETURNING 1", (agent_key, job)).fetchone()
        except psycopg.errors.UniqueViolation:  # agents_one_judge: the job has an active judge
            return False
        return row is not None

    def _move_agent_row(self, agent_key: str, job: str, keep: int) -> str | None:
        conn = self._conn
        row = conn.execute(
            "WITH old AS (SELECT job FROM agents WHERE agent_key = %s AND left_at IS NULL AND job <> %s "
            "FOR UPDATE) "
            "UPDATE agents a SET job = %s, judge = false, verifier = false, last_seen = now(), "
            "last_read_id = COALESCE((SELECT id FROM messages WHERE job = %s ORDER BY id DESC "
            "OFFSET %s LIMIT 1), 0), "
            "reply_reminded_id = (SELECT COALESCE(MAX(id), 0) FROM messages), "
            "calls_at_post = a.tool_calls, silence_nudged_at = NULL, "
            "roster_seen = %s || old.job, roster_synced_at = NULL "
            "FROM old WHERE a.agent_key = %s AND EXISTS (SELECT 1 FROM jobs WHERE job = %s AND status = 'active') "
            "RETURNING old.job",
            (agent_key, job, job, job, keep, MOVED_PREFIX, agent_key, job)).fetchone()
        if row:
            return row[0]
        same = conn.execute("SELECT 1 FROM agents a JOIN jobs j ON j.job = a.job WHERE a.agent_key = %s "
                            "AND a.left_at IS NULL AND a.job = %s AND j.status = 'active'",
                            (agent_key, job)).fetchone()
        return job if same else None

    def set_job_goal(self, job: str, goal: str) -> bool:
        conn = self._conn
        row = conn.execute("SELECT goal FROM jobs WHERE job = %s AND status = 'active'", (job,)).fetchone()
        if row is None:
            return False
        if row[0] != goal:
            conn.execute("UPDATE jobs SET goal = %s, verdict = NULL, verdict_reason = NULL, "
                         "verdict_next = NULL, verdict_by = NULL, verdict_at = NULL WHERE job = %s",
                         (goal, job))
        return True

    def claim_verifier(self, agent_key: str, job: str) -> bool:
        return self._conn.execute(
            "UPDATE agents SET verifier = true WHERE agent_key = %s AND job = %s AND left_at IS NULL "
            "AND NOT judge RETURNING 1", (agent_key, job)).fetchone() is not None

    def verification_counts(self, job: str) -> tuple[int, int]:
        row = self._conn.execute(
            "SELECT count(*) FILTER (WHERE m.message LIKE 'VERIFIED%%'), "
            "count(*) FILTER (WHERE m.message LIKE 'FAILED%%') FROM messages m "
            "WHERE m.job = %s AND m.agent_name IN (SELECT name FROM agents WHERE job = %s AND verifier)",
            (job, job)).fetchone()
        return int(row[0]), int(row[1])

    def reserve_spawn(self, agent_key: str, job: str, per_agent: int, per_job: int) -> SpawnGrant:
        conn = self._conn
        with conn.transaction():
            job_row = conn.execute("SELECT spawns FROM jobs WHERE job = %s FOR UPDATE", (job,)).fetchone()
            agent_row = conn.execute("SELECT spawns FROM agents WHERE agent_key = %s AND job = %s "
                                     "AND left_at IS NULL", (agent_key, job)).fetchone()
            if job_row is None or agent_row is None:
                return SpawnGrant(False, 0, 0, "member")
            mine, total = agent_row[0], job_row[0]
            refused = "agent" if mine >= per_agent else "job" if total >= per_job else None
            if refused:
                return SpawnGrant(False, mine, total, refused)
            conn.execute("UPDATE agents SET spawns = spawns + 1 WHERE agent_key = %s", (agent_key,))
            conn.execute("UPDATE jobs SET spawns = spawns + 1 WHERE job = %s", (job,))
            return SpawnGrant(True, mine + 1, total + 1)

    def record_verdict(self, job: str, judge_name: str, verdict: str, reason: str,
                       next_steps: str | None = None) -> bool:
        check_name(judge_name, "judge name")
        if verdict not in VERDICTS:
            raise BoardError(f"unknown verdict {verdict!r}")
        return self._conn.execute(
            "UPDATE jobs SET verdict = %s, verdict_reason = %s, verdict_next = %s, verdict_by = %s, verdict_at = now() "
            "WHERE job = %s AND EXISTS (SELECT 1 FROM agents a WHERE a.job = %s AND a.name = %s "
            "AND a.judge AND a.left_at IS NULL) RETURNING 1",
            (verdict, reason, next_steps, judge_name, job, job, judge_name)).fetchone() is not None

    def active_agent_name(self, agent_key: str) -> str | None:
        row = self._conn.execute("SELECT name FROM agents WHERE agent_key = %s AND left_at IS NULL",
                                 (agent_key,)).fetchone()
        return row[0] if row else None

    def was_member(self, agent_key: str, job: str) -> bool:
        return self._conn.execute("SELECT 1 FROM agents WHERE agent_key = %s AND job = %s",
                                  (agent_key, job)).fetchone() is not None

    def tool_started(self, agent_key: str, tool_name: str | None) -> Member | None:
        row = self._conn.execute(
            "UPDATE agents a SET state = 'running', current_tool = %s, tool_started_at = now(), turn_ended_at = NULL, "
            "tool_calls = tool_calls + 1, last_seen = now() WHERE agent_key = %s AND left_at IS NULL "
            "RETURNING name, job, EXISTS (SELECT 1 FROM agent_routes r "
            "WHERE r.agent_key = a.agent_key AND r.state = 'unverified'), verifier, model",
            ((tool_name or "?")[:TOOL_NAME_MAX], agent_key)).fetchone()
        return Member(*row) if row else None

    def record_route(self, agent_key: str, session_id: str | None, state: str,
                     job: str | None = None) -> None:
        if state not in ROUTE_STATES:
            raise BoardError(f"unknown route state {state!r}")
        self._conn.execute(
            "INSERT INTO agent_routes (agent_key, session_id, state, job) VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (agent_key) DO UPDATE SET session_id = EXCLUDED.session_id, "
            "state = EXCLUDED.state, job = EXCLUDED.job, created_at = now()",
            (agent_key, session_id, state, job))

    def claim_route(self, agent_key: str, session_id: str | None, from_state: str | None,
                    state: str, job: str | None = None) -> bool:
        if state not in ROUTE_STATES:
            raise BoardError(f"unknown route state {state!r}")
        if from_state is None:  # only if no route exists: the primary key picks one winner
            row = self._conn.execute(
                "INSERT INTO agent_routes (agent_key, session_id, state, job) VALUES (%s, %s, %s, %s) "
                "ON CONFLICT (agent_key) DO NOTHING RETURNING 1",
                (agent_key, session_id, state, job)).fetchone()
        else:  # the row lock serialises concurrent updates; losers re-check the WHERE and miss
            row = self._conn.execute(
                "UPDATE agent_routes SET session_id = %s, state = %s, job = %s, created_at = now() "
                "WHERE agent_key = %s AND state = %s RETURNING 1",
                (session_id, state, job, agent_key, from_state)).fetchone()
        return row is not None

    def route(self, agent_key: str) -> Route:
        row = self._conn.execute(
            "SELECT r.state, r.job, r.session_id, a.job FROM (SELECT %s::text AS k) x "
            "LEFT JOIN agent_routes r ON r.agent_key = x.k LEFT JOIN agents a ON a.agent_key = x.k",
            (agent_key,)).fetchone()
        return Route(*row)

    def tool_finished(self, agent_key: str) -> None:
        self._conn.execute("UPDATE agents SET current_tool = NULL, tool_started_at = NULL, last_seen = now() "
                           "WHERE agent_key = %s AND left_at IS NULL", (agent_key,))

    def agent_stopped(self, agent_key: str) -> None:
        self._conn.execute("UPDATE agents SET left_at = now(), state = 'completed', current_tool = NULL, "
                           "tool_started_at = NULL WHERE agent_key = %s AND left_at IS NULL", (agent_key,))

    def set_agent_role(self, agent_key: str, role: str) -> None:
        from swarm.roles import custom_role
        if custom_role(role) is None:
            raise ValueError("role must be a custom role identifier, not judge/verifier")
        self._conn.execute("UPDATE agents SET role = %s WHERE agent_key = %s AND left_at IS NULL",
                           (role, agent_key))

    def set_agent_runtime(self, agent_key: str, harness: str | None, model: str | None) -> None:
        self._conn.execute("UPDATE agents SET harness = COALESCE(%s, harness), model = COALESCE(%s, model) "
                           "WHERE agent_key = %s AND left_at IS NULL", (harness, model, agent_key))

    def agent_turn_ended(self, agent_key: str) -> None:
        self._conn.execute("UPDATE agents SET turn_ended_at = now(), current_tool = NULL, tool_started_at = NULL, "
                           "last_seen = now() WHERE agent_key = %s AND left_at IS NULL", (agent_key,))

    def turns_resumed(self, job: str) -> list[str]:
        rows = self._conn.execute(
            "UPDATE agents SET turn_ended_at = now(), last_seen = now() "
            "WHERE job = %s AND left_at IS NULL AND turn_ended_at IS NOT NULL RETURNING agent_key",
            (job,)).fetchall()
        return [r[0] for r in rows]

    def finish_quiet_agents(self, quiet_seconds: float) -> list[str]:
        rows = self._conn.execute(
            "UPDATE agents SET left_at = now(), state = 'completed', current_tool = NULL, tool_started_at = NULL "
            "WHERE left_at IS NULL AND turn_ended_at IS NOT NULL "
            "AND turn_ended_at < now() - make_interval(secs => %s) RETURNING agent_key",
            (float(quiet_seconds),)).fetchall()
        return [r[0] for r in rows]

    def leave(self, agent_key: str | None = None, name: str | None = None) -> bool:
        match, params = _agent_match(agent_key, name)
        cur = self._conn.execute("UPDATE agents SET left_at = now(), state = 'left', current_tool = NULL "
                                 "WHERE left_at IS NULL AND " + match, params)
        return cur.rowcount > 0

    def close_agent(self, agent_key: str, reason: str, seen_before=None) -> bool:
        return self._conn.execute(
            "UPDATE agents SET left_at = now(), state = 'left', left_reason = %s, current_tool = NULL, "
            "tool_started_at = NULL WHERE agent_key = %s AND left_at IS NULL "
            "AND (%s::timestamptz IS NULL OR last_seen <= %s::timestamptz) RETURNING agent_key",
            (reason, agent_key, seen_before, seen_before)).fetchone() is not None

    def claim_resume(self, agent_key: str, resume_of: str, job: str) -> str | None:
        conn = self._conn
        try:
            with conn.transaction():
                cur = conn.execute("SELECT name, left_at, job FROM agents WHERE agent_key = %s",
                                   (agent_key,)).fetchone()
                if cur is not None and cur[1] is None and cur[2] == job:
                    return cur[0]
                old = conn.execute("SELECT name, role, job, left_at, last_read_id, judge, verifier "
                                   "FROM agents WHERE agent_key = %s FOR UPDATE", (resume_of,)).fetchone()
                if old is None or old[2] != job or old[3] is None:
                    return None
                name, role, _, _, cursor, judge, verifier = old
                if conn.execute("SELECT 1 FROM agents WHERE name = %s AND left_at IS NULL",
                                (name,)).fetchone():
                    return None
                judge = bool(judge) and conn.execute(
                    "SELECT 1 FROM agents WHERE job = %s AND judge AND left_at IS NULL",
                    (job,)).fetchone() is None
                conn.execute(_CLAIM_RESUME, (agent_key, name, job, role, compat.node(),
                                             getpass.getuser(), cursor, judge, bool(verifier), resume_of))
                return name
        except psycopg.errors.UniqueViolation:   # the name (or the judge seat) was taken meanwhile
            return None

    def job_state(self, job: str) -> str | None:
        r = self._conn.execute("SELECT status FROM jobs WHERE job = %s", (job,)).fetchone()
        return r[0] if r else None

    # ---- pause / resume ------------------------------------------------------------------

    _PAUSE_COLS = "id, job, paused_at, paused_by, reason, manifest, resumed_at, resumed_by, resumed_host, outcome"

    @staticmethod
    def _pause_record(r) -> PauseRecord:
        (pid, job, at, by, reason, manifest, rat, rby, rhost, outcome) = r
        return PauseRecord(id=pid, job=job, paused_at=at, paused_by=by, reason=reason,
                           manifest=json.loads(manifest), resumed_at=rat, resumed_by=rby,
                           resumed_host=rhost, outcome=None if outcome is None else json.loads(outcome))

    def _open(self, job: str) -> PauseRecord | None:
        r = self._conn.execute(f"SELECT {self._PAUSE_COLS} FROM job_pauses WHERE job = %s AND resumed_at IS NULL "
                               "ORDER BY id DESC LIMIT 1", (job,)).fetchone()
        return self._pause_record(r) if r else None

    def pause_job(self, job: str, by: str | None, reason: str | None,
                  cwds: Mapping[str, str] | None = None) -> PauseRecord | None:
        conn = self._conn
        with conn.transaction():
            row = conn.execute("SELECT status, goal, task, description, project, verdict, waiting_on, max_hours "
                               "FROM jobs WHERE job = %s FOR UPDATE", (job,)).fetchone()
            if row is None:
                return None
            if row[0] == "paused":
                return self._open(job)
            if row[0] != "active":
                return None
            now = conn.execute("SELECT now()").fetchone()[0]
            job_row = dict(zip(("status", "goal", "task", "description", "project", "verdict",
                                "waiting_on", "max_hours"), row))
            names = ("agent_key", "name", "role", "host", "os_user", "harness", "model", "last_read_id",
                     "tool_calls", "current_tool", "last_seen", "judge", "verifier", "resume_of", "turn_ended_at")
            rows = []
            for r in conn.execute(f"SELECT {', '.join('a.' + n for n in names)}, s.status, r.session_id, "
                                  "EXISTS (SELECT 1 FROM transcripts t WHERE t.job = a.job AND t.agent_key = a.agent_key "
                                  "AND t.role = 'orchestrator') "
                                  "FROM agents a JOIN agent_status s ON s.agent_key = a.agent_key "
                                  "LEFT JOIN agent_routes r ON r.agent_key = a.agent_key "
                                  "WHERE a.job = %s AND a.left_at IS NULL ORDER BY a.joined_at, a.agent_key "
                                  "FOR UPDATE OF a", (job,)).fetchall():
                a = dict(zip(names, r[:len(names)]))
                a["status"], a["session_id"], a["orchestrator"] = r[len(names):]
                rows.append(a)
            last = conn.execute("SELECT COALESCE(MAX(id), 0) FROM messages WHERE job = %s", (job,)).fetchone()[0]
            manifest = build_manifest(job, now, by, reason, job_row, rows, last, cwds)
            conn.execute("UPDATE agents SET left_at = now(), state = 'left', left_reason = %s, current_tool = NULL, "
                         "tool_started_at = NULL WHERE job = %s AND left_at IS NULL", (LEFT_PAUSED, job))
            conn.execute("UPDATE jobs SET status = 'paused' WHERE job = %s", (job,))
            conn.execute("INSERT INTO job_pauses (job, paused_at, paused_by, reason, manifest) "
                         "VALUES (%s, %s, %s, %s, %s)", (job, now, by, reason, json.dumps(manifest)))
            return self._open(job)

    def open_pause(self, job: str) -> PauseRecord | None:
        return self._open(job)

    def pauses(self, job: str) -> list[PauseRecord]:
        return [self._pause_record(r) for r in self._conn.execute(
            f"SELECT {self._PAUSE_COLS} FROM job_pauses WHERE job = %s ORDER BY id", (job,)).fetchall()]

    def begin_resume(self, job: str, pause_id: int, by: str | None, host: str | None) -> bool:
        conn = self._conn
        with conn.transaction():
            if conn.execute("UPDATE jobs SET status = 'active' WHERE job = %s AND status = 'paused' "
                            "AND EXISTS (SELECT 1 FROM job_pauses WHERE id = %s AND job = %s AND resumed_at IS NULL) "
                            "RETURNING job", (job, pause_id, job)).fetchone() is None:
                return False
            return conn.execute("UPDATE job_pauses SET resumed_at = now(), resumed_by = %s, resumed_host = %s "
                                "WHERE id = %s AND resumed_at IS NULL RETURNING id",
                                (by, host, pause_id)).fetchone() is not None

    def record_resume_outcome(self, pause_id: int, outcome: dict) -> bool:
        return self._conn.execute("UPDATE job_pauses SET outcome = %s WHERE id = %s RETURNING id",
                                  (json.dumps(outcome), pause_id)).fetchone() is not None

    def set_job_supervise(self, job: str, on: bool) -> bool:
        return self._conn.execute("UPDATE jobs SET supervise = %s WHERE job = %s RETURNING job",
                                  (bool(on), job)).fetchone() is not None

    # ---- supervisor restarts ------------------------------------------------------------

    def record_restart(self, job: str, agent_key: str, old_agent_key: str, reason: str, harness: str,
                       minutes_cap: float, outcome: str | None = None,
                       max_per_job: int | None = None, max_job_minutes: float | None = None,
                       max_host_running: int | None = None, max_host_minutes: float | None = None,
                       day_start: _dt.datetime | None = None) -> Restart | None:
        if outcome is not None and outcome not in RESTART_OUTCOMES:
            raise ValueError(outcome)
        conn = self._conn
        host = compat.node()
        with conn.transaction():
            # One job's inserts in turn (every lineage, host and user), so two at once can't both
            # count the same attempt, nor both pass the job cap. Each statement below runs after
            # the lock is had and, under READ COMMITTED, sees every insert committed before.
            conn.execute("SELECT pg_advisory_xact_lock(hashtext('restarts:' || %s))", (job,))
            if max_per_job is not None and conn.execute(
                    "SELECT count(*) FROM restarts WHERE job = %s", (job,)).fetchone()[0] >= int(max_per_job):
                return None
            if outcome is None and any(v is not None for v in (max_job_minutes, max_host_running,
                                                                max_host_minutes)):
                # the host caps span jobs: one host's inserts in turn too. Always job lock, then
                # host lock (the only two taken here, in that order): no deadlock.
                conn.execute("SELECT pg_advisory_xact_lock(hashtext('restarts-host:' || %s))", (host,))
                rows = lambda col, v: [Restart(*r) for r in conn.execute(
                    f"SELECT {_RESTART_COLS} FROM restarts WHERE {col} = %s", (v,)).fetchall()]
                if restart_over_limits(rows("job", job), rows("host", host), float(minutes_cap),
                                       max_job_minutes=max_job_minutes, max_host_running=max_host_running,
                                       max_host_minutes=max_host_minutes, day_start=day_start):
                    return None
            row = conn.execute(
                "INSERT INTO restarts (job, agent_key, attempt, reason, old_agent_key, harness, host, "
                "os_user, minutes_cap, ended_at, outcome) VALUES (%s, %s, 1 + (SELECT count(*) FROM restarts "
                "WHERE job = %s AND agent_key = %s), %s, %s, %s, %s, %s, %s, "
                "CASE WHEN %s::text IS NULL THEN NULL ELSE now() END, %s) "
                f"ON CONFLICT (old_agent_key) DO NOTHING RETURNING {_RESTART_COLS}",
                (job, agent_key, job, agent_key, reason, old_agent_key, harness, compat.node(),
                 getpass.getuser(), float(minutes_cap), outcome, outcome)).fetchone()
        return Restart(*row) if row else None

    def set_restart_agent(self, restart_id: int, new_agent_key: str) -> bool:
        return self._conn.execute("UPDATE restarts SET new_agent_key = %s WHERE id = %s RETURNING id",
                                  (new_agent_key, restart_id)).fetchone() is not None

    def finish_restart(self, restart_id: int, outcome: str) -> bool:
        if outcome not in RESTART_OUTCOMES:
            raise ValueError(outcome)
        return self._conn.execute("UPDATE restarts SET ended_at = now(), outcome = %s "
                                  "WHERE id = %s AND ended_at IS NULL RETURNING id",
                                  (outcome, restart_id)).fetchone() is not None

    def was_replaced(self, agent_key: str) -> bool:
        return self._conn.execute("SELECT 1 FROM restarts WHERE old_agent_key = %s",
                                  (agent_key,)).fetchone() is not None

    def restarts(self, job: str | None = None, agent_key: str | None = None, host: str | None = None,
                 os_user: str | None = None, since: _dt.datetime | None = None) -> list[Restart]:
        conds, args = [], []
        for col, v in (("job", job), ("agent_key", agent_key), ("host", host), ("os_user", os_user)):
            if v is not None:
                conds.append(f"{col} = %s")
                args.append(v)
        if since is not None:
            conds.append("at >= %s")
            args.append(since)
        where = (" WHERE " + " AND ".join(conds)) if conds else ""
        return self._fetch(Restart, f"SELECT {_RESTART_COLS} FROM restarts{where} ORDER BY id", args)

    def agents(self, job: str, include_departed: bool = True) -> list[AgentStatus]:
        # `status --job`/watch used ORDER BY (ended_at IS NULL) DESC, joined_at; `who` used
        # ended_at IS NULL ... ORDER BY joined_at, which is the same order on that subset.
        active = "" if include_departed else " AND ended_at IS NULL"
        return self._fetch(AgentStatus, f"SELECT {_AGENT_STATUS_COLS} FROM agent_status WHERE job = %s{active} "
                           "ORDER BY (ended_at IS NULL) DESC, joined_at", (job,))

    def roster(self, job: str) -> list[RosterEntry]:
        # Only cheap columns: the view's per-agent message count is never computed here.
        return self._fetch(RosterEntry, f"SELECT {_ROSTER_COLS} FROM agent_status WHERE job = %s "
                           "ORDER BY (ended_at IS NULL) DESC, joined_at", (job,))

    # ---- per-agent sync state ----------------------------------------------------------

    @staticmethod
    def _sync(row) -> SyncState:
        owed = tuple(OwedReply(o["id"], o["sender"], _dt.datetime.fromisoformat(o["at"]))
                     for o in (row[14] or ()))
        return SyncState(*row[:6], tuple(row[6] or ()), *row[7:14], owed)

    def sync_state(self, agent_key: str) -> SyncState | None:
        row = self._conn.execute(_SYNC_FROM.format(extra="") + " WHERE me.agent_key = %s",
                                 (agent_key,)).fetchone()
        return self._sync(row) if row else None

    def turn_state(self, agent_key: str, job: str):
        """One round trip: the agent's sync state joined to every roster row of the job."""
        rows = self._conn.execute(
            _SYNC_FROM.format(extra=", s.agent_key, s.name, s.role, s.status, s.current_tool, "
                                    "s.ended_at IS NULL")
            + " LEFT JOIN agent_status s ON s.job = %s WHERE me.agent_key = %s "
            "ORDER BY (s.ended_at IS NULL) DESC, s.joined_at", (job, agent_key)).fetchall()
        if not rows:
            return self.roster(job), None
        roster = [RosterEntry(*r[_SYNC_WIDTH:]) for r in rows if r[_SYNC_WIDTH] is not None]
        return roster, self._sync(rows[0][:_SYNC_WIDTH])

    def record_roster_sync(self, agent_key: str, snapshot: str, full: bool) -> None:
        synced = ", roster_synced_at = now()" if full else ""
        self._conn.execute(f"UPDATE agents SET roster_seen = %s{synced} WHERE agent_key = %s",
                           (snapshot, agent_key))

    def record_memory_recall(self, agent_key: str, shown_ids) -> None:
        # Append the ids not seen yet (in the order given), keep the newest MEMORY_SEEN_MAX.
        self._conn.execute(
            "UPDATE agents SET memory_recalled_at = now(), memory_seen = ("
            "SELECT COALESCE(array_agg(x ORDER BY o), '{}') FROM ("
            "SELECT x, o FROM unnest(memory_seen || ARRAY(SELECT y FROM unnest(%s::text[]) "
            "WITH ORDINALITY u(y, p) WHERE y <> ALL(memory_seen) ORDER BY p)) WITH ORDINALITY t(x, o) "
            "ORDER BY o DESC LIMIT %s) newest) WHERE agent_key = %s",
            (list(dict.fromkeys(shown_ids)), MEMORY_SEEN_MAX, agent_key))

    def record_remembered(self, name: str) -> None:
        self._conn.execute("UPDATE agents SET remembered_at = now() WHERE name = %s AND left_at IS NULL", (name,))

    def record_nudge(self, agent_key: str) -> None:
        self._conn.execute("UPDATE agents SET nudged_at = now() WHERE agent_key = %s", (agent_key,))

    def record_silence_nudge(self, agent_key: str) -> None:
        self._conn.execute("UPDATE agents SET silence_nudged_at = now() WHERE agent_key = %s", (agent_key,))

    def record_reply_reminder(self, agent_key: str, upto_id: int) -> None:
        self._conn.execute("UPDATE agents SET reply_reminded_id = GREATEST(reply_reminded_id, %s) "
                           "WHERE agent_key = %s", (upto_id, agent_key))

    def agent_events(self, since: _dt.datetime, job: str | None = None) -> list[AgentEvent]:
        jf, jp = self._job_filter(job)
        return self._fetch(AgentEvent, f"SELECT name, job, role, joined_at, left_at, state FROM agents WHERE "
                           f"(joined_at > %s OR left_at > %s){jf} ORDER BY COALESCE(left_at, joined_at)",
                           (since, since, *jp))

    # ---- messages ------------------------------------------------------------------

    def _insert_message(self, job: str, name: str, text: str, to: str | None,
                        agent_key: str | None) -> int:
        self.ensure_job(job)
        with self._conn.transaction():  # the lock is held to commit: ids commit in order
            self._conn.execute("SELECT pg_advisory_xact_lock(%s)", (POST_LOCK,))
            msg_id = self._conn.execute(
                "INSERT INTO messages (job, agent_name, message, to_agent, agent_key, host) "
                "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
                (job, name, text, to, agent_key, compat.node())).fetchone()[0]
        self._conn.execute("UPDATE agents SET last_seen = now(), last_post_at = now(), "
                           "calls_at_post = tool_calls WHERE name = %s AND left_at IS NULL", (name,))
        return msg_id

    def read_unread(self, agent_key: str | None = None, name: str | None = None,
                    job: str | None = None, advance: bool = True) -> ReadResult:
        match, params = _agent_match(agent_key, name)
        row = self._conn.execute(f"SELECT agent_key, name, job, last_read_id FROM agents WHERE {match} "
                                 "AND left_at IS NULL", params).fetchone()
        if not row:
            return ReadResult([], 0)
        key, me, my_job, last = row
        # One statement, so one snapshot: nothing that commits between the page and the job's
        # top id can be jumped over (the old separate max(id) query could skip such a message).
        rows = self._conn.execute(_READ, {"job": job or my_job, "last": last, "me": me,
                                          "lim": int(self.board_cfg["read_limit"])}).fetchall()
        top, unread = rows[0][0], rows[0][1]
        messages = [Message(*r[2:]) for r in rows if r[2] is not None]
        remaining = unread - len(messages)
        if not advance:
            return ReadResult(messages, remaining)
        new = messages[-1].id if remaining else max(last, top or 0)
        moved = self._conn.execute(
            "UPDATE agents SET last_read_id = %s, last_seen = now() WHERE agent_key = %s "
            "AND last_read_id = %s", (new, key, last)).rowcount
        # A parallel read of this agent moved the cursor first and owns these messages.
        return ReadResult(messages, remaining) if moved else ReadResult([], 0)

    def recent_messages(self, limit: int, job: str | None = None,
                        active_jobs_only: bool = False) -> list[Message]:
        if job:
            jf, jp = " WHERE job = %s", (job,)
        elif active_jobs_only:
            jf, jp = " WHERE job IN (SELECT job FROM jobs WHERE status = 'active')", ()
        else:
            jf, jp = "", ()
        newest_first = self._fetch(Message, f"SELECT {_MESSAGE_COLS} FROM messages{jf} ORDER BY id DESC LIMIT %s",
                                   (*jp, limit))
        return newest_first[::-1]

    def messages_after(self, after_id: int, job: str | None = None) -> list[Message]:
        jf, jp = self._job_filter(job)
        return self._fetch(Message, f"SELECT {_MESSAGE_COLS} FROM messages WHERE id > %s{jf} ORDER BY id",
                           (after_id, *jp))

    def last_message_id(self, job: str | None = None) -> int:
        jf, jp = self._job_filter(job)
        return self._conn.execute(f"SELECT COALESCE(max(id), 0) FROM messages WHERE true{jf}",
                                  jp).fetchone()[0]

    # ---- status ----------------------------------------------------------------------

    def job_status(self, job: str) -> JobStatus | None:
        row = self._conn.execute(f"SELECT {_JOB_STATUS_COLS} FROM job_status WHERE job = %s",
                                 (job,)).fetchone()
        return JobStatus(*row) if row else None

    def jobs(self, include_closed: bool = False) -> list[JobStatus]:
        where = "" if include_closed else "WHERE status = 'active' "
        return self._fetch(
            JobStatus, f"SELECT {_JOB_STATUS_COLS} FROM job_status {where}"
            "ORDER BY (status = 'active') DESC, COALESCE(activated_at, created_at), job")

    def session_jobs(self, session: str) -> list[JobStatus]:
        # job_status is a GROUP BY view: a filter on session_id is not pushed into it (only one on
        # the grouping column job is), so rolling up every job of the board costs seconds. Find
        # the session's jobs first, then roll up just those.
        names = [r[0] for r in self._conn.execute(
            "SELECT job FROM jobs WHERE session_id = %s", (session,)).fetchall()]
        if not names:
            return []
        return self._fetch(
            JobStatus, f"SELECT {_JOB_STATUS_COLS} FROM job_status WHERE job = ANY(%s) "
            "ORDER BY (status = 'active') DESC, COALESCE(activated_at, created_at), job", (names,))

    # ---- change notification -----------------------------------------------------------

    def subscribe(self, messages_only: bool = False) -> None:
        self._listening = (CHANNEL_MESSAGES,) if messages_only else (CHANNEL_MESSAGES, CHANNEL_STATE)
        self._listen()

    def _listen(self) -> None:
        """LISTEN on the wanted channels, unless that can't work here: a standby refuses it, and
        so may a pooler. Then wait_for_change polls (a short sleep) and tries again later."""
        self._polling = bool(self.degraded)
        if not self._polling:
            try:
                for channel in self._listening:
                    self._conn.execute(f"LISTEN {channel}")
            except psycopg.Error as exc:
                if self._conn.closed or self._conn.broken:
                    raise BoardUnavailable(str(exc)) from exc
                self._polling = True
        self._retry_at = time.monotonic() + LIVE_RETRY_SECONDS

    def _retry_live(self) -> bool:
        """While degraded or polling, now and then: is a primary reachable again (switch to it), or
        does LISTEN work now? True if the connection changed (the caller redraws)."""
        if time.monotonic() < self._retry_at:
            return False
        if self.degraded:
            db = {**self.cfg["database"], "connect_timeout": min(2, self.cfg["database"]["connect_timeout"])}
            try:
                conn = _connect({**self.cfg, "database": db})
            except BoardUnavailable:
                self._retry_at = time.monotonic() + LIVE_RETRY_SECONDS
                return False
            old, self._conn, self.degraded = self._conn, conn, None
            old.close()
            conn.query_timeout = old.query_timeout
        self._listen()
        return True

    def wait_for_change(self, timeout: float) -> bool:
        if self._listening and (self.degraded or self._polling):
            time.sleep(timeout)   # polling: no notifications here; the caller re-queries anyway
            return self._retry_live()
        woke = False
        for _ in self._conn.notifies(timeout=timeout, stop_after=1):
            woke = True
        if woke:  # a burst counts once: take whatever else is already pending
            for _ in self._conn.notifies(timeout=_DRAIN_TIMEOUT):
                pass
        return woke

    # ---- helpers -----------------------------------------------------------------------

    def _fetch(self, cls: type, query: str, params: Sequence | None = None) -> list:
        """Rows of `query` as `cls` instances; the SELECT lists columns in the dataclass's field order."""
        return [cls(*r) for r in self._conn.execute(query, params).fetchall()]

    @staticmethod
    def _job_filter(job: str | None) -> tuple[str, tuple]:
        return (" AND job = %s", (job,)) if job else ("", ())

    # ---- transcripts ---------------------------------------------------------------------

    _TRANSCRIPT_COLS = ("job, agent_key, agent_name, role, host, captured_at, final, raw_bytes, "
                        "stored_bytes, redactions, session_id, sha256, harness, capture_failed")

    _IMAGE_LOCK = "SELECT pg_advisory_xact_lock(hashtext('swarm-transcript-images'))"

    def save_transcript(self, row: TranscriptRow) -> bool:
        if row.role not in TRANSCRIPT_ROLES:
            raise ValueError(f"unknown transcript role {row.role!r}")
        check_images(row.images)   # recomputed: a sha256 names a file
        with self._conn.transaction():
            self._conn.execute(self._IMAGE_LOCK)   # transcript writes are rare: serialise them
            if not self._save_transcript_row(row):
                return False
            self._conn.execute("DELETE FROM transcript_image_refs WHERE job = %s AND agent_key = %s",
                               (row.job, row.agent_key))
            for img in dict((i.sha256, i) for i in row.images).values():
                self._conn.execute("INSERT INTO transcript_images (sha256, mime, bytes, data) "
                                   "VALUES (%s, %s, %s, %s) ON CONFLICT (sha256) DO NOTHING",
                                   (img.sha256, img.mime, int(img.size), img.data))
                self._conn.execute("INSERT INTO transcript_image_refs (job, agent_key, sha256) "
                                   "VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                                   (row.job, row.agent_key, img.sha256))
            self._drop_orphan_images()
        return True

    def _drop_orphan_images(self) -> None:
        self._conn.execute("DELETE FROM transcript_images i WHERE NOT EXISTS "
                           "(SELECT 1 FROM transcript_image_refs r WHERE r.sha256 = i.sha256) "
                           "AND NOT EXISTS (SELECT 1 FROM memory_ref_images m WHERE m.sha256 = i.sha256)")

    def transcript_image(self, sha256: str) -> TranscriptImage | None:
        r = self._conn.execute("SELECT sha256, mime, bytes, data, first_seen FROM transcript_images "
                               "WHERE sha256 = %s", (sha256,)).fetchone()
        return None if r is None else TranscriptImage(r[0], r[1], int(r[2]), bytes(r[3]), r[4])

    def transcript_images(self, job: str | None = None, agent_key: str | None = None) -> list[TranscriptImage]:
        conds, params = [], []
        for col, val in (("r.job", job), ("r.agent_key", agent_key)):
            if val is not None:
                conds.append(f"{col} = %s")
                params.append(val)
        where = (" WHERE " + " AND ".join(conds)) if conds else ""
        rows = self._conn.execute(
            "SELECT DISTINCT i.sha256, i.mime, i.bytes, i.first_seen FROM transcript_images i "
            f"JOIN transcript_image_refs r ON r.sha256 = i.sha256{where} ORDER BY i.sha256", params).fetchall()
        return [TranscriptImage(r[0], r[1], int(r[2]), None, r[3]) for r in rows]

    def _save_transcript_row(self, row: TranscriptRow) -> bool:
        got = self._conn.execute(
            "INSERT INTO transcripts (job, agent_key, agent_name, role, host, session_id, captured_at, "
            "final, raw_bytes, stored_bytes, redactions, sha256, body, harness, capture_failed) "
            "VALUES (%s, %s, %s, %s, %s, %s, COALESCE(%s, now()), %s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (job, agent_key) DO UPDATE SET agent_name = EXCLUDED.agent_name, "
            "role = EXCLUDED.role, host = EXCLUDED.host, session_id = EXCLUDED.session_id, "
            "captured_at = EXCLUDED.captured_at, final = EXCLUDED.final, raw_bytes = EXCLUDED.raw_bytes, "
            "stored_bytes = EXCLUDED.stored_bytes, redactions = EXCLUDED.redactions, "
            "sha256 = EXCLUDED.sha256, body = EXCLUDED.body, harness = EXCLUDED.harness, "
            "capture_failed = EXCLUDED.capture_failed "
            "WHERE (transcripts.sha256 <> EXCLUDED.sha256 OR (EXCLUDED.final AND NOT transcripts.final)) "
            "AND NOT (EXCLUDED.capture_failed IS NOT NULL AND transcripts.final "
            "AND transcripts.capture_failed IS NULL) "
            "RETURNING 1",
            (row.job, row.agent_key, row.agent_name, row.role, row.host, row.session_id, row.captured_at,
             bool(row.final), int(row.raw_bytes), len(row.body), int(row.redactions), row.sha256,
             row.body, row.harness, row.failed)).fetchone()
        return got is not None

    def refresh_transcript(self, job: str, agent_key: str, final: bool) -> bool:
        cur = self._conn.execute("UPDATE transcripts SET captured_at = now(), final = final OR %s, "
                                 "capture_failed = CASE WHEN %s THEN NULL ELSE capture_failed END "
                                 "WHERE job = %s AND agent_key = %s", (bool(final), bool(final), job, agent_key))
        return cur.rowcount > 0

    def mark_capture_failed(self, job: str, agent_key: str, reason: str, marker: TranscriptRow) -> str:
        with self._conn.transaction():
            if self._conn.execute("UPDATE transcripts SET final = true, capture_failed = %s "
                                  "WHERE job = %s AND agent_key = %s AND NOT final",
                                  (reason, job, agent_key)).rowcount:
                return "marked"
            got = self._conn.execute(
                "INSERT INTO transcripts (job, agent_key, agent_name, role, host, session_id, captured_at, "
                "final, raw_bytes, stored_bytes, redactions, sha256, body, harness, capture_failed) "
                "VALUES (%s, %s, %s, %s, %s, %s, COALESCE(%s, now()), true, 0, %s, 0, %s, %s, %s, %s) "
                "ON CONFLICT (job, agent_key) DO NOTHING RETURNING 1",
                (job, agent_key, marker.agent_name, marker.role, marker.host, marker.session_id,
                 marker.captured_at, len(marker.body), marker.sha256, marker.body, marker.harness,
                 reason)).fetchone()
            return "stored" if got is not None else "kept"

    def pending_final_transcripts(self, host: str, os_user: str, harness: str, since) -> list[tuple[str, str]]:
        rows = self._conn.execute(
            "SELECT a.job, a.agent_key FROM agents a LEFT JOIN transcripts t "
            "ON t.job = a.job AND t.agent_key = a.agent_key WHERE a.left_at IS NOT NULL AND a.left_at >= %s "
            "AND a.host = %s AND a.os_user = %s AND a.harness = %s AND (t.agent_key IS NULL OR NOT t.final) "
            "ORDER BY a.left_at", (since, host, os_user, harness)).fetchall()
        return [(r[0], r[1]) for r in rows]

    def transcripts(self, job: str | None = None, agent_name: str | None = None,
                    agent_key: str | None = None, role: str | None = None) -> list[TranscriptSummary]:
        conds, params = [], []
        for col, val in (("job", job), ("agent_name", agent_name), ("agent_key", agent_key), ("role", role)):
            if val is not None:
                conds.append(f"{col} = %s")
                params.append(val)
        where = (" WHERE " + " AND ".join(conds)) if conds else ""
        # the rows with their images ((sha256, bytes) pairs) in one statement: one snapshot
        rows = self._conn.execute(
            f"SELECT {self._TRANSCRIPT_COLS}, COALESCE((SELECT array_agg(ARRAY[r.sha256, i.bytes::text] "
            "ORDER BY r.sha256) FROM transcript_image_refs r JOIN transcript_images i ON i.sha256 = r.sha256 "
            f"WHERE r.job = t.job AND r.agent_key = t.agent_key), '{{}}') FROM transcripts t{where} "
            "ORDER BY captured_at, job, agent_key", params).fetchall()
        out = []
        for r in rows:
            imgs = tuple((sha, int(size)) for sha, size in r[14])
            ib = sum(size for _, size in imgs)
            out.append(TranscriptSummary(*r[:7], r[7] + ib, r[8] + ib, *r[9:12], ib, imgs, harness=r[12],
                                         failed=r[13]))
        return out

    def transcript_body(self, job: str, agent_key: str) -> bytes | None:
        row = self._conn.execute("SELECT body FROM transcripts WHERE job = %s AND agent_key = %s "
                                 "AND NOT (capture_failed IS NOT NULL AND raw_bytes = 0)",
                                 (job, agent_key)).fetchone()
        return None if row is None else decompress_transcript(bytes(row[0]))

    def _delete_transcripts(self, rows, before: _dt.datetime, jobs) -> int:
        n = 0
        with self._conn.transaction():
            self._conn.execute(self._IMAGE_LOCK)
            if jobs:
                n += self._conn.execute("DELETE FROM transcripts WHERE job = ANY(%s)", (list(jobs),)).rowcount
            for job, key in rows:
                if job in jobs:
                    continue
                n += self._conn.execute("DELETE FROM transcripts WHERE job = %s AND agent_key = %s "
                                        "AND captured_at < %s", (job, key, before)).rowcount
            self._conn.execute("DELETE FROM transcript_image_refs r WHERE NOT EXISTS (SELECT 1 FROM "
                               "transcripts t WHERE t.job = r.job AND t.agent_key = r.agent_key)")
            self._drop_orphan_images()
        return n

    # ---- memory provenance (schema 8) ------------------------------------------------------
    # Every write runs in one transaction after the transcript image lock: that serialises it
    # with the transcript image writes and deletes, and makes the first-writer check race-free.

    _MREF_COLS = ("document_id, bank, job, agent_key, agent_name, harness, host, session_id, tool_call_id, "
                  "writer, raw_bytes, redactions, created_at, checked_at, patched, octet_length(excerpt)")

    def _save_memory_ref(self, ref: MemoryRef) -> str:
        c = self._conn
        with c.transaction():
            c.execute(self._IMAGE_LOCK)
            old = c.execute("SELECT agent_key FROM memory_refs WHERE document_id = %s", (ref.document_id,)).fetchone()
            if old is not None and old[0] != ref.agent_key:
                return "kept"
            c.execute(
                "INSERT INTO memory_refs (document_id, bank, job, agent_key, agent_name, harness, host, session_id, "
                "tool_call_id, writer, created_at, checked_at, patched, raw_bytes, redactions, excerpt) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, COALESCE(%s, now()), NULL, %s, %s, %s, %s) "
                "ON CONFLICT (document_id) DO UPDATE SET bank = EXCLUDED.bank, job = EXCLUDED.job, "
                "agent_name = EXCLUDED.agent_name, harness = EXCLUDED.harness, host = EXCLUDED.host, "
                "session_id = EXCLUDED.session_id, tool_call_id = EXCLUDED.tool_call_id, writer = EXCLUDED.writer, "
                "created_at = EXCLUDED.created_at, checked_at = NULL, patched = EXCLUDED.patched, "
                "raw_bytes = EXCLUDED.raw_bytes, redactions = EXCLUDED.redactions, excerpt = EXCLUDED.excerpt",
                (ref.document_id, ref.bank, ref.job, ref.agent_key, ref.agent_name, ref.harness, ref.host,
                 ref.session_id, ref.tool_call_id, ref.writer, ref.created_at, bool(ref.patched),
                 int(ref.raw_bytes), int(ref.redactions), ref.excerpt))
            c.execute("DELETE FROM memory_ref_images WHERE document_id = %s", (ref.document_id,))
            for img in dict((i.sha256, i) for i in ref.images).values():
                c.execute("INSERT INTO transcript_images (sha256, mime, bytes, data) VALUES (%s, %s, %s, %s) "
                          "ON CONFLICT (sha256) DO NOTHING", (img.sha256, img.mime, int(img.size), img.data))
                c.execute("INSERT INTO memory_ref_images (document_id, sha256) VALUES (%s, %s) "
                          "ON CONFLICT DO NOTHING", (ref.document_id, img.sha256))
            self._drop_orphan_images()
        return "inserted" if old is None else "updated"

    def memory_refs(self, job=None, agent_name=None, agent_key=None, document_id=None) -> list[MemoryRef]:
        conds, params = [], []
        for col, val in (("job", job), ("agent_name", agent_name), ("agent_key", agent_key),
                         ("document_id", document_id)):
            if val is not None:
                conds.append(f"m.{col} = %s")
                params.append(val)
        where = (" WHERE " + " AND ".join(conds)) if conds else ""
        # the rows with their images in one statement: one snapshot
        rows = self._conn.execute(
            f"SELECT {self._MREF_COLS}, COALESCE((SELECT array_agg(ARRAY[i.sha256, i.mime, i.bytes::text] "
            "ORDER BY i.sha256) FROM memory_ref_images r JOIN transcript_images i ON i.sha256 = r.sha256 "
            "WHERE r.document_id = m.document_id), '{}'), "
            "(SELECT array_agg(i.first_seen ORDER BY i.sha256) FROM memory_ref_images r "
            "JOIN transcript_images i ON i.sha256 = r.sha256 WHERE r.document_id = m.document_id) "
            f"FROM memory_refs m{where} ORDER BY m.created_at, m.document_id", params).fetchall()
        out = []
        for r in rows:
            imgs = tuple(TranscriptImage(sha, mime, int(size), None, first)
                         for (sha, mime, size), first in zip(r[16], r[17] or ()))
            out.append(MemoryRef(document_id=r[0], bank=r[1], job=r[2], agent_key=r[3], agent_name=r[4],
                                 harness=r[5], host=r[6], session_id=r[7], tool_call_id=r[8], writer=r[9],
                                 raw_bytes=int(r[10]), redactions=int(r[11]), created_at=r[12], checked_at=r[13],
                                 patched=bool(r[14]), stored_bytes=int(r[15] or 0), images=imgs))
        return out

    def memory_ref_excerpt(self, document_id: str) -> bytes | None:
        row = self._conn.execute("SELECT excerpt FROM memory_refs WHERE document_id = %s", (document_id,)).fetchone()
        return None if row is None or row[0] is None else decompress_capped(bytes(row[0]))

    def mark_memory_refs_checked(self, document_ids) -> None:
        ids = list(dict.fromkeys(document_ids))
        if ids:
            self._conn.execute("UPDATE memory_refs SET checked_at = now() WHERE document_id = ANY(%s)", (ids,))

    def delete_memory_refs(self, document_ids, expected=None) -> int:
        ids = list(dict.fromkeys(document_ids))
        if expected is not None:
            ids = [d for d in ids if d in expected]
        if not ids:
            return 0
        with self._conn.transaction():
            self._conn.execute(self._IMAGE_LOCK)
            if expected is None:
                n = self._conn.execute("DELETE FROM memory_refs WHERE document_id = ANY(%s)", (ids,)).rowcount
            else:   # only rows still as read: created_at and checked_at unchanged (NULL-safe)
                n = sum(self._conn.execute(
                    "DELETE FROM memory_refs WHERE document_id = %s AND created_at IS NOT DISTINCT FROM %s "
                    "AND checked_at IS NOT DISTINCT FROM %s", (d, *expected[d])).rowcount for d in ids)
            self._drop_orphan_images()   # memory_ref_images went with the rows (ON DELETE CASCADE)
        return n
