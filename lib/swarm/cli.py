#!/usr/bin/env python3
"""swarm -- a message board for coordinating a swarm of agents (Postgres backend by default).

Every agent in a swarm gets a unique, human-readable name (Simpsons characters first,
random English first names once those run out), posts short unstructured messages
(<= message_max_chars) to the board for its job, and reads only the messages that are
new since its last read. Messages older than retention_days are purged.

Nothing in this file is environment-specific. Connection details, the database name and
all tunables come from a TOML config (default ~/.config/swarm/config.toml, override with
$SWARM_CONFIG); the password comes from an env file named in that config, or $PGPASSWORD.
Storage itself lives behind the `board` package (bin/board/): this file only parses
arguments, renders, and drives the board's primitives.

Subcommands (run with --help for details):
  init            create the storage (if missing), schema and name pool
  install-hooks   obsolete: the hooks come from the plugin (hooks/hooks.json)
  bootstrap       set the swarm up for this host (run by the plugin at session start)
  migrate         retire the old ~/.claude/skills/swarm install (its settings.json hooks, the dir)
  doctor          check this machine's setup (plugin, hooks, leftovers, venv, launcher, config, board)
  upgrade         upgrade the plugin for claude and/or codex, then bootstrap, migrate and doctor
  activate        open a job and switch the board on for subagents spawned from now on
  deactivate      switch the board off for a job and close it
  status          jobs overview, or one job's details and agents
  watch           live full-screen dashboard of jobs, agents and messages ([watch_database])
  tail            follow the board live ([watch_database])
  job             create a job or update its description
  join            allocate a unique name to an agent key for a job
  post            post a message as a named agent
  read            print messages new since this agent's last read (advances its cursor)
  who             list active agents on a job
  remember        store a durable fact in an existing memory bank (Hindsight; optional)
  learn           retain job learnings in an existing bank, or list available banks
  recall          query the configured general banks and any explicit project bank
  spool retry     requeue memories parked as .stuck after 24 hours of failing
  leave           release an agent's name
  purge           apply retention now
  transcript      list, show or export archived agent transcripts ([transcripts] enabled);
                  `transcript show --memory DOC_ID` works without them
  memory refs     memories swarm agents saved, and where they came from (provenance)
  hook            Claude Code hook entrypoint (reads hook JSON on stdin)
"""
from __future__ import annotations

# Module top stays stdlib-only: swarm_hooks imports this module on every tool call, so the
# `board` package and `spool` are imported inside the functions that use them.
import argparse
import contextlib
import json
import os
import re
import sys
import time
import tomllib
from pathlib import Path

from swarm.paths import PLUGIN_ROOT as SKILL_DIR  # noqa: E402  (the old name, kept for callers)
# Every board-derived string is shown through term_safe: a control character in a post,
# a name or a job becomes visible notation instead of terminal input or a forged line.
from swarm.textsafe import term_safe  # noqa: E402  (stdlib-only)
from swarm import compat
DEFAULT_CONFIG = Path(os.environ.get("SWARM_CONFIG", "~/.config/swarm/config.toml")).expanduser()

# The Postgres defaults before they became "swarm" / "swarm_board". A config that has a
# [database] section but leaves one of these keys out relied on the old default, and keeps it
# (see load_config); `swarm doctor` warns and asks for the key to be set explicitly.
LEGACY_DATABASE_DEFAULTS = {"user": "agent_board", "dbname": "agent-message-board"}

DEFAULTS = {
    "database": {"host": "localhost", "port": 5432, "user": "swarm",
                 "dbname": "swarm_board", "admin_dbname": "postgres",
                 "password_env_file": "", "connect_timeout": 5, "sslmode": "prefer",
                 # client-side deadline for every query (0 = none): a stalled one raises
                 # BoardUnavailable instead of blocking forever; below the hooks' timeouts
                 "query_timeout_seconds": 8,
                 # psycopg's automatic named prepared statements; off because they deadlock
                 # LISTENing connections through a connection pooler (see board/postgres.py _connect)
                 "prepared_statements": False,
                 # sent as the connection's application_name (shows in pg_stat_activity); "" = unset
                 "application_name": ""},
    # Optional connection for the watchers (`watch`, `tail`) only, e.g. the Postgres primary
    # directly when a pooler in front of it mishandles LISTEN/NOTIFY. Any [database] key; each
    # one left unset or empty falls back to [database]. Empty: the watchers use [database].
    "watch_database": {},
    "board": {"retention_days": 7, "message_max_chars": 200, "agent_stale_hours": 12,
              "read_limit": 50,
              # a new agent's first read: the job's newest join_history messages (0 = none)
              "join_history": 30,
              # the hooks show each agent a full roster at least this often (diffs in between)
              "roster_refresh_minutes": 10,
              # `watch` and `status --job` hide finished agents (completed/left/dead) whose end
              # or last contact is older than this; `a` / --all-agents shows them
              "watch_recent_minutes": 10, "watch_interval_s": 10, "watch_min_redraw_s": 2,
              # nudge an agent to post a status after this many tool calls or minutes without
              # posting (once per quiet window; 0 disables that trigger)
              "silence_nudge_calls": 15, "silence_nudge_minutes": 10,
              # agent_status: no hook contact for idle_minutes -> idle, for dead_minutes -> dead
              # (no SubagentStop ever came). A single tool call longer than tool_timeout_minutes
              # stops counting as running.
              "idle_minutes": 5, "dead_minutes": 30, "tool_timeout_minutes": 60,
              # Sandboxed agents often can't open a raw DB connection; `post` then writes the
              # message here and the hooks (which run outside the sandbox) deliver it. Must be a
              # directory the agents' sandbox can write to, and this user's own: a shared
              # path like /tmp/claude/swarm-spool lets another OS user deny or redirect it. A
              # path under /tmp must name the user: {uid} expands to the numeric uid.
              "spool_dir": "~/.local/state/swarm/spool"},
    "hook": {"marker_dir": "~/.local/state/swarm/active", "hook_min_interval_s": 15},
    # An open job closes by itself (status completed, closed_by "auto") once every agent of its
    # current run is done (completed or left; dead ones don't hold it open) and nothing happened
    # on it (no join, no hook contact, no post) for auto_close_minutes. 0 turns it off.
    #
    #
    # Two more ways an open job ends, so none stays open forever. stall_hours: a job that made
    # no progress for that long (no agent posted, joined or recorded a verdict; tool calls and
    # heartbeats don't count) is closed "failed"; one that keeps progressing runs as long as it
    # likes. `activate --stall-hours N` overrides it per job, 0 = never. orphan_minutes: a job
    # with no live agent (all done, dead or gone; a waiting job included) and no board activity
    # for that long is closed "cancelled". 0 turns either off. Neither closes a job with a goal
    # and no met verdict; goal_stall_hours is that job's own stall limit (0 = never, the default).
    "job": {"auto_close_minutes": 30, "stall_hours": 4, "orphan_minutes": 30, "goal_stall_hours": 0},
    # Codex fires SubagentStop after every turn of a child: it counts as completed once no new
    # turn came for this long
    "codex": {"stop_quiet_minutes": 3},
    # Default model per role for the spawned agents ([models.claude], [models.codex]: role ->
    # model name). No host sections by default, so off until configured.
    "models": {"mode": "default"},
    # The transcript archive (bin/transcripts.py), off by default: every swarm agent's transcript
    # (and the orchestrator's slice of each job) redacted, compressed and kept on the board for
    # retention_days, up to max_total_mb in all (0 = no size limit); running agents are
    # re-captured every snapshot_minutes; one over max_mb compressed keeps its head and tail.
    "transcripts": {"enabled": False, "retention_days": 30, "max_total_mb": 2048,
                    "snapshot_minutes": 15, "max_mb": 50},
    # The supervisor (swarm.supervisor): closes stuck agents and restarts them headless. Off by
    # default; every key and its default is in swarm/supervisor/settings.py (DEFAULTS).
    "supervise": {"enabled": False},
    # [board] backend = "sqlite": one database file shared by every agent on this machine.
    # Outside the sandboxes on purpose; sandboxed agents' posts spool (see bin/board/sqlite.py).
    # Not under ~/.local/state/swarm, which Codex sandboxes may write.
    "sqlite": {"path": "~/.local/share/swarm-board/board.sqlite3", "busy_timeout_ms": 10000},
    # [board] backend = "file": the board directory (state.json, messages.jsonl, lock); like
    # sqlite, outside the sandbox-writable state dir
    "file": {"path": "~/.local/share/swarm-board/board"},
    # Swarm agents spawning subagents of their own, enforced by the PreToolUse hook: only with a
    # `[swarm spawn: <why>]` line of at least min_justification_chars in the child's prompt, at
    # most max_per_agent per agent and max_per_job per job (the orchestrator's own spawns don't
    # count), and no deeper than max_depth (1 = the orchestrator's agents; 2 = their helpers,
    # which can't spawn). max_per_job = 0 turns agent spawning off.
    "spawn": {"max_per_agent": 2, "max_per_job": 4, "max_depth": 2, "min_justification_chars": 30},
    # Optional project memory in Hindsight. With `url` empty the feature is off entirely.
    "hindsight": {"url": "", "api_key_file": "", "timeout_seconds": 3,
                  "default_bank": "coding", "recall_banks": ["coding", "hermes"],
                  "recall_max_items": 8, "recall_max_chars": 1500, "recall_max_tokens": 1024,
                  "recall_minutes": 15, "recall_start_seconds": 6.0, "remember_nudge_minutes": 20,
                  "remember_max_chars": 1000, "retry_after_seconds": 60},
}


# --------------------------------------------------------------------------- config

def implicit_legacy_database_keys(user: dict) -> list[str]:
    """The [database] keys (user, dbname) a parsed config leaves out while having the section:
    they keep the old default (LEGACY_DATABASE_DEFAULTS), not the new one. Empty when there is
    no [database] section (a fresh setup gets the new defaults)."""
    db = user.get("database")
    if not isinstance(db, dict):
        return []
    return [k for k in LEGACY_DATABASE_DEFAULTS if k not in db]


def load_config(path: Path = DEFAULT_CONFIG) -> dict:
    cfg = json.loads(json.dumps(DEFAULTS))
    if path.exists():
        with open(path, "rb") as fh:
            user = tomllib.load(fh)
        for section, values in user.items():
            cfg.setdefault(section, {}).update(values)
        for key in implicit_legacy_database_keys(user):
            cfg["database"][key] = LEGACY_DATABASE_DEFAULTS[key]
        db = user.get("database")
        if (isinstance(db, dict) and db
                and not (user.get("board") or {}).get("backend")):
            # an install from before the default became "file": it relied on the old "postgres"
            # default, so it stays on it (`swarm doctor` says to set it explicitly)
            cfg["board"]["backend"] = "postgres"
            cfg["board"]["backend_implied"] = True
    spool = cfg.get("board", {}).get("spool_dir")
    if isinstance(spool, str) and "{uid}" in spool:   # a per-user /tmp spool
        cfg["board"]["spool_dir"] = spool.replace("{uid}", str(compat.uid()))
    return cfg


def watcher_config(cfg: dict) -> dict:
    """The config `watch` and `tail` connect with: [database] with every key [watch_database]
    sets (and doesn't leave empty) on top. Everything else is shared; cfg is not modified."""
    out = dict(cfg)
    overrides = {k: v for k, v in (cfg.get("watch_database") or {}).items() if v not in (None, "")}
    base = cfg["database"]
    if "host" in overrides or "hosts" in overrides:   # its servers replace [database]'s, whichever key
        base = {k: v for k, v in base.items() if k not in ("host", "hosts")}
    out["database"] = {**base, **overrides}
    return out


def watcher_db_label(cfg: dict) -> str | None:
    """The watchers' server ("host", plus ":port" / "/dbname" where those differ) when it is not
    the swarm's own; None when it is, or when the backend is not Postgres."""
    from swarm.board import board_backend   # lazy: the hooks import this module and never the board
    if board_backend(cfg) != "postgres":
        return None
    swarm_db, watch_db = cfg["database"], watcher_config(cfg)["database"]
    if all(swarm_db.get(k) == watch_db.get(k) for k in ("host", "hosts", "port", "dbname")):
        return None
    from swarm.board import database_hosts
    label = ",".join(h for h, _ in database_hosts(watch_db))
    if watch_db.get("port") != swarm_db.get("port"):
        label += f":{watch_db['port']}"
    if watch_db.get("dbname") != swarm_db.get("dbname"):
        label += f"/{watch_db['dbname']}"
    return label


def _error_name(exc: BaseException) -> str:
    """The name of the underlying error (BoardUnavailable wraps e.g. psycopg's OperationalError)."""
    return type(exc.__cause__ or exc).__name__


def cmd_init(cfg: dict, args) -> int:
    from swarm.board import IncompatibleStorage
    from swarm.board.autoinit import initialize
    try:
        result = initialize(cfg)
    except IncompatibleStorage as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    for note in result.notes:
        print(note)
    print(f"schema ready; name pool: {result.pool}")
    return 0


# Commands that don't set the board up by themselves first (see auto_init).
NO_AUTO_INIT = ("init", "install-hooks", "hook", "spool", "bootstrap", "migrate", "doctor", "notices",
                "upgrade", "update")


def auto_init(cfg: dict) -> None:
    """What every CLI command but NO_AUTO_INIT does first, so `swarm init` is never required: set
    the board up if it isn't for this code (board.ensure_initialized: a stamp check once done).
    Hooks come from the plugin: nothing is registered here. One line on stderr when it did
    something; never fails the command (an unreachable board is left to the command itself,
    which reports it, or spools a post). Then the notices a detached `swarm bootstrap` left for
    the user, once (a Codex user whose hooks aren't trusted yet sees them nowhere else). Hooks
    never call this: they only set the board up."""
    from swarm.board import BoardUnavailable, SCHEMA_VERSION, ensure_initialized
    from swarm.board.autoinit import enabled
    if not enabled():
        return
    try:
        res = ensure_initialized(cfg)
        for note in (res.setup.notes if res.setup else ()):
            print(f"swarm: {note}", file=sys.stderr)
        if res.action == "initialized":
            print(f"swarm: board set up (schema version {SCHEMA_VERSION})", file=sys.stderr)
        elif res.action == "newer":
            print(f"swarm: warning: the board has schema version {res.version}, newer than this "
                  f"code's {SCHEMA_VERSION}; left untouched (upgrade the skill)", file=sys.stderr)
    except BoardUnavailable:
        pass
    except Exception as exc:
        print(f"swarm: automatic init failed ({_error_name(exc)}: {exc}); run `swarm init`",
              file=sys.stderr)
    from swarm.bootstrap import take_notices
    text = take_notices()
    if text:
        print(text, file=sys.stderr)


def claude_settings_path() -> Path:
    """The Claude Code user settings the old skill registered its hooks in ($CLAUDE_SETTINGS
    overrides). Only `swarm migrate` rewrites it (bootstrap.migrate, through safefile)."""
    return Path(os.environ.get("CLAUDE_SETTINGS", "~/.claude/settings.json")).expanduser()


# --------------------------------------------------------------------------- auto-close

def auto_close_minutes(cfg: dict) -> float:
    return float((cfg.get("job") or {}).get("auto_close_minutes") or 0)


def job_limits(cfg: dict) -> tuple[float, float, float]:
    """([job] stall_hours, [job] orphan_minutes, [job] goal_stall_hours); 0 = that rule is off."""
    job = cfg.get("job") or {}
    return (float(job.get("stall_hours") or 0), float(job.get("orphan_minutes") or 0),
            float(job.get("goal_stall_hours") or 0))


def parse_duration(text: str) -> float:
    """Seconds in a duration like "90m", "2h", "1h30m", "45s" (a bare number is minutes).
    ValueError if it isn't one or is zero."""
    m = re.fullmatch(r"(?:(\d+(?:\.\d+)?)h)?(?:(\d+(?:\.\d+)?)m)?(?:(\d+(?:\.\d+)?)s)?", text.strip())
    if re.fullmatch(r"\d+(?:\.\d+)?", text.strip()):
        seconds = float(text) * 60
    elif m and any(m.groups()):
        seconds = sum(float(v) * k for v, k in zip(m.groups(), (3600, 60, 1)) if v)
    else:
        raise ValueError(f"not a duration: {text!r} (like 90m, 2h, 1h30m)")
    if seconds <= 0:
        raise ValueError("a duration must be more than zero")
    return seconds


def _duration_text(seconds: float) -> str:
    h, rest = divmod(int(round(seconds)), 3600)
    m, s = divmod(rest, 60)
    return "".join(f"{v}{u}" for v, u in ((h, "h"), (m, "m"), (s, "s")) if v) or "0s"


def codex_quiet_seconds(cfg: dict) -> float:
    return float((cfg.get("codex") or {}).get("stop_quiet_minutes", 3)) * 60


def sweep_jobs(board, cfg: dict, deadline: float | None = None) -> list:
    """Close the jobs that are done and quiet (Board.sweep_auto_close over [job]
    auto_close_minutes) and remove this machine's markers of auto-closed jobs, so the hooks stop
    enrolling agents on them. Returns the board.AutoClosed list. Cheap and idempotent: the CLI
    runs it from status, purge, join and activate, watch and tail once a minute, and the hooks
    on SubagentStart and SubagentStop (never on the per-tool-call ones). A no-op when disabled.
    With [transcripts] on, the jobs it closed get their final transcripts (bounded by
    `deadline`, a time.monotonic() value; failures are logged, never raised). First, whatever
    auto-close says: Codex agents quiet since their last turn for [codex] stop_quiet_minutes are
    completed (bookkeeping, any user's sweep), and this machine+user's ended Codex agents get
    their final transcripts (only the owner can read them). Then this machine+user's ended Claude
    agents missing a final transcript get it (swarm.supervisor.lost: pending finals), and, with
    [supervise] enabled, this machine+user's stuck agents are closed (swarm.supervisor.stuck).
    Never unbounded: with no `deadline`, transcripts.SWEEP_SECONDS from now (an
    agent's transcript can be made to redact for as long as it likes)."""
    if deadline is None:
        from swarm import transcripts as _tr
        deadline = time.monotonic() + _tr.SWEEP_SECONDS
    # Agent bookkeeping, not job closing: it runs with auto-close off too, so status/watch stop
    # showing a finished Codex agent as working and its owner can finalize its transcript.
    try:
        board.finish_quiet_agents(codex_quiet_seconds(cfg))  # any user's sweep may do it
    except Exception as exc:                                 # never fails status/join/activate
        from swarm import transcripts as _t
        _t.log(f"sweep: finishing quiet Codex agents failed: {type(exc).__name__}")
    try:
        from swarm import transcripts
        transcripts.finalize_owned(board, cfg, deadline)     # only this machine+user's own agents
    except Exception as exc:
        from swarm import transcripts as _t
        _t.log(f"transcripts: finalizing own agents failed: {type(exc).__name__}")
    try:   # final transcripts of this machine+user's ended Claude agents still missing one
        from swarm.supervisor import lost
        lost.finalize_pending_owned(board, cfg, deadline)
    except Exception as exc:
        from swarm import transcripts as _t
        _t.log(f"transcripts: finalizing ended Claude agents' transcripts failed: {type(exc).__name__}")
    try:   # the supervisor's closing of stuck agents (owner-only; off unless [supervise] enabled)
        from swarm.supervisor import stuck
        stuck.close_stuck_owned(board, cfg, deadline)
    except Exception as exc:
        from swarm import transcripts as _t
        _t.log(f"supervisor: closing stuck agents failed: {type(exc).__name__}")
    minutes = auto_close_minutes(cfg)
    closed = []
    if minutes > 0:
        closed = board.sweep_auto_close(minutes, lambda job: OrchestratorWatch(cfg, minutes, job))
    stall_hours, orphan_minutes, goal_stall_hours = job_limits(cfg)
    try:   # best effort: never fails the caller (a per-job cap applies even with the defaults off)
        closed += board.sweep_expiry(stall_hours, orphan_minutes,
                                     lambda job: OrchestratorWatch(cfg, orphan_minutes, job),
                                     goal_stall_hours=goal_stall_hours)
    except Exception as exc:
        from swarm.board import BoardUnavailable
        if isinstance(exc, BoardUnavailable):
            raise
        from swarm import transcripts as _t
        _t.log(f"sweep: expiry failed: {type(exc).__name__}")
    _drop_auto_closed_markers(board, cfg)
    if closed and transcripts_enabled(cfg):
        from swarm import transcripts
        transcripts.capture_closed(board, cfg, [c.job for c in closed], deadline)
    return closed


def orchestrator_seen_path(marker: Path) -> Path:
    """Beside a job's marker: touched by the hooks at every tool call of the session the marker
    is bound to (the orchestrator's own, which have no agent_id), so its mtime says when the
    orchestrating session was last at work. Not *.json: the hooks' marker globs skip it."""
    return marker.with_suffix(".seen")


def respawn_state_path(marker: Path) -> Path:
    """Beside a job's marker: what the main-session hooks last told the orchestrator about a
    not_met verdict (swarm.respawn.state_path). Removed with the marker."""
    return marker.with_suffix(".respawn")


MARKER_REMOVE_WAIT = 30.0   # seconds deactivate waits for a claim in flight (claims take ms)
MARKER_SWEEP_WAIT = 1.0     # ...and the auto-close sweep, which runs inside hooks


@contextlib.contextmanager
def locked_marker(path: Path, timeout: float):
    """The marker at `path`, open and exclusively flock-ed, checked to still be the file at
    `path` (a claim replaces it rather than rewriting it). Whoever changes or removes a marker
    holds this, so a claim (swarm.hooks) and a removal never interleave. Yields None if the
    lock isn't had within `timeout`; raises FileNotFoundError if there is no marker."""
    import stat
    deadline = time.monotonic() + timeout
    while True:
        # The marker dir is sandbox-writable: never follow a link, never block opening a
        # FIFO; anything but a plain file of ours is no marker.
        try:
            fd = compat.open(path, os.O_RDONLY | compat.O_NOFOLLOW | compat.O_NONBLOCK | compat.O_CLOEXEC)
        except FileNotFoundError:
            raise
        except OSError as exc:   # ELOOP: a symlink
            raise FileNotFoundError(f"not a marker: {path}") from exc
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != compat.uid():
            os.close(fd)
            raise FileNotFoundError(f"not a marker: {path}")
        fh = os.fdopen(fd, encoding="utf-8")
        try:
            locked = False
            while not locked:
                try:
                    compat.flock(fh, compat.LOCK_EX | compat.LOCK_NB)
                    locked = True
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        break
                    time.sleep(0.02)
            if not locked:
                yield None
                return
            if os.stat(path).st_ino != os.fstat(fh.fileno()).st_ino:
                continue   # replaced while we waited: lock the new one
            yield fh
            return
        finally:
            fh.close()   # releases the lock


