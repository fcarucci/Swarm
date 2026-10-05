"""The storage-neutral interface of the swarm message board.

Everything the CLI and the hooks need from storage goes through a `Board`. A backend is a
subclass implementing every abstract method; `bin/board/postgres.py` is the production one,
`bin/board/memory.py` the in-process reference used by the tests.

Conventions every backend must follow (the per-method docstrings refine them):

* Time. Every timestamp crossing the boundary is a timezone-aware UTC-comparable `datetime`
  (tzinfo set; the CLI calls `.astimezone()` on it). "now" is the board's clock, `Board.now()`,
  not the caller's: with a shared server the server clock is authoritative, so the CLI computes
  "ago" strings against `board.now()`.
* Configuration. A board is constructed from the already-parsed config dict (`cfg`, the same
  dict `swarm.load_config` returns, all sections with defaults filled). Backends read
  `cfg["board"]` tunables (retention_days, message_max_chars, agent_stale_hours, read_limit,
  join_history, idle_minutes, dead_minutes, tool_timeout_minutes) and their own section (Postgres:
  `cfg["database"]`). They never parse TOML or print.
* Rendering stays in the CLI: backends return plain data (the frozen dataclasses below), never
  formatted text.
* Errors. Connection trouble is `BoardUnavailable`: at open, and (Postgres) mid-call when a
  query gets no reply within `[database] query_timeout_seconds` or the connection is lost, after
  which that board object fails every call at once. Bad storage state found by setup is
  `IncompatibleStorage`; everything else propagates as whatever the backend raises (callers
  treat it as fatal, the hooks swallow and log it). No method retries on its own.
* Concurrency. Many processes (one hook per tool call of every agent, plus CLIs) use the board
  at once, each with its own `Board` object. Each method is individually safe under that
  concurrency as stated in its docstring; there are no multi-call transactions. One `Board`
  object is used by one thread.
* Agents. An agent row is keyed by `agent_key` (the hook's agent_id: stable per subagent).
  It is *active* while `left_at` is None. Names are unique among active agents only; a
  departed agent keeps its row (and its name, cursor and counters) until purged.
* Messages. Ids are positive integers assigned by the board, unique across ALL jobs and
  increasing with insertion order (read cursors compare ids across the whole board). Ids become
  visible IN ORDER: once a reader can see message N, every message with a smaller id that will
  ever exist is visible too (Postgres serialises inserts with an advisory lock held to commit).
  Cursor reads depend on it: without it a reader could move past an id committed late and
  never see that message.

Derived agent status (`derive_agent_status`) is a pure function here so every backend agrees
on it; see its docstring for why Postgres nevertheless reads it from its view.
"""
from __future__ import annotations

import abc
import contextlib
import datetime as _dt
import hashlib
import json
import lzma
import re
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Mapping, NamedTuple, Sequence

from ..paths import DATA_DIR, PLUGIN_ROOT as SKILL_DIR  # noqa: E402,F401  (SKILL_DIR: old name)
from ..textsafe import strip_controls

# Name sources in allocation order: every free Simpsons name is tried before any English one.
NAME_SOURCES = ("simpsons", "english")

MEMORY_SEEN_MAX = 500   # memory ids remembered per agent as "already shown" (newest kept)

AGENT_STATES = ("started", "running", "completed", "left", "dead")   # stored lifecycle state
AGENT_STATUSES = ("started", "running", "idle", "dead", "completed", "left")  # derived
JOB_STATUSES = ("active", "paused", "completed", "cancelled", "failed")
CLOSED_JOB_STATUSES = ("completed", "cancelled", "failed")
TOOL_NAME_MAX = 80   # current_tool is stored truncated to this many characters
# How far the hooks got routing a subagent to a job (see Route): the tag in its spawn prompt was
# not readable yet (pending), it joined the session's only job before the tag could be read
# (unverified), or the decision is made (final; job None = on no board).
ROUTE_STATES = ("pending", "unverified", "final")
VERDICTS = ("met", "not_met")   # a judge's verdict on a job's goal
# Auto-close (Board.sweep_auto_close): the closed_by of a job the sweep closed, the derived agent
# statuses that keep a job open (an agent still at work, or silent for less than dead_minutes),
# and the cap on the outcome it writes.
AUTO_CLOSED_BY = "auto"
AUTO_CLOSE_BLOCKING = ("started", "running", "idle")
AUTO_CLOSE_OUTCOME_MAX = 200
# The supervisor (swarm.supervisor): an agent it closed has left_reason STUCK_PREFIX + one of
# STUCK_REASONS; only those are restarted. A replacement's row has resume_of = the
# agent_key it replaced.
STUCK_PREFIX = "stuck:"
STUCK_REASONS = ("dead", "tool", "silent", "orphaned")
# How a supervisor replacement ended (Restart.outcome; None while it runs). "refused": recorded
# ended at once, no replacement started.
RESTART_OUTCOMES = ("running", "completed", "timeout", "max_turns", "failed", "not_enrolled",
                    "stuck", "cancelled", "refused")
