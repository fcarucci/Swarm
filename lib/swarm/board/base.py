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
from swarm import compat

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
# 12 jobs.status 'paused' and the job_pauses table (pause/resume manifests).
SCHEMA_VERSION = 12

# A moved agent's roster_seen holds MOVED_PREFIX + the job it came from until its next PreToolUse
# turn tells it (no schema change: the hooks own the text, and it never parses as a snapshot).
MOVED_PREFIX = "@moved "

# Memory provenance (Board.save_memory_ref): what a writer name may look like (the built-in
# `swarm-remember`, or a name from [provenance] writers), and how large a stored excerpt may be
# once decompressed (a read never trusts a row's size: see decompress_capped).
# Boards created before writers became configurable have a CHECK on memory_refs.writer that allows
# only the names stored then (swarm-remember and two legacy ones); a new writer name is refused
# there by the database. New boards have no such CHECK.
WRITER_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,39}")
EXCERPT_MAX_RAW = 4 * 1024 * 1024
# How large a stored transcript body may be once decompressed (Board.transcript_body reads it
# through decompress_capped): a transcript row can be forged or corrupted like a memory ref (a
# sandboxed agent with a writable SQLite/file board), and an lzma bomb of a few KB would otherwise
# decompress to gigabytes in `swarm transcript show` or a supervisor brief. 128 MiB is well above
# a real capture (60 MB is a huge Claude transcript) and still bounded in memory;
# transcripts._fit cuts a capture to it (head + tail), so every body the swarm writes reads back.
# A stored body over it (or corrupt) raises BoardError on read.
TRANSCRIPT_MAX_RAW = 128 * 1024 * 1024
# What one memory ref may carry in images (checked on save): an excerpt is ~20 turns, and
# [provenance] excerpt_image_mb (default 5) already limits the images the hook keeps. These caps
# stop a forged or runaway ref from filling the shared image store; they sit well above what
# the hook produces, so a normal save never hits them.
MEMORY_REF_IMAGES_MAX = 32
MEMORY_REF_IMAGE_BYTES_MAX = 16 * 1024 * 1024

# Agent names: a name is rendered into other agents' hook
# context and the operator's terminal, so it is 1-64 ASCII letters, digits, spaces and . ' _ -,
# not starting or ending with a space. The shipped pools and the "<name> <NNN>" fallback fit.
NAME_MAX = 64
NAME_PATTERN = r"[A-Za-z0-9._'-](?:[A-Za-z0-9 ._'-]{0,62}[A-Za-z0-9._'-])?"   # also the Postgres CHECK
NAME_RULE = re.compile(NAME_PATTERN)
SHA256_RULE = re.compile(r"[0-9a-f]{64}")


def valid_name(name) -> bool:
    """Whether `name` is an acceptable agent name (NAME_RULE, the whole string)."""
    return isinstance(name, str) and NAME_RULE.fullmatch(name) is not None


def check_name(name, what: str = "name") -> str:
    """`name` if valid_name(name), else ValueError naming `what`."""
    if not valid_name(name):
        raise ValueError(f"invalid {what} {name!r}: use 1-{NAME_MAX} letters, digits, spaces and "
                         f". ' _ - (no leading or trailing space)")
    return name


def valid_pool(names: Mapping[str, Sequence[str]]) -> dict[str, list[str]]:
    """`names` ({source: [name]}) without the names valid_name refuses (setup skips them)."""
    return {source: [n for n in lst if valid_name(n)] for source, lst in names.items()}


def check_images(images) -> None:
    """Every TranscriptImage's sha256 is 64 lowercase hex digits and the digest of its data:
    the key names a file in the file backend and in `transcript export`. ValueError otherwise."""
    for img in images:
        if not isinstance(img.sha256, str) or SHA256_RULE.fullmatch(img.sha256) is None:
            raise ValueError(f"invalid image sha256 {img.sha256!r}")
        if img.data is None or hashlib.sha256(img.data).hexdigest() != img.sha256:
            raise ValueError(f"image sha256 {img.sha256} does not match its data")


def check_excerpt(excerpt: bytes | None, raw_bytes: int) -> None:
    """A memory ref's excerpt is complete lzma that decompresses to exactly `raw_bytes` bytes, at
    most EXCERPT_MAX_RAW; no excerpt means raw_bytes 0. ValueError otherwise (nothing stored), so
    every stored excerpt reads back through decompress_capped."""
    if excerpt is None:
        if raw_bytes:
            raise ValueError(f"raw_bytes {raw_bytes} given without an excerpt")
        return
    try:
        out = decompress_capped(excerpt)
    except BoardError as exc:
        raise ValueError(f"invalid excerpt: {exc}") from None
    if len(out) != raw_bytes:
        raise ValueError(f"excerpt decompresses to {len(out)} bytes, not raw_bytes {raw_bytes}")


def check_memory_ref_images(images) -> None:
    """At most MEMORY_REF_IMAGES_MAX distinct images, MEMORY_REF_IMAGE_BYTES_MAX bytes together,
    each image's size the length of its data. ValueError otherwise."""
    distinct = {i.sha256: i for i in images}
    if len(distinct) > MEMORY_REF_IMAGES_MAX:
        raise ValueError(f"{len(distinct)} images in one memory ref (at most {MEMORY_REF_IMAGES_MAX})")
    for img in distinct.values():
        if img.data is None or img.size != len(img.data):
            raise ValueError(f"image {img.sha256}: size {img.size} is not the length of its data")
    total = sum(len(i.data) for i in distinct.values())
    if total > MEMORY_REF_IMAGE_BYTES_MAX:
        raise ValueError(f"memory ref images total {total} bytes (at most {MEMORY_REF_IMAGE_BYTES_MAX})")


# --------------------------------------------------------------------------- errors

class BoardError(Exception):
    """Base class for errors the board raises on purpose."""


class BoardUnavailable(BoardError):
    """The storage cannot be reached (e.g. no network from inside a sandbox).

    Raised by `open_board()` / backend constructors, which connect eagerly so callers can react
    before doing anything (the CLI spools a `post` instead). Always raised `from` the underlying
    exception; `str()` equals `str(__cause__)`, and callers that print the error type use
    `type(exc.__cause__ or exc).__name__` (e.g. "OperationalError"), keeping CLI output unchanged."""


def database_hosts(db: dict) -> list[tuple[str, int]]:
    """The Postgres servers of a [database] section as [(host, port), ...], in the order given.

    `hosts` (if set) or else `host` may be one name, a comma-separated string, or a TOML array
    (elements may themselves be comma-separated); each entry may carry its own port as host:port
    ([v6::addr]:port for IPv6). `port` is the default for entries without one: a single port for
    all, or a list (array or comma-separated) with one port per host. A single plain host and
    port give exactly what the section always meant."""
    raw = db.get("hosts") or db.get("host") or "localhost"
    parts = [raw] if isinstance(raw, str) else list(raw)
    tokens = [t.strip() for part in parts for t in str(part).split(",") if t.strip()]
    port = db.get("port", 5432)
    ports = [p for p in (str(x).strip() for q in (port if isinstance(port, (list, tuple)) else [port])
                         for x in str(q).split(",")) if p]
    if len(ports) > 1 and len(ports) != len(tokens):
        raise BoardError(f"[database] port lists {len(ports)} ports for {len(tokens)} hosts; "
                         "give one port, or one per host")
    out = []
    for i, tok in enumerate(tokens):
        host, own = tok, None
        if tok.startswith("["):
            host, _, rest = tok[1:].partition("]")
            own = rest[1:] if rest.startswith(":") else None
        elif tok.count(":") == 1:
            host, own = tok.split(":")
        default = ports[i] if len(ports) > 1 else (ports[0] if ports else "5432")
        try:
            out.append((host, int(own or default)))
        except ValueError:
            raise BoardError(f"[database] host {tok!r}: the port is not a number") from None
    return out


class ReadOnlyBoard(BoardError):
    """A write on a board opened read-only (`open_read_only`: `swarm supervise --dry-run`)."""


class IncompatibleStorage(BoardError):
    """`Board.setup` found existing storage it must not use (Postgres: a non-UTF8 database).

    `str()` is the complete human-readable explanation; the CLI prints "ERROR: <str>" to stderr
    and exits 1."""


# Pause/resume (schema 12). A paused job refuses new joins and posts (JobPaused) until it is
# resumed; PAUSE_WRITER is the name the pause/resume notices are posted under (the one writer
# allowed to post to a paused job), LEFT_PAUSED the left_reason of the agents a pause closed.
PAUSE_WRITER = "swarm-pause"
LEFT_PAUSED = "paused"
MANIFEST_VERSION = 1


class JobPaused(BoardError):
    """The job is paused: no agent may join it and nobody may post to it until `swarm resume`."""

    def __init__(self, job: str, paused_at=None, paused_by: str | None = None, reason: str | None = None):
        self.job, self.paused_at, self.paused_by, self.reason = job, paused_at, paused_by, reason
        since = f" since {paused_at.strftime('%Y-%m-%d %H:%M UTC')}" if paused_at else ""
        by = f" by {paused_by}" if paused_by else ""
        why = f": {reason}" if reason else ""
        super().__init__(f"job {job} is paused{since}{by}{why}. Nothing can join or post until it is "
                         f"resumed: swarm resume --job {job}")


@dataclass(frozen=True)
class PauseRecord:
    """One pause of a job (a job_pauses row). manifest: the resume manifest (version
    MANIFEST_VERSION, see swarm.pause): {"version", "job", "paused_at", "paused_by", "reason",
    "job_state": {...}, "agents": [{agent_key, agent_name, role, kind, host, os_user, harness,
    model, session_id, cursor, tool_calls, last_tool, turn_ended_at, last_seen, status_at_pause,
    judge, verifier, resume_of}]}. resumed_at None: still paused. outcome: what resume did per
    agent ({agent_key: {"status": ..., ...}}), None until a resume recorded something."""
    id: int
    job: str
    paused_at: _dt.datetime
    paused_by: str | None
    reason: str | None
    manifest: dict
    resumed_at: _dt.datetime | None = None
    resumed_by: str | None = None
    resumed_host: str | None = None
    outcome: dict | None = None