def mark_orchestrator_seen(marker: Path, deadline: float | None = None) -> bool:
    """Record a tool call of the orchestrating session on the marker's "seen" file
    (orchestrator_seen_path). Lockless when the file exists: os.utime never creates one, so a
    touch racing the marker's removal (which deletes both under the marker's lock) can't bring
    .seen back, and nothing the auto-close sweep does can delay or drop it (it holds no lock
    while closing; OrchestratorWatch.after_close reopens a job touched meanwhile). Only a
    marker without one yet (just activated) gets it created, under the marker's lock and only
    while the marker exists, waiting until `deadline` (time.monotonic(); default
    MARKER_SWEEP_WAIT from now) at most. True if recorded."""
    # The marker dir is sandbox-writable: .seen is reached through a verified descriptor of the
    # dir (safefs), so a planted link is never followed and a FIFO never blocks (then: not
    # recorded).
    from swarm import safefs
    seen = orchestrator_seen_path(marker)
    try:
        d = safefs.open_base(marker.parent, create=False)
    except (OSError, ValueError):
        return False
    try:
        try:
            fd = safefs.open_existing(d, seen.name, os.O_RDONLY)
        except FileNotFoundError:
            pass
        except OSError:          # a link, FIFO, hard link or another user's file
            return False
        else:
            try:
                compat.utime_fd(fd, seen.name, d)
            finally:
                os.close(fd)
            return True
        if deadline is None:
            deadline = time.monotonic() + MARKER_SWEEP_WAIT
        try:
            with locked_marker(marker, max(0.0, deadline - time.monotonic())) as fh:
                if fh is None:
                    return False
                safefs.touch(d, seen.name)
                return True
        except FileNotFoundError:   # no marker (any other error reaches the hook's error log)
            return False
    finally:
        os.close(d)


def _mtime_ns(path: Path) -> int | None:
    try:
        return path.stat().st_mtime_ns
    except FileNotFoundError:
        return None


class OrchestratorWatch:
    """For Board.sweep_auto_close, per job about to be closed: the .seen files of this
    machine's markers of the job, stamped when it is made (right before the close). active():
    one was touched within `minutes` (keep the job open). after_close(undo): one was touched
    since the stamp, i.e. the orchestrating session worked during the close: undo() (the
    board's undo_auto_close: the close is reverted, same run) and its result. That check runs
    under the marker's lock, so a deactivate (which removes the marker, and its .seen, under
    that lock before closing the job) either goes first, and nothing is reverted, or waits for
    the revert and then closes the job itself (and undo_auto_close never reverts a close it
    didn't make). No lock is held across the close:
    touches never wait for it. Only this machine's markers: another machine's sweep can't see
    them (a job waiting on something there says so with `swarm wait`)."""

    def __init__(self, cfg: dict, minutes: float, job: str):
        self.cfg, self.job = cfg, job
        mdir = Path(cfg["hook"]["marker_dir"]).expanduser()
        self.since_ns = int((time.time() - minutes * 60) * 1e9)
        self.markers = [p for p in (sorted(mdir.glob("*.json")) if mdir.is_dir() else [])
                        if _read_marker(p).get("job") == job]
        self.stamps = {p: _mtime_ns(orchestrator_seen_path(p)) for p in self.markers}

    def active(self) -> bool:
        if any(ns is not None and ns >= self.since_ns for ns in self.stamps.values()):
            return True
        # a post (or wait) queued here within the window and not delivered yet: a sandboxed
        # agent without network is still at work
        from swarm import spool
        return spool.pending_since(self.cfg, self.job, self.since_ns / 1e9)

    def _touched(self, marker: Path) -> bool:
        now = _mtime_ns(orchestrator_seen_path(marker))
        return now is not None and now != self.stamps[marker]

    def after_close(self, undo) -> bool:
        for path in self.markers:
            try:
                with locked_marker(path, MARKER_SWEEP_WAIT) as fh:
                    # busy (a claim, say): still revert for a touch, if the marker is still there
                    if self._touched(path) and (fh is not None or path.exists()):
                        return bool(undo())
            except FileNotFoundError:
                continue   # removed (deactivated): its job stays closed
        return False


def _unlink_in_marker_dir(path: Path, *names: str) -> None:
    """Remove the entries `names` of the directory of `path` (a marker), relative to a verified
    descriptor of it (safefs): a link is removed, never followed; missing is fine."""
    from swarm import safefs
    try:
        d = safefs.open_base(path.parent, create=False)
    except FileNotFoundError:
        return
    try:
        for name in names:
            safefs.unlink(d, name)
    finally:
        os.close(d)


def _write_marker(marker: Path, data: dict) -> None:
    """Write a job marker (0600) through a verified descriptor of the marker dir (safefs:
    created 0700 if missing, no symlinked component, this user's): a fresh temp file renamed
    over the name, so a planted link or FIFO there is replaced, never followed. Its .seen goes
    first: a new run or binding starts unseen."""
    from swarm import safefs
    with safefs.dir_fd(marker.parent, create=True) as d:
        safefs.unlink(d, orchestrator_seen_path(marker).name)
        safefs.unlink(d, respawn_state_path(marker).name)
        safefs.write_atomic(d, marker.name, json.dumps(data), 0o600)


def remove_marker(path: Path, keep=None, wait: float | None = None) -> bool:
    """Unlink a marker, only ever under its lock (so a claim in flight can't bring it back);
    `keep(path)` true (checked under the lock) leaves it. Waits up to `wait` seconds (default
    MARKER_REMOVE_WAIT) for the lock. True if it is gone or kept as asked; False, marker left
    in place, if the lock couldn't be had."""
    try:
        with locked_marker(path, MARKER_REMOVE_WAIT if wait is None else wait) as fh:
            if fh is None:
                return False
            if keep is None or not keep(path):
                _unlink_in_marker_dir(path, path.name, orchestrator_seen_path(path).name,
                                      respawn_state_path(path).name)
    except FileNotFoundError:
        pass
    return True


def _drop_auto_closed_markers(board, cfg: dict) -> None:
    """Unlink every marker here whose job the sweep closed (this one or another machine's). Only
    if the close is newer than the marker: `activate` writes the marker after reopening the
    job, so a marker rewritten since belongs to a new run and stays."""
    from swarm.board import AUTO_CLOSED_BY
    mdir = Path(cfg["hook"]["marker_dir"]).expanduser()
    for path in sorted(mdir.glob("*.json")) if mdir.is_dir() else []:
        job = _read_marker(path).get("job")
        js = board.job_status(job) if job else None
        if js is None or js.status == "active" or js.closed_by != AUTO_CLOSED_BY or js.finished_at is None:
            continue
        closed_at = js.finished_at.timestamp()
        # busy (a claim in flight): left for the next sweep; a hook can't wait long
        remove_marker(path, keep=lambda p: closed_at < p.stat().st_mtime, wait=MARKER_SWEEP_WAIT)


SWEEPER_TRANSCRIPT_SECONDS = 5.0   # at most this long per sweep on transcripts in watch/tail


class Sweeper:
    """sweep_jobs at most once every `every` seconds, for the long-running `watch` and `tail`
    (the first call sweeps at once)."""

    def __init__(self, cfg: dict, every: float = 60.0, clock=time.monotonic):
        self.cfg, self.every, self.clock, self.next = cfg, every, clock, None

    def __call__(self, board) -> list:
        from swarm.board import BoardUnavailable
        if getattr(board, "degraded", None) or (self.next is not None and self.clock() < self.next):
            return []   # (a standby can't write: nothing to sweep)
        self.next = self.clock() + self.every
        try:
            closed = sweep_jobs(board, self.cfg, time.monotonic() + SWEEPER_TRANSCRIPT_SECONDS)
        except BoardUnavailable:
            raise   # the caller reconnects, as for any other query
        except Exception:
            return []   # a full-screen view has nowhere to say it; the next sweep tries again
        if transcripts_enabled(self.cfg):
            from swarm import transcripts
            try:   # a due snapshot round of this machine's transcripts
                transcripts.snapshot(board, self.cfg, time.monotonic() + SWEEPER_TRANSCRIPT_SECONDS)
            except BoardUnavailable:
                raise
            except Exception as exc:
                transcripts.log(f"watch snapshot: {type(exc).__name__}: {exc}")
        return closed


def _sweep(board, cfg: dict) -> list:
    """sweep_jobs for the one-shot commands: a failed sweep is reported on stderr and the
    command carries on (activate still writes its marker, join still prints the name). Losing
    the board is not a sweep failure: that raises, as any other query would."""
    from swarm.board import BoardUnavailable
    try:
        return sweep_jobs(board, cfg)
    except BoardUnavailable:
        raise
    except Exception as exc:
        print(f"auto-close sweep failed ({_error_name(exc)}: {exc})", file=sys.stderr)
        return []


def _say_closed(closed) -> None:
    for c in closed:
        print(f"{term_safe(c.job)}: {term_safe(c.outcome)}")


# --------------------------------------------------------------------------- rendering

NAME_PALETTE = (31, 32, 33, 34, 35, 36, 91, 92, 93, 94, 95, 96)
STATUS_COLORS = {"running": 32, "started": 36, "idle": 33, "completed": 34, "active": 32,
                 "dead": 31, "failed": 31, "left": 90, "cancelled": 90, "waiting": 35, "paused": 35,
                 "waiting (goal not met)": 35}   # (board.WAITING_GOAL)


def _sgr(code: int, text: str) -> str:
    return f"\033[{code}m{text}\033[0m"


def _bold(text: str, color: bool) -> str:
    return _sgr(1, text) if color else text


def _name_color(name: str) -> int:
    return NAME_PALETTE[sum(map(ord, name)) % len(NAME_PALETTE)]


def _paint_name(name: str, color: bool) -> str:
    """An agent name in a colour derived from the name, so each author keeps one colour."""
    return _sgr(_name_color(name), name) if color else name


def _paint_head(text: str, name: str, color: bool) -> str:
    """Colour the leading (possibly truncated) part of `text` that is `name`, as `swarm tail`
    would colour the whole name; `text` is plain, so cutting it first is ANSI-safe."""
    if not color or not name:
        return text
    n = min(len(name), len(text))
    return _sgr(_name_color(name), text[:n]) + text[n:]


def _paint_name_cell(name: str, padded: str, color: bool) -> str:
    """Like _paint_name, but colours a padded table cell (transcript list's AGENT column) instead
    of the bare name, so the same name keeps the same colour as `swarm tail`."""
    return _sgr(NAME_PALETTE[sum(map(ord, name)) % len(NAME_PALETTE)], padded) if color and name else padded


# claude vs codex, distinct from NAME_PALETTE and STATUS_COLORS so a HOST cell never looks like a
# name or a status.
HOST_COLORS = {"claude": 36, "codex": 35}


def _paint_host(cell: str, padded: str, color: bool) -> str:
    code = HOST_COLORS.get(cell)
    return _sgr(code, padded) if color and code else padded


def _paint_dim(cell: str, padded: str, color: bool) -> str:
    return _sgr("2", padded) if color else padded


def _paint_redacted(cell: str, padded: str, color: bool) -> str:
    """REDACTED count: yellow when it's not zero."""
    if not color:
        return padded
    try:
        hit = int(cell.strip()) > 0
    except ValueError:
        hit = False
    return _sgr(33, padded) if hit else padded


def _paint_final(cell: str, padded: str, color: bool) -> str:
    """FINAL cell: yes green, "capture failed" (or anything mentioning a size refusal) red, any
    other non-empty value (no, a kept snapshot) yellow."""
    if not color:
        return padded
    v = cell.strip()
    if v == "yes":
        return _sgr(32, padded)
    if not v:
        return padded
    if v == CAPTURE_FAILED or "large" in v:
        return _sgr(31, padded)
    return _sgr(33, padded)


def _paint_status(status: str, text: str, color: bool) -> str:
    """`text` (the status, possibly padded) in the status's colour, if it has one."""
    return _sgr(STATUS_COLORS[status], text) if color and status in STATUS_COLORS else text


def _msg_prefix(ts, job: str | None, who: str, to: str | None, color: bool) -> str:
    """`HH:MM:SS [job] who → to: `, the head of a tail/watch line; [job] only when job is given."""
    scope = f"[{term_safe(job)}] " if job is not None else ""
    target = f" → {term_safe(to)}" if to else ""
    return f"{ts.astimezone().strftime('%H:%M:%S')} {scope}{_paint_name(term_safe(who), color)}{target}: "


def _agent_event_line(ev, since) -> tuple:
    """(timestamp, text) for an agent joining, or leaving if it left after `since`."""
    ts, verb = (ev.left_at, ev.state) if ev.left_at and ev.left_at > since else (ev.joined_at, "joined")
    return ts, f"*** {verb}" + (f" ({term_safe(ev.role)})" if ev.role and verb == "joined" else "")


def _show_agent_events(board, since, job: str | None, say):
    """Print agents that joined or left after `since`; returns the board time to poll from next."""
    now = board.now()
    for ev in board.agent_events(since, job):
        ts, text = _agent_event_line(ev, since)
        say(ts, ev.job, ev.name, None, text)
    return now


# `watch` and `tail` reconnect after losing the board, waiting this long before the first
# attempt and doubling up to the max while it stays unreachable.
RECONNECT_MIN_SECONDS = 1.0
RECONNECT_MAX_SECONDS = 30.0


def _follow(cfg: dict, run, notice, pause) -> None:
    """Run `run(board)` until it returns, reconnecting whenever the board becomes unavailable.

    For the long-lived commands (`watch`, `tail`): a query past its deadline or a dropped
    connection raises BoardUnavailable (see board/postgres.py); instead of dying, or hanging,
    they call notice(text) once per outage, pause with back-off (pause(seconds) returns False to
    give up, e.g. the user quit), open a new board and call run() on it again. run() must
    subscribe itself, so every new connection LISTENs again. A board that is unreachable from
    the start is waited for the same way (never a traceback), with a notice per failed attempt
    saying when the next one is, until it connects or the user quits."""
    from swarm.board import BoardUnavailable, open_board
    board, delay = None, RECONNECT_MIN_SECONDS
    while board is None:  # the first connection
        try:
            board = open_board(cfg, readers=True)
        except BoardUnavailable as exc:
            notice(f"board unreachable ({_error_name(exc)}: {exc}), retrying in {delay:g}s…")
            if not pause(delay):
                return
            delay = min(delay * 2, RECONNECT_MAX_SECONDS)
    while True:
        try:
            with board:
                run(board)
            return
        except BoardUnavailable as exc:
            notice(f"board unreachable ({_error_name(exc)}: {exc}); reconnecting…")
        board, delay = None, RECONNECT_MIN_SECONDS
        while board is None:
            if not pause(delay):
                return
            delay = min(delay * 2, RECONNECT_MAX_SECONDS)
            try:
                board = open_board(cfg, readers=True)
            except BoardUnavailable:
                pass


def degraded_notice(host: str) -> str:
    return f"degraded: reading from {host} (no primary)"


def cmd_tail(cfg: dict, job: str | None, backlog: int, interval: float, show_agents: bool,
             color: bool) -> int:
    """Follow the board live (all jobs unless --job): messages plus agents joining/leaving.
    Survives losing the board: it reconnects and continues after the last message shown.
    Connects with watcher_config(cfg): [watch_database] over [database]."""
    db_label = watcher_db_label(cfg)
    db_note = f" (db: {db_label})" if db_label else ""

    def say(ts, jb, who, to, text) -> None:
        print(_msg_prefix(ts, None if job else jb, who, to, color) + term_safe(text), flush=True)

    def show(messages, last_id: int) -> int:
        for m in messages:
            say(m.created_at, m.job, m.agent_name, m.to_agent, m.message)
            last_id = m.id
        return last_id

    # last_id, since, following: kept across reconnects; each set as soon as it is known, so a
    # connection lost halfway through the startup neither replays the backlog nor skips a step
    state: dict = {}
    sweeper = Sweeper(cfg)

    def run(board) -> None:
        board.subscribe(messages_only=True)
        if "last_id" not in state:
            rows = board.recent_messages(backlog, job=job) if backlog > 0 else []
            state["last_id"] = show(rows, 0) if rows else board.last_message_id(job)
        if "since" not in state:
            state["since"] = board.now()
        if "following" not in state:
            state["following"] = True
            print(f"--- following {'job ' + job if job else 'all jobs'}{db_note} (Ctrl-C to stop) ---",
                  flush=True)
        else:
            print("--- reconnected ---", flush=True)
        state["degraded"] = None

        def note_degraded() -> None:  # said when it changes: standby reached, or a primary back
            now = board.degraded
            if now != state["degraded"]:
                print(f"--- {degraded_notice(now) if now else 'primary reachable again'} ---", flush=True)
                state["degraded"] = now
        if board.degraded:
            note_degraded()
        while True:  # catch up first: after a reconnect, what was posted during the outage
            state["last_id"] = show(board.messages_after(state["last_id"], job), state["last_id"])
            if show_agents:
                state["since"] = _show_agent_events(board, state["since"], job, say)
            sweeper(board)
            board.wait_for_change(interval)
            note_degraded()

    def pause(seconds: float) -> bool:
        time.sleep(seconds)
        return True

    try:
        _follow(watcher_config(cfg), run, lambda text: print(f"--- {text} ---", flush=True), pause)
    except KeyboardInterrupt:
        pass
    return 0


JOB_TAG = "[swarm job:"
ROLE_TAG = "[swarm role:"
JUDGE_TAG_LINE = f"{ROLE_TAG} judge]"   # makes a subagent its job's judge (jobs with a goal)
VERIFIER_TAG_LINE = f"{ROLE_TAG} verifier]"   # a read-only checker of the other agents' claims
SPAWN_TAG = "[swarm spawn:"   # a swarm agent's justification for spawning a subagent of its own


def tag_line(job: str) -> str:
    """The line that routes a subagent to `job` when it is in the subagent's spawn prompt."""
    return f"{JOB_TAG} {job}]"


def safe_job(job: str) -> str:
    """Filesystem-safe marker name for a job."""
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in job)[:100] or "job"


def fmt(rows) -> str:
    """Render board.Message objects (from Board.read_new) as `[HH:MM] who → to: message` lines."""
    out = []
    for m in rows:
        target = f" → {term_safe(m.to_agent)}" if m.to_agent else ""
        out.append(f"[{m.created_at.astimezone().strftime('%H:%M')}] {term_safe(m.agent_name)}{target}: "
                   f"{term_safe(m.message)}")
    return "\n".join(out)


def _ago(ts, now) -> str:
    if ts is None:
        return "-"
    s = int((now - ts).total_seconds())
    if s < 60:
        return f"{s}s ago"
    if s < 3600:
        return f"{s // 60}m ago"
    if s < 86400:
        return f"{s // 3600}h{(s % 3600) // 60:02d}m ago"
    return f"{s // 86400}d ago"


def _table(headers: list[str], rows: list[list[str]], color: bool, status_col: int | None = None,
          paint: dict | None = None, bold_header: bool = False) -> str:
    """Plain aligned table; the status column (or any column in `paint`, `{col: fn(cell, padded,
    color) -> str}`) is coloured without breaking alignment; column widths are always computed
    from the plain text, so a wide colour code never shifts them. Cells are board data: each goes
    through term_safe before it is measured and coloured."""
    rows = [[term_safe(c) for c in r] for r in rows]
    widths = [max([len(h)] + [len(r[i]) for r in rows]) for i, h in enumerate(headers)]
    head = "  ".join(h.ljust(w) for h, w in zip(headers, widths)).rstrip()
    out = [_bold(head, color) if bold_header else head]
    for r in rows:
        cells = []
        for i, (c, w) in enumerate(zip(r, widths)):
            padded = c.ljust(w)
            if i == status_col:
                padded = _paint_status(c.split(" ")[0], padded, color)
            elif paint and i in paint:
                padded = paint[i](c, padded, color)
            cells.append(padded)
        out.append("  ".join(cells).rstrip())
    return "\n".join(out)


def _job_status_word(board, j, now) -> str:
    """The job's shown status: waiting / idle / active for an open job (derive_job_status)."""
    from swarm.board import derive_job_status
    return derive_job_status(j, float(board.board_cfg.get("idle_minutes", 5)), now)


def _compact_status(board, j, now) -> str:
    """The compact watch's status word: the derived one only for a waiting goal job, else the stored
    status (a goal-less job keeps showing what it showed before)."""
    from swarm.board import WAITING_GOAL
    word = _job_status_word(board, j, now)
    return word if word == WAITING_GOAL else j.status


def _waiting_word(j, now) -> str:
    """The WAITING ON column: what the job waits for and for how long, "" if it isn't waiting."""
    if not j.waiting_on:
        return ""
    since = _ago(j.waiting_since, now).removesuffix(" ago")
    return f"{j.waiting_on} · {since}"


def open_and_paused(board, include_closed: bool) -> list:
    """The jobs a listing shows: with include_closed all of them, else the active ones plus the paused
    ones (board.jobs(False) is active only: the sweeps and the supervisor rely on that)."""
    rows = board.jobs(include_closed)
    if not include_closed:
        rows = rows + [j for j in board.jobs(True) if j.status == "paused"]
    return rows


def jobs_overview(board, include_closed: bool, color: bool, sup: dict | None = None,
                  rows: list | None = None) -> str:
    """rows: the jobs to list (default: board.jobs(include_closed)); `watch --session` passes its own."""
    now = board.now()
    if rows is None:
        rows = open_and_paused(board, include_closed)
    if not rows:
        return "no active jobs" + ("" if include_closed else " (--all includes closed ones)")
    table = [[j.job, _job_status_word(board, j, now), str(j.agents), str(j.running + j.started), str(j.idle),
              str(j.completed), str(j.dead_or_left), str(j.messages), _ago(j.activated_at, now),
              _ago(j.last_activity_at, now), _ago(j.finished_at, now), _verdict_word(j),
              _waiting_word(j, now), j.description or ""]
             for j in rows]
    return _table(["JOB", "STATUS", "AGENTS", "RUNNING", "IDLE", "DONE", "LEFT/DEAD", "MSGS",
                   "ACTIVATED", "LAST ACTIVITY", "FINISHED", "VERDICT", "WAITING ON", "DESCRIPTION"],
                  table, color, status_col=1)


def _verdict_word(j) -> str:
    """The VERDICT column: "-" for a job without a goal, "none" before the judge's first verdict."""
    return "-" if not j.goal else (j.verdict or "none") + ("*" if j.completion_forced else "")


def _verdict_line(j, now) -> str:
    if j.verdict:
        line = (f"verdict    {term_safe(j.verdict)} by {term_safe(j.verdict_by)}, {_ago(j.verdict_at, now)}: "
                f"{term_safe(j.verdict_reason)}")
        if j.verdict == "not_met" and j.verdict_next:   # what the judge wants done, and will re-check
            line += "\nnext       " + term_safe(j.verdict_next.strip(), keep_newlines=True).replace("\n", "\n           ")
        return line
    return "verdict    none yet (" + (f"judge: {term_safe(j.judge)}" if j.judge else "no judge on the job yet") + ")"


