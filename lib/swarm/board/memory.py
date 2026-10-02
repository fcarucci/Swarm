"""In-memory reference backend for the swarm board.

NOT FOR PRODUCTION. Everything lives in this Python process and is lost when it exits, so it
cannot coordinate agents (each hook call is its own process). It exists for two reasons:

* it is the proof that the `Board` interface in base.py is backend-agnostic: it is written
  against the docstrings alone, with no knowledge of Postgres;
* it is the backend the offline tests run the CLI and the hooks against.

Select it with `[board] backend = "memory"`. Every `MemoryBoard` whose config names the same
store (`[memory] store = "<name>"`, default "default") shares one `MemoryStore` in this process,
so a test can open many boards (one per hook call, as in real life) over the same data.

Thread-safe: every method runs under the store's lock; `wait_for_change` waits on a
`threading.Condition` over that lock. Derived status comes from `derive_agent_status`.

Test hooks (not part of the Board interface): `get_store(name)`, `reset_store(name)`,
`MemoryStore.available` (False makes construction raise BoardUnavailable), and the plain dict
rows in `MemoryStore.agents` / `.jobs` / `.messages`, which tests may backdate directly.
"""
from __future__ import annotations

import copy
import datetime as _dt
import getpass
import os
import random
import threading
from typing import Mapping, Sequence

from .base import (LEFT_PAUSED, MOVED_PREFIX, PauseRecord, build_manifest, check_images, check_name, decompress_capped, decompress_transcript, MemoryRef, valid_pool, restart_over_limits, STUCK_PREFIX, AUTO_CLOSE_BLOCKING, AUTO_CLOSED_BY, MEMORY_SEEN_MAX, NAME_SOURCES, RESTART_OUTCOMES, Restart,
                   ROUTE_STATES, TOOL_NAME_MAX, AgentEvent, AgentStatus, Board, BoardError, BoardUnavailable, JobStatus, Member, Message,
                   OwedReply, ReadResult, Route, SCHEMA_VERSION, SetupResult, SpawnGrant, SyncState, TRANSCRIPT_ROLES,
                   TranscriptImage, TranscriptRow, TranscriptSummary, VERDICTS,
                   derive_agent_status, load_name_pool)

# Per-agent sync state (SyncState), reset on a fresh row.
_SYNC_DEFAULTS = {"roster_seen": None, "roster_synced_at": None, "memory_recalled_at": None,
                  "memory_seen": (), "remembered_at": None, "nudged_at": None,
                  "calls_at_post": 0, "silence_nudged_at": None, "reply_reminded_id": 0}

_UTC = _dt.timezone.utc


_SUMMARY_FIELDS = ("job", "agent_key", "agent_name", "role", "host", "captured_at", "final",
                   "raw_bytes", "stored_bytes", "redactions", "session_id", "sha256")


def transcript_key(job: str, agent_key: str) -> str:
    """The store key of a transcript row (a string: the file backend keeps rows in JSON)."""
    return f"{job}\x00{agent_key}"


class MemoryStore:
    """The shared state behind MemoryBoards with the same store name."""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.changed = threading.Condition(self.lock)
        self.available = True
        self.pool: dict[str, list[str]] = {s: [] for s in NAME_SOURCES}
        self.jobs: dict[str, dict] = {}
        self.agents: dict[str, dict] = {}      # agent_key -> row
        self.messages: list[dict] = []         # in id order
        self.routes: dict[str, dict] = {}      # agent_key -> {session_id, state, job, created_at}
        self.transcripts: dict[str, dict] = {}  # transcript_key(job, agent_key) -> row, no body
        self.transcript_bodies: dict[str, bytes] = {}
        self.image_bodies: dict[str, bytes] = {}   # sha256 -> image bytes; FileStore: files
        self.memory_refs: dict[str, dict] = {}   # document_id -> row (Board.save_memory_ref), no excerpt
        self.next_id = 1
        self.restarts: list[dict] = []         # supervisor restarts (Board.record_restart), by id
        self.next_restart_id = 1
        self.pauses: list[dict] = []           # job pauses (Board.pause_job), by id
        self.next_pause_id = 1
        self.schema_version: int | None = None  # set by setup (the version a real store records)
        self.msg_version = 0                   # bumped on every new message
        self.state_version = 0                 # bumped on every agent/job change

    # transcript bodies (compressed), by transcript key; FileStore keeps them in files
    def transcript_body(self, key: str) -> bytes | None:
        return self.transcript_bodies.get(key)

    def put_transcript_body(self, key: str, body: bytes) -> None:
        self.transcript_bodies[key] = body

    def drop_transcript_body(self, key: str) -> None:
        self.transcript_bodies.pop(key, None)

    # transcript images, by sha256 (their metadata and references are in the transcript rows)
    def image_body(self, sha: str) -> bytes | None:
        return self.image_bodies.get(sha)

    def put_image_body(self, sha: str, data: bytes) -> None:
        self.image_bodies[sha] = data

    def drop_image_body(self, sha: str) -> None:
        self.image_bodies.pop(sha, None)

    def touch(self, messages: bool = False) -> None:
        if messages:
            self.msg_version += 1
        self.state_version += 1
        self.changed.notify_all()


_STORES: dict[str, MemoryStore] = {}
_STORES_LOCK = threading.Lock()


def _store_name(cfg: dict) -> str:
    return str((cfg.get("memory") or {}).get("store", "default"))


def _available_store(cfg: dict) -> MemoryStore:
    """The config's store; BoardUnavailable (from a ConnectionError, like a real driver's) if
    a test has marked it unavailable."""
    store = get_store(_store_name(cfg))
    if not store.available:
        msg = "memory board store is marked unavailable"
        raise BoardUnavailable(msg) from ConnectionError(msg)
    return store


def get_store(name: str = "default") -> MemoryStore:
    with _STORES_LOCK:
        if name not in _STORES:
            _STORES[name] = MemoryStore()
        return _STORES[name]


def reset_store(name: str = "default") -> MemoryStore:
    """Empty the named store in place (no data, no names in the pool, available again) and
    return it. Boards already open on it keep working and see the empty store."""
    store = get_store(name)
    with store.lock:
        fresh = MemoryStore()
        for attr in ("available", "pool", "jobs", "agents", "messages", "routes", "transcripts",
                     "transcript_bodies", "image_bodies", "next_id", "schema_version",
                     "restarts", "next_restart_id", "memory_refs", "pauses", "next_pause_id"):
            setattr(store, attr, getattr(fresh, attr))
        store.touch(messages=True)
    return store


# Verdict fields of a job, cleared when it is (re-)opened.
_NO_VERDICT = {"verdict": None, "verdict_reason": None, "verdict_next": None, "verdict_by": None, "verdict_at": None}

# ---- purge steps (store lock held; each returns whether it changed anything) -----------

def _paused_jobs(s: MemoryStore) -> set:
    """A paused job keeps its messages and departed agents however old (resume needs them)."""
    return {job for job, j in s.jobs.items() if j["status"] == "paused"}


def _purge_messages(s: MemoryStore, keep: _dt.datetime) -> bool:
    before = len(s.messages)
    paused = _paused_jobs(s)
    s.messages = [m for m in s.messages if m["created_at"] >= keep or m["job"] in paused]
    return len(s.messages) != before