def build_manifest(job: str, paused_at: _dt.datetime, by: str | None, reason: str | None,
                   job_row: Mapping, agents: Sequence[Mapping], last_message_id: int,
                   cwds: Mapping[str, str] | None = None) -> dict:
    """The resume manifest of PauseRecord, from plain rows (every backend's pause_job uses this).
    `job_row`: goal, task, description, project, verdict, waiting_on, max_hours. `agents`: one
    mapping per active agent with agent_key, name, role, host, os_user, harness, model, session_id,
    last_read_id, tool_calls, current_tool, turn_ended_at, last_seen, status, judge, verifier,
    resume_of, orchestrator (bool). Only board metadata goes in: no transcript text, no secrets."""
    def iso(v):
        return v.isoformat() if isinstance(v, _dt.datetime) else v
    cwds = cwds or {}
    return {
        "version": MANIFEST_VERSION, "job": job, "paused_at": iso(paused_at), "paused_by": by,
        "reason": reason,
        "job_state": {k: job_row.get(k) for k in ("goal", "task", "description", "project", "verdict",
                                                  "waiting_on", "max_hours")}
                     | {"last_message_id": last_message_id},
        "agents": [
            {"agent_key": a["agent_key"], "agent_name": a["name"], "role": a.get("role"),
             "kind": "orchestrator" if a.get("orchestrator") else "subagent",
             "host": a.get("host"), "os_user": a.get("os_user"), "harness": a.get("harness"),
             "model": a.get("model"), "session_id": a.get("session_id"),
             "cursor": int(a.get("last_read_id") or 0), "cwd": cwds.get(a["agent_key"]),
             "task": job_row.get("task"), "tool_calls": int(a.get("tool_calls") or 0),
             "last_tool": a.get("current_tool"), "turn_ended_at": iso(a.get("turn_ended_at")),
             "last_seen": iso(a.get("last_seen")), "status_at_pause": a.get("status"),
             "judge": bool(a.get("judge")), "verifier": bool(a.get("verifier")),
             "resume_of": a.get("resume_of")}
            for a in agents],
    }


def decompress_capped(body: bytes, cap: int = EXCERPT_MAX_RAW, what: str = "excerpt") -> bytes:
    """lzma-decompress a stored excerpt (or, with cap=TRANSCRIPT_MAX_RAW and what="transcript",
    a transcript body), refusing one that would exceed `cap` bytes or is corrupt: rows can be
    forged by anything that can write the store (a sandboxed agent with a writable SQLite/file
    board), so a read never trusts their size. BoardError names `what`."""
    d = lzma.LZMADecompressor()
    try:
        out = d.decompress(body, max_length=cap + 1)
    except lzma.LZMAError as exc:
        raise BoardError(f"stored {what} is not valid lzma: {exc}") from None
    if len(out) > cap or not d.eof:
        raise BoardError(f"stored {what} is larger than {cap} bytes uncompressed (or cut short): refused")
    return out


def decompress_transcript(body: bytes) -> bytes:
    """A stored transcript body through decompress_capped (TRANSCRIPT_MAX_RAW)."""
    return decompress_capped(body, TRANSCRIPT_MAX_RAW, "transcript")


# --------------------------------------------------------------------------- data

@dataclass(frozen=True)
class Message:
    """One board message. `to_agent` is the addressee's name or None (broadcast)."""
    id: int
    created_at: _dt.datetime
    job: str
    agent_name: str
    to_agent: str | None
    message: str


@dataclass(frozen=True)
class PostResult:
    id: int
    truncated: bool   # the message was longer than message_max_chars and was cut


@dataclass(frozen=True)
class SetupResult:
    """What `Board.setup` did. `notes` are lines the CLI prints verbatim before the pool summary
    (Postgres: "created database <name> (UTF8)" when it created one; empty otherwise).
    `pool` maps each source in NAME_SOURCES order to the number of names now in the pool."""
    notes: tuple[str, ...]
    pool: dict[str, int]


@dataclass(frozen=True)
class AgentStatus:
    """One agent with its derived status (the Postgres `agent_status` view, row for row).

    status: see derive_agent_status. messages: posts on `job` by `name` created at or after
    joined_at. last_contact_at: last hook contact (last_seen). ended_at: left_at (None = active).
    left_reason: why it left when the supervisor (or a runner) closed it, e.g. "stuck:dead",
    "limit:timeout"; resume_of: the agent_key this replacement took over from."""
    job: str
    name: str
    role: str | None
    status: str
    current_tool: str | None
    tool_calls: int
    messages: int
    joined_at: _dt.datetime
    last_contact_at: _dt.datetime
    last_post_at: _dt.datetime | None
    ended_at: _dt.datetime | None
    host: str | None
    agent_key: str
    harness: str | None = None
    model: str | None = None
    os_user: str | None = None
    left_reason: str | None = None
    resume_of: str | None = None


@dataclass(frozen=True)
class JobStatus:
    """One job with its rollup (the Postgres `job_status` view, row for row).

    Counts are over ALL agent rows of the job (active and departed) by derived status:
    agents = all of them; started/running/idle/completed = that status; dead_or_left = dead or
    left. messages = all messages of the job. last_activity_at = the later of the newest
    agent last_contact_at and the newest message created_at (None if neither exists)."""
    job: str
    status: str
    description: str | None
    task: str | None
    outcome: str | None
    created_by: str | None
    session_id: str | None
    created_at: _dt.datetime
    activated_at: _dt.datetime | None
    finished_at: _dt.datetime | None
    agents: int
    started: int
    running: int
    idle: int
    completed: int
    dead_or_left: int
    messages: int
    last_activity_at: _dt.datetime | None
    project: str | None = None   # the memory project (Hindsight bank); None = use the job name
    # Goal and judge: what "done" means (None = no goal), the judge's latest verdict (VERDICTS,
    # None = none yet) with its reason, judge name and time, the name of the job's ACTIVE judge
    # (None if none), and whether it was closed completed without a met verdict (--force).
    goal: str | None = None
    verdict: str | None = None
    verdict_reason: str | None = None
    verdict_by: str | None = None
    verdict_at: _dt.datetime | None = None
    completion_forced: bool = False
    judge: str | None = None
    # What an open job is waiting for (`swarm wait --on`), and since when; None = not waiting.
    waiting_on: str | None = None
    waiting_since: _dt.datetime | None = None
    # Who closed it: AUTO_CLOSED_BY when the auto-close sweep did, else what close_job was given
    # (the CLI passes $USER); None while open and for jobs closed before the column existed.
    closed_by: str | None = None
    supervise: bool = True   # False: swarm activate --no-supervise (no closing, no restarts)
    # The judge's instructions with a not_met verdict (what to change, where, what it re-checks);
    # None for met and for verdicts recorded before the column existed.
    verdict_next: str | None = None
    # A per-job stall limit in hours: how long the job may go without progress (`activate
    # --stall-hours`; column max_hours; None = the [job] stall_hours default, 0 = never) and when a bounded wait (`wait --for`) expires (None = unbounded).
    max_hours: float | None = None
    waiting_until: _dt.datetime | None = None


@dataclass(frozen=True)
class Restart:
    """One supervisor replacement (Board.record_restart): started, or refused, for the closed
    agent old_agent_key, by the supervisor of host/os_user."""
    id: int
    job: str
    agent_key: str            # lineage root
    attempt: int              # 1-based within (job, agent_key)
    at: _dt.datetime
    reason: str               # e.g. "stuck:dead", "stuck:orphaned"; "outage" when the restart was held for an outage
    old_agent_key: str
    new_agent_key: str | None
    harness: str
    host: str
    os_user: str
    minutes_cap: float
    ended_at: _dt.datetime | None
    outcome: str | None       # RESTART_OUTCOMES; None while running


def restart_reserved_minutes(r: "Restart") -> float:
    """What a restart holds of the minute caps: a running one its whole cap, an ended one what it
    used (at most its cap), a refused one nothing."""
    if r.outcome == "refused":
        return 0.0
    if r.ended_at is None:
        return float(r.minutes_cap)
    return max(0.0, min((r.ended_at - r.at).total_seconds() / 60, float(r.minutes_cap)))


def restart_overlaps(r: "Restart", since: _dt.datetime) -> bool:
    """Whether a restart ran at any time since `since` (began after it, still runs, or ended after it)."""
    return r.at >= since or r.ended_at is None or r.ended_at >= since


_CAP_SLACK = 1e-6   # float minutes


def restart_over_limits(job_rows: list, host_rows: list, minutes_cap: float, *,
                        max_job_minutes: float | None = None, max_host_running: int | None = None,
                        max_host_minutes: float | None = None,
                        day_start: _dt.datetime | None = None) -> bool:
    """Whether a new restart reserving `minutes_cap` would break a cap, given the job's rows and
    the host's rows (every OS user's) read under the insert's lock (Board.record_restart)."""
    if max_job_minutes is not None and \
            sum(restart_reserved_minutes(r) for r in job_rows) + minutes_cap > max_job_minutes + _CAP_SLACK:
        return True
    if max_host_running is not None and sum(1 for r in host_rows if r.ended_at is None) >= int(max_host_running):
        return True
    if max_host_minutes is not None:
        day = [r for r in host_rows if day_start is None or restart_overlaps(r, day_start)]
        if sum(restart_reserved_minutes(r) for r in day) + minutes_cap > max_host_minutes + _CAP_SLACK:
            return True
    return False


@dataclass(frozen=True)
class AutoClosed:
    """A job Board.sweep_auto_close closed, with the outcome it recorded."""
    job: str
    outcome: str


@dataclass(frozen=True)
class ReadResult:
    """One cursor read: `messages` (oldest first) and `remaining`, how many more unread messages
    matched but were held back by read_limit (they come on the next reads)."""
    messages: list
    remaining: int = 0


@dataclass(frozen=True)
class RosterEntry:
    """One agent of a job as the roster shows it. status is the derived status (as in
    AgentStatus); active is False once the agent departed (left_at set)."""
    agent_key: str
    name: str
    role: str | None
    status: str
    current_tool: str | None
    active: bool


@dataclass(frozen=True)
class OwedReply:
    """A message addressed to an agent (to_agent = its name, on its job, since it joined) that
    it has been shown (id <= its cursor) and not answered: it has posted nothing addressed to
    the sender with a higher id."""
    id: int
    sender: str
    created_at: _dt.datetime