def _supervise_line(board, j, sup: dict | None) -> str | None:
    """The job's `supervise` head line, or None when there's nothing worth showing."""
    from swarm.supervisor import budget
    rs = board.restarts(job=j.job)
    if not j.supervise:
        return "supervise  off for this job (--no-supervise)"
    if sup is None or not sup.get("enabled"):
        return "supervise  off ([supervise] enabled = false)" if rs else None
    now = board.now()
    used = sum(budget.charged_minutes(r, now) for r in rs)
    return (f"supervise  on: restarts {len(rs)}/{sup['max_restarts_per_job']}, minutes "
            f"{used:.0f}/{sup['max_restart_minutes']}")


def job_detail(board, job: str, color: bool, recent_minutes: int | None = None, hint: str = "",
               transcripts: list | None = None, sup: dict | None = None, include_agents: bool = True) -> str:
    """The job's head lines and agents table. transcripts (the job's TranscriptSummary rows, only
    when the archive is on) adds a transcripts line and a STORED column to the table. sup ([supervise]
    settings, or None) adds a `supervise` line."""
    now = board.now()
    j = board.job_status(job)
    if not j:
        return f"no such job: {job}"
    ts = term_safe
    block = lambda text: ts((text or "").strip(), keep_newlines=True).replace("\n", "\n           ")  # noqa: E731
    activated = (f"activated  {_ago(j.activated_at, now)}" + (f" by {ts(j.created_by)}" if j.created_by else "")
                 + (f", session {ts(j.session_id)}" if j.session_id else ""))
    from swarm.board import WAITING_GOAL
    shown = _job_status_word(board, j, now)
    verifications = board.verification_counts(job)
    sup_line = _supervise_line(board, j, sup)
    head = [f"job        {ts(job)}  [{_paint_status(shown, shown, color)}]", activated,
            f"activity   {j.messages} messages, last {_ago(j.last_activity_at, now)}"]
    # (field value, its line): a line is shown only when its field is set.
    optional = ((j.waiting_on, f"waiting    on {ts(j.waiting_on)}, since {_ago(j.waiting_since, now)}"),
                (shown == WAITING_GOAL, "waiting    for an agent, the judge's met or a person (not auto-closed)"),
                (j.finished_at, f"finished   {_ago(j.finished_at, now)}" + _closed_by_note(j)),
                (j.description, f"about      {ts(j.description)}"),
                (j.project, f"project    {ts(j.project)} (memory)"),
                (j.task, "task       " + block(j.task)),
                (j.goal, "goal       " + block(j.goal)),
                (j.goal, _verdict_line(j, now)),
                (j.completion_forced, "forced     completed without a met verdict"),
                (any(verifications), "checks     {} verified, {} failed (verifiers)".format(*verifications)),
                (j.outcome, f"outcome    {ts(j.outcome)}"),
                (sup_line, sup_line or ""),
                (transcripts is not None, _transcripts_line(transcripts or [])))
    head += [line for value, line in optional if value]
    if not include_agents:
        return "\n".join(head)
    stored = None if transcripts is None else _stored_by_key(transcripts)
    return "\n".join(head) + "\n\n" + agents_table(board, job, color, now, recent_minutes, hint, stored)


def _closed_by_note(j) -> str:
    from swarm.board import AUTO_CLOSED_BY
    if j.closed_by == AUTO_CLOSED_BY:
        return " (auto-closed; activate reopens it)"
    return f" by {term_safe(j.closed_by)}" if j.closed_by else ""


FINISHED = ("completed", "left", "dead")


def _recent_agents(rows: list, now, recent_minutes: int | None) -> tuple[list, int]:
    """(agents to show, how many are hidden). Active agents always show; a finished one
    (completed/left/dead) only if its end or last contact is within recent_minutes of now.
    recent_minutes None shows everyone. The order of `rows` is kept."""
    if recent_minutes is None:
        return list(rows), 0
    import datetime as dt
    cutoff = now - dt.timedelta(minutes=recent_minutes)
    last = lambda a: max(t for t in (a.ended_at, a.last_contact_at) if t is not None)  # noqa: E731
    shown = [a for a in rows if a.status not in FINISHED or last(a) >= cutoff]
    return shown, len(rows) - len(shown)


def _short_model(model: str | None) -> str:
    """claude-opus-5-5 -> opus-5-5; anything else as is (cut to 18 chars)."""
    if not model:
        return ""
    return (model[len("claude-"):] if model.startswith("claude-") else model)[:18]


def agents_table(board, job: str, color: bool, now, recent_minutes: int | None = None,
                 hint: str = "", stored: dict | None = None) -> str:
    """The job's agents table. With recent_minutes, older finished agents are left out and a
    dim "(N older finished agents hidden · <hint>)" line follows the table. stored (agent_key ->
    stored transcript bytes) adds a STORED column ("-" for an agent without a transcript)."""
    if hasattr(board, 'watch_agents'):
        rows, hidden = board.watch_agents(job, recent_minutes)
    else:
        rows, hidden = _recent_agents(board.agents(job), now, recent_minutes)
    if not rows and not hidden:
        return "(no agents yet)"
    attempts = {r.new_agent_key: r.attempt for r in board.restarts(job=job) if r.new_agent_key}

    def _status_cell(a):
        cell = a.left_reason if a.ended_at is not None and a.left_reason else a.status
        return f"{cell} (restarted ×{attempts[a.agent_key]})" if a.agent_key in attempts else cell

    # LAST CONTACT covers posts too: posting updates last_seen.
    table = [[a.name, a.role or "", a.harness or "", _short_model(a.model), _status_cell(a),
              str(a.tool_calls), str(a.messages), _ago(a.joined_at, now),
              _ago(a.last_contact_at, now)]
             + ([] if stored is None else [_stored_cell(stored.get(a.agent_key))])
             + [a.current_tool or ""]
             for a in rows]
    headers = ["AGENT", "ROLE", "HOST", "MODEL", "STATUS", "CALLS", "MSGS", "JOINED", "LAST CONTACT"]
    out = _table(headers + ([] if stored is None else ["STORED"]) + ["TOOL"], table, color, status_col=4)
    if hidden:
        note = f"({hidden} older finished agent{'s' if hidden != 1 else ''} hidden · {hint})"
        out += "\n" + (_sgr(2, note) if color else note)
    return out


# --------------------------------------------------------------------------- transcripts

def transcripts_cfg(cfg: dict) -> dict:
    """[transcripts] with its defaults filled in (transcripts.settings)."""
    from swarm import transcripts
    return transcripts.settings(cfg)


def transcripts_enabled(cfg: dict) -> bool:
    from swarm import transcripts
    return transcripts.enabled(cfg)


def _size(n: int | float | None) -> str:
    """Bytes for people: "512 B", "1.5 KB", "12.3 MB", "2.0 GB"."""
    from swarm.transcript_view import human_size
    return human_size(n)


def _images_note(images: int, image_bytes: int) -> str:
    """", 2 images (5.9 KB)", or "" without images."""
    return f", {images} image{'s' if images != 1 else ''} ({_size(image_bytes)})" if images else ""


def _ratio(raw: int, stored: int) -> str:
    return f"{raw / stored:.1f}x" if stored else "-"


def _mb(value) -> str:
    """A max_total_mb for people: "2048 MB", "1.5 MB", "0.04 MB"; "" when it is 0 (no limit)."""
    mb = float(value or 0)
    return f"{mb:g} MB" if mb else ""


def _keep_note(cfg: dict) -> str:
    """How long / how much the archive keeps: "30 days / up to 2048 MB"."""
    tc = transcripts_cfg(cfg)
    mb = _mb(tc["max_total_mb"])
    return f"{tc['retention_days']} days / " + (f"up to {mb}" if mb else "no size limit")


def _limit_note(cfg: dict) -> str:
    tc = transcripts_cfg(cfg)
    return (_mb(tc["max_total_mb"]) or "no size limit") + f"/{tc['retention_days']}d"


def transcripts_footer(board, cfg: dict) -> str:
    """The `status` footer line on the archive (only asked for when it is on)."""
    t = board.transcript_totals()
    if not t.jobs:
        return f"transcripts: none stored, limit {_limit_note(cfg)}"
    day = t.oldest.astimezone().strftime("%Y-%m-%d") if t.oldest else "-"
    return (f"transcripts: {_size(t.stored)} stored ({_size(t.raw)} raw, ratio {_ratio(t.raw, t.stored)}"
            f"{_images_note(t.images, t.image_bytes)}), limit {_limit_note(cfg)}, "
            f"{t.jobs} job{'s' if t.jobs != 1 else ''}, oldest {day}")


def _transcripts_line(rows: list) -> str:
    """The `status --job` head line on the job's transcripts (capture-failed rows counted apart:
    they hold no transcript)."""
    from swarm.board.base import bodiless
    failed = sum(1 for r in rows if getattr(r, "failed", None))
    note = f"; {failed} capture failed (swarm transcript list)" if failed else ""
    rows = [r for r in rows if not bodiless(r)]
    if not rows:
        return "transcripts none stored" + note
    from swarm.board.base import transcript_totals_of
    t = transcript_totals_of(rows)
    return (f"transcripts {len(rows)} stored, {_size(t.stored)} ({_size(t.raw)} raw"
            f"{_images_note(t.images, t.image_bytes)})" + note)


CAPTURE_FAILED = "capture failed"   # the STORED / FINAL cell of a capture-failed row


def _stored_by_key(rows: list) -> dict:
    """agent_key -> stored bytes, or CAPTURE_FAILED for a capture-failed row."""
    return {r.agent_key: CAPTURE_FAILED if getattr(r, "failed", None) else r.stored_bytes for r in rows}


def _stored_cell(v) -> str:
    return "-" if v is None else v if isinstance(v, str) else _size(v)


def _when(ts) -> str:
    return ts.astimezone().strftime("%Y-%m-%d %H:%M") if ts else "-"


def _transcripts_off() -> int:
    print("transcripts are off: set [transcripts] enabled = true in the swarm config "
          "(then run `swarm init` once)", file=sys.stderr)
    return 1


def _transcript_list(board, cfg: dict, args) -> int:
    rows = sorted(board.transcripts(job=args.job, agent_name=args.agent),
                  key=lambda r: (r.job, r.captured_at))
    if not rows:
        print(f"no transcripts (kept {_keep_note(cfg)})")
        return 0
    from swarm.board.base import transcript_totals_of
    replaces: dict = {}   # (job, key) -> the key a supervisor replacement took over from
    for job in {r.job for r in rows}:
        replaces.update({(job, a.agent_key): a.resume_of or "" for a in board.agents(job)})
    table = [[r.job, r.agent_name or "", r.role, r.harness or "", _size(r.raw_bytes), _size(r.stored_bytes),
              _ratio(r.raw_bytes, r.stored_bytes), str(len(r.images)), str(r.redactions),
              CAPTURE_FAILED if r.failed else "yes" if r.final else "no", _when(r.captured_at),
              replaces.get((r.job, r.agent_key), ""),
              r.agent_key] for r in rows]
    print(_table(["JOB", "AGENT", "ROLE", "HOST", "RAW", "STORED", "RATIO", "IMAGES", "REDACTED", "FINAL",
                  "CAPTURED", "REPLACES", "KEY"], table, _transcript_use_color(args), bold_header=True,
                 paint={1: _paint_name_cell, 3: _paint_host, 4: _paint_dim, 5: _paint_dim,
                        8: _paint_redacted, 9: _paint_final}))
    t = transcript_totals_of(rows)   # images counted once however many transcripts show them
    print(f"total: {len(rows)} transcript{'s' if len(rows) != 1 else ''}, {_size(t.stored)} stored "
          f"({_size(t.raw)} raw, ratio {_ratio(t.raw, t.stored)}{_images_note(t.images, t.image_bytes)})")
    return 0


def _transcript_show(board, cfg: dict, args) -> int:
    import re
    from swarm.board import BoardError
    from swarm.board.base import bodiless
    if args.grep:
        try:
            re.compile(args.grep)
        except re.error as exc:
            print(f"bad --grep pattern: {term_safe(exc)}", file=sys.stderr)
            return 2
    if getattr(args, "memory", None) is not None:
        both = next((flag for flag, v in (("--job", args.job), ("--agent", args.agent), ("--key", args.key),
                                          ("--orchestrator", args.orchestrator)) if v), None)
        if both:
            print(f"--memory can't be combined with {both}: a memory names its own transcript", file=sys.stderr)
            return 2
        return _memory_show(board, cfg, args)
    if not (args.agent or args.orchestrator or args.key):
        print("say which transcript: --agent NAME, --orchestrator (with --job) or --key AGENT_KEY",
              file=sys.stderr)
        return 2
    if args.orchestrator and not args.job:
        print("--orchestrator needs --job", file=sys.stderr)
        return 2
    rows = board.transcripts(job=args.job, agent_name=args.agent, agent_key=args.key,
                             role="orchestrator" if args.orchestrator else None)
    who = ("the orchestrator" if args.orchestrator and not args.agent and not args.key
           else args.agent or f"key {args.key}")
    missing = f"no transcript for {who}" + (f" in {args.job}" if args.job else "") + \
        f"; transcripts are kept {_keep_note(cfg)}"
    if len(rows) > 1 and len({r.job for r in rows}) > 1:
        print(f"{len(rows)} transcripts for {who}; pick one with --job or --key:", file=sys.stderr)
        for r in sorted(rows, key=lambda r: r.captured_at):
            print(f"  {term_safe(r.job)}  {term_safe(r.role)}  {_when(r.captured_at)}  {_size(r.stored_bytes)}  "
                  f"key {term_safe(r.agent_key)}",
                  file=sys.stderr)
        return 1
    if len(rows) > 1:   # one job: the original agent and its supervisor replacements, oldest first
        replaces = {a.agent_key: a.resume_of for a in board.agents(rows[0].job)}
        runs = sorted(rows, key=lambda r: (_restart_depth(r.agent_key, replaces), r.captured_at))
        chunks = []
        for i, r in enumerate(runs, 1):
            try:
                body = None if bodiless(r) else board.transcript_body(r.job, r.agent_key)
            except BoardError as exc:
                body, unreadable = None, f"--- full transcript: unreadable ({term_safe(exc)}) ---"
            else:
                unreadable = _failed_note(r) if bodiless(r) else None
            if body is None and unreadable is None:
                continue
            head = (f"=== {term_safe(r.agent_name)}, run {i} of {len(runs)}: key {term_safe(r.agent_key)}, "
                    f"captured {_when(r.captured_at)}"
                    + (f", replaces {term_safe(replaces[r.agent_key])}" if replaces.get(r.agent_key) else "")
                    + ("\n" + _snapshot_label(r) if r.failed and body is not None else ""))
            chunks.append(head + "\n" + (unreadable if body is None
                                          else _render_transcript(board, body, args, _refs_of(board, r))))
        if not chunks:
            print(missing, file=sys.stderr)
            return 1
        return _emit_transcript("\n".join(chunks), args)
    if rows and bodiless(rows[0]):
        print(_failed_note(rows[0]).strip("- "), file=sys.stderr)
        return 1
    try:
        body = board.transcript_body(rows[0].job, rows[0].agent_key) if rows else None
    except BoardError as exc:   # corrupt or over TRANSCRIPT_MAX_RAW (a forged or damaged row)
        print(f"full transcript: unreadable ({term_safe(exc)})", file=sys.stderr)
        return 1
    if body is None:
        print(missing, file=sys.stderr)
        return 1
    out = _render_transcript(board, body, args, _refs_of(board, rows[0]))
    if rows[0].failed:   # the final capture failed: this is the last redacted snapshot
        if args.format == "jsonl":   # the output stays JSONL: the label goes to stderr
            print(_snapshot_label(rows[0]).strip("- "), file=sys.stderr)
        else:
            out = _snapshot_label(rows[0]) + "\n" + out
    return _emit_transcript(out, args)


def _failed_note(r) -> str:
    """What a bodiless capture-failed row says instead of a transcript: it holds none."""
    return (f"--- full transcript: capture failed ({term_safe(r.failed)}); nothing stored, "
            f"{_when(r.captured_at)} ---")


def _snapshot_label(r) -> str:
    """The label of a capture-failed row that kept its last redacted snapshot."""
    return (f"--- final capture failed ({term_safe(r.failed)}); last redacted snapshot from "
            f"{_when(r.captured_at)} ---")


def _refs_of(board, r) -> list:
    """The memory refs saved from transcript row `r` (its "memory saved" marks); only asked for
    in text mode's marks, and never fatal to showing the transcript."""
    from swarm.board import BoardError
    try:
        return board.memory_refs(job=r.job, agent_key=r.agent_key)
    except BoardError as exc:   # e.g. a malformed memory-ref store: show the transcript anyway
        print(f"memory marks left out: {term_safe(exc)}", file=sys.stderr)
        return []


def _memory_show(board, cfg: dict, args) -> int:
    """`transcript show --memory DOC_ID`: who saved the memory, in which tool call, whether it is
    still in Hindsight, the stored excerpt, and where the call is in the full transcript (if
    it is still stored). Every board-derived character goes through provenance.printable."""
    from swarm import provenance, transcript_view, transcripts
    from swarm.board import BoardError
    doc = args.memory
    if not provenance.valid_document_id(doc):
        print(f"not a document id: {term_safe(repr(doc))[:220]}", file=sys.stderr)
        return 2
    refs = board.memory_refs(document_id=doc)
    if not refs:
        print(f"no provenance recorded for memory {doc}", file=sys.stderr)
        return 1
    r = refs[0]
    lines = [f"memory {r.document_id}  bank {r.bank}  written with {r.writer}",
             f"saved by {r.agent_name} ({r.harness or '?'}, key {r.agent_key}) on job {r.job}, {_when(r.created_at)}"
             + (f", tool call {r.tool_call_id}" if r.tool_call_id else ""),
             f"in Hindsight: {provenance.status(cfg, r)}"]
    ex = None   # the excerpt: already sanitised (render_text/json_safe_line), kept apart from the
                # board-derived `lines`/tail_line below so provenance.printable's own term_safe pass
                # (needed for those) can't mangle colour's own ESC bytes in it
    try:
        body = board.memory_ref_excerpt(doc)
    except BoardError as exc:   # over EXCERPT_MAX_RAW or corrupt: refused by decompress_capped
        body, lines = None, lines + [f"excerpt: unreadable ({exc})"]
    else:
        if body is None:
            lines.append("excerpt: none was captured (the hook error log says why)")
    if body is not None:
        text = body.decode("utf-8", errors="replace")
        tail = args.tail if args.tail and args.tail > 0 else None
        if args.format == "jsonl":
            ex = transcripts.restore_images(
                "\n".join(transcript_view.json_safe_line(ln)
                          for ln in transcript_view.select_lines(text, tail, args.grep)),
                lambda sha: getattr(board.transcript_image(sha), "data", None))
        else:
            ex = transcript_view.render_text(transcript_view.select(transcript_view.turns(text), tail, args.grep),
                                             _transcript_use_color(args))
        lines.append(f"--- excerpt ({_size(r.raw_bytes)}, {r.redactions} redacted, {len(r.images)} images) ---")
    tail_line = _full_transcript_position(board, r)
    out = provenance.printable("\n".join(lines))
    if ex is not None:
        out += "\n" + ex
    out += "\n" + provenance.printable(tail_line)
    return _emit_transcript(out, args)


def _full_transcript_position(board, r) -> str:
    """Where the memory's tool call is in the agent's stored transcript, and how to read on."""
    from swarm import transcript_view
    from swarm.board import BoardError
    from swarm.board.base import bodiless
    failed = next((t for t in board.transcripts(job=r.job, agent_key=r.agent_key) if t.failed), None)
    if failed is not None and bodiless(failed):
        return _failed_note(failed)
    label = _snapshot_label(failed) + "\n" if failed is not None else ""
    try:
        body = board.transcript_body(r.job, r.agent_key)
    except BoardError as exc:   # a forged ref can point at a corrupt or oversized body
        return f"--- full transcript: unreadable ({exc}) ---"
    if body is None:
        return "--- full transcript: no longer stored (the excerpt above is kept with the memory) ---"
    items = transcript_view.turns(body.decode("utf-8", errors="replace"))
    i = transcript_view.anchor_index(items, r.tool_call_id, r.created_at)
    if i is None:
        return label + f"--- full transcript: stored (job {r.job}, key {r.agent_key}), but the call is not in it ---"
    import shlex
    return label + (f"--- full transcript: turn {i + 1} of {len(items)}; from there: "
            f"swarm transcript show --job {shlex.quote(r.job)} --key {shlex.quote(r.agent_key)} "
            f"--tail {len(items) - i + 5} ---")


def _restart_depth(key: str, replaces: dict) -> int:
    """How many restarts precede the run `key` along its resume_of chain (0: the original)."""
    seen = {key}
    while (key := replaces.get(key)) and key not in seen:
        seen.add(key)
    return len(seen) - 1


def _render_transcript(board, body: bytes, args, refs=()) -> str:
    """One stored transcript as `swarm transcript show` prints it (--format, --tail, --grep). In
    text mode, a "memory saved" turn follows each memory write in `refs` (its MemoryRefs)."""
    from swarm import transcript_view
    text = body.decode("utf-8", errors="replace")
    tail = args.tail if args.tail and args.tail > 0 else None
    if args.format == "jsonl":
        from swarm import transcripts
        out = "\n".join(transcript_view.json_safe_line(ln)
                        for ln in transcript_view.select_lines(text, tail, args.grep))
        # the original image blocks back in (text mode shows [image ...] markers instead)
        return transcripts.restore_images(out, lambda sha: getattr(board.transcript_image(sha), "data", None))
    items = transcript_view.mark_memories(transcript_view.turns(text), refs)
    return transcript_view.render_text(transcript_view.select(items, tail, args.grep), _transcript_use_color(args))


def _emit_transcript(out: str, args) -> int:
    """Print `out` (or write it 0600 to --output)."""
    out = out + "\n" if out else ""
    if args.output:
        _write_private(Path(args.output).expanduser(), out.encode())
        print(f"wrote {len(out.encode())} bytes to {args.output}", file=sys.stderr)
    else:
        sys.stdout.write(out)
    return 0


def _private_mkdir(d: Path) -> None:
    """Create `d` (and missing parents) with mode 0700 for d itself; an existing d is left as is
    (the files in it are 0600 anyway)."""
    d.parent.mkdir(parents=True, exist_ok=True)
    try:
        d.mkdir(mode=0o700)
    except FileExistsError:
        pass


def _write_private(path: Path, data: bytes) -> None:
    """Write a file readable by this user only (0600, also when it already existed)."""
    import stat
    fd = compat.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        if stat.S_ISREG(os.fstat(fd).st_mode):   # not a device like /dev/stdout
            compat.fchmod(fd, 0o600)
        fh.write(data)


MEMORY_INDEX_COLUMNS = ("file", "document_id", "bank", "writer", "agent_name", "agent_key", "harness",
                        "tool_call_id", "created_at", "raw_bytes", "redactions", "images")
INDEX_COLUMNS = ("file", "agent_name", "agent_key", "role", "host", "session_id", "captured_at",
                 "final", "raw_bytes", "stored_bytes", "redactions", "images", "harness")