def _mark_stale_dead(s: MemoryStore, now: _dt.datetime, stale: _dt.datetime) -> bool:
    hits = [a for a in s.agents.values() if a["left_at"] is None and a["last_seen"] < stale]
    for a in hits:
        a.update(left_at=now, state="dead", current_tool=None)
    return bool(hits)


def _drop_agents(s: MemoryStore, doomed) -> bool:
    keys = [k for k, a in s.agents.items() if doomed(a)]
    for k in keys:
        del s.agents[k]
    return bool(keys)


def _drop_departed(s: MemoryStore, keep: _dt.datetime) -> bool:
    paused = _paused_jobs(s)
    return _drop_agents(s, lambda a: a["left_at"] is not None and a["left_at"] < keep and a["job"] not in paused)


def _drop_old_restarts(s: MemoryStore, keep: _dt.datetime) -> bool:
    before = len(s.restarts)
    s.restarts = [r for r in s.restarts if r["at"] >= keep]
    return len(s.restarts) != before


def _drop_old_routes(s: MemoryStore, keep: _dt.datetime) -> bool:
    keys = [k for k, r in s.routes.items() if r["created_at"] < keep]
    for k in keys:
        del s.routes[k]
    return bool(keys)


def _drop_quiet_jobs(s: MemoryStore, keep: _dt.datetime) -> bool:
    """Jobs past retention with no messages and no active agents go, with their departed agents."""
    busy = {m["job"] for m in s.messages} | {a["job"] for a in s.agents.values() if a["left_at"] is None}
    doomed = [job for job, j in s.jobs.items()
              if (j["finished_at"] or j["activated_at"] or j["created_at"]) < keep and job not in busy
              and j["status"] != "paused"]
    for job in doomed:
        del s.jobs[job]
    _drop_agents(s, lambda a: a["job"] in doomed)
    return bool(doomed)