# The storage layout this code needs. Every backend's `setup` records it in the store (Postgres:
# the board_meta row 'schema_version'; SQLite: PRAGMA user_version; file: the schema_version
# file; memory: the store), and board.ensure_initialized runs `setup` on a board that records an
# older one (or none). Bump it with every schema change (new table, column, view or trigger):
# 2 added jobs.closed_by, 3 the transcripts table, 4 transcript_images and transcript_image_refs,
# 5 agents.harness/model/turn_ended_at/os_user and transcripts.harness, 6 agents.left_reason/
# resume_of, jobs.supervise and the restarts table, 7 the checks on agent names (messages.
# agent_name/to_agent, agents.name: NAME_RULE) and on transcript_images.sha256 (64 lowercase hex),
# added so that existing rows don't block the upgrade (Postgres CHECK ... NOT VALID, SQLite
# triggers), 8 memory_refs and memory_ref_images (memory provenance; the name check of 7 on
# memory_refs.agent_name, image keys through transcript_images), 9 transcripts.capture_failed
# (a final capture that kept failing: the row is an audit marker without a body),
# 10 jobs.verdict_next (the judge's instructions with a not_met verdict), 11 jobs.max_hours (a
# job's own stall limit, in hours) and jobs.waiting_until (when a bounded `swarm wait --for` expires),
# 12 jobs.status 'paused' and the job_pauses table (pause/resume manifests), 13 the job_status
# view's shown_status column (what `status` shows, incl. "waiting (goal not met)"; view only),
# 14 the index messages(job, created_at) (job_status's per-job max(created_at);
# `swarm watch` redraws read it), 16 jobs.plugin_data (a JSON object of per-job settings that CLI
# plugins keep with the job: Board.job_data / set_job_data).
SCHEMA_VERSION = 16

JOB_DATA_KEY = re.compile(r"[a-z0-9][a-z0-9_.-]{0,63}")
JOB_DATA_VALUE_MAX = 2000


def check_job_data(key: str, value: str | None) -> None:
    """ValueError unless (key, value) may be stored by set_job_data."""
    if not isinstance(key, str) or JOB_DATA_KEY.fullmatch(key) is None:
        raise ValueError("job data key must be 1-64 of a-z, 0-9, _, ., - (starting with a letter or digit)")
    if value is not None and (not isinstance(value, str) or len(value) > JOB_DATA_VALUE_MAX):
        raise ValueError(f"job data value must be a string of at most {JOB_DATA_VALUE_MAX} characters")


def parse_job_data(raw: str | None) -> dict[str, str]:
    """A job's stored plugin data as a dict; a damaged value counts as empty."""
    try:
        data = json.loads(raw) if raw else {}
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str)}


def merged_job_data(raw: str | None, key: str, value: str | None) -> str:
    """The JSON text of a job's plugin data `raw` with `key` set to `value` (None removes it)."""
    data = parse_job_data(raw)
    if value is None:
        data.pop(key, None)
    else:
        data[key] = value
    return json.dumps(data, sort_keys=True)

# 15 the message cap is a per-board setting stored in the board
# (Postgres board_meta 'message_max_chars' and a replaceable NOT VALID CHECK on a text column
# instead of varchar(N); SQLite board_meta table and a trigger instead of the table CHECK;
# file/memory: a field of the store), see Board.message_cap.


# The message cap: the longest a board message may be, in characters. One authoritative value per
# board, stored in the board (Board.message_cap); [board] message_max_chars is only the value a NEW
# board starts with (and the fallback while a board has none stored). Changed with
# `swarm config board.message_max_chars N` (Board.set_message_cap).
MESSAGE_CAP_DEFAULT = 200
MESSAGE_CAP_MIN = 50
MESSAGE_CAP_MAX = 4000