SHA256_HEX = None   # re.compile(r"[0-9a-f]{64}"), compiled on first use (module top stays light)


class ExportRefused(Exception):
    """An export target entry that isn't a plain file of ours (a planted link, a FIFO, ...)."""


def _sandbox_writable_roots(cfg: dict) -> list[Path]:
    """Directories a sandboxed agent may write, as far as this host can tell: the swarm's own
    sandbox-writable dirs (the state dir, [board] spool_dir, [hook] marker_dir) and the
    writable_roots of the user's Codex config (base table and profiles). A Codex session's
    own workspace is one too, but can't be known from here."""
    from swarm import paths
    roots = [paths.state_dir(), Path(cfg["board"]["spool_dir"]).expanduser(),
             Path(cfg["hook"]["marker_dir"]).expanduser()]
    try:
        from swarm.hosts.codex import codex_home
        with open(codex_home() / "config.toml", "rb") as fh:
            data = tomllib.load(fh)
    except (OSError, ValueError):
        data = {}
    tables = [data.get("sandbox_workspace_write")]
    profiles = data.get("profiles")
    if isinstance(profiles, dict):
        tables += [p.get("sandbox_workspace_write") for p in profiles.values() if isinstance(p, dict)]
    for t in tables:
        found = t.get("writable_roots") if isinstance(t, dict) else None
        roots += [Path(r).expanduser() for r in found or () if isinstance(r, str)]
    return roots


def _inside_writable_root(cfg: dict, target: Path) -> Path | None:
    """The sandbox-writable root `target` is (or would be created) inside, or None."""
    real = Path(os.path.realpath(target))
    for root in _sandbox_writable_roots(cfg):
        r = Path(os.path.realpath(root))
        if real == r or r in real.parents:
            return root
    return None


def _export_write(d: int, name: str, data: bytes) -> None:
    """Write `name` in the export dir fd `d` (safefs.write_atomic: a fresh temp file renamed
    into place, never through the old entry). An old entry that isn't a single-link regular
    file of this user was not written by an export: refuse rather than replace it."""
    from swarm import safefs
    if safefs.exists(d, name) and not _plain_file(d, name):
        raise ExportRefused(name)
    safefs.write_atomic(d, name, data)


def _transcript_export(board, cfg: dict, args) -> int:
    """Every transcript of the job as <agent>.jsonl, its images under images/, and index.tsv;
    the job's memory excerpts as memory/<doc id>.jsonl (their images under images/ too) and
    memory.tsv. The files are written relative to the target dir's fd, opened
    without following a link anywhere on its path (safefs.open_base); file names come from
    board data, so a sha256 that isn't 64 hex digits and a document id that fails
    provenance.valid_document_id are skipped."""
    import re
    from swarm import safefs, transcripts
    from swarm.board import BoardError
    global SHA256_HEX
    SHA256_HEX = SHA256_HEX or re.compile(r"[0-9a-f]{64}")
    rows = sorted(board.transcripts(job=args.job), key=lambda r: (r.role, r.agent_name or "", r.agent_key))
    refs = board.memory_refs(job=args.job)
    if not rows and not refs:
        print(f"no transcripts for {term_safe(args.job)}; transcripts are kept {_keep_note(cfg)}", file=sys.stderr)
        return 1
    target = Path(os.path.abspath(Path(args.dir).expanduser() if args.dir
                                  else Path.cwd() / f"transcripts-{safe_job(args.job)}"))
    root = _inside_writable_root(cfg, target)
    if root is not None and not getattr(args, "force", False):
        print(f"swarm transcript export: refusing {term_safe(target)}: it is inside {term_safe(root)}, which sandboxed agents "
              f"can write (they could plant links there). Pick another directory, or pass --force.",
              file=sys.stderr)
        return 1
    try:
        d = safefs.open_base(str(target), create=True)
    except (OSError, ValueError) as exc:
        print(f"swarm transcript export: refusing {term_safe(target)}: {term_safe(exc)}", file=sys.stderr)
        return 1
    images_fd = memory_fd = None
    try:
        used, index, skipped = set(), ["\t".join(INDEX_COLUMNS)], []
        for r in rows:
            base = "orchestrator" if r.role == "orchestrator" else safe_job(r.agent_name or r.agent_key)
            if base in used:
                base = f"{base}-{safe_job(r.agent_key)[:40]}"
            used.add(base)
            name = base + ".jsonl"
            try:
                body = board.transcript_body(r.job, r.agent_key) or b""
            except BoardError as exc:   # corrupt or over TRANSCRIPT_MAX_RAW (a forged row): not written
                body = b""
                print(f"{term_safe(name)}: transcript unreadable ({term_safe(exc)})", file=sys.stderr)
            _export_write(d, name, body)   # images stay placeholders; the files are in images/
            files = []
            for img in board.transcript_images(job=r.job, agent_key=r.agent_key):
                if not isinstance(img.sha256, str) or not SHA256_HEX.fullmatch(img.sha256):
                    skipped.append(img.sha256)
                    continue
                file = f"{img.sha256}.{transcripts.IMAGE_EXT.get(img.mime, 'bin')}"
                if images_fd is None:
                    images_fd = safefs.open_sub(d, "images", create=True)
                if not safefs.exists(images_fd, file):
                    full = board.transcript_image(img.sha256)
                    if full is None:
                        continue
                    _export_write(images_fd, file, full.data)
                elif not _plain_file(images_fd, file):
                    raise ExportRefused(f"images/{file}")
                files.append(f"images/{file}")
            cells = (name, r.agent_name or "", r.agent_key, r.role, r.host or "", getattr(r, "session_id", "") or "",
                     r.captured_at.isoformat() if r.captured_at else "",
                     CAPTURE_FAILED if r.failed else "yes" if r.final else "no",
                     r.raw_bytes, r.stored_bytes, r.redactions, ",".join(files), r.harness or "")
            index.append("\t".join(str(c).replace("\t", " ").replace("\n", " ") for c in cells))
        if rows:
            _export_write(d, "index.tsv", ("\n".join(index) + "\n").encode())
        exported, bad_ids = 0, []
        if refs:
            from swarm import provenance
            mindex, mused = ["\t".join(MEMORY_INDEX_COLUMNS)], set()
            for r in refs:
                if not provenance.valid_document_id(r.document_id):   # a forged row: never a file name
                    bad_ids.append(r.document_id)
                    continue
                base = _unused_name(safe_job(r.document_id), r.document_id, mused)
                mused.add(base)
                name = f"{base}.jsonl"
                try:
                    body = board.memory_ref_excerpt(r.document_id) or b""
                except BoardError as exc:   # over EXCERPT_MAX_RAW or corrupt: refused, not written
                    body = b""
                    print(f"memory {r.document_id}: excerpt unreadable ({term_safe(exc)})", file=sys.stderr)
                if memory_fd is None:
                    memory_fd = safefs.open_sub(d, "memory", create=True)
                _export_write(memory_fd, name, body)
                files = []
                for img in r.images:
                    if not isinstance(img.sha256, str) or not SHA256_HEX.fullmatch(img.sha256):
                        skipped.append(img.sha256)
                        continue
                    file = f"{img.sha256}.{transcripts.IMAGE_EXT.get(img.mime, 'bin')}"
                    if images_fd is None:
                        images_fd = safefs.open_sub(d, "images", create=True)
                    if not safefs.exists(images_fd, file):
                        full = board.transcript_image(img.sha256)
                        if full is None:
                            continue
                        _export_write(images_fd, file, full.data)
                    elif not _plain_file(images_fd, file):
                        raise ExportRefused(f"images/{file}")
                    files.append(f"images/{file}")
                cells = (f"memory/{name}", r.document_id, r.bank, r.writer, r.agent_name, r.agent_key,
                         r.harness or "", r.tool_call_id or "", r.created_at.isoformat() if r.created_at else "",
                         r.raw_bytes, r.redactions, ",".join(files))
                mindex.append("\t".join(str(c).replace("\t", " ").replace("\n", " ") for c in cells))
                exported += 1
            _export_write(d, "memory.tsv", ("\n".join(mindex) + "\n").encode())
    except ExportRefused as exc:
        print(f"swarm transcript export: refusing to write {term_safe(target / str(exc))}: something other than a "
              f"file of yours is already there (a link?); nothing was written through it", file=sys.stderr)
        return 1
    except (OSError, ValueError) as exc:   # safefs.open_sub: a planted link or foreign dir
        print(f"swarm transcript export: refusing {term_safe(target)}: {term_safe(exc)}", file=sys.stderr)
        return 1
    finally:
        for fd in (images_fd, memory_fd):
            if fd is not None:
                os.close(fd)
        os.close(d)
    for sha in skipped:
        print(f"skipped an image with an invalid sha256 ({term_safe(sha)[:80]})", file=sys.stderr)
    for doc in bad_ids:
        print(f"skipped a memory ref with an invalid document id ({term_safe(repr(doc))[:80]})", file=sys.stderr)
    print(f"exported {len(rows)} transcript{'s' if len(rows) != 1 else ''}"
          + (f" and {exported} memory excerpt{'s' if exported != 1 else ''}" if refs else "")
          + f" of {term_safe(args.job)} to {term_safe(target)}")
    return 0


def _unused_name(base: str, key: str, used: set) -> str:
    """`base`, or, if it is taken (safe_job is lossy and cuts at 100, and a forged id can be
    chosen to equal another's renamed file), base cut to 80 plus a hash of `key`, then plus a
    counter, until the name is not in `used`."""
    import hashlib
    if base not in used:
        return base
    name = f"{base[:80]}-{hashlib.sha256(key.encode()).hexdigest()[:12]}"
    n = 1
    while name in used:
        n += 1
        name = f"{base[:80]}-{hashlib.sha256(key.encode()).hexdigest()[:12]}-{n}"
    return name


def _plain_file(d: int, name: str) -> bool:
    """Whether `name` in dir fd `d` is a regular, single-link file of this user (not followed)."""
    import stat
    try:
        st = compat.lstat(name, dir_fd=d)
    except FileNotFoundError:
        return False
    return stat.S_ISREG(st.st_mode) and st.st_uid == compat.uid() and st.st_nlink == 1


TRANSCRIPT_COMMANDS = {"list": _transcript_list, "show": _transcript_show, "export": _transcript_export}


MEMORY_CHECK_SECONDS = 30.0   # `memory refs --check`: all of its questions to Hindsight


def _refresh_hindsight_caps(cfg: dict) -> None:
    """Best effort: re-read what the Hindsight server supports into the cache the hooks read
    (hindsight.refresh_caps). Never fails the command."""
    if not str((cfg.get("hindsight") or {}).get("url") or "").strip():
        return
    try:
        from swarm import hindsight
        hindsight.refresh_caps(cfg)
    except Exception:
        pass


def _board_memory(board, cfg: dict, args) -> int:
    """`swarm memory refs`: the recorded memory references (provenance), filtered by job/agent."""
    import time
    from swarm import provenance
    rows = board.memory_refs(job=args.job, agent_name=args.agent)
    if not rows:
        print("no memory references" + (f" for {term_safe(args.job)}" if args.job else ""))
        return 0
    head = ["DOC_ID", "BANK", "JOB", "AGENT", "HOST", "WRITER", "SAVED", "EXCERPT", "REDACTED", "IMAGES"]
    table = [[r.document_id, r.bank, r.job, r.agent_name, r.harness or "", r.writer, _when(r.created_at),
              _size(r.stored_bytes) if r.stored_bytes else "-", str(r.redactions), str(len(r.images))]
             for r in rows]
    if args.check:   # read-only: asks Hindsight, never drops a ref (that is `swarm purge`)
        _refresh_hindsight_caps(cfg)
        found = provenance.statuses(cfg, rows, time.monotonic() + MEMORY_CHECK_SECONDS)
        head.append("HINDSIGHT")
        for r, row in zip(rows, table):
            row.append(found.get(r.document_id, "unknown"))
    print(provenance.printable(_table(head, table, False)))   # _table term_safes every cell
    print(f"total: {len(rows)}; show one with `swarm transcript show --memory <DOC_ID>`")
    return 0


def _board_transcript(board, cfg: dict, args) -> int:
    # provenance does not depend on the archive: show --memory works with it off
    if not transcripts_enabled(cfg) and not (args.tcmd == "show" and getattr(args, "memory", None) is not None):
        return _transcripts_off()
    return TRANSCRIPT_COMMANDS[args.tcmd](board, cfg, args)


_ANSI = None


def _clip(line: str, width: int) -> str:
    """Cut a line to `width` visible columns, keeping (and closing) ANSI colour codes."""
    global _ANSI
    import re
    _ANSI = _ANSI or re.compile(r"\033\[[0-9;?]*[A-Za-z]")
    out, seen, pos = [], 0, 0
    for m in _ANSI.finditer(line):
        text = line[pos:m.start()]
        if seen + len(text) > width:
            out.append(text[: width - seen]); seen = width
            break
        out.append(text); seen += len(text)
        out.append(m.group()); pos = m.end()
    else:
        text = line[pos:]
        out.append(text[: max(0, width - seen)])
    return "".join(out) + ("\033[0m" if "\033[" in line else "")


def _visible_len(line: str) -> int:
    import re
    return len(re.sub(r"\033\[[0-9;?]*[A-Za-z]", "", line))


def _shift(line: str, offset: int) -> str:
    """Scroll a line `offset` visible columns to the left, keeping its ANSI colour codes; like a
    scrolled message, a line that still shows something starts with "…"."""
    import re
    if offset <= 0:
        return line
    out, skip, first = [], offset, True
    for part in re.split(r"(\033\[[0-9;?]*[A-Za-z])", line):
        if part.startswith("\033["):
            out.append(part)
            continue
        part, skip = part[skip:], max(0, skip - len(part))
        if part and first:
            part, first = "…" + part[1:], False
        out.append(part)
    return "".join(out)


def _active_jobs_by_activity(board) -> list[str]:
    """Active job names in the job table's own stable order (by start, then name): ordering
    by recent activity swapped the agent sections around on every refresh."""
    return [j.job for j in board.jobs(False)]


def session_jobs(board, session: str) -> tuple[list, list]:
    """(shown, ever) for `watch --session S`: the jobs of session S that are shown, and every job
    S has had (closed ones too). shown = S's active jobs; when none is active, the job S
    finished last, so the final state stays on screen while the linger runs. A job's session is
    JobStatus.session_id (set by `activate`, kept by all backends)."""
    ever = board.session_jobs(session)
    active = [j for j in ever if j.status == "active"]
    if active or not ever:
        return active, ever
    return [max(ever, key=lambda j: (j.finished_at or j.created_at, j.job))], ever


class IdleExit:
    """`watch --exit-when-idle SECONDS`: the linger clock. The session is idle whenever it has no
    active job, including before it has ever had one (the clock starts at construction), so a
    pane for a session whose job never appears still closes. observe() each frame with whether
    a job of the session is active; expired() once it has been idle for `seconds`. A job that
    activates during the linger restarts it. `clock` is injectable (tests)."""

    def __init__(self, seconds: float, clock=time.monotonic):
        self.seconds, self.clock = seconds, clock
        self.idle_since = clock()

    def observe(self, has_active: bool) -> None:
        if has_active:
            self.idle_since = None
        elif self.idle_since is None:
            self.idle_since = self.clock()

    def expired(self) -> bool:
        return self.idle_since is not None and self.clock() - self.idle_since >= self.seconds


def _watch_header(board, job: str | None, interval: float, color: bool, interactive: bool, now,
                  recent_minutes: int | None = None, hide_agents: bool = False,
                  db_label: str | None = None, sup: dict | None = None,
                  session: str | None = None, session_rows: list | None = None) -> tuple[list[str], set[int]]:
    """The title line, then the job table (or the one job's head) and each active job's agents,
    and the indices of the lines that stay put when ←/→ scroll the rest (title, section headings).
    recent_minutes: hide finished agents older than this (None: show all). hide_agents: one dim
    line instead of every agents table (the job table keeps the per-job counts). db_label: the
    server watch is connected to, when it is not the swarm's own (see watcher_db_label). sup:
    [supervise] settings (parsed once per redraw by the caller, not per row), passed on to
    job_detail so the job head's supervise line matches `status --job`."""
    import datetime as dt
    scope = f"job {term_safe(job)}" if job else "all active jobs"
    if session:
        scope = f"session {term_safe(session)[:8]}" + (f" · job {term_safe(job)}" if job else "")
    agents_key = "v show agents" if hide_agents else "v hide agents"
    keys = (f"↑/↓ PgUp/PgDn history, G live, ←/→ Home/End sideways, w wrap, a all agents, "
            f"{agents_key}, q quit" if interactive else
            "Ctrl-C to quit (keys unavailable on Windows)" if compat.IS_WINDOWS else "Ctrl-C to quit")
    db = f" · db: {db_label}" if db_label else ""
    out = [_bold(f"swarm watch · {scope}{db} · {dt.datetime.now().strftime('%H:%M:%S')}", color)
           + f"   (refresh ≤{interval:g}s · {keys})", ""]
    pinned = {0}
    if job:
        jobs = [job]
        out += job_detail(board, job, color, sup=sup, include_agents=False).splitlines()
    else:
        pinned.add(len(out))
        out += [_bold("JOBS", color)] + jobs_overview(board, False, color, rows=session_rows).splitlines()
        jobs = _active_jobs_by_activity(board)
    if session_rows is not None:
        jobs = [j.job for j in session_rows]
    if hide_agents:
        note = "(agents hidden · v to show)"
        pinned.add(len(out) + 1)
        return out + ["", _sgr(2, note) if color else note], pinned
    for jb in jobs:
        pinned.add(len(out) + 1)
        out += ["", _bold(f"AGENTS · {term_safe(jb)}", color)] + agents_table(
            board, jb, color, now, recent_minutes, "press a to show all").splitlines()
    return out, pinned


def _scroll_title(entries: list[tuple], cols: int, view: dict, table_width: int = 0) -> str:
    """Clamp the horizontal scroll to the longest message or table line (table_width: the widest
    line of the job and agents tables); describe the mode for the title."""
    widest = max([table_width] + [pl + len(m) for _, pl, m in entries])
    view["max_offset"] = max(0, table_width - cols,
                             max((len(m) - (cols - pl) for _, pl, m in entries), default=0))
    view["offset"] = min(view["offset"], view["max_offset"])
    mode = "wrap" if view["wrap"] else (f"scrolled {view['offset']} →" if view["offset"] else "")
    more = " · longer than the screen: ←/→ or w" if not view["wrap"] and widest > cols and not view["offset"] else ""
    return (f"  ({mode})" if mode else "") + more


def _message_block(prefix: str, pl: int, msg: str, cols: int, view: dict) -> list[str]:
    """One message as screen lines: wrapped under its prefix, or one line scrolled by the offset."""
    if view["wrap"]:
        import textwrap
        chunks = textwrap.wrap(msg, max(10, cols - pl), break_on_hyphens=False) or [""]
        return [prefix + chunks[0]] + [" " * pl + c for c in chunks[1:]]
    text = msg[view["offset"]:]
    return [prefix + ("…" + text[1:] if view["offset"] and text else text)]


def _newest_that_fit(blocks: list[list[str]], room: int) -> list[str]:
    """Newest at the bottom. Fill upwards with whole messages only, so the top of the pane never
    starts with a continuation line whose author has scrolled off."""
    n = _fit_count(blocks, room)
    return [line for block in blocks[len(blocks) - n:] for line in block]


def _fit_count(blocks: list[list[str]], room: int) -> int:
    """How many of the newest blocks fit, whole, in `room` lines."""
    used = n = 0
    for block in reversed(blocks):
        if used + len(block) > room:
            break
        used, n = used + len(block), n + 1
    return n


# How many messages `watch` fetches while scrolled back into history: the bound on how far back
# ↑/PgUp can go, so a huge board costs a bounded query per redraw. At the live tail it fetches
# only what fits.
WATCH_HISTORY = 500


def _oldest_bottom(line_counts: list[int], room: int) -> int:
    """The oldest index the bottom message may scroll back to: the last one of the oldest
    messages that still fill the pane together, so the pane never shows empty space on top."""
    used = 0
    for i, n in enumerate(line_counts):
        used += n
        if used > room:
            return max(0, i - 1)
    return len(line_counts) - 1


def _scroll_bottom(msgs: list, view: dict, oldest: int) -> int:
    """Index in msgs (oldest first) of the newest message to show. Applies the pending
    view["scroll"] (messages, + is older) and pins the view to view["anchor"], the id of its
    bottom message, so new posts don't move it. At the live tail anchor is None."""
    delta, view["scroll"] = view.get("scroll", 0), 0
    last = len(msgs) - 1
    if view.get("anchor") is None:
        if delta <= 0 or last < 0:
            return last
        view["mark"] = msgs[-1].id  # the newest message when we left the tail
        idx = last
    else:
        idx = max((i for i, m in enumerate(msgs) if m.id <= view["anchor"]), default=0)
    idx = max(oldest, idx - delta)
    if idx >= last:
        view["anchor"] = None
        return last
    view["anchor"] = msgs[idx].id
    return idx


def _history_title(msgs: list, bottom: int, view: dict) -> str:
    """"  (scrolled back N · M newer · G for live)" while scrolled back, else ""."""
    if view.get("anchor") is None:
        return ""
    below = [m.id for m in msgs[bottom + 1:]]
    mark = max(view.get("mark") or 0, msgs[bottom].id)
    back, newer = sum(1 for i in below if i <= mark), sum(1 for i in below if i > mark)
    return f"  (scrolled back {back}" + (f" · {newer} newer" if newer else "") + " · G for live)"


def _watch_messages(board, limit: int, job: str | None, session_rows: list | None) -> list:
    """The newest `limit` messages to show, oldest first: the one job's, the session's jobs'
    (session_rows, when `watch --session`), or all active jobs'."""
    if session_rows is not None and not job:  # merge each of the session's jobs' newest messages
        return sorted((m for j in session_rows for m in board.recent_messages(limit, job=j.job)),
                      key=lambda m: m.id)[-limit:]
    return board.recent_messages(limit, job=job, active_jobs_only=not job)


def _fit(text: str, width: int) -> str:
    """Plain text cut to `width` columns, with a final "…" when something was cut."""
    return text if len(text) <= width else text[:max(0, width - 1)] + "…"[:width]


def _compact_agent_line(a, width: int, color: bool) -> str:
    """One agent, at most `width` columns: name [judge|verifier] status model tool. Under
    pressure the tool is cut (or dropped) first, then the model, then the name is cut."""
    name = term_safe(a.name) + (f" [{a.role}]" if a.role in ("judge", "verifier") else "")
    status = term_safe(a.left_reason if a.ended_at is not None and a.left_reason else a.status)
    model, tool = term_safe(_short_model(a.model)), term_safe(a.current_tool or "")
    room = width - 2
    if len(name) + 1 + len(status) > room:      # not even name + status: cut the name
        name, model, tool = _fit(name, max(1, room - len(status) - 1)), "", ""
    elif len(name) + 1 + len(status) + 1 + len(model) > room:
        model, tool = "", ""
    left = room - len(name) - 1 - len(status) - (1 + len(model) if model else 0)
    tool = _fit(tool, left - 1) if tool and left >= 6 else ""
    line = "  " + _paint_head(name, term_safe(a.name), color) + " " + _paint_status(a.status, status, color)
    return _clip(line + (" " + model if model else "") + (" " + tool if tool else ""), width)