class MemoryBoard(Board):
    """In-process Board over a shared MemoryStore. Reference/test backend only."""

    def __init__(self, cfg: dict):
        super().__init__(cfg)
        self._store = self._make_store(cfg)
        self._closed = False
        self._subscribed: bool | None = None   # None: not subscribed; else messages_only
        self._seen = 0

    # ---- lifecycle -------------------------------------------------------------

    @classmethod
    def _open_store(cls, cfg: dict) -> MemoryStore:
        """The store this board works on. A subclass that keeps the same rows elsewhere (the
        file backend) returns its own MemoryStore subclass, whose `lock` loads and persists."""
        return _available_store(cfg)

    def _make_store(self, cfg: dict) -> MemoryStore:
        """This board's store (the file backend's may be read-only)."""
        return self._open_store(cfg)

    @classmethod
    def setup(cls, cfg: dict, names: Mapping[str, Sequence[str]]) -> SetupResult:
        names = valid_pool(names)   # a name that could forge context is never handed out
        store = cls._open_store(cfg)
        with store.lock:
            known = {n for lst in store.pool.values() for n in lst}
            for source in NAME_SOURCES:
                new = [n for n in dict.fromkeys(names.get(source, ())) if n not in known]
                store.pool.setdefault(source, []).extend(new)
                known.update(new)
            store.schema_version = max(store.schema_version or 0, SCHEMA_VERSION)
            return SetupResult(notes=(), pool={s: len(store.pool.get(s, [])) for s in NAME_SOURCES})

    @classmethod
    def schema_version(cls, cfg: dict) -> int | None:
        return _available_store(cfg).schema_version

    def close(self) -> None:
        self._closed = True

    def _s(self) -> MemoryStore:
        if self._closed:
            raise BoardError("board is closed")
        return self._store

    def now(self) -> _dt.datetime:
        return _dt.datetime.now(_UTC)

    # ---- retention ---------------------------------------------------------------

    def purge(self) -> None:
        s = self._s()
        b = self.board_cfg
        with s.lock:
            now = self.now()
            keep = now - _dt.timedelta(days=int(b["retention_days"]))
            stale = now - _dt.timedelta(hours=int(b["agent_stale_hours"]))
            # Every step runs (a list, not a short-circuiting `or`), in the documented order.
            changed = [_purge_messages(s, keep), _mark_stale_dead(s, now, stale),
                       _drop_departed(s, keep), _drop_quiet_jobs(s, keep), _drop_old_routes(s, keep),
                       _drop_old_restarts(s, keep)]
            if any(changed):
                s.touch()

    # ---- jobs --------------------------------------------------------------------

    def _new_job(self, job: str, **fields) -> dict:
        row = {"job": job, "status": "active", "description": None, "task": None, "outcome": None,
               "created_by": None, "session_id": None, "created_at": self.now(),
               "activated_at": None, "finished_at": None, "project": None, "goal": None,
               **_NO_VERDICT, "completion_forced": False, "waiting_on": None, "waiting_since": None,
               "waiting_until": None, "max_hours": None, "closed_by": None}
        row.update(fields)
        return row

    def ensure_job(self, job: str, description: str | None = None,
                   created_by: str | None = None) -> None:
        s = self._s()
        with s.lock:
            j = s.jobs.get(job)
            if j is None:
                s.jobs[job] = self._new_job(job, description=description, created_by=created_by)
            elif description is not None:
                j["description"] = description
            s.touch()

    def open_job(self, job: str, description: str | None, task: str | None,
                 session_id: str | None, created_by: str | None, project: str | None = None,
                 goal: str | None = None) -> None:
        s = self._s()
        with s.lock:
            now = self.now()
            j = s.jobs.get(job)
            if j is None:
                s.jobs[job] = self._new_job(job, description=description, task=task,
                                            session_id=session_id, created_by=created_by,
                                            activated_at=now, project=project, goal=goal)
            else:
                j.update(status="active", activated_at=now, finished_at=None, outcome=None,
                         completion_forced=False, spawns=0, waiting_on=None, waiting_since=None,
                         waiting_until=None, max_hours=None, closed_by=None, **_NO_VERDICT)
                for k, v in (("description", description), ("task", task), ("session_id", session_id),
                             ("project", project), ("goal", goal)):
                    if v is not None:
                        j[k] = v
            s.touch()

    def close_job(self, job: str, status: str, outcome: str | None, forced: bool = False,
                  closed_by: str | None = None) -> bool:
        s = self._s()
        with s.lock:
            j = s.jobs.get(job)
            self._close(job, j, status, outcome, forced, closed_by)
            return j is not None

    def _close(self, job: str, j: dict | None, status: str, outcome: str | None, forced: bool,
               closed_by: str | None) -> None:
        """close_job's effects (store lock held): the job's active agents leave, then the job
        row (if any) is closed."""
        s = self._store
        now = self.now()
        for a in s.agents.values():
            if a["job"] == job and a["left_at"] is None:
                a.update(left_at=now, state="left", current_tool=None, tool_started_at=None)
        if j is not None:
            j.update(status=status, finished_at=j["finished_at"] or now, completion_forced=bool(forced),
                     waiting_on=None, waiting_since=None, waiting_until=None, closed_by=closed_by)
            if outcome is not None:
                j["outcome"] = outcome
        s.touch()

    def auto_close_job(self, job: str, before: _dt.datetime, outcome: str) -> _dt.datetime | None:
        s = self._s()
        with s.lock:
            j = s.jobs.get(job)
            if j is None or j["status"] != "active" or j.get("waiting_on") or \
                    (j["goal"] and j["verdict"] != "met"):
                return None
            start = j["activated_at"] or j["created_at"]
            if start >= before:
                return None
            now = self.now()
            rows = [a for a in s.agents.values() if a["job"] == job]
            if not any(a["state"] in ("completed", "left") and a["left_at"] is not None
                       and a["left_at"] >= start for a in rows):
                return None
            if any(self._derived(a, now) in AUTO_CLOSE_BLOCKING for a in rows):
                return None
            if any(t is not None and t >= before for a in rows
                   for t in (a["joined_at"], a["last_seen"], a["left_at"])):
                return None
            if any(m["job"] == job and m["created_at"] >= before for m in s.messages):
                return None
            self._close(job, j, "completed", outcome, False, AUTO_CLOSED_BY)
            return j["finished_at"]

    def undo_auto_close(self, job: str, closed_at: _dt.datetime) -> bool:
        s = self._s()
        with s.lock:
            j = s.jobs.get(job)
            if j is None or j["status"] != "completed" or j.get("closed_by") != AUTO_CLOSED_BY \
                    or j["finished_at"] != closed_at:
                return False
            j.update(status="active", finished_at=None, outcome=None, closed_by=None)
            s.touch()
            return True

    def set_waiting(self, job: str, on: str | None, until: _dt.datetime | None = None) -> bool:
        s = self._s()
        with s.lock:
            j = s.jobs.get(job)
            if j is None or j["status"] != "active":
                return False
            j.update(waiting_on=on, waiting_since=None if on is None else self.now(),
                     waiting_until=None if on is None else until)
            s.touch()
            return True

    def set_job_max_hours(self, job: str, hours: float | None) -> bool:
        s = self._s()
        with s.lock:
            j = s.jobs.get(job)
            if j is None:
                return False
            j["max_hours"] = hours
            s.touch()
            return True

    def bind_job_session(self, job: str, session_id: str) -> None:
        s = self._s()
        with s.lock:
            j = s.jobs.get(job)
            if j is not None and j["session_id"] is None:
                j["session_id"] = session_id
                s.touch()

    # ---- agents --------------------------------------------------------------------

    def _active(self, agent_key: str) -> dict | None:
        a = self._store.agents.get(agent_key)
        return a if a is not None and a["left_at"] is None else None

    def _active_named(self, name: str) -> dict | None:
        """The active agent holding `name` (active names are unique, so at most one)."""
        return next((a for a in self._store.agents.values()
                     if a["name"] == name and a["left_at"] is None), None)

    def _find_active(self, agent_key: str | None, name: str | None) -> dict | None:
        """The active agent with agent_key if one is given, else the one named `name`."""
        return self._active(agent_key) if agent_key else self._active_named(name)

    def _name_held(self, name: str) -> bool:
        return self._active_named(name) is not None

    def _free_name(self) -> str:
        """A random free pool name, sources in NAME_SOURCES order; else "<english name> NNN"."""
        for source in NAME_SOURCES:
            free = [n for n in self._store.pool.get(source, []) if not self._name_held(n)]
            if free:
                return random.choice(free)
        while True:
            candidate = f"{random.choice(load_name_pool()['english'])} {random.randint(100, 999)}"
            if not self._name_held(candidate):
                return candidate

    def _allocate_name(self, agent_key: str, job: str, role: str | None = None) -> str:
        s = self._s()
        self.purge()
        self.ensure_job(job)
        with s.lock:
            now = self.now()
            row = s.agents.get(agent_key)
            if row is not None and row["left_at"] is None:
                same = row["job"] == job
                row.update(last_seen=now, job=job, judge=row.get("judge", False) and same,
                           verifier=row.get("verifier", False) and same)
                s.touch()
                return row["name"]
            if row is not None and ((row.get("left_reason") or "").startswith(STUCK_PREFIX)
                                    or any(r["old_agent_key"] == agent_key for r in s.restarts)):
                return row["name"]      # supervisor-closed or replaced: stays departed, never revived
            if row is not None and not self._name_held(row["name"]):
                row.update(left_at=None, state="started", job=job, last_seen=now,
                           current_tool=None, tool_started_at=None, turn_ended_at=None,
                           judge=False, verifier=False, left_reason=None)
                s.touch()
                return row["name"]
            name = self._free_name()
            s.agents[agent_key] = {
                "agent_key": agent_key, "name": name, "job": job, "role": role,
                "host": os.uname().nodename, "os_user": getpass.getuser(),
                "harness": None, "model": None, "turn_ended_at": None,
                "joined_at": now, "last_seen": now,
                "last_read_id": self._history_cursor(job), "left_at": None, "state": "started",
                "tool_calls": 0, "current_tool": None, "tool_started_at": None,
                "last_post_at": None, "judge": False, "verifier": False, "left_reason": None,
                "resume_of": None, **_SYNC_DEFAULTS}
            s.touch()
            return name

    def _history_cursor(self, job: str) -> int:
        """The cursor that leaves exactly the job's newest join_history messages unread."""
        ids = [m["id"] for m in self._messages_of(job)]
        keep = max(0, int(self.board_cfg.get("join_history", 30)))
        if not ids:
            return 0
        return ids[-keep - 1] if len(ids) > keep else 0

    def claim_judge(self, agent_key: str, job: str) -> bool:
        s = self._s()
        with s.lock:
            a = self._active(agent_key)
            if a is None or a["job"] != job:
                return False
            if any(o.get("judge") and o["left_at"] is None and o["job"] == job
                   for k, o in s.agents.items() if k != agent_key):
                return False
            a["judge"] = True
            s.touch()
            return True

    def reserve_spawn(self, agent_key: str, job: str, per_agent: int, per_job: int) -> SpawnGrant:
        s = self._s()
        with s.lock:
            a, j = self._active(agent_key), s.jobs.get(job)
            if a is None or a["job"] != job or j is None:
                return SpawnGrant(False, 0, 0, "member")
            mine, total = a.get("spawns", 0), j.get("spawns", 0)
            refused = "agent" if mine >= per_agent else "job" if total >= per_job else None
            if refused:
                return SpawnGrant(False, mine, total, refused)
            a["spawns"], j["spawns"] = mine + 1, total + 1
            s.touch()
            return SpawnGrant(True, mine + 1, total + 1)

    def _move_agent_row(self, agent_key: str, job: str, keep: int) -> str | None:
        s = self._s()
        with s.lock:
            a = self._active(agent_key)
            j = s.jobs.get(job)
            if a is None or j is None or j["status"] != "active":
                return None
            old = a["job"]
            if old == job:
                return job
            ids = [m["id"] for m in self._messages_of(job)]
            cursor = (ids[-keep - 1] if len(ids) > keep else 0) if ids else 0
            top = max((m["id"] for m in self._messages_of(None)), default=0)
            a.update(job=job, judge=False, verifier=False, last_seen=self.now(), last_read_id=cursor,
                     reply_reminded_id=top, calls_at_post=a["tool_calls"], silence_nudged_at=None,
                     roster_seen=MOVED_PREFIX + old, roster_synced_at=None)
            s.touch()
            return old

    def set_job_goal(self, job: str, goal: str) -> bool:
        s = self._s()
        with s.lock:
            j = s.jobs.get(job)
            if j is None or j["status"] != "active":
                return False
            if j.get("goal") != goal:
                j.update(goal=goal, **_NO_VERDICT)
                s.touch()
            return True

    def claim_verifier(self, agent_key: str, job: str) -> bool:
        s = self._s()
        with s.lock:
            a = self._active(agent_key)
            if a is None or a["job"] != job or a.get("judge"):
                return False
            a["verifier"] = True
            s.touch()
            return True

    def verification_counts(self, job: str) -> tuple[int, int]:
        s = self._s()
        with s.lock:
            names = {a["name"] for a in s.agents.values() if a["job"] == job and a.get("verifier")}
            texts = [m["message"] for m in s.messages if m["job"] == job and m["agent_name"] in names]
            return (sum(t.startswith("VERIFIED") for t in texts), sum(t.startswith("FAILED") for t in texts))

    def _active_judge(self, job: str) -> dict | None:
        return next((a for a in self._store.agents.values()
                     if a.get("judge") and a["left_at"] is None and a["job"] == job), None)

    def record_verdict(self, job: str, judge_name: str, verdict: str, reason: str,
                       next_steps: str | None = None) -> bool:
        check_name(judge_name, "judge name")
        if verdict not in VERDICTS:
            raise BoardError(f"unknown verdict {verdict!r}")
        s = self._s()
        with s.lock:
            judge, j = self._active_judge(job), s.jobs.get(job)
            if j is None or judge is None or judge["name"] != judge_name:
                return False
            j.update(verdict=verdict, verdict_reason=reason, verdict_next=next_steps, verdict_by=judge_name, verdict_at=self.now())
            s.touch()
            return True

    def active_agent_name(self, agent_key: str) -> str | None:
        s = self._s()
        with s.lock:
            a = self._active(agent_key)
            return a["name"] if a else None

    def was_member(self, agent_key: str, job: str) -> bool:
        s = self._s()
        with s.lock:
            a = s.agents.get(agent_key)
            return a is not None and a["job"] == job

    def tool_started(self, agent_key: str, tool_name: str | None) -> Member | None:
        s = self._s()
        with s.lock:
            a = self._active(agent_key)
            if not a:
                return None
            now = self.now()
            a.update(state="running", current_tool=(tool_name or "?")[:TOOL_NAME_MAX],
                     tool_started_at=now, turn_ended_at=None, tool_calls=a["tool_calls"] + 1, last_seen=now)
            s.touch()
            route = s.routes.get(agent_key)
            return Member(a["name"], a["job"], bool(route and route["state"] == "unverified"),
                          bool(a.get("verifier")), a.get("model"))

    def record_route(self, agent_key: str, session_id: str | None, state: str,
                     job: str | None = None) -> None:
        if state not in ROUTE_STATES:
            raise BoardError(f"unknown route state {state!r}")
        s = self._s()
        with s.lock:
            s.routes[agent_key] = {"session_id": session_id, "state": state, "job": job,
                                   "created_at": self.now()}

    def claim_route(self, agent_key: str, session_id: str | None, from_state: str | None,
                    state: str, job: str | None = None) -> bool:
        s = self._s()
        with s.lock:
            if (s.routes.get(agent_key) or {}).get("state") != from_state:
                if state not in ROUTE_STATES:
                    raise BoardError(f"unknown route state {state!r}")
                return False
            self.record_route(agent_key, session_id, state, job)
            return True

    def route(self, agent_key: str) -> Route:
        s = self._s()
        with s.lock:
            r = s.routes.get(agent_key) or {}
            a = s.agents.get(agent_key)
            return Route(r.get("state"), r.get("job"), r.get("session_id"), a["job"] if a else None)

    def tool_finished(self, agent_key: str) -> None:
        s = self._s()
        with s.lock:
            a = self._active(agent_key)
            if a:
                a.update(current_tool=None, tool_started_at=None, last_seen=self.now())
                s.touch()

    def agent_stopped(self, agent_key: str) -> None:
        s = self._s()
        with s.lock:
            a = self._active(agent_key)
            if a:
                a.update(left_at=self.now(), state="completed", current_tool=None, tool_started_at=None)
                s.touch()

    def set_agent_role(self, agent_key: str, role: str) -> None:
        from swarm.roles import custom_role
        if custom_role(role) is None:
            raise ValueError("role must be a custom role identifier, not judge/verifier")
        s = self._s()
        with s.lock:
            a = self._active(agent_key)
            if a:
                a["role"] = role
                s.touch()

    def set_agent_runtime(self, agent_key: str, harness: str | None, model: str | None) -> None:
        s = self._s()
        with s.lock:
            a = self._active(agent_key)
            if a:
                if harness is not None:
                    a["harness"] = harness
                if model is not None:
                    a["model"] = model
                s.touch()

    def agent_turn_ended(self, agent_key: str) -> None:
        s = self._s()
        with s.lock:
            a = self._active(agent_key)
            if a:
                now = self.now()
                a.update(turn_ended_at=now, current_tool=None, tool_started_at=None, last_seen=now)
                s.touch()

    def turns_resumed(self, job: str) -> list[str]:
        s = self._s()
        with s.lock:
            now = self.now()
            keys = [k for k, a in s.agents.items() if a["job"] == job and a["left_at"] is None
                    and a.get("turn_ended_at") is not None]
            for k in keys:
                s.agents[k].update(turn_ended_at=now, last_seen=now)
            if keys:
                s.touch()
            return keys

    def finish_quiet_agents(self, quiet_seconds: float) -> list[str]:
        s = self._s()
        with s.lock:
            cutoff = self.now() - _dt.timedelta(seconds=quiet_seconds)
            done = [k for k, a in s.agents.items() if a["left_at"] is None
                    and a.get("turn_ended_at") is not None and a["turn_ended_at"] < cutoff]
            for k in done:
                s.agents[k].update(left_at=self.now(), state="completed", current_tool=None,
                                   tool_started_at=None)
            if done:
                s.touch()
            return done

    def leave(self, agent_key: str | None = None, name: str | None = None) -> bool:
        s = self._s()
        with s.lock:
            a = self._find_active(agent_key, name)
            if a is None:
                return False
            a.update(left_at=self.now(), state="left", current_tool=None)
            s.touch()
            return True

    def close_agent(self, agent_key: str, reason: str, seen_before: _dt.datetime | None = None) -> bool:
        s = self._s()
        with s.lock:
            a = self._active(agent_key)
            if a is None or (seen_before is not None and a["last_seen"] > seen_before):
                return False
            a.update(left_at=self.now(), state="left", left_reason=reason, current_tool=None,
                     tool_started_at=None)
            s.touch()
            return True

    def claim_resume(self, agent_key: str, resume_of: str, job: str) -> str | None:
        s = self._s()
        with s.lock:   # the job exists if resume_of is its departed row; nothing is created otherwise
            cur = s.agents.get(agent_key)
            if cur is not None and cur["left_at"] is None and cur["job"] == job:
                return cur["name"]
            old = s.agents.get(resume_of)
            if old is None or old["job"] != job or old["left_at"] is None or self._name_held(old["name"]):
                return None
            now = self.now()
            judge = bool(old.get("judge")) and not any(
                o.get("judge") and o["left_at"] is None and o["job"] == job for o in s.agents.values())
            s.agents[agent_key] = {
                "agent_key": agent_key, "name": old["name"], "job": job, "role": old["role"],
                "host": os.uname().nodename, "os_user": getpass.getuser(),
                "harness": None, "model": None, "turn_ended_at": None,
                "joined_at": now, "last_seen": now, "last_read_id": old.get("last_read_id", 0),
                "left_at": None, "state": "started", "tool_calls": 0, "current_tool": None,
                "tool_started_at": None, "last_post_at": None, "judge": judge,
                "verifier": bool(old.get("verifier")), "left_reason": None, "resume_of": resume_of,
                **_SYNC_DEFAULTS}
            s.touch()
            return old["name"]

    # ---- pause / resume ------------------------------------------------------------------

    @staticmethod
    def _pause_record(p: dict) -> PauseRecord:
        return PauseRecord(id=p["id"], job=p["job"], paused_at=p["paused_at"], paused_by=p["paused_by"],
                           reason=p["reason"], manifest=copy.deepcopy(p["manifest"]),
                           resumed_at=p["resumed_at"], resumed_by=p["resumed_by"],
                           resumed_host=p["resumed_host"], outcome=copy.deepcopy(p["outcome"]))

    def _open_pause(self, job: str) -> dict | None:
        return next((p for p in reversed(self._store.pauses) if p["job"] == job and p["resumed_at"] is None),
                    None)

    def pause_job(self, job: str, by: str | None, reason: str | None,
                  cwds: Mapping[str, str] | None = None) -> PauseRecord | None:
        s = self._s()
        with s.lock:
            j = s.jobs.get(job)
            if j is None:
                return None
            if j["status"] == "paused":
                p = self._open_pause(job)
                return self._pause_record(p) if p else None
            if j["status"] != "active":
                return None
            now = self.now()
            rows = []
            for a in s.agents.values():
                if a["job"] != job or a["left_at"] is not None:
                    continue
                t = s.transcripts.get(transcript_key(job, a["agent_key"]))
                rows.append({**a, "status": self._derived(a, now),
                             "session_id": (s.routes.get(a["agent_key"]) or {}).get("session_id"),
                             "orchestrator": bool(t and t["role"] == "orchestrator")})
            last = max((m["id"] for m in self._messages_of(job)), default=0)
            manifest = build_manifest(job, now, by, reason, j, rows, last, cwds)
            for a in s.agents.values():
                if a["job"] == job and a["left_at"] is None:
                    a.update(left_at=now, state="left", left_reason=LEFT_PAUSED, current_tool=None,
                             tool_started_at=None)
            j["status"] = "paused"
            p = {"id": s.next_pause_id, "job": job, "paused_at": now, "paused_by": by, "reason": reason,
                 "manifest": manifest, "resumed_at": None, "resumed_by": None, "resumed_host": None,
                 "outcome": None}
            s.next_pause_id += 1
            s.pauses.append(p)
            s.touch()
            return self._pause_record(p)

    def open_pause(self, job: str) -> PauseRecord | None:
        s = self._s()
        with s.lock:
            p = self._open_pause(job)
            return self._pause_record(p) if p else None

    def pauses(self, job: str) -> list[PauseRecord]:
        s = self._s()
        with s.lock:
            return [self._pause_record(p) for p in s.pauses if p["job"] == job]

    def begin_resume(self, job: str, pause_id: int, by: str | None, host: str | None) -> bool:
        s = self._s()
        with s.lock:
            j = s.jobs.get(job)
            p = next((p for p in s.pauses if p["id"] == pause_id and p["job"] == job), None)
            if j is None or j["status"] != "paused" or p is None or p["resumed_at"] is not None:
                return False
            j["status"] = "active"
            p.update(resumed_at=self.now(), resumed_by=by, resumed_host=host)
            s.touch()
            return True

    def record_resume_outcome(self, pause_id: int, outcome: dict) -> bool:
        s = self._s()
        with s.lock:
            p = next((p for p in s.pauses if p["id"] == pause_id), None)
            if p is None:
                return False
            p["outcome"] = copy.deepcopy(outcome)
            s.touch()
            return True

    def set_job_supervise(self, job: str, on: bool) -> bool:
        s = self._s()
        with s.lock:
            j = s.jobs.get(job)
            if j is None:
                return False
            j["supervise"] = bool(on)
            s.touch()
            return True

    # ---- supervisor restarts ------------------------------------------------------------

    def record_restart(self, job: str, agent_key: str, old_agent_key: str, reason: str, harness: str,
                       minutes_cap: float, outcome: str | None = None,
                       max_per_job: int | None = None, max_job_minutes: float | None = None,
                       max_host_running: int | None = None, max_host_minutes: float | None = None,
                       day_start: _dt.datetime | None = None) -> Restart | None:
        if outcome is not None and outcome not in RESTART_OUTCOMES:
            raise ValueError(outcome)
        s = self._s()
        with s.lock:
            if any(r["old_agent_key"] == old_agent_key for r in s.restarts):
                return None
            if max_per_job is not None and sum(1 for r in s.restarts if r["job"] == job) >= int(max_per_job):
                return None
            host = os.uname().nodename
            if outcome is None and restart_over_limits(
                    [Restart(**r) for r in s.restarts if r["job"] == job],
                    [Restart(**r) for r in s.restarts if r["host"] == host], float(minutes_cap),
                    max_job_minutes=max_job_minutes, max_host_running=max_host_running,
                    max_host_minutes=max_host_minutes, day_start=day_start):
                return None
            now = self.now()
            row = {"id": s.next_restart_id, "job": job, "agent_key": agent_key,
                   "attempt": 1 + sum(1 for r in s.restarts if r["job"] == job and r["agent_key"] == agent_key),
                   "at": now, "reason": reason, "old_agent_key": old_agent_key, "new_agent_key": None,
                   "harness": harness, "host": os.uname().nodename, "os_user": getpass.getuser(),
                   "minutes_cap": float(minutes_cap), "ended_at": now if outcome else None,
                   "outcome": outcome}
            s.next_restart_id += 1
            s.restarts.append(row)
            s.touch()
            return Restart(**row)

    def set_restart_agent(self, restart_id: int, new_agent_key: str) -> bool:
        s = self._s()
        with s.lock:
            for r in s.restarts:
                if r["id"] == restart_id:
                    r["new_agent_key"] = new_agent_key
                    s.touch()
                    return True
            return False

    def finish_restart(self, restart_id: int, outcome: str) -> bool:
        if outcome not in RESTART_OUTCOMES:
            raise ValueError(outcome)
        s = self._s()
        with s.lock:
            for r in s.restarts:
                if r["id"] == restart_id and r["ended_at"] is None:
                    r.update(ended_at=self.now(), outcome=outcome)
                    s.touch()
                    return True
            return False

    def was_replaced(self, agent_key: str) -> bool:
        s = self._s()
        with s.lock:
            return any(r["old_agent_key"] == agent_key for r in s.restarts)

    def restarts(self, job: str | None = None, agent_key: str | None = None, host: str | None = None,
                 os_user: str | None = None, since: _dt.datetime | None = None) -> list[Restart]:
        s = self._s()
        with s.lock:
            want = {"job": job, "agent_key": agent_key, "host": host, "os_user": os_user}
            return [Restart(**r) for r in s.restarts
                    if all(v is None or r[k] == v for k, v in want.items())
                    and (since is None or r["at"] >= since)]

    def _derived(self, a: dict, now: _dt.datetime) -> str:
        b = self.board_cfg
        return derive_agent_status(
            a["state"], a["current_tool"], a["tool_started_at"], a["last_seen"], now,
            idle_minutes=int(b["idle_minutes"]), dead_minutes=int(b["dead_minutes"]),
            tool_timeout_minutes=int(b["tool_timeout_minutes"]))

    def _status(self, a: dict, now: _dt.datetime) -> AgentStatus:
        status = self._derived(a, now)
        msgs = sum(1 for m in self._store.messages if m["job"] == a["job"]
                   and m["agent_name"] == a["name"] and m["created_at"] >= a["joined_at"])
        role = "judge" if a.get("judge") else "verifier" if a.get("verifier") else a["role"]
        return AgentStatus(job=a["job"], name=a["name"], role=role,
                           status=status,
                           current_tool=a["current_tool"], tool_calls=a["tool_calls"], messages=msgs,
                           joined_at=a["joined_at"], last_contact_at=a["last_seen"],
                           last_post_at=a["last_post_at"], ended_at=a["left_at"], host=a["host"],
                           agent_key=a["agent_key"], harness=a.get("harness"),
                           model=a.get("model"), os_user=a.get("os_user"),
                           left_reason=a.get("left_reason"), resume_of=a.get("resume_of"))

    def agents(self, job: str, include_departed: bool = True) -> list[AgentStatus]:
        s = self._s()
        with s.lock:
            now = self.now()
            rows = [a for a in s.agents.values() if a["job"] == job
                    and (include_departed or a["left_at"] is None)]
            rows.sort(key=lambda a: (a["left_at"] is not None, a["joined_at"]))
            return [self._status(a, now) for a in rows]

    # ---- per-agent sync state ----------------------------------------------------------

    def sync_state(self, agent_key: str) -> SyncState | None:
        s = self._s()
        with s.lock:
            a = s.agents.get(agent_key)
            if a is None:
                return None
            return SyncState(name=a["name"], joined_at=a["joined_at"], now=self.now(),
                             tool_calls=a["tool_calls"], last_post_at=a["last_post_at"],
                             replies_owed=self._replies_owed(a),
                             **{k: a.get(k, v) for k, v in _SYNC_DEFAULTS.items()})

    def _replies_owed(self, a: dict) -> tuple:
        mine = self._messages_of(a["job"])
        owed = []
        for m in mine:
            if m["to_agent"] != a["name"] or m["created_at"] < a["joined_at"] \
                    or not a.get("reply_reminded_id", 0) < m["id"] <= a["last_read_id"]:
                continue
            answered = any(r["agent_name"] == a["name"] and r["to_agent"] == m["agent_name"]
                           and r["id"] > m["id"] for r in mine)
            if not answered:
                owed.append(OwedReply(m["id"], m["agent_name"], m["created_at"]))
        return tuple(owed)

    def _update_row(self, agent_key: str, **fields) -> None:
        s = self._s()
        with s.lock:
            a = s.agents.get(agent_key)
            if a is not None:
                a.update(fields)
                s.touch()

    def record_roster_sync(self, agent_key: str, snapshot: str, full: bool) -> None:
        self._update_row(agent_key, roster_seen=snapshot,
                         **({"roster_synced_at": self.now()} if full else {}))

    def record_memory_recall(self, agent_key: str, shown_ids) -> None:
        s = self._s()
        with s.lock:
            a = s.agents.get(agent_key)
            if a is None:
                return
            seen = list(a.get("memory_seen", ()))
            seen += [i for i in dict.fromkeys(shown_ids) if i not in seen]
            a.update(memory_recalled_at=self.now(), memory_seen=tuple(seen[-MEMORY_SEEN_MAX:]))
            s.touch()

    def record_remembered(self, name: str) -> None:
        s = self._s()
        with s.lock:
            a = self._active_named(name)
            if a is not None:
                a["remembered_at"] = self.now()
                s.touch()

    def record_nudge(self, agent_key: str) -> None:
        self._update_row(agent_key, nudged_at=self.now())

    def record_silence_nudge(self, agent_key: str) -> None:
        self._update_row(agent_key, silence_nudged_at=self.now())

    def record_reply_reminder(self, agent_key: str, upto_id: int) -> None:
        s = self._s()
        with s.lock:
            a = s.agents.get(agent_key)
            if a is not None:
                a["reply_reminded_id"] = max(a.get("reply_reminded_id", 0), upto_id)
                s.touch()

    def agent_events(self, since: _dt.datetime, job: str | None = None) -> list[AgentEvent]:
        s = self._s()
        with s.lock:
            rows = [a for a in s.agents.values() if (job is None or a["job"] == job)
                    and (a["joined_at"] > since or (a["left_at"] is not None and a["left_at"] > since))]
            rows.sort(key=lambda a: a["left_at"] or a["joined_at"])
            return [AgentEvent(name=a["name"], job=a["job"], role=a["role"], joined_at=a["joined_at"],
                               left_at=a["left_at"], state=a["state"]) for a in rows]

    # ---- messages ------------------------------------------------------------------

    def _messages_of(self, job: str | None) -> list[dict]:
        """Stored messages of `job` (all jobs if None), in id order."""
        return [m for m in self._store.messages if job is None or m["job"] == job]

    @staticmethod
    def _msg(m: dict) -> Message:
        return Message(id=m["id"], created_at=m["created_at"], job=m["job"],
                       agent_name=m["agent_name"], to_agent=m["to_agent"], message=m["message"])

    def _insert_message(self, job: str, name: str, text: str, to: str | None,
                        agent_key: str | None) -> int:
        s = self._s()
        self.ensure_job(job)
        with s.lock:
            now = self.now()
            msg_id = s.next_id
            s.next_id += 1
            s.messages.append({"id": msg_id, "job": job, "agent_name": name, "created_at": now,
                               "message": text, "to_agent": to, "agent_key": agent_key,
                               "host": os.uname().nodename})
            poster = self._active_named(name)
            if poster is not None:
                poster.update(last_seen=now, last_post_at=now, calls_at_post=poster["tool_calls"])
            s.touch(messages=True)
            return msg_id

    def read_unread(self, agent_key: str | None = None, name: str | None = None,
                    job: str | None = None, advance: bool = True) -> ReadResult:
        s = self._s()
        with s.lock:  # the lock makes the read and the cursor move one atomic step
            reader = self._find_active(agent_key, name)
            if reader is None:
                return ReadResult([], 0)
            of_job = self._messages_of(job or reader["job"])
            last = reader["last_read_id"]
            unread = [m for m in of_job if m["id"] > last and m["agent_name"] != reader["name"]]
            rows = unread[: int(self.board_cfg["read_limit"])]
            remaining = len(unread) - len(rows)
            if advance:
                top = rows[-1]["id"] if remaining else max((m["id"] for m in of_job), default=last)
                reader.update(last_read_id=max(last, top), last_seen=self.now())
                s.touch()
            return ReadResult([self._msg(m) for m in rows], remaining)

    def recent_messages(self, limit: int, job: str | None = None,
                        active_jobs_only: bool = False) -> list[Message]:
        s = self._s()
        with s.lock:
            rows = self._messages_of(job)
            if job is None and active_jobs_only:
                active = {j for j, r in s.jobs.items() if r["status"] == "active"}
                rows = [m for m in rows if m["job"] in active]
            return [self._msg(m) for m in rows[-limit:]] if limit > 0 else []

    def messages_after(self, after_id: int, job: str | None = None) -> list[Message]:
        s = self._s()
        with s.lock:
            return [self._msg(m) for m in self._messages_of(job) if m["id"] > after_id]

    def last_message_id(self, job: str | None = None) -> int:
        s = self._s()
        with s.lock:
            return max((m["id"] for m in self._messages_of(job)), default=0)

    # ---- status ----------------------------------------------------------------------

    def _job_status(self, j: dict, now: _dt.datetime) -> JobStatus:
        s = self._store
        sts = [self._status(a, now) for a in s.agents.values() if a["job"] == j["job"]]
        msgs = self._messages_of(j["job"])
        stamps = [a.last_contact_at for a in sts] + [m["created_at"] for m in msgs]
        count = lambda *st: sum(1 for a in sts if a.status in st)  # noqa: E731
        return JobStatus(
            job=j["job"], status=j["status"], description=j["description"], task=j["task"],
            outcome=j["outcome"], created_by=j["created_by"], session_id=j["session_id"],
            created_at=j["created_at"], activated_at=j["activated_at"], finished_at=j["finished_at"],
            agents=len(sts), started=count("started"), running=count("running"), idle=count("idle"),
            completed=count("completed"), dead_or_left=count("dead", "left"), messages=len(msgs),
            last_activity_at=max(stamps) if stamps else None, project=j.get("project"),
            goal=j["goal"], verdict=j["verdict"], verdict_reason=j["verdict_reason"],
            verdict_by=j["verdict_by"], verdict_at=j["verdict_at"],
            completion_forced=j["completion_forced"],
            judge=(self._active_judge(j["job"]) or {}).get("name"),
            waiting_on=j.get("waiting_on"), waiting_since=j.get("waiting_since"),
            closed_by=j.get("closed_by"), supervise=j.get("supervise", True),
            verdict_next=j.get("verdict_next"), max_hours=j.get("max_hours"),
            waiting_until=j.get("waiting_until"))

    def job_status(self, job: str) -> JobStatus | None:
        s = self._s()
        with s.lock:
            j = s.jobs.get(job)
            return self._job_status(j, self.now()) if j else None

    def jobs(self, include_closed: bool = False) -> list[JobStatus]:
        s = self._s()
        with s.lock:
            now = self.now()
            rows = [self._job_status(j, now) for j in s.jobs.values()
                    if include_closed or j["status"] == "active"]
            # stable: active first, then by when the job started, then name -- never by
            # activity, which reshuffled the watch table on every refresh
            rows.sort(key=lambda r: (r.status != "active", r.activated_at or r.created_at, r.job))
            return rows

    # ---- transcripts ---------------------------------------------------------------------
    # Rows live in store.transcripts; a row's "body" is read and written through
    # store.transcript_body / put_transcript_body / drop_transcript_body, so the file backend
    # can keep bodies in files of their own.

    def save_transcript(self, row: TranscriptRow) -> bool:
        if row.role not in TRANSCRIPT_ROLES:
            raise ValueError(f"unknown transcript role {row.role!r}")
        check_images(row.images)   # recomputed: a sha256 names a file
        s = self._s()
        with s.lock:
            key = transcript_key(row.job, row.agent_key)
            old = s.transcripts.get(key)
            if old is not None and old["sha256"] == row.sha256 and (old["final"] or not row.final):
                return False
            if row.failed is not None and old is not None and old["final"] and not old.get("capture_failed"):
                return False   # a capture-failed marker never replaces a final capture
            now = self.now()
            known = self._image_meta(s)
            refs = []
            for img in dict((i.sha256, i) for i in row.images).values():
                meta = known.get(img.sha256)
                if meta is None:
                    s.put_image_body(img.sha256, img.data)
                    meta = {"sha256": img.sha256, "mime": img.mime, "size": int(img.size), "first_seen": now}
                refs.append(dict(meta))
            s.put_transcript_body(key, row.body)
            s.transcripts[key] = {
                "job": row.job, "agent_key": row.agent_key, "agent_name": row.agent_name,
                "role": row.role, "host": row.host, "session_id": row.session_id,
                "harness": row.harness,
                "final": bool(row.final), "raw_bytes": int(row.raw_bytes),
                "stored_bytes": len(row.body), "redactions": int(row.redactions),
                "sha256": row.sha256, "captured_at": row.captured_at or now, "images": refs,
                "capture_failed": row.failed}
            self._drop_orphan_images(s, {r["sha256"] for r in (old or {}).get("images", ())})
            return True

    def refresh_transcript(self, job: str, agent_key: str, final: bool) -> bool:
        s = self._s()
        with s.lock:
            row = s.transcripts.get(transcript_key(job, agent_key))
            if row is None:
                return False
            row["captured_at"] = self.now()
            row["final"] = bool(row["final"] or final)
            if final:   # the final capture found the stored content: it no longer failed
                row["capture_failed"] = None
            return True

    def mark_capture_failed(self, job: str, agent_key: str, reason: str, marker: TranscriptRow) -> str:
        s = self._s()
        with s.lock:
            key = transcript_key(job, agent_key)
            old = s.transcripts.get(key)
            if old is not None:
                if old["final"]:
                    return "kept"
                old["final"], old["capture_failed"] = True, reason   # the snapshot stays as it is
                return "marked"
            s.put_transcript_body(key, marker.body)
            s.transcripts[key] = {
                "job": job, "agent_key": agent_key, "agent_name": marker.agent_name, "role": marker.role,
                "host": marker.host, "session_id": marker.session_id, "harness": marker.harness,
                "final": True, "raw_bytes": 0, "stored_bytes": len(marker.body), "redactions": 0,
                "sha256": marker.sha256, "captured_at": marker.captured_at or self.now(), "images": [],
                "capture_failed": reason}
            return "stored"

    def pending_final_transcripts(self, host: str, os_user: str, harness: str, since) -> list[tuple[str, str]]:
        s = self._s()
        with s.lock:
            hits = []
            for a in s.agents.values():
                if a["left_at"] is None or a["left_at"] < since or a.get("host") != host \
                        or a.get("os_user") != os_user or a.get("harness") != harness:
                    continue
                row = s.transcripts.get(transcript_key(a["job"], a["agent_key"]))
                if row is None or not row["final"]:
                    hits.append((a["left_at"], a["job"], a["agent_key"]))
            return [(j, k) for _, j, k in sorted(hits)]

    # Each row's "images" is its references: [{sha256, mime, size, first_seen}]; the image
    # bytes are store.image_body(sha256), kept while any row (transcript or memory ref) refers to it.
    @staticmethod
    def _image_rows(s) -> list:
        return [*s.transcripts.values(), *s.memory_refs.values()]

    @staticmethod
    def _image_meta(s) -> dict:
        return {m["sha256"]: m for r in MemoryBoard._image_rows(s) for m in r.get("images", ())}

    @staticmethod
    def _drop_orphan_images(s, candidates) -> None:
        if candidates:
            live = {m["sha256"] for r in MemoryBoard._image_rows(s) for m in r.get("images", ())}
            for sha in set(candidates) - live:
                s.drop_image_body(sha)

    @staticmethod
    def _summary(r: dict) -> TranscriptSummary:
        imgs = r.get("images", ())
        ib = sum(m["size"] for m in imgs)
        f = {f: r[f] for f in _SUMMARY_FIELDS}
        f["raw_bytes"] += ib
        f["stored_bytes"] += ib
        return TranscriptSummary(**f, image_bytes=ib, images=tuple((m["sha256"], m["size"]) for m in imgs),
                                 harness=r.get("harness"), failed=r.get("capture_failed"))

    def transcript_image(self, sha256: str) -> TranscriptImage | None:
        s = self._s()
        with s.lock:
            meta = self._image_meta(s).get(sha256)
            data = s.image_body(sha256) if meta else None
        if meta is None or data is None:
            return None
        return TranscriptImage(sha256, meta["mime"], meta["size"], data, meta["first_seen"])

    def transcript_images(self, job: str | None = None, agent_key: str | None = None) -> list[TranscriptImage]:
        s = self._s()
        with s.lock:
            out = {}
            for r in s.transcripts.values():
                if (job is None or r["job"] == job) and (agent_key is None or r["agent_key"] == agent_key):
                    for m in r.get("images", ()):
                        out.setdefault(m["sha256"], TranscriptImage(m["sha256"], m["mime"], m["size"],
                                                                    None, m["first_seen"]))
            return [out[k] for k in sorted(out)]

    def transcripts(self, job: str | None = None, agent_name: str | None = None,
                    agent_key: str | None = None, role: str | None = None) -> list[TranscriptSummary]:
        s = self._s()
        with s.lock:
            rows = [r for r in s.transcripts.values()
                    if (job is None or r["job"] == job) and (agent_name is None or r["agent_name"] == agent_name)
                    and (agent_key is None or r["agent_key"] == agent_key) and (role is None or r["role"] == role)]
            rows.sort(key=lambda r: (r["captured_at"], r["job"], r["agent_key"]))
            return [self._summary(r) for r in rows]

    def transcript_body(self, job: str, agent_key: str) -> bytes | None:
        s = self._s()
        with s.lock:
            key = transcript_key(job, agent_key)
            row = s.transcripts.get(key)
            if row is None or (row.get("capture_failed") and not row["raw_bytes"]):
                return None   # none, or a bodiless capture-failed marker
            body = s.transcript_body(key)
        return None if body is None else decompress_transcript(body)

    def _delete_transcripts(self, rows, before: _dt.datetime, jobs) -> int:
        s = self._s()
        with s.lock:
            doomed = {transcript_key(j, k) for j, k in rows}
            keys = [k for k, r in s.transcripts.items()
                    if r["job"] in jobs or (k in doomed and r["captured_at"] < before)]
            dropped = set()
            for k in keys:
                dropped.update(m["sha256"] for m in s.transcripts[k].get("images", ()))
                del s.transcripts[k]
                s.drop_transcript_body(k)
            self._drop_orphan_images(s, dropped)
            return len(keys)

    # ---- memory provenance (schema 8) ------------------------------------------------------
    # Rows live in store.memory_refs; an excerpt is stored like a transcript body, under the key
    # "memory\x1f<document_id>" (no transcript key contains \x1f before a \x00), so the file
    # backend keeps it in a file of its own with no code of its own.

    _MREF_FIELDS = ("document_id", "bank", "job", "agent_key", "agent_name", "harness", "host", "session_id",
                    "tool_call_id", "writer", "raw_bytes", "redactions", "created_at", "checked_at", "patched",
                    "stored_bytes")

    @staticmethod
    def _mref_key(document_id: str) -> str:
        return "memory\x1f" + document_id

    def _save_memory_ref(self, ref: MemoryRef) -> str:
        s = self._s()
        with s.lock:
            old = s.memory_refs.get(ref.document_id)
            if old is not None and old["agent_key"] != ref.agent_key:
                return "kept"
            now = self.now()
            known = self._image_meta(s)
            imgs = []
            for img in dict((i.sha256, i) for i in ref.images).values():
                meta = known.get(img.sha256)
                if meta is None:
                    s.put_image_body(img.sha256, img.data)
                    meta = {"sha256": img.sha256, "mime": img.mime, "size": int(img.size), "first_seen": now}
                imgs.append(dict(meta))
            imgs.sort(key=lambda m: m["sha256"])
            key = self._mref_key(ref.document_id)
            if ref.excerpt is None:
                s.drop_transcript_body(key)
            else:
                s.put_transcript_body(key, ref.excerpt)
            s.memory_refs[ref.document_id] = {
                "document_id": ref.document_id, "bank": ref.bank, "job": ref.job, "agent_key": ref.agent_key,
                "agent_name": ref.agent_name, "harness": ref.harness, "host": ref.host,
                "session_id": ref.session_id, "tool_call_id": ref.tool_call_id, "writer": ref.writer,
                "raw_bytes": int(ref.raw_bytes), "redactions": int(ref.redactions),
                "created_at": ref.created_at or now, "checked_at": None, "patched": bool(ref.patched),
                "stored_bytes": len(ref.excerpt or b""), "images": imgs}
            self._drop_orphan_images(s, {m["sha256"] for m in (old or {}).get("images", ())})
            s.touch()        # wakes `watch` like any other state change; the file store writes nothing for it
            return "inserted" if old is None else "updated"

    def _mref(self, r: dict) -> MemoryRef:
        return MemoryRef(**{f: r[f] for f in self._MREF_FIELDS},
                         images=tuple(TranscriptImage(m["sha256"], m["mime"], m["size"], None, m["first_seen"])
                                      for m in r.get("images", ())))

    def memory_refs(self, job=None, agent_name=None, agent_key=None, document_id=None) -> list[MemoryRef]:
        s = self._s()
        with s.lock:
            rows = [r for r in s.memory_refs.values()
                    if (job is None or r["job"] == job) and (agent_name is None or r["agent_name"] == agent_name)
                    and (agent_key is None or r["agent_key"] == agent_key)
                    and (document_id is None or r["document_id"] == document_id)]
            rows.sort(key=lambda r: (r["created_at"], r["document_id"]))
            return [self._mref(r) for r in rows]

    def memory_ref_excerpt(self, document_id: str) -> bytes | None:
        s = self._s()
        with s.lock:
            if document_id not in s.memory_refs:
                return None
            body = s.transcript_body(self._mref_key(document_id))
        return None if body is None else decompress_capped(body)

    def mark_memory_refs_checked(self, document_ids) -> None:
        s = self._s()
        with s.lock:
            now = self.now()
            for d in document_ids:
                if d in s.memory_refs:
                    s.memory_refs[d]["checked_at"] = now

    def delete_memory_refs(self, document_ids, expected=None) -> int:
        s = self._s()
        with s.lock:
            dropped, n = set(), 0
            for d in dict.fromkeys(document_ids):
                r = s.memory_refs.get(d)
                if r is None:
                    continue
                if expected is not None:
                    cur = self._mref(r)
                    if d not in expected or tuple(expected[d]) != (cur.created_at, cur.checked_at):
                        continue
                s.memory_refs.pop(d)
                n += 1
                dropped.update(m["sha256"] for m in r.get("images", ()))
                s.drop_transcript_body(self._mref_key(d))
            self._drop_orphan_images(s, dropped)
            return n

    # ---- change notification -----------------------------------------------------------

    def _version(self) -> int:
        s = self._store
        return s.msg_version if self._subscribed else s.state_version

    def subscribe(self, messages_only: bool = False) -> None:
        s = self._s()
        with s.lock:
            self._subscribed = messages_only
            self._seen = self._version()

    def wait_for_change(self, timeout: float) -> bool:
        s = self._s()
        with s.lock:
            if self._subscribed is None:
                self.subscribe(False)
            changed = s.changed.wait_for(lambda: self._version() != self._seen, timeout=timeout)
            self._seen = self._version()
            return bool(changed)