@dataclass(frozen=True)
class SyncState:
    """What the hooks last told one agent, kept in its agent row, plus the board's clock.

    roster_seen: the roster snapshot last shown (opaque text owned by the hooks);
    roster_synced_at: when the last FULL roster was shown; memory_recalled_at: last memory
    recall; memory_seen: memory ids already shown (at most MEMORY_SEEN_MAX, oldest dropped);
    remembered_at: last memory stored by this agent; nudged_at: last "store what you learned"
    reminder; joined_at and name: from the row; now: board time when this was read.
    Talking on the board: tool_calls and last_post_at (as in AgentStatus); calls_at_post, the
    tool_calls count at its last post (0 if it never posted); silence_nudged_at, the last
    "post a status" nudge; reply_reminded_id, the highest addressed-message id it has been
    reminded about; replies_owed, the OwedReply rows with id > reply_reminded_id, by id."""
    name: str
    joined_at: _dt.datetime
    now: _dt.datetime
    roster_seen: str | None = None
    roster_synced_at: _dt.datetime | None = None
    memory_recalled_at: _dt.datetime | None = None
    memory_seen: tuple = ()
    remembered_at: _dt.datetime | None = None
    nudged_at: _dt.datetime | None = None
    tool_calls: int = 0
    calls_at_post: int = 0
    last_post_at: _dt.datetime | None = None
    silence_nudged_at: _dt.datetime | None = None
    reply_reminded_id: int = 0
    replies_owed: tuple = ()


@dataclass(frozen=True)
class Member:
    """An active agent as PreToolUse sees it (what tool_started returns): its name, the job of
    its row, whether its route is still "unverified" (joined before its tag was readable), and
    whether it is a verifier (read-only: the hooks refuse its writing tools)."""
    name: str
    job: str
    verify_tag: bool = False
    verifier: bool = False
    model: str | None = None


@dataclass(frozen=True)
class Route:
    """The routing record of one agent_key (all None if none was recorded): state (one of
    ROUTE_STATES), job (the routed job; None while pending or when routed to no board),
    session_id (the Claude session it was recorded for), plus member_job, the job of the
    agent's board row, active or departed (None if it has none)."""
    state: str | None
    job: str | None
    session_id: str | None
    member_job: str | None


@dataclass(frozen=True)
class SpawnGrant:
    """The outcome of Board.reserve_spawn: whether the agent may spawn one more subagent, and
    the counts after the call (a refusal leaves them unchanged). `refused` names the cap hit:
    "agent" or "job" (None when granted, or "member" if the key is not an active agent of job)."""
    granted: bool
    agent_spawns: int
    job_spawns: int
    refused: str | None = None


@dataclass(frozen=True)
class AgentEvent:
    """An agent row as `swarm tail` needs it to print joins and departures.
    state is the stored lifecycle state (AGENT_STATES), not the derived status."""
    name: str
    job: str
    role: str | None
    joined_at: _dt.datetime
    left_at: _dt.datetime | None
    state: str


TRANSCRIPT_ROLES = ("subagent", "orchestrator")


@dataclass(frozen=True)
class TranscriptRow:
    """One transcript to store (Board.save_transcript), built by transcripts.make_row.

    body: the redacted JSONL, lzma-compressed (stored_bytes = len(body)); raw_bytes: its size
    uncompressed; sha256: hex digest of the full redacted text, the "unchanged" test;
    redactions: how many secrets were replaced; role one of TRANSCRIPT_ROLES. captured_at None
    means the board's now (a set value is for imports and tests).

    failed (schema 9): the reason the final capture kept failing (Board.mark_capture_failed). Set
    on a non-final snapshot, the snapshot stays (final now, its body, sha256, sizes and images
    kept: raw_bytes still describes it); with no snapshot, the row is a bodiless marker
    (transcripts.capture_failed_row: raw_bytes 0, no redactions, no images, an empty body that
    Board.transcript_body never returns: `bodiless`)."""
    job: str
    agent_key: str
    agent_name: str
    role: str
    host: str | None
    session_id: str | None
    final: bool
    raw_bytes: int
    redactions: int
    sha256: str
    body: bytes
    captured_at: _dt.datetime | None = None
    images: tuple["TranscriptImage", ...] = ()   # the images the body refers to, with data
    harness: str | None = None
    failed: str | None = None


@dataclass(frozen=True)
class TranscriptImage:
    """An image taken out of a transcript (transcripts.extract_images): stored once per sha256
    (of the raw image bytes) however many transcripts refer to it, not recompressed. data is
    None where only the metadata was asked for; first_seen is set by the board."""
    sha256: str
    mime: str
    size: int
    data: bytes | None = None
    first_seen: _dt.datetime | None = None


@dataclass(frozen=True)
class MemoryRef:
    """Where a memory an agent saved came from (Board.save_memory_ref / memory_refs): the
    Hindsight document (document_id, bank), who wrote it (job, agent_key, agent_name, harness,
    host, session_id) in which tool call (tool_call_id), with which tool (writer, one of
    WRITER_NAME), and an excerpt of the agent's transcript around it.

    excerpt: on save, the lzma of the redacted JSONL (None: no excerpt); on reads always None
    (Board.memory_ref_excerpt returns it decompressed) and stored_bytes = its stored length.
    raw_bytes: its size uncompressed; redactions: secrets replaced. images: on save, the images
    the excerpt refers to, with data (stored in the transcript image store); on reads, metadata
    only. created_at None on save means the board's now; checked_at is when provenance.prune
    last found the document (set by mark_memory_refs_checked); patched: whether the document's
    metadata was patched with the provenance."""
    document_id: str
    bank: str
    job: str
    agent_key: str
    agent_name: str
    harness: str | None
    host: str | None
    session_id: str | None
    tool_call_id: str | None
    writer: str
    excerpt: bytes | None = None
    raw_bytes: int = 0
    redactions: int = 0
    images: tuple["TranscriptImage", ...] = ()
    created_at: _dt.datetime | None = None
    checked_at: _dt.datetime | None = None
    patched: bool = False
    stored_bytes: int = 0


class TranscriptTotals(NamedTuple):
    """Board.transcript_totals. stored and raw include every stored image once (images are not
    compressed: they count the same in both); image_bytes / images are those images alone."""
    stored: int
    raw: int
    jobs: int
    oldest: _dt.datetime | None
    image_bytes: int = 0
    images: int = 0


@dataclass(frozen=True)
class TranscriptSummary:
    """A stored transcript without its body (Board.transcripts). raw_bytes and stored_bytes
    include the images it refers to (image_bytes of them); images: their (sha256, size).
    failed: set on a capture-failed row (TranscriptRow.failed): a kept snapshot, or a bodiless
    marker (`bodiless`)."""
    job: str
    agent_key: str
    agent_name: str
    role: str
    host: str | None
    captured_at: _dt.datetime
    final: bool
    raw_bytes: int
    stored_bytes: int
    redactions: int
    session_id: str | None = None
    sha256: str | None = None
    image_bytes: int = 0
    images: tuple[tuple[str, int], ...] = ()
    harness: str | None = None
    failed: str | None = None


# --------------------------------------------------------------------------- pure helpers

def bodiless(row) -> bool:
    """Whether a stored transcript (TranscriptRow or TranscriptSummary) is a capture-failed marker
    with no text at all: failed, and nothing stored (raw_bytes 0; a kept snapshot has more)."""
    return bool(getattr(row, "failed", None)) and not getattr(row, "raw_bytes", 0)


def transcript_rotation_victims(summaries: Sequence[TranscriptSummary], now: _dt.datetime,
                                days: float, max_bytes: int, active_jobs) -> tuple[list, list]:
    """What Board.rotate_transcripts deletes, from the summaries alone: (rows older than the
    time limit, as (job, agent_key); whole jobs to drop for the size limit, oldest first).
    See rotate_transcripts for the rules."""
    active = set(active_jobs)
    old = []
    if days and days > 0:
        cut = now - _dt.timedelta(days=days)
        old = [(s.job, s.agent_key) for s in summaries if s.job not in active and s.captured_at < cut]
    jobs_out = []
    if max_bytes and max_bytes > 0:
        gone = set(old)
        left = [s for s in summaries if (s.job, s.agent_key) not in gone]
        refs: dict = {}      # sha256 -> [size, how many remaining transcripts refer to it]
        for s in left:
            for sha, size in s.images:
                refs.setdefault(sha, [size, 0])[1] += 1
        total = sum(s.stored_bytes - s.image_bytes for s in left) + sum(v[0] for v in refs.values())
        newest: dict = {}
        by_job: dict = {}
        for s in left:
            if s.job in active:
                continue
            newest[s.job] = max(newest.get(s.job, s.captured_at), s.captured_at)
            by_job.setdefault(s.job, []).append(s)
        for job in sorted(newest, key=lambda j: (newest[j], j)):
            if total <= max_bytes:
                break
            jobs_out.append(job)
            for s in by_job[job]:
                total -= s.stored_bytes - s.image_bytes
                for sha, _size in s.images:
                    refs[sha][1] -= 1
                    if refs[sha][1] == 0:    # its last reference: the image goes too
                        total -= refs[sha][0]
    return old, jobs_out


def transcript_totals_of(summaries: Sequence[TranscriptSummary]) -> TranscriptTotals:
    """The TranscriptTotals of the summaries: each image counted once."""
    images = {sha: size for s in summaries for sha, size in s.images}
    text_stored = sum(s.stored_bytes - s.image_bytes for s in summaries)
    text_raw = sum(s.raw_bytes - s.image_bytes for s in summaries)
    img = sum(images.values())
    return TranscriptTotals(text_stored + img, text_raw + img, len({s.job for s in summaries}),
                            min((s.captured_at for s in summaries), default=None), img, len(images))


def derive_job_status(js: JobStatus, idle_minutes: float, now: _dt.datetime) -> str:
    """What `status` and `watch` show for a job. A closed job shows its stored status. An open
    (stored "active") one shows "waiting" while it waits for something (Board.set_waiting),
    "active" while an agent is started or running or anything happened within idle_minutes,
    else "idle": open, but nobody is working on it and nobody said what it waits for."""
    if js.status != "active":
        return js.status
    if js.waiting_on:
        return "waiting"
    if js.started or js.running:
        return "active"
    last = js.last_activity_at or js.activated_at or js.created_at
    return "active" if (now - last).total_seconds() < idle_minutes * 60 else "idle"


def run_start(js: JobStatus) -> _dt.datetime:
    """When the job's current run began: its last activation, else its creation."""
    return js.activated_at or js.created_at


def run_agents(agents: Sequence[AgentStatus], start: _dt.datetime) -> list[AgentStatus]:
    """The agents of the run that began at `start`: still active, or departed since. A reopened
    job keeps its earlier runs' agents as departed rows (all left before it was closed); they
    neither keep it open nor close it."""
    return [a for a in agents if a.ended_at is None or a.ended_at >= start]


def auto_close_candidate(js: JobStatus, before: _dt.datetime) -> bool:
    """A cheap first look, from the rollup alone, at whether the sweep should examine a job:
    open, not waiting, its goal (if any) met, agents but none started/running/idle, and nothing
    in the rollup newer than `before`. Board.auto_close_job decides for real."""
    return (js.status == "active" and not js.waiting_on and (not js.goal or js.verdict == "met")
            and js.agents > 0 and not (js.started or js.running or js.idle)
            and run_start(js) < before
            and (js.last_activity_at is None or js.last_activity_at < before))