NATURAL = 10 ** 6   # "no width limit": a compact line is built whole, then windowed to the pane


def _window(line: str, offset: int, cols: int) -> str:
    """The `cols` visible columns of `line` from `offset` on (ANSI-safe: cut by visible
    characters); a "…" leads a scrolled line and ends one that goes on past the pane."""
    line = _shift(line, offset)
    if _visible_len(line) <= cols:
        return line
    return _clip(line, cols - 1) + "…" if cols > 1 else _clip(line, cols)


def _compact_frame(board, job: str | None, color: bool, view: dict, rows: list | None,
                   cols: int, height: int) -> list[str]:
    """The narrow view (`watch --compact`, for a side pane of 50-70 columns): a title, then per
    open job (all of the session's, one section each) its id line and one line per agent, then
    as many recent messages as the height leaves, one line each. Lines are built whole and cut
    to `cols` visible columns after scrolling sideways by view["offset"] (Left/Right, Home
    resets), which is clamped to the longest line and kept in the view; the title stays."""
    import datetime as dt
    now = board.now()
    recent = None if view.get("all_agents") else view.get("recent_minutes")
    scope = f"session {term_safe(view['session'])[:8]}" if view.get("session") else "active jobs"
    title = _clip(_bold(_fit(f"swarm · {scope} · {dt.datetime.now().strftime('%H:%M:%S')}", cols), color), cols)
    out = []
    if rows is None:
        rows = [board.job_status(job)] if job else open_and_paused(board, False)
        rows = [j for j in rows if j]
    if not rows:
        out.append("(no jobs yet)")
    for j in rows:
        out.append(_bold(f"{term_safe(j.job)} [{_compact_status(board, j, now)}]", color))
        if hasattr(board, 'watch_agents'):
            agents, hidden = board.watch_agents(j.job, recent)
        else:
            agents, hidden = _recent_agents(board.agents(j.job), now, recent)
        out += [_compact_agent_line(a, NATURAL, color) for a in agents]
        if not agents:
            out.append("  (no agents yet)")
        if hidden:
            out.append(f"  ({hidden} older hidden)")
    room = height - len(out) - 2  # the title and one line for the MESSAGES heading
    if room >= 2:
        msgs = _watch_messages(board, room, job, rows if view.get("session") else None)
        if msgs:
            out.append(_bold("MESSAGES", color))
            for m in msgs[-room:]:
                stamp = m.created_at.astimezone().strftime('%H:%M') + " "
                text = f"{term_safe(m.agent_name)}: {term_safe(m.message).replace(chr(10), ' ')}"
                out.append(stamp + _paint_head(text, term_safe(m.agent_name), color))
    out = out[:max(0, height - 1)]
    view["max_offset"] = max(0, max((_visible_len(ln) for ln in out), default=0) - cols)
    view["offset"] = min(max(0, view.get("offset", 0)), view["max_offset"])
    return [title] + [_window(ln, view["offset"], cols) for ln in out]


def _watch_frame(board, job: str | None, interval: float, color: bool, interactive: bool,
                 view: dict, sup: dict | None = None) -> list[str]:
    """One screen. view["recent_minutes"] hides older finished agents unless view["all_agents"].
    view["anchor"] (an id) pins the messages pane back in history; see _scroll_bottom. sup:
    [supervise] settings, parsed once per redraw by the caller (see cmd_watch), not per row."""
    import shutil
    now = board.now()
    cols, rows = shutil.get_terminal_size((120, 40))
    recent = None if view.get("all_agents") else view.get("recent_minutes")
    session = view.get("session")
    srows = None
    if session:
        srows, _ever = session_jobs(board, session)
        if view.get("idle_exit") is not None:
            view["idle_exit"].observe(any(j.status == "active" for j in srows))
    if view.get("compact"):
        return _compact_frame(board, job, color, view, srows, cols, rows - 1)
    out, pinned = _watch_header(board, job, interval, color, interactive, now, recent,
                                bool(view.get("hide_agents")), view.get("db_label"), sup,
                                session, srows)
    header = len(out)
    table_width = max((_visible_len(ln) for i, ln in enumerate(out) if i not in pinned), default=0)
    # Messages fill whatever height is left (at least 5 lines).
    room = max(5, rows - len(out) - 3)
    history = view.get("anchor") is not None or view.get("scroll", 0) > 0
    msgs = _watch_messages(board, WATCH_HISTORY if history else room, job, srows)
    # Each message is a fixed prefix (time, job, author, addressee) plus its text. Scrolling
    # moves only the text, so you can always see who said it; wrap mode shows it all instead.
    plain = lambda m: _msg_prefix(m.created_at, None if job else m.job, m.agent_name, m.to_agent, False)  # noqa: E731
    counts = [len(_message_block("", len(plain(m)), term_safe(m.message), cols, view)) for m in msgs[:room + 1]]
    bottom = _scroll_bottom(msgs, view, _oldest_bottom(counts, room))
    shown = msgs[max(0, bottom + 1 - room): bottom + 1]  # each message takes at least one line
    entries = []
    for m in shown:
        args = (m.created_at, None if job else m.job, m.agent_name, m.to_agent)
        entries.append((_msg_prefix(*args, color), len(plain(m)), term_safe(m.message)))
    title = _scroll_title(entries, cols, view, table_width)
    # The tables scroll sideways by the same offset as the messages; titles and headings stay.
    out = [ln if i in pinned else _shift(ln, view["offset"]) for i, ln in enumerate(out[:header])]
    out += ["", _bold("MESSAGES", color) + title + _history_title(msgs, bottom, view)]
    if not entries:
        out.append("(none yet)")
    blocks = [_message_block(prefix, pl, msg, cols, view) for prefix, pl, msg in entries]
    pane = max(5, rows - 1 - len(out))
    view["page"] = max(1, _fit_count(blocks, pane))  # PgUp/PgDn move by the messages on screen
    out += _newest_that_fit(blocks, pane)
    return [_clip(line, cols) for line in out[: rows - 1]]


# Key sequences `watch` understands, tried in order at each position of the input.
WATCH_KEYS = (("\x1b[A", "older"), ("\x1b[B", "newer"), ("\x1b[5~", "page_older"),
              ("\x1b[6~", "page_newer"), ("k", "older"), ("j", "newer"), ("G", "live"),
              ("\x1b[C", "right"), ("\x1b[D", "left"), ("\x1b[H", "home"), ("\x1b[1~", "home"),
              ("\x1b[F", "end"), ("\x1b[4~", "end"), ("l", "right"), ("h", "left"), ("0", "home"),
              ("$", "end"), ("w", "wrap"), ("a", "agents"), ("v", "hide_agents"), ("q", "quit"))
# action -> view update, given the view and the scroll step
VIEW_ACTIONS = {
    "wrap": lambda view, step: view.update(wrap=not view["wrap"]),
    "agents": lambda view, step: view.update(all_agents=not view.get("all_agents")),
    "hide_agents": lambda view, step: view.update(hide_agents=not view.get("hide_agents")),
    "home": lambda view, step: view.update(offset=0),
    "end": lambda view, step: view.update(offset=view["max_offset"]),
    "right": lambda view, step: view.update(offset=max(0, view["offset"] + step)),
    "left": lambda view, step: view.update(offset=max(0, view["offset"] - step)),
    # History: a pending move in messages (+ older), applied and clamped by the next frame.
    "older": lambda view, step: view.update(scroll=view.get("scroll", 0) + 1),
    "newer": lambda view, step: view.update(scroll=view.get("scroll", 0) - 1),
    "page_older": lambda view, step: view.update(scroll=view.get("scroll", 0) + view.get("page", 1)),
    "page_newer": lambda view, step: view.update(scroll=view.get("scroll", 0) - view.get("page", 1)),
    "live": lambda view, step: view.update(anchor=None, scroll=0),
}


def _apply_keys(data: str, view: dict) -> bool:
    """Apply keypresses to the view. Returns False to quit."""
    import shutil
    step = max(10, shutil.get_terminal_size((120, 40)).columns // 3)
    i = 0
    while i < len(data):
        hit = next(((seq, action) for seq, action in WATCH_KEYS if data.startswith(seq, i)), None)
        if hit is None:
            i += 1  # a key we don't use
            continue
        i += len(hit[0])
        if hit[1] == "quit":
            return False
        VIEW_ACTIONS[hit[1]](view, step)
    return True


def _read_keys(fd: int | None) -> str | None:
    """Keys typed within the next 0.2s, or None if none (or not interactive: then just wait)."""
    if fd is None:
        time.sleep(0.2)
        return None
    import select
    return os.read(fd, 64).decode(errors="ignore") if select.select([fd], [], [], 0.2)[0] else None


def _screen_on(out, fd: int | None):
    """Enter the full-screen view; returns the terminal settings _screen_off restores."""
    saved = None
    if fd is not None:
        import termios
        import tty
        saved = termios.tcgetattr(fd)
        tty.setcbreak(fd)  # keys arrive one at a time, unechoed; Ctrl-C still interrupts
    if out.isatty():
        out.write("\033[?1049h\033[?25l")  # alternate screen, hide cursor
    return saved


def _screen_off(out, fd: int | None, saved) -> None:
    if saved is not None:
        import termios
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
    if out.isatty():
        out.write("\033[?25h\033[?1049l")
        out.flush()


class _Recorder:
    """A board stand-in for the refresh thread: forwards every call to the real board and keeps
    each answer (a snapshot): `data` by method and arguments, `msgs` the recent_messages answers
    by limit (a smaller limit is served from a bigger one), `attrs` plain attributes. `taken` is
    the board's clock when the refresh started and `taken_mono` the local one at that same
    moment, so a replay can age the snapshot (idle, dead) between refreshes."""

    def __init__(self, board):
        self._board, self.data, self.attrs, self.msgs = board, {}, {}, []
        self.agent_windows = []
        self.taken, self.taken_mono = board.now(), time.monotonic()

    def __getattr__(self, name):
        value = getattr(self._board, name)
        if not callable(value):
            self.attrs[name] = value
            return value

        def call(*args, **kwargs):
            key = (name, repr((args, sorted(kwargs.items()))))
            if key in self.data:
                result = self.data[key]
                return list(result) if isinstance(result, list) else result
            if name == 'recent_messages' and len(args) == 1:
                kw = repr(sorted(kwargs.items()))
                for limit, saved_kw, rows in self.msgs:
                    if kw == saved_kw and limit >= args[0]:
                        return list(rows[-args[0]:]) if args[0] > 0 else []
            result = value(*args, **kwargs)
            self.data[key] = result
            if name == 'watch_agents' and len(args) == 2:
                self.agent_windows.append((args[0], result))
            if name == "recent_messages" and len(args) == 1:
                self.msgs.append((args[0], repr(sorted(kwargs.items())), result))
            return result
        return call


class SnapshotMiss(Exception):
    """The frame asked a snapshot for something it did not record."""


class _Replay:
    """A board stand-in for the key thread: answers from a _Recorder, never a query. now() moves
    on with the local clock, so idle and dead keep ageing between refreshes. Lists are copied, so
    a frame that sorts or edits one cannot corrupt the snapshot."""

    def __init__(self, snap: _Recorder):
        self._snap = snap

    def now(self):
        import datetime as dt
        return self._snap.taken + dt.timedelta(seconds=time.monotonic() - self._snap.taken_mono)

    def __getattr__(self, name):
        snap = self._snap
        if name in snap.attrs:
            return snap.attrs[name]

        def call(*args, **kwargs):
            try:
                result = snap.data[(name, repr((args, sorted(kwargs.items()))))]
            except KeyError:
                result = None
                if name == "recent_messages" and len(args) == 1:   # a shorter window of a longer one
                    kw = repr(sorted(kwargs.items()))
                    for limit, kw2, rows in snap.msgs:
                        if kw2 == kw and limit >= args[0]:
                            result = rows[-args[0]:] if args[0] > 0 else []
                            break
                if result is None and name == 'watch_agents' and len(args) == 2 and args[1] is None:
                    for saved_job, saved in snap.agent_windows:
                        if saved_job == args[0] and saved[1] == 0:
                            result = saved
                            break
                if result is None:
                    raise SnapshotMiss(name) from None
            return list(result) if isinstance(result, list) else result
        return call


def _take_snapshot(board, frame, view: dict, job: str | None) -> _Recorder:
    """Run frame(recorder): every board answer the frame used is kept. Then also the newest
    WATCH_HISTORY messages (60 in the compact view, which has no history), of which every other
    window (scrolling back, a taller terminal) is a slice, so those keys need no query either."""
    if hasattr(board, 'watch_snapshot'):
        recent = None if view.get('all_agents') else view.get('recent_minutes')
        board = board.watch_snapshot(job, view.get('session'), recent,
                                     60 if view.get('compact') else WATCH_HISTORY)
    rec = _Recorder(board)
    session = view.get("session")
    srows = session_jobs(rec, session)[0] if session else None
    _watch_messages(rec, 60 if view.get("compact") else WATCH_HISTORY, job, srows)
    frame(rec)
    return rec


class _RefreshGate:
    """Burst notifications mark dirty; they never move the existing redraw deadline."""
    def __init__(self, interval, minimum=2):
        self.interval = max(float(interval), float(minimum))
        self.minimum = max(0.01, float(minimum))
        self.last = None
        self.dirty = False

    def due(self, now, changed=False):
        self.dirty |= changed
        return self.last is None or now >= self.last + self.interval or (
            self.dirty and now >= self.last + self.minimum)

    def refreshed(self, now):
        self.last, self.dirty = now, False


def _watch_loop(board, out, fd: int | None, interval: float, view: dict, draw, refresh=None) -> None:
    """Redraw on change, on keys and every `interval` seconds; returns when the user quits, or
    when view["idle_exit"] (an IdleExit, set by --exit-when-idle) has expired.

    refresh=None: draw() queries the board and the loop waits for it (tests, simple callers).
    With refresh (cmd_watch): a background thread owns the board: it waits for changes and calls
    refresh() -> snapshot (queries; may take seconds), and draw(snapshot) renders one frame from
    that snapshot without touching the board. Keys are read on this thread and redraw at once
    from the last snapshot, so they never wait for a query. A view change that needs data the
    snapshot lacks (history) shows the old frame until the next refresh, which is requested."""
    board.subscribe()
    if refresh is not None:
        return _watch_loop_threaded(board, out, fd, interval, view, draw, refresh)
    dirty, changed = True, False
    gate = _RefreshGate(interval, view.get('min_redraw', 2))
    while True:
        if dirty or gate.due(time.monotonic(), changed):
            out.write("\033[H" + "\n".join(line + "\033[K" for line in draw()) + "\033[J")
            out.flush()
            dirty = False
            gate.refreshed(time.monotonic())
        # Poll keys and board changes in short slices: a keypress redraws at once, and a
        # burst of hook updates lands in one slice, so it costs one redraw, not dozens.
        keys = _read_keys(fd)
        if keys is not None:
            if not _apply_keys(keys, view):
                return
            dirty = True
        changed = board.wait_for_change(0.1)  # coalesce bursts; keys still render immediately
        idle = view.get("idle_exit")
        if idle is not None and idle.expired():
            return


def _watch_loop_threaded(board, out, fd, interval: float, view: dict, draw, refresh) -> None:
    """The event-driven loop: a selector waits on the sources and calls the callback registered
    for each. stdin readable -> on_keys (dispatches through WATCH_KEYS/VIEW_ACTIONS, then renders
    from the cached snapshot: no query on this path). A socket the refresh thread writes to (and
    SIGWINCH writes to) -> on_event (renders). The only timer is the next wanted redraw (clock and
    idle/dead ageing), used as the selector's timeout; nothing polls for input. The refresh
    thread owns the board: LISTEN/NOTIFY waits and queries happen there, never here. It refreshes
    on a board change, every `interval` seconds, and when a render asked for data the snapshot
    lacks (SnapshotMiss). Quitting does not wait for a refresh stuck in a query (daemon thread)."""
    import selectors
    import signal
    import socket
    import threading
    state = {"snap": None, "error": None, "quit": False}
    stop, wake = threading.Event(), threading.Event()
    rd_sock, wr_sock = socket.socketpair()
    rd_sock.setblocking(False)
    wr_sock.setblocking(False)

    def poke(byte: bytes) -> None:
        try:
            wr_sock.send(byte)
        except OSError:   # full (a poke is already pending) or closed
            pass

    def worker() -> None:
        gate = _RefreshGate(interval, view.get('min_redraw', 2))
        try:
            while not stop.is_set():
                changed = board.wait_for_change(0.1)   # drains pending notifications; stop checked between
                if gate.due(time.monotonic(), wake.is_set() or changed):
                    wake.clear()
                    state["snap"] = refresh()
                    gate.refreshed(time.monotonic())
                    poke(b"s")
        except BaseException as exc:   # the loop re-raises it (BoardUnavailable: _follow reconnects)
            state["error"] = exc
            poke(b"e")

    def render() -> None:
        if state["snap"] is None:
            return
        try:
            lines = draw(_Replay(state["snap"]))
        except SnapshotMiss:
            wake.set()   # the view needs data the snapshot lacks: refresh, keep the old frame
            return
        out.write("\033[H" + "\n".join(line + "\033[K" for line in lines) + "\033[J")
        out.flush()

    def on_keys() -> None:
        data = os.read(fd, 64).decode(errors="ignore")
        if not data or not _apply_keys(data, view):   # EOF (the terminal is gone) quits too
            state["quit"] = True
            return
        render()

    def on_event() -> None:
        try:
            rd_sock.recv(4096)
        except BlockingIOError:
            pass
        if state["error"] is not None:
            raise state["error"]
        render()

    sel = selectors.DefaultSelector()
    sel.register(rd_sock, selectors.EVENT_READ, on_event)
    if fd is not None:   # (cmd_watch passes fd=None on Windows, which cannot select() on stdin)
        sel.register(fd, selectors.EVENT_READ, on_keys)
    winch_set, old_winch = False, None
    if hasattr(signal, "SIGWINCH") and threading.current_thread() is threading.main_thread():
        old_winch = signal.signal(signal.SIGWINCH, lambda *_: poke(b"r"))   # a resize re-renders
        winch_set = True
    out.write("\033[H" + _bold("loading…", False) + "\033[K")
    out.flush()
    thread = threading.Thread(target=worker, name="watch-refresh", daemon=True)
    thread.start()
    try:
        next_tick = time.monotonic()
        while not state["quit"]:
            for key, _ in sel.select(max(0.0, next_tick - time.monotonic())):
                key.data()
            if time.monotonic() >= next_tick:   # the clock and idle/dead ageing: a render, no query
                render()
                next_tick = time.monotonic() + max(interval, view.get('min_redraw', 2))
            idle = view.get("idle_exit")
            if idle is not None and idle.expired():
                return
    finally:
        stop.set()
        if winch_set:   # signal() returns None for a handler not set from Python: that is SIG_DFL
            signal.signal(signal.SIGWINCH, signal.SIG_DFL if old_winch is None else old_winch)
        thread.join(timeout=0.3)   # not for a query in flight: it is a daemon, the process is leaving
        sel.close()
        rd_sock.close()
        wr_sock.close()


def cmd_watch(cfg: dict, job: str | None, interval: float, color: bool, session: str | None = None,
              exit_when_idle: float | None = None, compact: bool = False) -> int:
    """Full-screen live view: jobs, each active job's agents, and the latest messages.

    Redraws as soon as the board changes (board.wait_for_change: new messages, agent or job
    changes) and at least every `interval` seconds, because idle and dead are derived from
    elapsed time. Finished agents older than board.watch_recent_minutes are hidden; `a` toggles.
    ↑/↓ (k/j) and PgUp/PgDn scroll the messages back into history (up to WATCH_HISTORY), G
    returns to the live tail. Survives losing the board: a one-line notice replaces the top
    line while it reconnects with back-off (q still quits), then the view carries on.
    Connects with watcher_config(cfg): [watch_database] over [database].
    session: show only the jobs activated by that host session (new ones join the view).
    exit_when_idle (with session): once the session has no active job (from startup, if it has
    none), keep the final state on screen that many seconds, then return 0 (a job activating meanwhile cancels).
    compact: the narrow layout for a side pane (_compact_frame); Left/Right keep scrolling its
    long lines sideways (Home resets), as in the full view.
    On a terminal the loop is event driven (_watch_loop_threaded): keys are callbacks from the
    WATCH_KEYS/VIEW_ACTIONS keymap, rendered at once from the last snapshot; queries, the sweep
    and LISTEN/NOTIFY run on a background refresh thread, so no key waits for the database."""
    interval = float(cfg['board'].get('watch_interval_s', 10)) if interval is None else interval
    if interval <= 0:
        raise ValueError('watch interval must be positive')
    view = {"min_redraw": cfg['board'].get('watch_min_redraw_s', 2),
            "offset": 0, "wrap": False, "max_offset": 0, "all_agents": False,
            "anchor": None, "scroll": 0, "mark": None, "page": 1,
            "recent_minutes": int(cfg["board"]["watch_recent_minutes"]),
            "db_label": watcher_db_label(cfg), "session": session, "compact": compact,
            "idle_exit": IdleExit(exit_when_idle) if session and exit_when_idle is not None else None}
    out = sys.stdout
    interactive = out.isatty() and sys.stdin.isatty() and not compat.IS_WINDOWS   # no termios: Ctrl-C quits
    fd = sys.stdin.fileno() if interactive else None
    saved = _screen_on(out, fd)

    sweeper = Sweeper(cfg)

    def run(board) -> None:
        def frame(b, v) -> list[str]:
            lines = _watch_frame(b, job, interval, color, interactive, v, _sup_or_none(cfg))
            if b.degraded and len(lines) > 1 and not lines[1]:   # the blank line under the title
                lines[1] = _bold(degraded_notice(b.degraded), color)
            return lines

        def refresh() -> _Recorder:   # on the refresh thread: every query happens here
            sweeper(board)
            return _take_snapshot(board, lambda b: frame(b, dict(view)), view, job)   # a view copy: the frame writes back clamps

        def draw(snap) -> list[str]:  # on the key thread: no query, only the last snapshot
            return frame(snap, view)
        if fd is None:   # no keys to wait on (not a terminal): the plain redraw loop is enough
            _watch_loop(board, out, fd, interval, view, lambda: draw(_Replay(refresh())))
        else:
            _watch_loop(board, out, fd, interval, view, draw, refresh)

    def notice(text: str) -> None:  # over the frame's top line; the rest of the frame stays
        out.write("\033[H" + _bold(text, color) + "\033[K")
        out.flush()

    def pause(seconds: float) -> bool:  # keys keep working while it waits; False on quit
        for _ in range(max(1, round(seconds / 0.2))):  # _read_keys waits 0.2s
            keys = _read_keys(fd)
            if keys is not None and not _apply_keys(keys, view):
                return False
        return True

    try:
        _follow(watcher_config(cfg), run, notice, pause)
    except KeyboardInterrupt:
        pass
    finally:
        _screen_off(out, fd, saved)
    return 0


# --------------------------------------------------------------------------- CLI

def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="swarm", description=__doc__.split("\n\n")[0])
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    sub = p.add_subparsers(dest="cmd", required=True)
    ini = sub.add_parser("init", help="create db/schema/name pool if missing (hooks come from the plugin)")
    ini.add_argument("--no-hooks", action="store_true", help="ignored (hooks come from the plugin)")
    sub.add_parser("install-hooks", help="obsolete: the hooks come from the plugin (prints how)")
    bs = sub.add_parser("bootstrap", help="set the swarm up for this host (run automatically by the plugin)")
    bs.add_argument("--host", choices=["claude", "codex"]); bs.add_argument("--quiet", action="store_true")
    bs.add_argument("--stamp", help=argparse.SUPPRESS)
    bs.add_argument("--no-color", action="store_true")
    mg = sub.add_parser("migrate", help="remove the old ~/.claude/skills/swarm install (hooks in settings.json, the directory)")
    mg.add_argument("--force", action="store_true")
    mg.add_argument("--no-color", action="store_true")
    sv = sub.add_parser("supervise", help="close stuck agents and restart them headless (one pass; "
                                          "the systemd timer runs it; [supervise] enabled)")
    sv.add_argument("--dry-run", action="store_true", help="print what it would do; change nothing")
    sv.add_argument("--job", help="only this job")
    svs = sv.add_subparsers(dest="scmd")
    sva = svs.add_parser("approve", help="trust a work dir's project configuration (.claude/settings*.json, "
                                          ".claude/hooks, .mcp.json, .codex, ...) for supervisor launches; "
                                          "interactive only")
    sva.add_argument("dir", help="the work dir whose current project configuration files you approve")
    nt = sub.add_parser("notices", help="what the last bootstrap left for the user (the SessionStart hook runs it)")
    nt.add_argument("--hook-output", action="store_true", required=True,
                    help="print it as SessionStart hook output (a fixed template), consuming it")
    nt.add_argument("--host", choices=["claude", "codex"])
    dr = sub.add_parser("doctor", help="check this machine's swarm setup and print a fix for each problem")
    dr.add_argument("--host", choices=["claude", "codex"])
    dr.add_argument("--no-color", action="store_true")
    # `update` is the old name of `upgrade`: a hidden alias (no help=, and left out of the usage list).
    for name in ("upgrade", "update"):
        kw = {"help": "upgrade the swarm marketplace and plugin for claude and/or codex (whichever is "
                      "installed), then bootstrap, migrate and doctor from the newly installed plugin"} \
            if name == "upgrade" else {}
        up = sub.add_parser(name, **kw)
        up.add_argument("--host", choices=["claude", "codex", "both"])
        up.add_argument("--force", action="store_true",
                        help="run bootstrap/migrate/doctor even when the plugin version didn't change "
                             "(migrate itself always runs with --force: active jobs only warn)")
        up.add_argument("--channel", choices=["release", "main"],
                        help="release: the newest vX.Y.Z tag (default); main: the tip of main. Remembered "
                             "in the config ([upgrade] channel), so a plain `swarm upgrade` keeps following it")
        up.add_argument("--no-color", action="store_true")
    j = sub.add_parser("job", help="create a job or change its description or goal; "
                                   "`job merge <from> --into <to>` merges two open jobs")
    j.add_argument("job", help="the job, or the word merge (then: job merge <from> --into <to>)")
    j.add_argument("rest", nargs="*", help=argparse.SUPPRESS)
    j.add_argument("--description")
    j.add_argument("--goal", help="set or replace the goal of an open job (a judge decides whether it is "
                                  "met); '-' reads it from stdin")
    j.add_argument("--into", metavar="JOB", help="with merge: the job that absorbs <from>")
    mv = sub.add_parser("move", help="move one live agent to another open job without stopping it")
    mv.add_argument("--as", dest="name"); mv.add_argument("--key")
    mv.add_argument("--to", required=True, metavar="JOB", help="the open job it moves to")
    jn = sub.add_parser("join"); jn.add_argument("--job", required=True)
    jn.add_argument("--key", required=True, help="stable unique id of the agent (e.g. hook agent_id)")
    jn.add_argument("--role")
    seat = jn.add_mutually_exclusive_group()
    seat.add_argument("--judge", action="store_true",
                      help="take the job's judge seat (for agents outside Claude Code, which get no hooks)")
    seat.add_argument("--verifier", action="store_true",
                      help="join as a read-only verifier (for agents outside Claude Code)")
    po = sub.add_parser("post"); po.add_argument("--job", required=True); po.add_argument("--as", dest="name", required=True)
    po.add_argument("--to"); po.add_argument("message", nargs="+")
    rd = sub.add_parser("read"); rd.add_argument("--as", dest="name"); rd.add_argument("--key")
    rd.add_argument("--job"); rd.add_argument("--peek", action="store_true", help="don't advance the cursor")
    w = sub.add_parser("who"); w.add_argument("--job", required=True)
    rm = sub.add_parser("remember", help="store a durable fact in the project's memory (needs [hindsight] url)")
    rm.add_argument("--job", required=True); rm.add_argument("--as", dest="name", required=True)
    rm.add_argument("--project", help="default: the job's explicit project, else [hindsight] default_bank")
    rm.add_argument("--create-bank", action="store_true", help="explicitly create the target bank if missing")
    rm.add_argument("fact", nargs="+")
    le = sub.add_parser("learn", help="retain self-contained job learnings in an existing bank")
    le.add_argument("--job"); le.add_argument("--bank", help="default: [hindsight] default_bank")
    le.add_argument("--list-banks", action="store_true", help="list existing Hindsight banks")
    le.add_argument("--create-bank", action="store_true", help="explicitly create the target bank if strictly necessary")
    le.add_argument("facts", nargs="?", help="use - to read facts from stdin; one fact per nonempty line")
    rc = sub.add_parser("recall", help="query configured recall_banks and any explicit job project")
    rc.add_argument("--job", required=True)
    rc.add_argument("query", nargs="*", help="default: the job task or description")
    sp = sub.add_parser("spool", help="manage the spool of queued posts and memories")
    sp.add_argument("action", choices=["retry"],
                    help="retry: requeue memories parked as .stuck after 24 hours of failing")
    lv = sub.add_parser("leave", help="release an agent's name; --session S: every unfinished agent of that "
                    "session's jobs leaves (for a session start: a restart killed them without a stop)")
    lv.add_argument("--as", dest="name"); lv.add_argument("--key"); lv.add_argument("--session")
    sub.add_parser("purge")
    tl = sub.add_parser("tail", help="follow the board live (Ctrl-C to stop)")
    tl.add_argument("--job", help="only this job (default: all jobs)")
    tl.add_argument("-n", "--backlog", type=int, default=20, help="show the last N messages first")
    tl.add_argument("--interval", type=float, default=2.0, help="poll interval in seconds (LISTEN wakes it early)")
    tl.add_argument("--no-agents", action="store_true", help="don't show agents joining/leaving")
    tl.add_argument("--no-color", action="store_true")
    ac = sub.add_parser("activate", help="turn the board on for subagents spawned from now on")
    ac.add_argument("--job", required=True); ac.add_argument("--description", help="one line")
    ac.add_argument("--task", help="the full brief; '-' reads it from stdin")
    ac.add_argument("--session", help="bind to this host session id (default: the calling Claude Code or Codex session)")
    ac.add_argument("--project", help="explicit memory project (Hindsight bank); omitted: configured general banks "
                    "(1-64 letters, digits, spaces and . ' _ -)")
    ac.add_argument("--goal", help="what \"done\" means; one judge agent decides whether it is met, "
                                   "and completion waits for its met verdict; '-' reads it from stdin")
    ac.add_argument("--adopt-running", action="store_true",
                    help="also enrol subagents that were already running (default: only ones spawned from now on)")
    ac.add_argument("--attach", action="store_true",
                    help="bind this session to an already active job without reopening it")
    ac.add_argument("--no-supervise", action="store_true",
                    help="the supervisor neither closes nor restarts this job's agents ([supervise])")
    ac.add_argument("--stall-hours", "--max-hours", dest="max_hours", type=float, metavar="N",
                    help="close the job (failed) after N hours without progress, instead of [job] "
                         "stall_hours; 0 = never (--max-hours is the old name)")
    de = sub.add_parser("deactivate", help="turn the board off for the job and close it")
    de.add_argument("--job", required=True)
    de.add_argument("--status", choices=["completed", "cancelled", "failed"], default="completed")
    de.add_argument("--outcome", help="short summary of how it ended")
    de.add_argument("--force", action="store_true",
                    help="complete a job with a goal without the judge's met verdict (recorded as forced)")
    de.add_argument("--delete-bank", action="store_true",
                    help="delete an explicit project bank only after learnings are retained elsewhere")
    vd = sub.add_parser("verdict", help="the job's judge records whether the goal is met (posted on the board)")
    vd.add_argument("--job", required=True); vd.add_argument("--as", dest="name", required=True)
    vd.add_argument("verdict", choices=["met", "not_met"])
    vd.add_argument("reason", nargs="*", help="why (met: the words after the verdict; not_met: use --reason)")
    vd.add_argument("--reason", dest="reason_opt", help="why the judge ruled so (required for not_met)")
    vd.add_argument("--next", dest="next_steps",
                    help="not_met: concrete instructions to meet the goal: what to change, where, and "
                         "what the judge will re-check (required for not_met)")
    wt = sub.add_parser("wait", help="mark an open job as waiting for something (shown by status/watch)")
    wt.add_argument("--job", required=True)
    wt.add_argument("--for", dest="for_", metavar="DURATION",
                    help="the wait expires after this long (90m, 2h, 1h30m; a bare number is minutes); "
                         "then the job is judged as not waiting")
    wt.add_argument("--on", nargs="+", required=True, help="what the job is waiting for")
    pz = sub.add_parser("pause", help="pause a job: stop new joins and posts, record every agent and "
                                      "store its final transcript, so `swarm resume` can continue it anywhere")
    pz.add_argument("--job", required=True)
    pz.add_argument("--reason", help="why (shown on the board and to the paused agents)")
    pz.add_argument("--wait", type=float, default=15.0, metavar="SECONDS",
                    help="how long to wait for other machines' agents to store their final transcript (default 15)")
    rs = sub.add_parser("resume", help="resume a paused job on this machine (re-creating its agents from the "
                                       "transcripts on the board), else: the job is no longer waiting")
    rs.add_argument("--job", required=True)
    rs.add_argument("--host", choices=["claude", "codex"],
                    help="run the resumed agents on this host (default: each agent's own; a different one "
                         "resumes from a briefing, not the transcript)")
    rs.add_argument("--workdir", help="where the agents work on this machine (default: their recorded directory "
                                      "if it exists here, else the current directory)")
    rs.add_argument("--only", nargs="+", metavar="NAME", help="resume only these agents")
    rs.add_argument("--dry-run", action="store_true", help="show what would be resumed; change nothing")
    rs.add_argument("--retry", action="store_true", help="redo the agents a previous resume of this job failed")
    st = sub.add_parser("status", help="jobs overview, or one job's agents with --job")
    st.add_argument("--job"); st.add_argument("--all", action="store_true", help="include closed jobs")
    st.add_argument("--all-agents", action="store_true",
                    help="with --job: also list finished agents older than board.watch_recent_minutes")
    st.add_argument("--no-color", action="store_true")
    wa = sub.add_parser("watch", help="live full-screen view of jobs, agents and messages (Ctrl-C to quit)")
    wa.add_argument("--job", help="focus on one job (default: all active jobs)")
    wa.add_argument("--interval", type=float, default=None, help="seconds between quiet refreshes (config watch_interval_s, default 10)")
    wa.add_argument("--session", help="show only the jobs activated by this host session id")
    wa.add_argument("--exit-when-idle", type=float, metavar="SECONDS",
                    help="with --session: once it has no active job, exit 0 after "
                         "SECONDS (a job activating meanwhile keeps it going)")
    wa.add_argument("--compact", action="store_true",
                    help="narrow layout for a side pane (50-70 columns): jobs, one line per agent, a few messages")
    wa.add_argument("--no-color", action="store_true")
    tr = sub.add_parser("transcript", help="archived agent transcripts ([transcripts] enabled)")
    trs = tr.add_subparsers(dest="tcmd", required=True)
    tls = trs.add_parser("list", help="stored transcripts: sizes, redactions, capture time")
    tls.add_argument("--job"); tls.add_argument("--agent", help="agent name")
    tls.add_argument("--no-color", action="store_true", help="never colour the table")
    tls.add_argument("--color", choices=["auto", "always"], default="auto",
                     help="colour even when stdout isn't a TTY (e.g. piped to `less -R`)")
    tsh = trs.add_parser("show", help="one transcript, as readable turns or raw JSONL")
    tsh.add_argument("--job"); tsh.add_argument("--agent", help="agent name (without --job: across jobs)")
    tsh.add_argument("--orchestrator", action="store_true", help="the orchestrating session's slice (needs --job)")
    tsh.add_argument("--key", help="agent key")
    tsh.add_argument("--format", choices=["text", "jsonl"], default="text")
    tsh.add_argument("--tail", type=int, help="only the last N turns (jsonl: lines)")
    tsh.add_argument("--grep", help="only turns (jsonl: lines) matching this regex, case-insensitive")
    tsh.add_argument("-o", "--output", help="write to this file instead of stdout")
    tsh.add_argument("--memory", metavar="DOC_ID",
                     help="the transcript excerpt a memory was saved from, and where it is in the full transcript")
    tsh.add_argument("--no-color", action="store_true", help="never colour the output (--format jsonl: never coloured anyway)")
    tsh.add_argument("--color", choices=["auto", "always"], default="auto",
                     help="colour even when stdout isn't a TTY (e.g. piped to `less -R`)")
    tex = trs.add_parser("export", help="every transcript of a job as .jsonl files plus index.tsv")
    tex.add_argument("--job", required=True)
    tex.add_argument("dir", nargs="?", help="default: ./transcripts-<job>")
    tex.add_argument("--force", action="store_true",
                     help="export into a directory sandboxed agents can write (refused by default)")
    mem = sub.add_parser("memory", help="memories swarm agents saved, and where they came from")
    mems = mem.add_subparsers(dest="mcmd", required=True)
    mr = mems.add_parser("refs", help="recorded memory references (provenance)")
    mr.add_argument("--job"); mr.add_argument("--agent", help="agent name")
    mr.add_argument("--check", action="store_true", help="ask Hindsight whether each memory still exists")
    hk = sub.add_parser("hook"); hk.add_argument("--host", choices=["claude", "codex"]); hk.add_argument("event", choices=["start", "turn", "done", "stop", "session-start", "session-stop"])
    sub.metavar = "{" + ",".join(k for k in sub.choices if k != "update") + "}"   # hide the alias
    return p


def _use_color(args) -> bool:
    if os.environ.get("NO_COLOR"):   # https://no-color.org: any non-empty value opts out
        return False
    return sys.stdout.isatty() and not args.no_color


def _transcript_use_color(args) -> bool:
    """TTY only (or --color=always), never with NO_COLOR/--no-color, never writing to --output
    (a file, not a terminal), and never for --format jsonl (checked by the callers, not here)."""
    if os.environ.get("NO_COLOR") or args.no_color:
        return False
    if getattr(args, "output", None):
        return False
    return args.color == "always" or sys.stdout.isatty()


def _marker(cfg: dict, job: str) -> Path:
    """The marker file that switches the hooks on for `job` (nothing is created: _write_marker
    creates the directory, through safefs)."""
    mdir = Path(cfg["hook"]["marker_dir"]).expanduser()
    return mdir / (safe_job(job) + ".json")


def _print_model_hint(cfg: dict) -> None:
    """When the calling host can't rewrite spawn input (Codex before it was confirmed to,
    or a Codex build without it), tell the orchestrator which model each role should get."""
    from swarm import hosts as _hosts, models
    h = _hosts.detect_cli_host(os.environ)
    try:
        rewrite = _hosts.get(h).supports_spawn_model_rewrite if h else True
    except KeyError:
        rewrite = True
    hint = models.spawn_hint(cfg, h) if h and not rewrite else None
    if hint:
        print(hint)


def _print_activation_footer(cfg: dict, details_fn) -> None:
    """The `swarm command:` line, then the activation-specific lines printed by `details_fn`,
    then the spawn-model hint when the calling host can't take the models from the hook.
    Shared by plain activate and --attach."""
    from swarm import hosts, paths
    print(f"swarm command: {paths.agent_bin()}")
    details_fn()
    _print_model_hint(cfg)


# A host session id as `activate` accepts it: Claude Code and Codex use UUIDs; this also keeps
# the plain ids the tests and scripts use. Never a glob character, a dot, a slash or a control.
SESSION_ID = None


def valid_session_id(session: str) -> bool:
    import re
    global SESSION_ID
    SESSION_ID = SESSION_ID or re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")
    return isinstance(session, str) and SESSION_ID.fullmatch(session) is not None


def cmd_activate(cfg: dict, args) -> int:
    from swarm.board import open_board
    from swarm import hosts, safefs
    from swarm.hooks import JOB_NAME
    if not JOB_NAME.fullmatch(args.job or ""):
        # the name is shown to agents inside shell commands and names marker files
        print(f"swarm activate: --job must be 1-64 letters, digits, '.', '_' or '-', starting with a "
              f"letter or digit, not {term_safe(args.job)[:80]!r}", file=sys.stderr)
        return 2
    if args.project is not None:
        from swarm.board.base import check_name
        try:   # the project names the job's Hindsight bank and is printed in the [memory ...] tag
            check_name(args.project, "project")   # the hook reads: no quote, bracket, control
        except ValueError as exc:
            print(f"swarm activate: --project: {term_safe(exc)}", file=sys.stderr)
            return 2
    marker = _marker(cfg, args.job)
    session = args.session or hosts.cli_session_id(os.environ)
    if session is not None and not valid_session_id(session):
        # the id is later looked up as a transcript file name: a plain id only
        print(f"swarm activate: --session must be a session id (a UUID: letters, digits, - and _, "
              f"at most 128), not {term_safe(session)[:80]!r}", file=sys.stderr)
        return 2
    try:   # the marker dir is sandbox-writable: refuse a symlinked or foreign one before any change
        os.close(safefs.open_base(marker.parent, create=True))
    except (OSError, ValueError) as exc:
        print(f"swarm activate: can't use the marker dir {term_safe(marker.parent)}: {term_safe(exc)}",
              file=sys.stderr)
        return 1
    if session and hosts.detect_cli_host(os.environ) == "codex":
        # Codex encrypts spawn messages, so a child's job tag can't be read: one job per session
        # (activate and --attach alike)
        other = next((m.get("job") for m in map(_read_marker, sorted(marker.parent.glob("*.json")))
                      if m.get("session_id") == session and m.get("job") not in (None, args.job)), None)
        if other:
            print(f'swarm activate: "{term_safe(other)}" is already active in this Codex session, and Codex '
                  f"subagents can't say which job they belong to (their prompts are encrypted); "
                  f"deactivate it first, or activate {args.job} from another session",
                  file=sys.stderr)
            return 1
    if args.attach:
        with open_board(cfg) as board:
            js = board.job_status(args.job)
        if js is None or js.status != "active":
            print(f"swarm activate --attach: job {args.job} is not active", file=sys.stderr)
            return 1
        marker = marker.with_name(f"{safe_job(args.job)}--{safe_job(session or 'unbound')}.json")
        _write_marker(marker, {"job": args.job, "session_id": session, "cwd": os.getcwd(),
                               "adopt_running": args.adopt_running, "attached": True,
                               **({"goal": True} if js.goal else {})})   # a new binding starts unseen
        _print_activation_footer(cfg, lambda: print(
            f"attached this session to {args.job}: its subagents spawned from now on join the board\n"
            f"put this line in every subagent prompt for this job:\n{tag_line(args.job)}"))
        return 0
    if args.max_hours is not None and args.max_hours < 0:
        print("swarm activate: --stall-hours must be 0 (never) or more", file=sys.stderr)
        return 2
    if args.task == "-" and args.goal == "-":
        print("swarm activate: only one of --goal and --task can be - (stdin)", file=sys.stderr)
        return 2
    task = sys.stdin.read() if args.task == "-" else args.task
    goal = (sys.stdin.read() if args.goal == "-" else args.goal or "").strip() or None
    # Run from Claude Code (the orchestrator's Bash tool), the job is bound to the calling
    # session at once: no other session can claim it, and it can share the session with others.
    with open_board(cfg) as board:
        board.purge()
        board.open_job(args.job, args.description, task, session, os.environ.get("USER"),
                       project=args.project or "", goal=goal)
        board.set_job_supervise(args.job, not args.no_supervise)
        if args.max_hours is not None:
            board.set_job_max_hours(args.job, args.max_hours)
        closed = _sweep(board, cfg)   # after open_job: never the job being activated
    _write_marker(marker, {"job": args.job, "session_id": session, "cwd": os.getcwd(),
                           "adopt_running": args.adopt_running,
                           **({"goal": True} if goal else {})})   # a new run starts unseen
    _say_closed(closed)

    def _details():
        print(f"activated {args.job}: subagents spawned from now on join the board\n"
              f"put this line in every subagent prompt for this job (it picks the job when this "
              f"session runs several):\n{tag_line(args.job)}")
        if goal:
            print(f"the job has a goal: spawn exactly one judge, with this line too in its prompt; it "
                  f"can't complete until the judge's verdict is met:\n{JUDGE_TAG_LINE}")
        print(f"optional: read-only verifiers that check the others' claims carry this line too:\n"
              f"{VERIFIER_TAG_LINE}")

    _print_activation_footer(cfg, _details)
    return 0


MARKER_MAX_BYTES = 64 * 1024


def _read_marker(path: Path) -> dict:
    """The marker's JSON object, or {} (missing, not JSON, not an object, or not a plain file
    of ours). The marker dir is sandbox-writable: read through safefs, so a FIFO doesn't
    block, a link isn't followed, and hard-linked or oversized files are skipped."""
    from swarm import safefs
    try:
        d = safefs.open_base(str(Path(path).parent), create=False)
    except (OSError, ValueError):
        return {}
    try:
        data = safefs.read(d, Path(path).name, limit=MARKER_MAX_BYTES)
    except ValueError:
        data = None
    finally:
        os.close(d)
    try:
        out = json.loads(data) if data is not None else {}
    except ValueError:
        return {}
    return out if isinstance(out, dict) else {}