def auto_close_outcome(agents: Sequence[AgentStatus], last: "Message | None") -> str:
    """The outcome the sweep records: "auto-closed: 3/3 agents completed; last post <name>:
    <text>" (", N dead" / ", N left" after the count when there are any; "no posts" if the job
    has none), the post cut with "…" so the whole fits AUTO_CLOSE_OUTCOME_MAX characters."""
    n = lambda status: sum(1 for a in agents if a.status == status)  # noqa: E731
    head = f"auto-closed: {n('completed')}/{len(agents)} agents completed"
    head += "".join(f", {n(st)} {st}" for st in ("dead", "left") if n(st))
    if last is None:
        return head + "; no posts"
    head += f"; last post {last.agent_name}: "
    text, _ = normalize_message(last.message, max(1, AUTO_CLOSE_OUTCOME_MAX - len(head)))
    return (head + text)[:AUTO_CLOSE_OUTCOME_MAX]


def normalize_message(message: str, cap: int) -> tuple[str, bool]:
    """Collapse all whitespace runs to single spaces and strip, drop terminal controls (C0, DEL,
    C1, U+2028/U+2029, bidi overrides and isolates: textsafe.strip_controls), then collapse
    again; if longer than `cap` characters (characters, not bytes), keep cap-1 characters plus
    "…". Returns (text, truncated)."""
    message = " ".join(strip_controls(" ".join(message.split())).split())
    truncated = len(message) > cap
    if truncated:
        message = message[: cap - 1] + "…"
    return message, truncated


def derive_agent_status(state: str, current_tool: str | None, tool_started_at: _dt.datetime | None,
                        last_contact_at: _dt.datetime, now: _dt.datetime, *, idle_minutes: int,
                        dead_minutes: int, tool_timeout_minutes: int) -> str:
    """The derived status of an agent, first matching rule wins:

      1. state in (completed, left, dead)                          -> state
      2. current_tool set and tool_started_at > now - tool_timeout  -> running
      3. last_contact_at < now - dead_minutes                       -> dead
      4. last_contact_at < now - idle_minutes                       -> idle
      5. otherwise                                                  -> state (started/running)

    Idle and dead are derived rather than stored because an agent that stopped talking cannot
    report it. Comparisons are strict, exactly as in the Postgres view.

    Where it is computed: this function is the specification, and backends without a query
    language (memory) call it. The Postgres backend reads the `agent_status`/`job_status` views
    instead, which encode the same CASE, because (a) people query those views directly and they
    must agree with the CLI, and (b) the views bake the thresholds in at `setup` time, so the
    CLI keeps showing exactly what SQL users see (a behaviour we must not change). The tests
    hold the two to the same table of cases."""
    if state in ("completed", "left", "dead"):
        return state
    if current_tool is not None and tool_started_at is not None and \
            tool_started_at > now - _dt.timedelta(minutes=tool_timeout_minutes):
        return "running"
    if last_contact_at < now - _dt.timedelta(minutes=dead_minutes):
        return "dead"
    if last_contact_at < now - _dt.timedelta(minutes=idle_minutes):
        return "idle"
    return state


def load_name_pool(data_dir: Path = DATA_DIR) -> dict[str, list[str]]:
    """The shipped name lists, {source: [names]} in NAME_SOURCES order (data/<source>_names.json).
    The CLI passes this to `Board.setup`; backends also use the "english" list for the
    last-resort "<name> <NNN>" fallback in allocate_name."""
    return {s: json.loads((data_dir / f"{s}_names.json").read_text(encoding="utf-8")) for s in NAME_SOURCES}


# --------------------------------------------------------------------------- the board

# Every Board method that can change the store: a read-only board (`open_read_only`) refuses
# each with ReadOnlyBoard before it runs (the backend refuses any write anyway: the file backend
# checks at the end of each transaction, SQLite's connection is mode=ro). read_unread/read_new
# are writes only when they advance the cursor.
WRITE_METHODS = (
    "purge", "ensure_job", "open_job", "close_job", "auto_close_job", "undo_auto_close",
    "sweep_auto_close", "bind_job_session", "allocate_name", "claim_judge", "claim_verifier",
    "set_waiting", "set_job_max_hours", "set_job_goal", "move_agent", "sweep_expiry", "reserve_spawn", "record_verdict", "tool_started", "record_route",
    "claim_route", "tool_finished", "agent_stopped", "set_agent_role", "set_agent_runtime",
    "agent_turn_ended",
    "turns_resumed", "finish_quiet_agents", "leave", "close_agent", "claim_resume",
    "set_job_supervise", "record_restart", "set_restart_agent", "finish_restart",
    "record_roster_sync", "record_memory_recall", "record_remembered", "record_nudge",
    "record_silence_nudge", "record_reply_reminder", "post", "read_unread", "read_new",
    "save_transcript", "refresh_transcript", "mark_capture_failed", "rotate_transcripts",
    "save_memory_ref", "mark_memory_refs_checked", "delete_memory_refs",
    "pause_job", "begin_resume", "record_resume_outcome",
)
_CURSOR_READS = {"read_unread": 3, "read_new": 3}   # method -> index of `advance` in *args


def refuse_writes(board: "Board", what: str) -> None:
    """Make `board` read-only: every WRITE_METHODS method raises ReadOnlyBoard naming `what`."""
    def refuse(name):
        real = getattr(board, name)

        def method(*args, **kwargs):
            if name in _CURSOR_READS:
                i = _CURSOR_READS[name]
                advance = kwargs.get("advance", args[i] if len(args) > i else True)
                if not advance:
                    return real(*args, **kwargs)
            raise ReadOnlyBoard(f"{what} is open read-only: {name} refused")
        method.__name__ = name
        return method
    board.read_only = True
    for name in WRITE_METHODS:
        setattr(board, name, refuse(name))


class Board(abc.ABC):
    """A connection to the message board. Use as a context manager, or call close().

    Construction connects eagerly and raises BoardUnavailable if storage is unreachable."""

    # True for a board opened by `open_read_only` on a backend that supports it (file, SQLite):
    # it creates, writes, truncates and renames nothing, and every write raises ReadOnlyBoard.
    read_only = False
    # Set (the server's name) when the primary was unreachable and a read-only command was
    # served by a standby instead (Postgres with several hosts): writes are not available.
    degraded: str | None = None

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.board_cfg = cfg["board"]

    # ---- lifecycle -------------------------------------------------------------

    @classmethod
    @abc.abstractmethod
    def setup(cls, cfg: dict, names: Mapping[str, Sequence[str]]) -> SetupResult:
        """Create or upgrade storage in place; idempotent (`swarm init`). Classmethod because the
        storage may not exist yet, so no Board can be opened.

        Creates the store if missing (Postgres: the database, UTF8 from template0, via
        admin_dbname), then the schema, triggers and status views (thresholds from cfg["board"]
        baked in), then adds every name in `names` ({source: [name]}) to the pool, skipping ones
        already present. Existing data is never dropped. Raises IncompatibleStorage if the store
        exists but cannot be used, BoardUnavailable if unreachable. Safe to run concurrently
        with normal board use."""

    @classmethod
    def schema_version(cls, cfg: dict) -> int | None:
        """The schema version the store records (see SCHEMA_VERSION): None when the store or
        its schema doesn't exist yet, 0 when it has a schema from before versions were recorded.
        Opens no Board (the schema may be missing). Raises BoardUnavailable if unreachable.
        Not abstract on purpose: a backend lacking it fails ensure_initialized (logged), not
        every Board construction."""
        raise NotImplementedError(f"{cls.__name__} does not report its schema version")

    @classmethod
    def store_missing(cls, cfg: dict, cheap: bool = True) -> bool:
        """Whether the store itself is gone (dropped database, deleted file or directory), so a
        stamp saying it was set up is stale. cheap=True: local checks only (a stat), for every
        open; cheap=False may ask the server, and is only called after opening failed. False
        when it can't tell (e.g. the server is unreachable): never re-initialise on doubt."""
        return False

    @classmethod
    def identity(cls, cfg: dict) -> str | None:
        """A stable string naming this store on this machine (for the "already initialised"
        stamp file), or None when the store doesn't outlive the process (no stamp)."""
        return None

    @abc.abstractmethod
    def close(self) -> None:
        """Release the connection. Idempotent; the Board is unusable afterwards."""

    def __enter__(self) -> "Board":
        return self

    @contextlib.contextmanager
    def op_timeout(self, seconds: float):
        """Within the block, a call that would wait on the store (a lock, a busy database, a
        server reply) for longer than `seconds` fails instead (BoardUnavailable or BoardError);
        used by the hooks to keep spool deliveries within their budget. Default: nothing to
        bound (an in-process store never waits)."""
        yield

    def __exit__(self, *exc) -> None:
        self.close()

    @abc.abstractmethod
    def now(self) -> _dt.datetime:
        """The board's current time (tz-aware). All stored timestamps come from this clock."""

    # ---- retention ---------------------------------------------------------------

    @abc.abstractmethod
    def purge(self) -> None:
        """Apply retention (R = retention_days, S = agent_stale_hours), in this order:

          1. delete messages created before now - R;
          2. active agents with last_seen before now - S: left_at = now, state = dead,
             current_tool = None (tool_started_at untouched; the name becomes free);
          3. delete agent rows with left_at before now - R;
          4. delete jobs whose coalesce(finished_at, activated_at, created_at) is before now - R
             and that have no messages and no active agents (their departed agents go too);
          5. delete routes recorded (record_route) before now - R;
          6. delete restarts with at before now - R.

        Each step is independently atomic; concurrent purges are harmless. Memory refs are
        never touched (retention follows the memory: provenance.prune)."""

    # ---- jobs --------------------------------------------------------------------

    @abc.abstractmethod
    def ensure_job(self, job: str, description: str | None = None,
                   created_by: str | None = None) -> None:
        """Create the job if missing (status active, created_at now, activated_at None). If it
        exists, only replace description when a non-None one is given; nothing else changes.
        Atomic upsert: concurrent calls for one job never fail."""

    @abc.abstractmethod
    def open_job(self, job: str, description: str | None, task: str | None,
                 session_id: str | None, created_by: str | None, project: str | None = None,
                 goal: str | None = None) -> None:
        """Activate or re-open a job (`swarm activate`). Atomic upsert. New job: all fields as
        given, status active, activated_at now. Existing job: status active, activated_at now,
        finished_at, outcome and closed_by cleared, and so are the verdict fields (verdict, verdict_reason,
        verdict_next, verdict_by, verdict_at: a new run is judged afresh) and completion_forced (False);
        description/task/session_id/project/goal replaced only when the new value is not None;
        created_by kept."""

    @abc.abstractmethod
    def close_job(self, job: str, status: str, outcome: str | None, forced: bool = False,
                  closed_by: str | None = None) -> bool:
        """Close a job (`swarm deactivate`). status is one of CLOSED_JOB_STATUSES.

        First every active agent of the job leaves (left_at now, state left, current_tool and
        tool_started_at None): with the marker gone the hooks stop tracking them, so they would
        otherwise drift to idle and dead. Then, if the job exists: status set, outcome replaced
        only if not None, finished_at now unless the job is already closed (then kept: closing
        a closed job again, e.g. to replace the auto-close outcome with a real summary, does not
        move when it ended), completion_forced = forced (the CLI passes True for a completion
        without a met verdict), closed_by as given. Returns whether the job existed."""

    @abc.abstractmethod
    def auto_close_job(self, job: str, before: _dt.datetime, outcome: str) -> _dt.datetime | None:
        """Close `job` for the auto-close sweep, atomically and only if ALL of this holds at the
        moment of the close (a compare-and-set: of concurrent calls at most one closes it):

          1. status active, waiting_on None, and no goal or its verdict is met;
          2. the run started (coalesce(activated_at, created_at)) before `before`;
          3. an agent of this run (left_at None or >= that start) has state completed or left,
             and no agent of the job has derived status started, running or idle
             (AUTO_CLOSE_BLOCKING; dead ones don't count);
          4. nothing happened at or after `before`: no message of the job created, no agent
             of the job with joined_at, last_seen or left_at >= before.

        Closing is close_job(job, "completed", outcome, closed_by=AUTO_CLOSED_BY): the agents
        still active (dead ones, by 3) leave, completion_forced False. Returns the finished_at this
        call wrote (the close's own timestamp, read back in the same statement or transaction:
        what undo_auto_close is given), or None if it didn't close the job."""

    @abc.abstractmethod
    def undo_auto_close(self, job: str, closed_at: _dt.datetime) -> bool:
        """Revert the auto-close sweep's own close of `job`, atomically and only if the job is
        still exactly as that close left it: status completed, closed_by AUTO_CLOSED_BY and
        finished_at == closed_at (so a later deactivate, or a re-activation and another close,
        is never undone). Then status active, finished_at, outcome and closed_by None; the run
        (activated_at), the agents and the verdict are kept, so the job auto-closes again, as
        the same run, once it is quiet. Returns whether it reverted."""

    def sweep_auto_close(self, minutes: float, watch=None) -> list[AutoClosed]:
        """Close every open job whose agents are all done and that has been quiet for `minutes`
        (`[job] auto_close_minutes`; 0 or less: nothing). Returns what it closed.

        `watch(job)`, if given, is this machine's view of the job's orchestrating session
        (cli.OrchestratorWatch), taken right before the close: its active() true keeps the job
        open; right after the close, after_close(undo) calls undo() if the session was at work
        meanwhile: undo_auto_close reverts this close (same run, agents and verdict), and the
        job is not reported closed.

        Template method, not overridden: candidates come from the rollup (auto_close_candidate),
        each one's run (run_agents) must have a completed or left agent and none started,
        running or idle; the outcome is auto_close_outcome over that run and the job's newest
        message; auto_close_job then re-checks everything atomically against the same
        `before` (now - minutes), so an agent joining or a post landing meanwhile keeps the job
        open, and two sweeps at once close it once. Idempotent; costs one jobs() query when
        nothing qualifies."""
        if not minutes or minutes <= 0:
            return []
        before = self.now() - _dt.timedelta(minutes=minutes)
        closed = []
        for js in self.jobs(False):
            if not auto_close_candidate(js, before):
                continue
            run = run_agents(self.agents(js.job), run_start(js))
            if not any(a.status in ("completed", "left") for a in run) or \
                    any(a.status in AUTO_CLOSE_BLOCKING for a in run):
                continue
            last = self.recent_messages(1, job=js.job)
            outcome = auto_close_outcome(run, last[-1] if last else None)
            seen = watch(js.job) if watch else None
            if seen is not None and seen.active():
                continue
            at = self.auto_close_job(js.job, before, outcome)
            if at is not None:
                if seen is not None and seen.after_close(
                        lambda job=js.job, at=at: self.undo_auto_close(job, at)):
                    continue   # the orchestrator was at work during the close: reverted
                closed.append(AutoClosed(js.job, outcome))
        return closed

    def progress_at(self, js: JobStatus) -> _dt.datetime:
        """When the job last made progress: the newest of its run start, its latest verdict, an
        agent joining, and a board message posted by an agent (not the system's own, "swarm").
        Tool calls and hook heartbeats (last_seen) are not progress: an agent polling in a loop
        for hours is not."""
        times = [run_start(js)] + [a.joined_at for a in self.agents(js.job)]
        if js.verdict_at:
            times.append(js.verdict_at)
        times += [m.created_at for m in self.recent_messages(20, job=js.job) if m.agent_name != "swarm"]
        return max(t for t in times if t)

    def sweep_expiry(self, stall_hours: float, orphan_minutes: float, watch=None) -> list[AutoClosed]:
        """Close the open jobs that outlived their welcome (`[job] stall_hours`, `orphan_minutes`;
        0 or less turns a rule off). Returns what it closed. Template method, not overridden.

        A bounded wait (`wait --for`) past its time is cleared first, so the job is judged as
        not waiting. A job that keeps making progress (progress_at) is never stalled, however long
        it runs. Then, per open job:
          * no progress for its limit (jobs.max_hours, else `stall_hours`; see progress_at):
            closed "failed", outcome "auto-closed: no progress for N h" plus its last verdict;
          * no agent started, running or idle (dead ones don't count; none at all is fine), no
            board activity (last_activity_at, run start, an expired wait's end) for
            `orphan_minutes`, not inside an unexpired bounded wait, and `watch(job).active()`
            (the orchestrating session, cli.OrchestratorWatch) false: closed "cancelled",
            outcome "auto-closed: no live agents for N min".
        Both are close_job(..., closed_by=AUTO_CLOSED_BY): the remaining agents leave, and the
        job shows as auto-closed. The job is read again right before the close, so an agent that
        joined meanwhile keeps it open. Idempotent; costs one jobs() query when nothing qualifies."""
        now = self.now()
        closed = []
        for js in self.jobs(False):
            seen_activity = js.last_activity_at
            if js.waiting_on and js.waiting_until is not None and js.waiting_until <= now:
                self.set_waiting(js.job, None)   # the bounded wait ran out
                js = replace(js, waiting_on=None, waiting_since=None,
                             last_activity_at=max(t for t in (js.last_activity_at, js.waiting_until) if t))
            cap = stall_hours if js.max_hours is None else js.max_hours
            if cap and cap > 0 and now - run_start(js) >= _dt.timedelta(hours=cap) and \
                    now - self.progress_at(js) >= _dt.timedelta(hours=cap):
                status = "failed"
                outcome = f"auto-closed: no progress for {cap:g} h"
                if js.verdict:
                    outcome += f"; last verdict {js.verdict}" + (f": {js.verdict_reason}" if js.verdict_reason else "")
                outcome = outcome[:AUTO_CLOSE_OUTCOME_MAX]
            elif orphan_minutes and orphan_minutes > 0 and not (js.started or js.running or js.idle) \
                    and not (js.waiting_on and js.waiting_until is not None) \
                    and now - (js.last_activity_at or run_start(js)) >= _dt.timedelta(minutes=orphan_minutes) \
                    and now - run_start(js) >= _dt.timedelta(minutes=orphan_minutes):
                if watch is not None and watch(js.job).active():
                    continue
                status = "cancelled"
                outcome = f"auto-closed: no live agents for {orphan_minutes:g} min"
            else:
                continue
            now_js = self.job_status(js.job)   # nothing changed since the rollup?
            if now_js is None or now_js.status != "active" or (status == "cancelled" and (
                    now_js.started or now_js.running or now_js.idle or now_js.last_activity_at != seen_activity)):
                continue
            self.close_job(js.job, status, outcome, closed_by=AUTO_CLOSED_BY)
            closed.append(AutoClosed(js.job, outcome))
        return closed

    @abc.abstractmethod
    def bind_job_session(self, job: str, session_id: str) -> None:
        """Set the job's session_id only if it is currently None (first spawning session wins,
        atomically). No-op if the job does not exist or is already bound."""

    # ---- agents --------------------------------------------------------------------

    def allocate_name(self, agent_key: str, job: str, role: str | None = None) -> str:
        """Join: `_allocate_name`, refused with JobPaused while the job is paused (nobody joins,
        or is revived into, a paused job; `swarm resume` re-enrols the paused agents itself, through
        claim_resume). The check and the join are two steps: an agent racing a pause can end up
        active on the paused job; its next hook turn is denied by the pause notice."""
        self.require_unpaused(job)
        return self._allocate_name(agent_key, job, role)

    def job_state(self, job: str) -> str | None:
        """The job's stored status (JOB_STATUSES), None if there is no such job. One cheap lookup
        (every join and post asks): the backends override this default."""
        js = self.job_status(job)
        return js.status if js else None

    def require_unpaused(self, job: str) -> None:
        """Raise JobPaused (with who/when/why) if the job is paused."""
        if self.job_state(job) == "paused":
            rec = self.open_pause(job)
            raise JobPaused(job, rec.paused_at if rec else None, rec.paused_by if rec else None,
                            rec.reason if rec else None)

    @abc.abstractmethod
    def _allocate_name(self, agent_key: str, job: str, role: str | None = None) -> str:
        """Give `agent_key` a name on `job` and return it. Idempotent per agent_key.

        Runs purge() first, then ensure_job(job). Then:
          * key is active: last_seen = now, job = `job` (moves it; a move to another job
            drops its judge seat), return its current name (role and everything else unchanged);
          * key departed with left_reason STUCK_PREFIX..., or named by a restart row
            (was_replaced): the supervisor closed it, so it stays departed (never revived,
            never reset): return its name, change nothing;
          * key exists but departed (e.g. resumed through SendMessage): revive it with the SAME
            name if no active agent holds that name now: left_at None, state started, job =
            `job`, last_seen now, current_tool/tool_started_at None, not the judge (it may
            claim_judge again); its read cursor,
            tool_calls, joined_at, role and host are KEPT; left_reason None (resume_of kept);
          * otherwise pick a free name: shuffle the free names of each source in NAME_SOURCES
            order and take the first one that can be claimed, "simpsons" exhausted before
            "english"; if none is free, "<random english name> <random 100..999>". The row
            is created or fully reset: name, job, role as given, host = this machine
            (compat.node()), joined_at = last_seen = now, left_at None, state started,
            not the judge, left_reason and resume_of None,
            tool_calls 0, current_tool/tool_started_at/last_post_at None, sync state empty
            (SyncState defaults: calls_at_post 0, reply_reminded_id 0, timestamps None). The read cursor is set so that exactly the job's newest
            `join_history` messages (all of them if fewer) are unread: a newcomer's first
            read is the job's recent history. join_history 0 means none.

        Guarantee: names are unique among active agents even under concurrent allocation from
        many processes (Postgres: partial unique index; a lost race moves on to the next free
        name). Never returns a name another active agent holds."""

    @abc.abstractmethod
    def claim_judge(self, agent_key: str, job: str) -> bool:
        """Make the ACTIVE agent with this key, on `job`, the job's judge, unless another active
        agent of the job is. Returns whether it is the judge now (True again for the judge
        itself). At most one active judge per job, even under concurrent claims (Postgres:
        partial unique index). A judge's derived role (roster, agents()) is "judge"; the seat
        frees when it departs."""

    @abc.abstractmethod
    def _move_agent_row(self, agent_key: str, job: str, keep: int) -> str | None:
        """One transaction of move_agent: the ACTIVE agent with this key goes to `job`, which must
        be an open job (status active). Returns the job it was on, or None if nothing was done
        (the key is not active, or `job` is missing or closed). Already on `job`: returns it and
        changes nothing. Otherwise: job = `job`; the judge and verifier seats are dropped (a
        seat belongs to one job); last_seen = now; the read cursor is set so exactly the newest
        `keep` messages of `job` are unread (a join's catch-up, delivered once; the old job's
        cursor would hide or flood); reply_reminded_id = the highest message id (owed replies of
        the old job are not nagged about), calls_at_post = tool_calls, silence_nudged_at None;
        roster_seen = MOVED_PREFIX + the old job and roster_synced_at None (the next turn shows
        a moved notice and the full roster). Name, role, host, tool_calls, joined_at, state and
        the current tool are kept: a running agent is not restarted or reset."""

    def move_agent(self, agent_key: str, job: str) -> str | None:
        """Move a live agent to another open job (`swarm move`, `swarm job merge`) without
        stopping it: its next hook call resolves the job from its row. See _move_agent_row for
        the row and the return value. A recorded route follows it (state final, job `job`), so
        a still-unverified route can't send it back to the job its spawn prompt names."""
        old = self._move_agent_row(agent_key, job, max(0, int(self.board_cfg.get("join_history", 30))))
        if old is not None and old != job:
            r = self.route(agent_key)
            if r.state is not None:
                self.record_route(agent_key, r.session_id, "final", job)
        return old

    @abc.abstractmethod
    def set_job_goal(self, job: str, goal: str) -> bool:
        """Set or replace the goal of an OPEN job (status active); False if the job is missing or
        closed. A goal different from the current one starts the judging afresh: verdict,
        verdict_reason, verdict_next, verdict_by and verdict_at are cleared (a met verdict
        covered the old goal). The same goal changes nothing. Judge seats are not touched."""

    @abc.abstractmethod
    def claim_verifier(self, agent_key: str, job: str) -> bool:
        """Mark the ACTIVE agent with this key, on `job`, as a verifier (any number per job;
        not its judge). Returns whether it is one now. Its derived role (roster, agents()) is
        "verifier"; the mark is dropped when the key claims a new name or moves job."""

    @abc.abstractmethod
    def verification_counts(self, job: str) -> tuple[int, int]:
        """(verified, failed): the job's messages starting "VERIFIED" / "FAILED", posted under
        the name of one of the job's verifiers (active or departed)."""

    @abc.abstractmethod
    def set_waiting(self, job: str, on: str | None, until: _dt.datetime | None = None) -> bool:
        """Record what the OPEN job is waiting for (waiting_on = on, waiting_since = now,
        waiting_until = until: when a bounded wait expires, None = unbounded), or with on=None
        clear all three (it is working again). False if the job is missing or closed.
        Setting a new reason restarts waiting_since; open_job and close_job clear all of them."""

    @abc.abstractmethod
    def set_job_max_hours(self, job: str, hours: float | None) -> bool:
        """jobs.max_hours = hours (this job's own stall limit; 0 = never, None = the [job]
        default). False if the job doesn't exist. open_job resets it to None."""

    @abc.abstractmethod
    def reserve_spawn(self, agent_key: str, job: str, per_agent: int, per_job: int) -> SpawnGrant:
        """Count one subagent spawn by the ACTIVE agent `agent_key` on `job`, unless it has
        already spawned per_agent, or the job's agents together per_job (then nothing changes).
        Atomic under concurrent calls (Postgres: the job row is locked). The job's count lives
        on the job, so agents purged or moved later don't give spawns back; open_job resets it
        (a new run of the job starts from zero)."""

    @abc.abstractmethod
    def record_verdict(self, job: str, judge_name: str, verdict: str, reason: str,
                       next_steps: str | None = None) -> bool:
        """The judge's verdict on the job's goal: only if `judge_name` is the job's ACTIVE judge
        (else False, nothing stored). Sets verdict (one of VERDICTS, anything else raises),
        verdict_reason, verdict_next = next_steps (None for met), verdict_by = judge_name, verdict_at = now; a later verdict replaces it.
        A judge_name that fails valid_name raises ValueError. Returns True if stored. Posting it on the board is the caller's business."""

    @abc.abstractmethod
    def active_agent_name(self, agent_key: str) -> str | None:
        """The name of the ACTIVE agent with this key, or None (unknown or departed)."""

    @abc.abstractmethod
    def was_member(self, agent_key: str, job: str) -> bool:
        """Whether an agent row with this key and job exists, active or departed. The hooks
        use it to let a resumed departed member back in without --adopt-running."""

    @abc.abstractmethod
    def tool_started(self, agent_key: str, tool_name: str | None) -> Member | None:
        """PreToolUse bookkeeping for an ACTIVE agent (no-op otherwise): state running,
        current_tool = (tool_name or "?")[:TOOL_NAME_MAX], tool_started_at = now, turn_ended_at None,
        tool_calls += 1 (atomic increment), last_seen = now. Returns Member(name, job of the
        row, verify_tag = its recorded route's state is "unverified"), or None if no active
        agent has this key: everything the hook needs, in one round trip."""

    # ---- routes (which of a session's jobs a subagent belongs to) ----------------------

    @abc.abstractmethod
    def record_route(self, agent_key: str, session_id: str | None, state: str,
                     job: str | None = None) -> None:
        """Record (replace) the agent's route: state one of ROUTE_STATES (anything else
        raises), job, session_id, recorded at now. Atomic upsert per agent_key. Routes are
        independent of agent rows and jobs (no reference checks) and are purged after
        retention_days."""

    @abc.abstractmethod
    def claim_route(self, agent_key: str, session_id: str | None, from_state: str | None,
                    state: str, job: str | None = None) -> bool:
        """Compare-and-set record_route: record the route only if the agent's recorded state is
        `from_state` right now (None: no route recorded). Returns whether this call recorded
        it; of several concurrent claims from the same state exactly one wins. A state not in
        ROUTE_STATES raises."""

    @abc.abstractmethod
    def route(self, agent_key: str) -> Route:
        """The agent's Route: the recorded state/job/session_id (all None if none) and the
        job of its agent row, active or departed (member_job, None if no row). One lookup."""

    @abc.abstractmethod
    def tool_finished(self, agent_key: str) -> None:
        """PostToolUse bookkeeping for an ACTIVE agent (no-op otherwise): current_tool and
        tool_started_at None, last_seen = now. State is left as is."""

    @abc.abstractmethod
    def agent_stopped(self, agent_key: str) -> None:
        """SubagentStop for an ACTIVE agent (no-op otherwise): left_at = now, state completed,
        current_tool and tool_started_at None. The name becomes free."""

    @abc.abstractmethod
    def set_agent_role(self, agent_key: str, role: str) -> None:
        """Set an ACTIVE agent's custom role label, preserving its identity and all state.
        No-op for unknown or departed keys. ValueError for an invalid role identifier or
        judge/verifier: those require claim_judge/claim_verifier. Existing judge/verifier
        flags are unchanged and still override this label in the roster."""

    @abc.abstractmethod
    def set_agent_runtime(self, agent_key: str, harness: str | None, model: str | None) -> None:
        """For the ACTIVE agent with this key, set harness and model to each value that is not
        None (the other stays). No-op for an unknown or departed key."""

    @abc.abstractmethod
    def agent_turn_ended(self, agent_key: str) -> None:
        """A host whose SubagentStop means "this turn ended" (Codex): for the ACTIVE agent,
        turn_ended_at = now, current_tool and tool_started_at None, last_seen = now. It stays
        active; tool_started clears turn_ended_at (another turn came)."""

    @abc.abstractmethod
    def turns_resumed(self, job: str) -> list[str]:
        """Codex follow-up (a new turn sent to a child, which fires no child hook until its
        next tool call): every ACTIVE agent of `job` whose turn_ended_at is set gets
        turn_ended_at = now and last_seen = now, i.e. its quiet window restarts. The child that
        got the turn clears it at its next tool call; a sibling that gets no further turn still
        completes one window later. Agents without a turn end are untouched. Returns their keys."""

    @abc.abstractmethod
    def finish_quiet_agents(self, quiet_seconds: float) -> list[str]:
        """Every ACTIVE agent whose turn_ended_at is set and more than quiet_seconds ago stops
        exactly as agent_stopped does. Returns their keys (each at most once under concurrent
        calls: the update's own WHERE decides)."""

    @abc.abstractmethod
    def leave(self, agent_key: str | None = None, name: str | None = None) -> bool:
        """`swarm leave`: the ACTIVE agent with this key (preferred when given) or else this
        name leaves: left_at = now, state left, current_tool None (tool_started_at untouched).
        Returns whether an active agent matched."""

    @abc.abstractmethod
    def close_agent(self, agent_key: str, reason: str, seen_before: _dt.datetime | None = None) -> bool:
        """Close an ACTIVE agent for the supervisor: left_at = now, state left, left_reason =
        reason, current_tool and tool_started_at None (the name becomes free). With seen_before,
        only if its last_seen is still <= seen_before (the value the detection read: any hook
        contact since keeps it). Returns whether it closed it. Atomic: one UPDATE ... WHERE."""

    @abc.abstractmethod
    def claim_resume(self, agent_key: str, resume_of: str, job: str) -> str | None:
        """Enrol a supervisor replacement under its predecessor's name. Only if `resume_of` is a
        DEPARTED row of `job` and no active agent holds its name: the row `agent_key` is created
        (or fully reset) with that name and role, resume_of = resume_of, host and os_user of
        this process, joined_at = last_seen = now, state started, the read cursor of
        `resume_of` (it sees what its predecessor hadn't read), the judge mark if the
        predecessor was the judge and no other active agent of the job is, the verifier mark
        if it was a verifier. Returns the name; None (nothing changed) otherwise. Idempotent:
        an agent_key already active on `job` returns its name. Atomic against concurrent
        allocate_name/claim_resume (Postgres: the partial unique index on active names)."""

    # ---- pause / resume (schema 12; swarm.pause is the only caller) ------------------

    @abc.abstractmethod
    def pause_job(self, job: str, by: str | None, reason: str | None,
                  cwds: Mapping[str, str] | None = None) -> PauseRecord | None:
        """Pause an ACTIVE job, atomically: jobs.status = 'paused'; the manifest (MANIFEST_VERSION,
        see PauseRecord) is built from the job and its active agents; every active agent is
        closed (left_at now, state left, left_reason LEFT_PAUSED, current tool cleared: its name
        is free but only claim_resume can take it, joins are refused); a job_pauses row is
        inserted (paused_at now, paused_by = by, reason) and returned. The job's waiting state is
        kept. An already paused job: returns its open PauseRecord, changes nothing. None if the
        job does not exist or is closed (completed, cancelled, failed). `cwds` maps agent_key to
        the agent's working directory where the caller knows it (manifest "cwd", else None).
        A manifest agent's `kind` is "orchestrator" when its transcript row is the orchestrator's,
        else "subagent" (build_manifest does the shaping for every backend)."""

    @abc.abstractmethod
    def open_pause(self, job: str) -> PauseRecord | None:
        """The pause that has not been resumed (resumed_at None) of a paused job; else None."""

    @abc.abstractmethod
    def pauses(self, job: str) -> list[PauseRecord]:
        """Every pause of the job, oldest first."""

    @abc.abstractmethod
    def begin_resume(self, job: str, pause_id: int, by: str | None, host: str | None) -> bool:
        """Atomically reopen: only if the job is 'paused' and pause `pause_id` is its open pause:
        jobs.status = 'active', activated_at kept, the pause row gets resumed_at now, resumed_by,
        resumed_host. Goal, verdict, waiting_on and the rest of the job are untouched. False (and
        nothing changed) otherwise, e.g. a second resume at the same time."""

    @abc.abstractmethod
    def record_resume_outcome(self, pause_id: int, outcome: dict) -> bool:
        """Store `outcome` (JSON-able: {agent_key: {"status": ..., ...}}) on the pause row,
        replacing the previous one. False if there is no such row."""

    @abc.abstractmethod
    def set_job_supervise(self, job: str, on: bool) -> bool:
        """jobs.supervise = on (the per-job kill switch). False if the job doesn't exist."""

    # ---- supervisor restarts ------------------------------------------------------------

    @abc.abstractmethod
    def record_restart(self, job: str, agent_key: str, old_agent_key: str, reason: str, harness: str,
                       minutes_cap: float, outcome: str | None = None,
                       max_per_job: int | None = None, max_job_minutes: float | None = None,
                       max_host_running: int | None = None, max_host_minutes: float | None = None,
                       day_start: _dt.datetime | None = None) -> "Restart | None":
        """Record a replacement for the closed agent old_agent_key (lineage root agent_key):
        attempt = 1 + the rows of (job, agent_key); at = now; host and os_user of this process.
        outcome given (only "refused") records it ended at once. At most one row per
        old_agent_key ever: a second call returns None and changes nothing (two supervisors
        at once start one replacement). With max_per_job, also None when the job already has
        that many rows (any lineage, host or user), counted atomically with the insert, so
        supervisors on several hosts can't together exceed [supervise] max_restarts_per_job.
        For a started one (no outcome), also None when it would break a minute or concurrency
        cap (restart_over_limits), checked with the insert under the same lock: the job's
        reserved minutes plus minutes_cap over max_job_minutes; this host's open rows (every OS
        user's) at max_host_running; this host's rows overlapping day_start plus minutes_cap over
        max_host_minutes. Returns the new row."""

    @abc.abstractmethod
    def set_restart_agent(self, restart_id: int, new_agent_key: str) -> bool:
        """The replacement's agent key, once known (Codex: its thread id). False if no such row."""

    @abc.abstractmethod
    def finish_restart(self, restart_id: int, outcome: str) -> bool:
        """ended_at = now, outcome = outcome (RESTART_OUTCOMES, anything else raises ValueError),
        only if the row hasn't ended yet. Returns whether this call finished it."""

    @abc.abstractmethod
    def was_replaced(self, agent_key: str) -> bool:
        """Whether a restart row names this key as old_agent_key (the supervisor replaced it, or
        refused to): such a key never comes back. One lookup on the unique old_agent_key."""

    @abc.abstractmethod
    def restarts(self, job: str | None = None, agent_key: str | None = None, host: str | None = None,
                 os_user: str | None = None, since: _dt.datetime | None = None) -> list["Restart"]:
        """Rows matching every filter given (since: at >= since), by id."""

    @abc.abstractmethod
    def agents(self, job: str, include_departed: bool = True) -> list[AgentStatus]:
        """Agents of a job with derived status. Order: active ones first, then by joined_at
        ascending. include_departed=False returns only active ones (`swarm who`)."""

    def roster(self, job: str) -> list[RosterEntry]:
        """Every agent of the job (active and departed) in agents() order. Backends may
        override it with something cheaper; the default derives it from agents()."""
        return [RosterEntry(a.agent_key, a.name, a.role, a.status, a.current_tool, a.ended_at is None)
                for a in self.agents(job)]

    # ---- per-agent sync state (what the hooks last told an agent) -------------------

    @abc.abstractmethod
    def sync_state(self, agent_key: str) -> SyncState | None:
        """The sync state of the agent row with this key (active or departed), None if none."""

    def turn_state(self, agent_key: str, job: str) -> tuple[list[RosterEntry], SyncState | None]:
        """(roster(job), sync_state(agent_key)): everything the PreToolUse hook needs after
        its read. Backends override it to fetch both in one round trip."""
        return self.roster(job), self.sync_state(agent_key)

    @abc.abstractmethod
    def record_roster_sync(self, agent_key: str, snapshot: str, full: bool) -> None:
        """The agent was shown the roster: roster_seen = snapshot; if `full` (the whole
        roster, not only a diff) also roster_synced_at = now. Any agent row with this key."""

    @abc.abstractmethod
    def record_memory_recall(self, agent_key: str, shown_ids: Sequence[str]) -> None:
        """A memory recall ran for the agent: memory_recalled_at = now; `shown_ids` are
        appended to memory_seen (ids already there are not duplicated), keeping the newest
        MEMORY_SEEN_MAX. Any agent row with this key."""

    @abc.abstractmethod
    def record_remembered(self, name: str) -> None:
        """The ACTIVE agent called `name` (if any) stored a memory: remembered_at = now."""

    @abc.abstractmethod
    def record_nudge(self, agent_key: str) -> None:
        """The agent was reminded to store what it learned: nudged_at = now."""

    @abc.abstractmethod
    def record_silence_nudge(self, agent_key: str) -> None:
        """The agent was nudged to post a status: silence_nudged_at = now."""

    @abc.abstractmethod
    def record_reply_reminder(self, agent_key: str, upto_id: int) -> None:
        """The agent was reminded of the replies it owes up to message `upto_id`:
        reply_reminded_id = max(reply_reminded_id, upto_id), so each is reminded once."""

    @abc.abstractmethod
    def agent_events(self, since: _dt.datetime, job: str | None = None) -> list[AgentEvent]:
        """Agent rows (of `job`, or all jobs) with joined_at > since or left_at > since,
        ordered by coalesce(left_at, joined_at) ascending. For `swarm tail`: the caller passes
        the board time of its previous poll."""

    # ---- messages ------------------------------------------------------------------

    def post(self, job: str, name: str, message: str, to: str | None = None,
             agent_key: str | None = None) -> PostResult:
        """Post `message` to `job` as `name` (optionally addressed `to` a name).

        Template method, not overridden: the text is normalised with normalize_message(cap =
        message_max_chars) here so every backend and every caller (CLI, spool flush) agrees,
        then stored by `_insert_message`. Raises ValueError if nothing is left after
        normalisation. `name` and `to` must pass valid_name (ValueError otherwise, nothing stored):
        a name is rendered into other agents' context. Not idempotent: posting twice stores two
        messages (exactly-once delivery of spooled posts is the spool's job, by claiming files)."""
        check_name(name, "agent name")
        if to is not None:
            check_name(to, "addressee name")
        text, truncated = normalize_message(message, int(self.board_cfg["message_max_chars"]))
        if not text:
            raise ValueError("empty message")
        if name != PAUSE_WRITER:
            self.require_unpaused(job)
        return PostResult(self._insert_message(job, name, text, to, agent_key), truncated)

    @abc.abstractmethod
    def _insert_message(self, job: str, name: str, text: str, to: str | None,
                        agent_key: str | None) -> int:
        """Store an already-normalised message and return its new id. Steps: ensure_job(job);
        insert (created_at now, host = compat.node(), agent_key as given); then the
        ACTIVE agent named `name`, if any (on any job), gets last_seen = last_post_at = now and
        calls_at_post = its tool_calls.
        The insert wakes wait_for_change() waiters."""

    def read_new(self, agent_key: str | None = None, name: str | None = None,
                 job: str | None = None, advance: bool = True) -> list[Message]:
        """read_unread(...).messages: kept for callers that don't care what is left."""
        return self.read_unread(agent_key, name, job, advance).messages

    @abc.abstractmethod
    def read_unread(self, agent_key: str | None = None, name: str | None = None,
                    job: str | None = None, advance: bool = True) -> ReadResult:
        """Messages new to an agent since its last read, oldest first, and how many are left.

        The reader is the ACTIVE agent with agent_key (if given) else with name; if there is
        none, return ReadResult([], 0) and change nothing. job defaults to the reader's own
        job. "Unread" = messages of that job with id > the reader's cursor and agent_name !=
        the reader's name. Returns the first read_limit of them by id, and `remaining` = how
        many more unread there are. Messages, remaining and the job's highest id all come
        from ONE consistent view of the board.

        advance=True then moves the cursor, never back: to the job's highest id in that view
        when nothing is left (so the reader's own posts are skipped for good), else to the id
        of the last message returned (the rest come on the next reads, nothing skipped); and
        sets last_seen = now. The move is a compare-and-set against the cursor the read
        started from: if a concurrent read of the same agent moved it first, this read changes
        nothing and returns ReadResult([], 0), so no message is delivered twice and none is
        lost. advance=False (`read --peek`) changes nothing. The cursor is one per agent row,
        not per job."""

    @abc.abstractmethod
    def recent_messages(self, limit: int, job: str | None = None,
                        active_jobs_only: bool = False) -> list[Message]:
        """The newest `limit` messages, returned OLDEST first. Scope: `job` if given; else all
        jobs, or only jobs whose status is active when active_jobs_only (`swarm watch`)."""

    @abc.abstractmethod
    def messages_after(self, after_id: int, job: str | None = None) -> list[Message]:
        """All messages with id > after_id (of `job`, or all jobs), ordered by id. Unbounded."""

    @abc.abstractmethod
    def last_message_id(self, job: str | None = None) -> int:
        """Highest message id (of `job`, or all jobs), 0 if there are none."""

    # ---- status ----------------------------------------------------------------------

    @abc.abstractmethod
    def job_status(self, job: str) -> JobStatus | None:
        """One job's rollup, or None if there is no such job (any status)."""

    @abc.abstractmethod
    def jobs(self, include_closed: bool = False) -> list[JobStatus]:
        """Job rollups: only status active unless include_closed. Order: active first, then
        coalesce(activated_at, created_at) ascending, then job name -- stable, so a live view
        does not reshuffle as jobs become active."""

    # ---- transcripts ([transcripts]; bin/transcripts.py does the capturing) ---------------

    @abc.abstractmethod
    def save_transcript(self, row: TranscriptRow) -> bool:
        """Upsert the transcript of (row.job, row.agent_key), one row per pair: every field
        replaced, stored_bytes = len(row.body), captured_at = row.captured_at or now. Skipped
        (returns False, nothing changes) when the stored row has the same sha256 and is final
        already or row is not final: an unchanged snapshot costs nothing, and a final capture
        of unchanged content still marks it final. Returns True when it wrote. Atomic per pair.
        A capture-failed row (row.failed) never replaces a final row that isn't one: it is only
        written over a missing, non-final or capture-failed row.

        Images: row.images (with data) are stored once per sha256 (an existing one is kept, with
        its first_seen), and the pair's image references are replaced by exactly these; an image
        no transcript and no memory ref refers to any more is deleted. Image writes are
        serialised with deletes, so an image is never deleted under a transcript that is being
        saved with it."""

    @abc.abstractmethod
    def refresh_transcript(self, job: str, agent_key: str, final: bool) -> bool:
        """A capture found the stored content unchanged (same sha256): captured_at = now and
        final = final OR the stored final (never downgraded); the body is not rewritten. A final
        refresh clears a capture-failed mark (the final capture found the kept snapshot's content).
        Returns whether the row exists."""

    @abc.abstractmethod
    def mark_capture_failed(self, job: str, agent_key: str, reason: str, marker: TranscriptRow) -> str:
        """The final capture of (job, agent_key) kept failing. A non-final row (a
        redacted snapshot) becomes final with failed = reason, everything else about it kept
        (body, sha256, sizes, captured_at, image references): "marked". With no row at all,
        `marker` (transcripts.capture_failed_row: bodiless) is stored: "stored". A final row
        (a real capture, or one already marked) is left as it is: "kept". Atomic per pair."""

    @abc.abstractmethod
    def pending_final_transcripts(self, host: str, os_user: str, harness: str,
                                  since: _dt.datetime) -> list[tuple[str, str]]:
        """(job, agent_key) of every ENDED agent row (left_at set, >= since) with this host,
        os_user and harness whose transcript row (same job and key) is missing or not final:
        the agents table left-joined to transcripts. Order: left_at ascending."""

    @abc.abstractmethod
    def transcripts(self, job: str | None = None, agent_name: str | None = None,
                    agent_key: str | None = None, role: str | None = None) -> list[TranscriptSummary]:
        """Stored transcripts (no bodies) matching every filter given, ordered by captured_at,
        then job, then agent_key."""

    @abc.abstractmethod
    def transcript_body(self, job: str, agent_key: str) -> bytes | None:
        """The stored transcript, decompressed (the redacted JSONL), or None if there is none or
        the row is a bodiless capture-failed marker (`bodiless`; a capture-failed row that kept
        its snapshot returns the snapshot). Through decompress_transcript:
        BoardError if the stored body is corrupt or over
        TRANSCRIPT_MAX_RAW uncompressed (a forged or damaged row)."""

    @abc.abstractmethod
    def transcript_image(self, sha256: str) -> TranscriptImage | None:
        """One stored image, with its data, or None. Includes the images only a memory ref
        refers to (transcript rotation never breaks an excerpt)."""

    @abc.abstractmethod
    def transcript_images(self, job: str | None = None,
                          agent_key: str | None = None) -> list[TranscriptImage]:
        """The stored images (no data) referred to by the transcripts matching the filters
        (none given: every stored image), each once, ordered by sha256."""

    def transcript_totals(self) -> TranscriptTotals:
        """Totals over every stored transcript, each image counted once. Default: from
        transcripts()."""
        return transcript_totals_of(self.transcripts())

    def rotate_transcripts(self, days: float, max_bytes: int, active_jobs: Sequence[str]) -> int:
        """Apply the transcript limits and return how many rows were deleted. Rows of
        `active_jobs` are never deleted. First every row captured more than `days` ago (0: no
        time limit); then, while the total stored_bytes exceeds `max_bytes` (0: no size limit),
        whole jobs, the one with the oldest newest-capture first. If the active jobs alone
        exceed the limit, it stops there (the caller warns). Template method over
        transcript_rotation_victims and _delete_transcripts."""
        now = self.now()
        old, jobs = transcript_rotation_victims(self.transcripts(), now, days, max_bytes, active_jobs)
        before = now - _dt.timedelta(days=days) if days and days > 0 else now
        return self._delete_transcripts(old, before, jobs) if old or jobs else 0

    @abc.abstractmethod
    def _delete_transcripts(self, rows: Sequence[tuple[str, str]], before: _dt.datetime,
                            jobs: Sequence[str]) -> int:
        """Delete the given (job, agent_key) rows that are still captured before `before` (one
        re-captured meanwhile stays) and every row of the given jobs, with their image
        references, then every image no transcript and no memory ref refers to any more; return
        how many transcripts went."""

    # ---- memory provenance (schema 8) ----------------------------------------------------

    def save_memory_ref(self, ref: MemoryRef) -> str:
        """Record where memory `ref.document_id` came from; returns "inserted", "updated" or
        "kept". Template method, not overridden: ValueError (nothing stored) for a writer not matching
        WRITER_NAME, an agent_name that fails valid_name (it is rendered to the operator and
        into other agents' context) or an image whose sha256 is not that of its data (check_images:
        the key names a file), an excerpt that fails check_excerpt (not lzma, over EXCERPT_MAX_RAW
        uncompressed, or raw_bytes not its uncompressed size) or images over
        check_memory_ref_images' caps; then _save_memory_ref stores it.

        The first writer wins: a row for the id with another agent_key is left as it is ("kept"),
        so a forged success line can't take over another agent's memory. The same agent_key
        replaces every field ("updated": checked_at becomes None, created_at ref.created_at or
        now, the excerpt and the image references exactly ref's). Images are stored once per
        sha256 in the transcript image store (an existing one kept, with its first_seen); an
        image no transcript and no memory ref refers to any more is deleted. Atomic, and
        serialised with the transcript image writes (an image is never deleted under a save)."""
        if not isinstance(ref.writer, str) or WRITER_NAME.fullmatch(ref.writer) is None:
            raise ValueError(f"unknown memory writer {ref.writer!r}")
        check_name(ref.agent_name, "agent name")
        check_excerpt(ref.excerpt, ref.raw_bytes)
        check_images(ref.images)
        check_memory_ref_images(ref.images)
        return self._save_memory_ref(ref)

    @abc.abstractmethod
    def _save_memory_ref(self, ref: MemoryRef) -> str:
        """save_memory_ref after its checks."""

    @abc.abstractmethod
    def memory_refs(self, job: str | None = None, agent_name: str | None = None,
                    agent_key: str | None = None, document_id: str | None = None) -> list[MemoryRef]:
        """The memory refs matching every filter given, ordered by created_at, then document_id.
        No excerpts (excerpt None, stored_bytes its stored size); images with metadata only,
        ordered by sha256."""

    @abc.abstractmethod
    def memory_ref_excerpt(self, document_id: str) -> bytes | None:
        """The ref's excerpt through decompress_capped (BoardError if over EXCERPT_MAX_RAW or
        corrupt); None when there is no such ref or it has no excerpt."""

    @abc.abstractmethod
    def mark_memory_refs_checked(self, document_ids: Sequence[str]) -> None:
        """checked_at = now for those of `document_ids` that exist (provenance.prune: Hindsight
        was asked about them and they stay)."""

    @abc.abstractmethod
    def delete_memory_refs(self, document_ids: Sequence[str],
                           expected: Mapping[str, tuple] | None = None) -> int:
        """Delete these refs with their image references, then every image no transcript and
        no memory ref refers to any more; return how many refs went. With `expected`
        ({document_id: (created_at, checked_at)}, as read by memory_refs), a ref goes only if
        it is in `expected` and both still match, checked in the same transaction as the
        delete: a row re-recorded or re-checked since it was read stays (provenance.prune)."""

    # ---- change notification -----------------------------------------------------------

    def subscribe(self, messages_only: bool = False) -> None:
        """Start listening for changes, before the caller's first read, so nothing between
        that read and the first wait_for_change() is missed. messages_only: only new messages
        count (tail); otherwise also any agent or job change (watch). Call at most once.
        Default: nothing (polling backends)."""

    def wait_for_change(self, timeout: float) -> bool:
        """Block until the board changed since the previous call (per subscribe()'s scope) or
        `timeout` seconds pass. Returns True if woken by a change. When it returns True it has
        consumed every change notification pending at that moment, so a burst counts once; a
        call with a tiny timeout therefore drains. It is a hint, not a guarantee: callers
        re-query after every return, and must refresh on timeout anyway (idle/dead derive from
        elapsed time). Default for backends without push: sleep `timeout`, return False."""
        time.sleep(timeout)
        return False