def _completion_refusal(js) -> str | None:
    """Why a job with a goal can't be completed yet (None if it can)."""
    from swarm.board import goal_unmet
    if js is None or not goal_unmet(js):
        return None
    latest = (f"latest verdict: {term_safe(js.verdict)} by {term_safe(js.verdict_by)}: "
              f"{term_safe(js.verdict_reason)}"
              + (f"; next: {term_safe(js.verdict_next)}" if js.verdict_next and js.verdict == "not_met" else "")
              if js.verdict
              else "no verdict yet" + ("" if js.judge else "; no judge on the job"))
    return (f"not completing {js.job}: the judge has not recorded a met verdict ({latest}). "
            f"Keep working until it does, close it with --status cancelled or failed, or "
            f"override with --force (recorded).")


def cmd_deactivate(cfg: dict, args) -> int:
    from swarm.board import AUTO_CLOSED_BY, open_board
    marker = _marker(cfg, args.job)
    completing = args.status == "completed"
    board = None
    try:
        board = open_board(cfg)
        js = board.job_status(args.job)
    except Exception as exc:
        if board is not None:
            board.close()
        board, js, down = None, None, exc
        # Without the database the verdict is unknown: a job with a goal stays open unless forced.
        if completing and _read_marker(marker).get("goal") and not args.force:
            print(f"not completing {args.job}: cannot check the judge's verdict (board not reachable: "
                  f"{_error_name(exc)}). Retry, or override with --force.", file=sys.stderr)
            return 1
    refusal = _completion_refusal(js) if completing else None
    if refusal and not args.force:
        board.close()
        print(refusal, file=sys.stderr)
        return 1
    if args.delete_bank:
        try:
            _delete_learned_bank(board, cfg, js, args.job)
        except Exception as exc:
            if board is not None:
                board.close()
            print(f"bank not deleted; job remains unchanged: {term_safe(exc)}", file=sys.stderr)
            return 1
    # first, so the board goes quiet even if the db write fails; never unlinked without its lock
    markers = [marker] + [m for m in marker.parent.glob(f"{safe_job(args.job)}--*.json")
                          if _read_marker(m).get("job") == args.job]
    stuck = [m for m in markers if not remove_marker(m)]
    if stuck:
        if board is not None:
            board.close()
        print(f"not deactivated: the marker{'s' if len(stuck) > 1 else ''} "
              f"{', '.join(term_safe(m) for m in stuck)} stayed locked for {MARKER_REMOVE_WAIT:g}s "
              f"(a hook holding it?); job {args.job} is still active. Retry.", file=sys.stderr)
        return 1
    if board is None:
        print(f"deactivated {args.job}; could not record status ({_error_name(down)})", file=sys.stderr)
        _learning_instructions(args.job)
        return 0
    forced = bool(refusal)
    try:
        with board:
            known = board.close_job(args.job, args.status, args.outcome, forced=forced,
                                    closed_by=os.environ.get("USER") or None)
            if known and transcripts_enabled(cfg):
                _capture_final_transcripts(board, cfg, args.job)
        how = args.status + (", forced without a met verdict" if forced else "")
        if js is not None and js.status != "active":   # e.g. replacing an auto-close outcome
            how += f"; it was already {js.status}" + (", auto-closed" if js.closed_by == AUTO_CLOSED_BY else "")
        print(f"deactivated {args.job} ({how})" if known
              else f"deactivated {args.job} (no such job in the database)")
    except Exception as exc:
        print(f"deactivated {args.job}; could not record status ({_error_name(exc)})",
              file=sys.stderr)
    _learning_instructions(args.job)
    return 0


def _learning_instructions(job: str) -> None:
    print(f"Required learnings step for {job}: distill what the job learned into self-contained facts. "
          "Run `swarm learn --list-banks` and retain them in the best-matching EXISTING bank, "
          f"strongly preferring banks that already cover the topic: `swarm learn --job {job} --bank BANK -`. "
          "Create a bank only when strictly necessary, with explicit --create-bank.")


def _delete_learned_bank(board, cfg: dict, js, job: str) -> None:
    """Only explicit project banks with confirmed exported learnings may be deleted.
    The durable provenance survives marker removal and reopening a CLI process. Check
    its Hindsight document too: a queued, failed or subsequently deleted export is no proof.
    """
    from swarm import hindsight
    if board is None or js is None or not js.project:
        raise ValueError("--delete-bank requires a job with an explicit --project bank")
    bank = hindsight.bank_id(js.project)
    general = hindsight.recall_banks(cfg)
    general.append(hindsight.bank_id(cfg["hindsight"].get("default_bank", "coding")))
    if bank in general:
        raise ValueError("refusing to delete a configured general memory bank")
    client = hindsight.Client(cfg)
    start = js.activated_at or js.created_at
    refs = [r for r in board.memory_refs(job=job)
            if r.writer == "swarm-learn" and r.bank != bank and r.created_at >= start]
    for ref in refs:
        doc = client.document(ref.bank, ref.document_id)
        meta = (doc or {}).get("document_metadata") or {}
        if meta.get("job") == job and meta.get("learning") == "true":
            client.delete_bank(bank)
            print(f'deleted explicit project bank "{bank}"; learnings retained in "{ref.bank}"')
            return
    raise ValueError(f"retain learnings elsewhere first: swarm learn --job {job} --bank EXISTING_BANK -")


def _capture_final_transcripts(board, cfg: dict, job: str) -> None:
    """Deactivate: the job's final transcripts (orchestrator slice and this machine's agents).
    A failure is reported on stderr; the job is closed either way."""
    from swarm import transcripts
    try:
        transcripts.capture_job(board, cfg, job, True,
                                warn=lambda msg: print(term_safe(msg), file=sys.stderr))
    except Exception as exc:
        print(f"transcripts of {job} not captured ({_error_name(exc)}: {exc})", file=sys.stderr)


def _cmd_install_hooks(cfg: dict, args) -> int:
    print("swarm: the hooks come from the plugin now (Claude Code: /plugin; Codex: codex plugin); "
          "nothing written. `swarm migrate` removes old settings.json entries.")
    return 0


def _cmd_supervise(cfg: dict, args) -> int:
    try:
        compat.require_posix("swarm supervise")
    except compat.Unsupported as exc:
        print(f"swarm supervise: {exc}", file=sys.stderr)
        return 1
    if getattr(args, "scmd", None) == "approve":
        return cmd_supervise_approve(cfg, args)
    from swarm.supervisor.command import cmd_supervise
    return cmd_supervise(cfg, args)


# Set in a harness session (Claude Code, Codex): approval is refused there. An agent's shell can
# unset them, but has no terminal on stdin: the TTY check is the real gate.
HARNESS_SESSION_VARS = ("CLAUDE_CODE_SESSION_ID", "CLAUDECODE", "CODEX_THREAD_ID", "CODEX_SESSION_ID")
APPROVE_WORD = "approve"


def cmd_supervise_approve(cfg: dict, args) -> int:
    """Approve the project configuration files of a work dir for supervisor launches. The
    supervisor refuses to start a replacement in a dir holding project config it has no approval
    for, because a sandboxed agent could have planted it; this is the out-of-band consent. So it
    must come from the user at a terminal: stdin a TTY, no harness session in the environment,
    and the word typed back. The listing and the store are the supervisor's
    (command.approval_candidates / save_approvals)."""
    def refuse(why: str) -> int:
        print(f"swarm supervise approve: not approved: {why}", file=sys.stderr)
        return 1

    in_session = [v for v in HARNESS_SESSION_VARS if os.environ.get(v)]
    if in_session:
        return refuse(f"run it yourself in a terminal, not from an agent session ({', '.join(in_session)} set)")
    if not sys.stdin.isatty():
        return refuse("it needs you at a terminal (stdin is not a TTY)")
    from swarm.supervisor import command
    try:
        entries = command.approval_candidates(cfg, args.dir)
    except (OSError, ValueError, RuntimeError) as exc:   # not an allowed dir, unapprovable, unsafe
        print(f"swarm supervise approve: {term_safe(exc)}", file=sys.stderr)
        return 1
    if not entries:
        print(f"nothing to approve in {term_safe(args.dir)}: no project configuration files there")
        return 0
    print("These files configure Claude Code or Codex sessions started in this directory:")
    for e in entries:
        print(f"  {term_safe(e.get('dir'))}/{term_safe(e.get('file'))}  sha256 {term_safe(e.get('sha256'))}")
    print("Approve them only if you wrote them or reviewed them: a supervisor replacement will run with "
          "them, unsandboxed. A later change to any of them needs a new approval.")
    print(f"Type {APPROVE_WORD} to approve: ", end="", flush=True)
    answer = sys.stdin.readline().strip()
    if answer != APPROVE_WORD:
        print()
        return refuse(f"you typed {term_safe(answer)[:40]!r}, not {APPROVE_WORD!r}")
    try:
        command.save_approvals(entries)
    except (OSError, ValueError, RuntimeError) as exc:   # PrivateDirError, or an entry it won't store
        print(f"swarm supervise approve: could not save the approval ({term_safe(exc)})", file=sys.stderr)
        return 1
    print(f"approved {len(entries)} file{'s' if len(entries) != 1 else ''}")
    return 0


def _hook_output_ok(text: str) -> bool:
    """Whether `text` is one line of SessionStart hook output: a JSON object with only
    systemMessage and hookSpecificOutput (anything else is not printed)."""
    if not isinstance(text, str) or "\n" in text.strip():
        return False
    try:
        out = json.loads(text)
    except ValueError:
        return False
    return isinstance(out, dict) and bool(out) and set(out) <= {"systemMessage", "hookSpecificOutput"}


def cmd_notices(cfg: dict, args) -> int:
    """SessionStart: print what the last bootstrap left for the user as hook output. The
    notice lives in the host-private dir and bootstrap.hook_output re-validates it into a fixed
    template; this only prints it. Never fails the hook."""
    try:
        from swarm import bootstrap
        text = bootstrap.hook_output(args.host)
    except Exception:
        return 0
    if text is not None and _hook_output_ok(text):
        print(text.strip())
    return 0


def cmd_bootstrap(cfg: dict, args) -> int:
    from swarm import bootstrap
    steps = bootstrap.bootstrap(args.host, config=args.config, stamp=Path(args.stamp) if args.stamp else None)
    shown = [s for s in steps if not args.quiet or s.status not in ("ok", "skipped")]
    if shown:
        print(bootstrap.format_steps(shown, _use_color(args)))
    return 1 if any(s.status == "failed" for s in steps) else 0


def cmd_doctor(cfg: dict, args) -> int:
    from swarm import bootstrap, hosts
    checks = bootstrap.doctor(args.host or hosts.detect_cli_host(os.environ), config=args.config)
    print(bootstrap.format_checks(checks, _use_color(args)))
    return 1 if any(c.ok is False for c in checks) else 0


def cmd_update(cfg: dict, args) -> int:
    from swarm import update
    return update.run_update(args.host, args.force, _use_color(args), config_path=args.config,
                             channel=args.channel)


def cmd_migrate(cfg: dict, args) -> int:
    from swarm import bootstrap
    settings = claude_settings_path()
    try:   # the markers of the config in use (--config), so an active job there is seen
        # cfg: the old-default board and spool moves
        steps = bootstrap.migrate(force=args.force, settings_path=settings, marker_dir=bootstrap.marker_dir_of(cfg),
                                  cfg=cfg)
    except json.JSONDecodeError as exc:
        # a malformed settings.json: which file and where, never its content; nothing was written
        print(f"swarm migrate: {settings} is not valid JSON ({exc.msg} at line {exc.lineno} "
              f"column {exc.colno}); fix it and retry", file=sys.stderr)
        return 1
    except (OSError, ValueError) as exc:
        detail = f"{exc.strerror}: {exc.filename}" if isinstance(exc, OSError) and exc.strerror else type(exc).__name__
        print(f"swarm migrate: failed ({detail})", file=sys.stderr)
        return 1
    print(bootstrap.format_steps(steps, _use_color(args)))
    return 1 if any(s.status in ("failed", "refused") for s in steps) else 0


def cmd_remember(cfg: dict, args) -> int:
    """Store one fact in the job's project memory; spool it when the board or Hindsight can't
    be reached from here (the hooks deliver it, like a spooled post). The memory gets a document
    id picked here and the provenance this process knows; every line that stored or
    queued it ends with `[memory <document_id> project "<project>"]`, which the PostToolUse hook
    reads to pin the memory to this tool call."""
    if not str((cfg.get("hindsight") or {}).get("url") or "").strip():
        print("project memory is off: set [hindsight] url in the swarm config", file=sys.stderr)
        return 1
    from swarm import hindsight, provenance
    from swarm.board import open_board
    from swarm.spool import flush_spool, spool_memory
    text = " ".join(" ".join(args.fact).split())
    cap = int(cfg["hindsight"]["remember_max_chars"])
    truncated = len(text) > cap
    text = text[: cap - 1] + "…" if truncated else text
    if not text:
        print("nothing to remember", file=sys.stderr)
        return 1
    from swarm.board.base import valid_name
    if args.project is not None and not valid_name(args.project):
        # the project is printed in the [memory ...] tag the hook reads: nothing that could forge one
        print(term_safe(f"invalid project {args.project!r}: use 1-64 letters, digits, spaces and "
                        f". ' _ - (no leading or trailing space)"), file=sys.stderr)
        return 1
    meta = provenance.cli_metadata(os.environ)
    doc = provenance.new_document_id()

    def queue(project: str | None) -> str:
        f = spool_memory(cfg, args.job, args.name, text, project, metadata=meta,
                         create_bank=args.create_bank)
        return " " + provenance.output_tag(f"swarm-spool-{f.stem}", project or "")

    try:
        board_cm = open_board(cfg)
    except Exception as exc:
        tag = queue(args.project)
        print(term_safe(f"queued (board not reachable from here: {_error_name(exc)}); it is stored "
                        f"automatically within seconds by the swarm hooks. This is normal inside a "
                        f"sandbox.{tag}"))
        return 0
    with board_cm as board:
        flush_spool(board, cfg)
        project = args.project
        try:
            project = project or hindsight.project_of(board.job_status(args.job), args.job, cfg)
            if not valid_name(project):   # set by `swarm activate --project` before this check existed
                print(term_safe(f"invalid project {project!r} of job {args.job}: pass --project with "
                                f"1-64 letters, digits, spaces and . ' _ -"), file=sys.stderr)
                return 1
            project = hindsight.remember(board, cfg, args.job, args.name, text, project,
                                         document_id=doc, metadata=meta,
                                         create_bank=args.create_bank)
        except hindsight.HindsightUnavailable as exc:
            tag = queue(project)
            board.record_remembered(args.name)
            print(term_safe(f"queued (memory not reachable from here: {exc}); the swarm hooks store it "
                            f"once Hindsight answers.{tag}"))
            return 0
        except hindsight.HindsightError as exc:
            if not exc.bank_scoped:  # a 4xx: this fact as sent is refused; say why
                print(term_safe(f"memory refused it: {exc}"), file=sys.stderr)
                return 1
            tag = queue(project)  # the bank's trouble
            board.record_remembered(args.name)
            print(term_safe(f"queued (memory refused it for now: {exc}); the swarm hooks retry it.{tag}"))
            return 0
    print(term_safe(f'remembered in project "{project}"' + (f" (truncated to {cap} chars)" if truncated else "")
                    + " " + provenance.output_tag(doc, project)))
    return 0


def cmd_learn(cfg: dict, args) -> int:
    """Retain one self-contained fact per stdin line with durable provenance.
    Unlike remember, this never queues or truncates: success certifies a completed
    retain, which is required before a disposable project bank may be deleted.
    """
    from swarm import hindsight, provenance, hosts
    from swarm.board import open_board
    from swarm.board.base import MemoryRef, valid_name
    if not hindsight.enabled(cfg):
        print("memory is off: set [hindsight] url in the swarm config", file=sys.stderr)
        return 1
    client = hindsight.Client(cfg)
    if args.list_banks:
        if args.job or args.bank or args.facts or args.create_bank:
            print("swarm learn --list-banks cannot be combined with retain options", file=sys.stderr)
            return 2
        try:
            print("\n".join(client.list_banks()))
        except (hindsight.HindsightError, hindsight.HindsightUnavailable) as exc:
            print(f"cannot list banks: {term_safe(exc)}", file=sys.stderr)
            return 1
        return 0
    if not args.job or args.facts != "-":
        print("usage: swarm learn --job JOB [--bank BANK] [--create-bank] -", file=sys.stderr)
        return 2
    bank = args.bank or cfg["hindsight"].get("default_bank", "coding")
    if not valid_name(bank):
        print("invalid bank: use 1-64 letters, digits, spaces and . ' _ -", file=sys.stderr)
        return 2
    facts = [line.strip() for line in sys.stdin.read().splitlines() if line.strip()]
    cap = int(cfg["hindsight"]["remember_max_chars"])
    if not facts:
        print("no learnings supplied", file=sys.stderr)
        return 1
    if any(len(fact) > cap for fact in facts):
        print(f"learning exceeds {cap} characters; split it into self-contained facts", file=sys.stderr)
        return 1
    # The hook's short timeout is unsuitable for synchronous fact extraction. This
    # explicit CLI step can wait; recall and normal remember keep their own budgets.
    learn_cfg = {**cfg, "hindsight": {**cfg["hindsight"], "timeout_seconds":
                                    max(120.0, float(cfg["hindsight"]["timeout_seconds"]))}}
    try:
        with open_board(cfg) as board:
            js = board.job_status(args.job)
            if js is None:
                print(f"no such job: {term_safe(args.job)}", file=sys.stderr)
                return 1
            meta = {**provenance.cli_metadata(os.environ), "learning": "true"}
            name = "orchestrator"
            retained = []
            for fact in facts:
                doc = provenance.new_document_id()
                hindsight.remember(board, learn_cfg, args.job, name, fact, bank,
                                   document_id=doc, metadata=meta, create_bank=args.create_bank,
                                   synchronous=True)
                retained.append(doc)
            # A partial batch must not authorise deletion: publish the durable evidence
            # only after every fact in this invocation has been retained successfully.
            for doc in retained:
                board.save_memory_ref(MemoryRef(
                    document_id=doc, bank=hindsight.bank_id(bank), job=args.job,
                    agent_key=f"learn:{js.session_id or args.job}", agent_name=name,
                    harness=hosts.detect_cli_host(os.environ), host=compat.node(),
                    session_id=hosts.cli_session_id(os.environ) or js.session_id,
                    tool_call_id=None, writer="swarm-learn"))
                print(term_safe(f'learned in bank "{bank}" ' + provenance.output_tag(doc, bank)))
    except (hindsight.HindsightError, hindsight.HindsightUnavailable) as exc:
        print(f"learning not retained: {term_safe(exc)}; retry before deleting the project bank",
              file=sys.stderr)
        return 1
    return 0


def cmd_recall(cfg: dict, args) -> int:
    from swarm import hindsight
    from swarm.board import open_board
    if not hindsight.enabled(cfg):
        print("memory is off: set [hindsight] url in the swarm config", file=sys.stderr)
        return 1
    with open_board(cfg, readers=True) as board:
        js = board.job_status(args.job)
    banks = hindsight.recall_banks(cfg, js)
    query = " ".join(args.query) or hindsight.recall_query(js, args.job)
    items = hindsight.Client(cfg).recall_many(
        banks, query, on_error=lambda bank, exc: print(
            f'recall bank "{bank}" failed: {term_safe(exc)}', file=sys.stderr))
    text, _ = hindsight.format_memories(items, ", ".join(banks), cfg, "recalled memories")
    print(text or "(no memories)")
    return 0


def cmd_spool_retry(cfg: dict) -> int:
    """Requeue the memories parked as .stuck; the next flush (any hook call) delivers them. Works
    on the spool directory only, so it runs inside a sandbox too."""
    from swarm.spool import retry_stuck
    n = retry_stuck(cfg)
    print(f"requeued {n} stuck {'memory' if n == 1 else 'memories'}")
    return 0


def _cmd_hook(cfg: dict, args) -> int:
    if args.event == "session-start":
        return 0   # handled by bin/swarm-hook's shell part
    from swarm.hooks import run_hook
    return run_hook(args.event, cfg, args.host)


# Commands that manage their own board connection (or need none).
COMMANDS = {
    "init": cmd_init,
    "install-hooks": _cmd_install_hooks,
    "supervise": _cmd_supervise,
    "bootstrap": cmd_bootstrap,
    "migrate": cmd_migrate,
    "doctor": cmd_doctor,
    "upgrade": cmd_update,
    "update": cmd_update,      # the old name, a hidden alias
    "activate": cmd_activate,
    "deactivate": cmd_deactivate,
    "hook": _cmd_hook,
    "notices": cmd_notices,
    "remember": cmd_remember,
    "learn": cmd_learn,
    "recall": cmd_recall,
    "spool": lambda cfg, args: cmd_spool_retry(cfg),
    "tail": lambda cfg, args: cmd_tail(cfg, args.job, args.backlog, args.interval, not args.no_agents,
                                       _use_color(args)),
    "watch": lambda cfg, args: cmd_watch(cfg, args.job, args.interval, _use_color(args),
                                       args.session, args.exit_when_idle, args.compact),
}


SYSTEM_NAME = "swarm"   # who posts the board notices of a move, a merge or a goal change


def _job_markers(cfg: dict, job: str) -> list[Path]:
    """Every marker of `job` in this machine's marker dir: <job>.json and the <job>--<session>.json
    attachments."""
    marker = _marker(cfg, job)
    found = [marker] if marker.exists() else []
    found += [m for m in sorted(marker.parent.glob(f"{safe_job(job)}--*.json")) if _read_marker(m).get("job") == job]
    return [m for m in found if _read_marker(m).get("job") == job]


def _bind_sessions(cfg: dict, job: str, sessions, goal: bool) -> None:
    """Make `job` visible to the hooks of each session (an agent's hooks act only for jobs whose
    marker is bound to its Claude session): an attached marker (`activate --attach`) for a session
    that has none for it."""
    have = {_read_marker(m).get("session_id") for m in _job_markers(cfg, job)}
    for sid in sorted({x for x in sessions if x and valid_session_id(x)} - have):
        _write_marker(_marker(cfg, job).with_name(f"{safe_job(job)}--{safe_job(sid)}.json"),
                      {"job": job, "session_id": sid, "cwd": os.getcwd(), "adopt_running": False,
                       "attached": True, **({"goal": True} if goal else {})})


def _mark_goal(cfg: dict, job: str) -> None:
    """The job has a goal now: its markers say so (the orchestrator hooks and deactivate read it)."""
    for m in _job_markers(cfg, job):
        data = _read_marker(m)
        if data and not data.get("goal"):
            _write_marker(m, {**data, "goal": True})


def _read_goal(args) -> str | None:
    goal = (sys.stdin.read() if args.goal == "-" else args.goal or "").strip()
    return goal or None


def _set_goal(board, cfg: dict, job: str, goal: str) -> int:
    js = board.job_status(job)
    had = js.goal if js else None
    if not board.set_job_goal(job, goal):
        print(f"swarm job: {job} is not an open job: its goal can't be changed", file=sys.stderr)
        return 1
    _mark_goal(cfg, job)
    if had != goal:
        board.post(job, SYSTEM_NAME, "the job's goal was " + ("changed" if had else "set")
                   + f" (see `swarm status --job {job}`): " + goal[:100].replace("\n", " "))
    js = board.job_status(job)
    print(f"goal of {job} " + ("unchanged" if had == goal else "set" if not had else "updated")
          + ("; the earlier verdict is cleared" if had and had != goal else ""))
    if js and js.judge is None:
        print(f"the job has no judge: spawn exactly one, with this line in its prompt (with the job's "
              f"tag line {tag_line(job)}); the job can't complete until its verdict is met:\n{JUDGE_TAG_LINE}")
    elif js and had != goal:
        print(f"{js.judge} is the judge: it was told on the board that the goal changed")
    return 0


def _board_job(board, cfg: dict, args) -> int | None:
    if args.job == "merge" and (args.rest or args.into):
        return _job_merge(board, cfg, args)
    if args.rest or args.into:
        print("swarm job: unexpected arguments (merge: swarm job merge <from> --into <to>)", file=sys.stderr)
        return 2
    goal = _read_goal(args) if args.goal is not None else None
    if args.goal is not None and goal is None:
        print("swarm job: --goal is empty", file=sys.stderr)
        return 2
    js = board.job_status(args.job)
    if goal is not None and js is not None and js.status != "active":
        print(f"swarm job: {args.job} is {js.status}: its goal can't be changed", file=sys.stderr)
        return 1
    board.ensure_job(args.job, args.description, os.environ.get("USER"))
    print(args.job)
    if goal is not None:
        return _set_goal(board, cfg, args.job, goal)


def _active_agents(board) -> list:
    """Every active agent of every open job."""
    return [a for j in board.jobs() for a in board.agents(j.job, include_departed=False)]


def _find_agent(board, name: str | None, key: str | None):
    for a in _active_agents(board):
        if (key and a.agent_key == key) or (name and not key and a.name == name):
            return a
    return None


def _sessions_of(board, agent_keys) -> set:
    return {board.route(k).session_id for k in agent_keys}


def _job_merge(board, cfg: dict, args) -> int:
    src, dst = (args.rest[0] if len(args.rest) == 1 else None), args.into
    if src is None or not dst or args.goal is not None:
        print("usage: swarm job merge <from> --into <to>", file=sys.stderr)
        return 2
    if src == dst:
        print(f"refused: can't merge {src} into itself", file=sys.stderr)
        return 1
    sj, dj = board.job_status(src), board.job_status(dst)
    for label, j, jn in (("from", sj, src), ("into", dj, dst)):
        if j is None:
            print(f"refused: no such job {term_safe(jn)} ({label})", file=sys.stderr)
            return 1
        if j.status != "active":
            print(f"refused: {jn} is {j.status}: "
                  + ("it is already closed" if label == "from" else "can't merge into a closed job"),
                  file=sys.stderr)
            return 1
    agents = board.agents(src, include_departed=False)
    src_markers = _job_markers(cfg, src)
    sessions = {_read_marker(m).get("session_id") for m in src_markers} | _sessions_of(board, [a.agent_key for a in agents])
    goal = dj.goal
    if sj.goal and sj.goal != dj.goal:
        goal = f"{dj.goal}\n{sj.goal}" if dj.goal else sj.goal
        board.set_job_goal(dst, goal)
    moved = [a for a in agents if board.move_agent(a.agent_key, dst) is not None]
    board.set_waiting(dst, None)
    _bind_sessions(cfg, dst, sessions, bool(goal))
    if goal:
        _mark_goal(cfg, dst)
    names = ", ".join(a.name for a in moved) or "no agents"
    board.post(dst, SYSTEM_NAME, f"merged job {src} into this job: {names} moved here"
               + (", its goal appended" if sj.goal and sj.goal != dj.goal else ""))
    stuck = [m for m in src_markers if not remove_marker(m)]
    board.close_job(src, "completed", f"merged into {dst}", closed_by=os.environ.get("USER") or None)
    if transcripts_enabled(cfg):
        _capture_final_transcripts(board, cfg, src)
    print(f"merged {src} into {dst}: {len(moved)} agent(s) moved ({names}); {src} is closed (completed, "
          f"outcome \"merged into {dst}\"). The agents keep running: each sees {dst}'s board on its next tool call.")
    if sj.goal and sj.goal != dj.goal:
        print(f"{src}'s goal was appended to {dst}'s")
    if sj.judge:
        print(f"{sj.judge} was {src}'s judge: it is now a normal member of {dst} (not stopped). Stop it "
              f"if it is no longer needed.")
    if goal and dj.judge is None:
        print(f"{dst} has a goal but no judge: spawn exactly one, with {tag_line(dst)} and this line "
              f"in its prompt:\n{JUDGE_TAG_LINE}")
    for m in stuck:
        print(f"note: marker {term_safe(m)} stayed locked; `swarm deactivate --job {src}` removes it later",
              file=sys.stderr)
    print(f"put this line in every subagent prompt for the merged job from now on:\n{tag_line(dst)}")
    return 0


def _board_move(board, cfg: dict, args) -> int:
    if bool(args.name) == bool(args.key):
        print("swarm move: give exactly one of --as NAME and --key K", file=sys.stderr)
        return 2
    a = _find_agent(board, args.name, args.key)
    if a is None:
        print(f"refused: no active agent {term_safe(args.name or args.key)} on an open job", file=sys.stderr)
        return 1
    dj = board.job_status(args.to)
    if dj is None or dj.status != "active":
        print(f"refused: {term_safe(args.to)} is not an open job", file=sys.stderr)
        return 1
    if a.job == args.to:
        print(f"refused: {a.name} is already on {args.to}", file=sys.stderr)
        return 1
    sj = board.job_status(a.job)
    sessions = _sessions_of(board, [a.agent_key])
    if board.move_agent(a.agent_key, args.to) is None:
        print(f"refused: could not move {a.name} to {args.to}", file=sys.stderr)
        return 1
    _bind_sessions(cfg, args.to, sessions, bool(dj.goal))
    print(f"moved {a.name} from {a.job} to {args.to}; it keeps running and sees {args.to}'s board "
          f"(a notice and the recent messages) on its next tool call")
    if sj and sj.judge == a.name:
        print(f"{a.name} was the judge of {a.job}: it is a normal member of {args.to} now, and {a.job} "
              f"has no judge (spawn one, or move it back).")
    if not board.agents(a.job, include_departed=False):
        print(f"{a.job} has no active agents left: deactivate it, or it auto-closes when quiet.")
    return 0


def _moved_job(board, job: str, name: str) -> str | None:
    """The job `name` was moved to, when a post names the job it was moved from (the commands an
    agent was shown carry its old job): `name` is not an active member of `job` but is one of
    another open job. None otherwise (a member posting on its own job, or a name no agent holds)."""
    from swarm.board.base import BoardError, valid_name
    if not valid_name(name):   # the post itself reports it
        return None
    try:
        if any(a.name == name for a in board.agents(job, include_departed=False)):
            return None
        a = _find_agent(board, name, None)
    except BoardError:   # the redirect is a courtesy: never at the cost of the post
        return None
    return a.job if a else None


def _board_post(board, cfg: dict, args) -> int | None:
    job = _moved_job(board, args.job, args.name)
    if job:
        print(f"note: {term_safe(args.name)} is on {job} now, not on {args.job}: posting there. "
              f"Use --job {job} from now on.", file=sys.stderr)
        args.job = job
    try:
        res = board.post(args.job, args.name, " ".join(args.message), args.to)
    except ValueError as exc:   # a name the board refuses (board.base.valid_name)
        print(f"not posted: {term_safe(exc)}", file=sys.stderr)
        return 1
    from swarm.fastpath import changed
    changed(args.job)
    print(f"posted #{res.id}" + (f" (truncated to {cfg['board']['message_max_chars']} chars)" if res.truncated else ""))


def _verdict_text(args) -> tuple[str, str | None] | None:
    """(reason, next steps) of a `swarm verdict`, or None (after saying why on stderr) when a
    not_met verdict lacks its --reason or --next: the judge must say why and what would meet the goal."""
    reason = (getattr(args, "reason_opt", None) or " ".join(args.reason or ())).strip()
    nxt = (getattr(args, "next_steps", None) or "").strip()
    if args.verdict == "met":
        if not reason:
            print("a verdict needs a reason: swarm verdict --job J --as NAME met \"<why>\"", file=sys.stderr)
            return None
        return reason, None
    missing = [flag for flag, v in (("--reason \"<why it is not met>\"", reason),
                                    ("--next \"<what to change, where, and what you will re-check>\"", nxt)) if not v]
    if missing:
        print("refused: a not_met verdict must say why and what to do to meet the goal; missing "
              + " and ".join(missing) + ". The workers are spawned with these instructions.", file=sys.stderr)
        return None
    return reason, nxt


def _board_verdict(board, cfg: dict, args) -> int:
    from swarm.spool import deliver_verdict
    text = _verdict_text(args)
    if text is None:
        return 1
    try:
        recorded = deliver_verdict(board, args.job, args.name, args.verdict, text[0], text[1])
    except ValueError as exc:   # a name the board refuses (board.base.valid_name)
        print(f"verdict not recorded: {term_safe(exc)}", file=sys.stderr)
        return 1
    if not recorded:
        print(f"refused: {term_safe(args.name)} is not the judge of job {term_safe(args.job)}", file=sys.stderr)
        return 1
    from swarm.fastpath import changed
    changed(args.job)
    print(f"verdict {args.verdict} recorded for {args.job}, and posted on the board")
    if args.verdict == "met":
        _learning_instructions(args.job)
    return 0


def _board_read(board, cfg: dict, args) -> None:
    res = board.read_unread(agent_key=args.key, name=args.name, job=args.job, advance=not args.peek)
    print(fmt(res.messages) if res.messages else "(no new messages)")
    if res.remaining:
        print(f"({res.remaining} more unread: run read again)")


def _board_who(board, cfg: dict, args) -> None:
    for a in board.agents(args.job, include_departed=False):
        # Tab-separated, the name first and exact (so it can be pasted into --to '<name>'),
        # then the harness in its own field so the name field is never altered.
        tool = f"in {term_safe(a.current_tool)}" if a.current_tool else ""
        print(f"{term_safe(a.name)}\t{term_safe(a.harness)}\t{term_safe(a.role)}\t{term_safe(a.status)}\t"
              f"last contact {a.last_contact_at.astimezone().strftime('%H:%M')}\t{tool}")


def _sup_or_none(cfg: dict) -> dict | None:
    """[supervise] settings, or None if [supervise] is missing or invalid (never raises)."""
    from swarm.supervisor.settings import SettingsError, settings
    try:
        return settings(cfg)
    except SettingsError:
        return None


def _board_status(board, cfg: dict, args) -> None:
    color = _use_color(args)
    if not board.degraded:   # a standby can't close anything
        _say_closed(_sweep(board, cfg))
    enabled = transcripts_enabled(cfg)
    sup = _sup_or_none(cfg)
    if not args.job:
        print(jobs_overview(board, args.all, color, sup=sup))
        if enabled:
            print(transcripts_footer(board, cfg))
        if sup and sup["enabled"]:
            from swarm.supervisor import budget
            from swarm.supervisor.settings import today_start
            now = board.now()   # the host's (every OS user's) runs that overlap today, as the cap counts them
            day = budget.day_rows(board.restarts(host=compat.node()), today_start())
            used = sum(budget.charged_minutes(r, now) for r in day)
            print(f"supervisor: on, today {used:.0f}/{sup['daily_restart_minutes']} restart minutes "
                  f"on this host")
        return
    recent = None if args.all_agents else int(cfg["board"]["watch_recent_minutes"])
    rows = board.transcripts(job=args.job) if enabled and board.job_status(args.job) else None
    print(job_detail(board, args.job, color, recent, "--all-agents to show", rows, sup=sup))


def _board_join(board, cfg: dict, args) -> int:
    """Allocate (or return) the agent's name. --judge / --verifier give agents that run outside
    Claude Code the seat the hooks give a tagged subagent: they have no hooks, so the CLI is how
    they read, post and record verdicts."""
    name = board.allocate_name(args.key, args.job, args.role)
    if board.active_agent_name(args.key) is None:   # allocate_name never revives a stuck close
        print(f"refused: {name} ({args.key}) was closed as stuck by the swarm supervisor and "
              f"can't rejoin: its replacement does the work", file=sys.stderr)
        return 1
    if args.judge and not board.claim_judge(args.key, args.job):
        js = board.job_status(args.job)
        print(f"refused: {term_safe(js.judge) if js and js.judge else 'another agent'} is already the judge of "
              f"job {args.job} (one per job)", file=sys.stderr)
        return 1
    if args.verifier and not board.claim_verifier(args.key, args.job):
        print(f"refused: could not make {name} a verifier of job {args.job}", file=sys.stderr)
        return 1
    _sweep(board, cfg)   # prints nothing on stdout: scripts read the name from it
    print(name)
    return 0


def _board_wait(board, cfg: dict, args) -> int:
    on = " ".join(args.on).strip()
    if not on:
        print("say what the job is waiting for: --on \"<what>\"", file=sys.stderr)
        return 1
    until = None
    if args.for_ is not None:
        try:
            seconds = parse_duration(args.for_)
        except ValueError as exc:
            print(f"swarm wait: --for: {exc}", file=sys.stderr)
            return 2
        import datetime as dt
        until = board.now() + dt.timedelta(seconds=seconds)
    if not board.set_waiting(args.job, on, until):
        print(f"{args.job} is not an open job", file=sys.stderr)
        return 1
    bound = f" (for up to {_duration_text(seconds)}; " if until else " ("
    print(f"{args.job} is waiting on: {on}{bound}back to active when an agent joins, or with resume)")
    return 0


def _board_pause(board, cfg: dict, args) -> int:
    from swarm import pause
    report = pause.pause(board, cfg, args.job, args.reason, wait=max(0.0, args.wait))
    if report is None:
        print(f"{args.job} is not an open job (nothing to pause)", file=sys.stderr)
        return 1
    for line in report.lines():
        print(term_safe(line))
    return 0


def _board_resume(board, cfg: dict, args) -> int:
    from swarm import pause
    rec, _ = pause.find_pause(board, args.job, args.retry)
    advanced = args.host or args.workdir or args.only or args.dry_run or args.retry
    if rec is None:
        if advanced:
            print(f"{args.job} is not paused: nothing to resume" +
                  (" (--retry: no earlier resume of it had failed agents)" if args.retry else ""), file=sys.stderr)
            return 1
        if not board.set_waiting(args.job, None):
            print(f"{args.job} is not an open job", file=sys.stderr)
            return 1
        print(f"{args.job} is no longer waiting")
        return 0
    report = pause.resume(board, cfg, args.job, host=args.host, workdir=args.workdir, only=args.only or (),
                          dry_run=args.dry_run, retry=args.retry)
    if report is None:
        print(f"{args.job} is not paused: nothing to resume", file=sys.stderr)
        return 1
    for line in report.lines():
        print(term_safe(line))
    return 1 if report.failed else 0


def _board_leave(board, cfg: dict, args) -> int | None:
    if args.session:
        if args.name or args.key:
            print("--session takes neither --as nor --key", file=sys.stderr)
            return 2
        n = sum(board.close_agent(a.agent_key, "session restarted")
                for j in board.jobs(True) if j.session_id == args.session
                for a in board.agents(j.job, include_departed=False))
        print(f"left {n}")
        return 0
    if not board.leave(agent_key=args.key, name=args.name):
        print("no active agent matched", file=sys.stderr)
        return 1
    print("left")


def _board_purge(board, cfg: dict, args) -> None:
    board.purge()
    if transcripts_enabled(cfg):
        from swarm import transcripts
        transcripts.rotate(board, cfg, warn=lambda m: print(m, file=sys.stderr))
    _prune_memory_refs(board, cfg)
    _say_closed(_sweep(board, cfg))
    print("purged")


PRUNE_LISTED = 20   # dropped memory refs `swarm purge` lists one by one at most


def _prune_memory_refs(board, cfg: dict) -> None:
    """`swarm purge`: drop the memory refs whose memory Hindsight says is gone (provenance.prune:
    only on proof, never when Hindsight is down or unsure); the summary goes to stderr."""
    import time
    from swarm import provenance
    _refresh_hindsight_caps(cfg)
    res = provenance.prune(board, cfg, deadline=time.monotonic() + provenance.PRUNE_SECONDS)
    if res.note:
        print(term_safe(res.note), file=sys.stderr)
        return
    if not (res.checked or res.unknown or res.skipped):
        return
    print(f"memory references: {res.checked} checked, {len(res.dropped)} dropped (their memory is gone "
          f"from Hindsight)"
          + (f", {res.kept_missing_bank} kept (their bank is not in Hindsight)" if res.kept_missing_bank else "")
          + (f", {res.unknown} kept without a clear answer (Hindsight down or unsure)" if res.unknown else "")
          + (f", {res.skipped} left for a later purge" if res.skipped else ""), file=sys.stderr)
    for r in res.dropped_refs[:PRUNE_LISTED]:   # board data: term_safe (a forged row's job, say)
        print(term_safe(f"  dropped memory ref {r.document_id} (job {r.job}, agent {r.agent_name}, bank {r.bank})"),
              file=sys.stderr)
    if len(res.dropped_refs) > PRUNE_LISTED:
        print(f"  … and {len(res.dropped_refs) - PRUNE_LISTED} more", file=sys.stderr)


# Commands that run on one board opened by main (which first delivers any spooled posts).
BOARD_COMMANDS = {
    "job": _board_job,
    "join": _board_join,
    "move": _board_move,
    "post": _board_post,
    "verdict": _board_verdict,
    "wait": _board_wait,
    "pause": _board_pause,
    "resume": _board_resume,
    "read": _board_read,
    "who": _board_who,
    "status": _board_status,
    "leave": _board_leave,
    "purge": _board_purge,
    "transcript": _board_transcript,
    "memory": _board_memory,
}


def main(argv=None) -> int:
    from swarm.spool import SpoolError
    compat.setup_stdio()
    try:
        return _main(argv)
    except SpoolError as exc:   # a post or memory that had to be queued, with no private spool
        print(f"cannot queue it (board not reachable from here): {exc}", file=sys.stderr)
        return 1


def _reads_only(args) -> bool:
    """Whether the command only reads the board, so a standby may serve it when no primary is up."""
    return (args.cmd in ("who", "status", "recall") or (args.cmd == "read" and args.peek)
            or (args.cmd == "learn" and args.list_banks)
            or (args.cmd == "transcript" and args.tcmd in TRANSCRIPT_COMMANDS))


NEEDS_PRIMARY = "cannot reach the board database: {}{}"


def _main(argv=None) -> int:
    """Parse and run one command. No command dies with a traceback for want of a primary: a board
    that can't be reached, or a server that is a standby (psycopg's read-only error, SQLSTATE
    25006, e.g. a single-host config pointing at one), is one line on stderr and exit 1."""
    from swarm.board import BoardUnavailable, JobPaused
    args = _parser().parse_args(argv)
    note = "" if _reads_only(args) else " (this command writes: it needs the primary)"
    try:
        return _dispatch(args)
    except BoardUnavailable as exc:
        print(NEEDS_PRIMARY.format(exc, note), file=sys.stderr)
        return 1
    except JobPaused as exc:   # a join or post to a paused job: one clear line, nothing queued
        print(f"swarm: {term_safe(str(exc))}", file=sys.stderr)
        return 1
    except Exception as exc:
        if getattr(exc, "sqlstate", None) != "25006":   # read_only_sql_transaction
            raise
        print(NEEDS_PRIMARY.format(f"{exc} (the server is a standby)", note), file=sys.stderr)
        return 1


def _dispatch(args) -> int:
    # doctor reads (and reports on) the config itself: a broken one must not stop it
    cfg = {} if args.cmd == "doctor" else load_config(args.config)
    # a supervise dry run writes nothing: no board setup or migration either
    if (args.cmd not in NO_AUTO_INIT
            and not (args.cmd == "supervise" and (args.dry_run or args.scmd))
            and not (args.cmd == "learn" and args.list_banks)):
        auto_init(cfg)
    if args.cmd in COMMANDS:
        return COMMANDS[args.cmd](cfg, args)

    from swarm.board import open_board
    from swarm.spool import flush_spool, spool_post, spool_verdict
    if args.cmd == "verdict" and _verdict_text(args) is None:   # refused before anything is sent or queued
        return 1
    try:
        board_cm = open_board(cfg, readers=_reads_only(args))
    except Exception as exc:  # BoardUnavailable in practice; any failure to open is treated alike
        if args.cmd == "post":  # sandboxed agent: queue it; the next hook call delivers it
            spool_post(cfg, args.job, args.name, " ".join(args.message), args.to)
            print(f"queued (board not reachable from here: {_error_name(exc)}); it is delivered "
                  f"automatically within seconds by the swarm hooks. This is normal inside a sandbox.")
            return 0
        if args.cmd == "resume" and (args.host or args.workdir or args.only or args.dry_run or args.retry):
            print(f"cannot reach the board database: {exc} (resuming a paused job needs the board)", file=sys.stderr)
            return 1
        if args.cmd in ("wait", "resume"):   # likewise (no network in a Codex sandbox)
            on = " ".join(args.on).strip() if args.cmd == "wait" else None
            if args.cmd == "wait" and not on:
                print("say what the job is waiting for: --on \"<what>\"", file=sys.stderr)
                return 1
            from swarm.spool import spool_wait
            until = None
            if args.cmd == "wait" and args.for_ is not None:
                try:
                    until = time.time() + parse_duration(args.for_)
                except ValueError as exc:
                    print(f"swarm wait: --for: {exc}", file=sys.stderr)
                    return 2
            spool_wait(cfg, args.job, on, until)
            print(f"queued (board not reachable from here: {_error_name(exc)}); the swarm hooks apply "
                  f"it within seconds. This is normal inside a sandbox.")
            return 0
        if args.cmd == "verdict":  # likewise; whether args.name is the judge is checked on delivery
            spool_verdict(cfg, args.job, args.name, args.verdict, *_verdict_text(args))
            print(f"queued (board not reachable from here: {_error_name(exc)}); it is delivered "
                  f"automatically within seconds by the swarm hooks, and counts only if you are the "
                  f"judge of {args.job} (if not, you are told on the board).")
            if args.verdict == "met":
                _learning_instructions(args.job)
            return 0
        print(f"cannot reach the board database: {exc}"
              + (" (this command writes: it needs the primary)" if not _reads_only(args) else ""),
              file=sys.stderr)
        return 1
    from swarm.board import BoardUnavailable
    try:
        with board_cm as board:
            if board.degraded:   # served by a standby: nothing to write (the spool waits for a primary)
                print(degraded_notice(board.degraded), file=sys.stderr)
            else:
                flush_spool(board, cfg)
            return BOARD_COMMANDS[args.cmd](board, cfg, args) or 0
    except BoardUnavailable as exc:  # lost mid-command (e.g. a query past its deadline): no retry,
        # and no spooling either: a post whose reply was lost may well have been committed
        print(f"cannot reach the board database: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
