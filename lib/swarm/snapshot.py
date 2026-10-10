"""`swarm snapshot`: the board's current view as JSON, and `--follow`, the same view as a stream.

A pure read (see board.open_snapshot_board: a read-only session, every write method refused), so
a dashboard can run it as often, or as long, as it likes. Bounded output: at most MAX_JOBS jobs,
MAX_AGENTS agents and MAX_BLOCKERS open blockers per job, MAX_MESSAGES messages per job (the
`truncated` list says what was cut).

The stream (NDJSON, one object per line):

  {"type":"snapshot","reason":"initial|reconnect|resync|mismatch","gen":N,...}   the whole view
  {"type":"delta","gen":N+1,...}      what changed since gen N: upserts/removals, new messages, cursor
  {"type":"ping","gen":N,...}         nothing changed (every keepalive_s)
  {"type":"degraded","degraded":true|false,"mode":...,"reason":...}
                                      polling instead of push (a standby, a pooler that refuses
                                      LISTEN) or disconnected; false once push is back

How it stays right: LISTEN comes before the first fetch (a change between the two is seen twice,
never lost); a notification is only a hint, every wake re-reads the jobs, agents and open blockers
(so updates and deletions show) and the messages past the cursor, and emits the difference. A full
snapshot is sent on connect, after any reconnect, and on leaving polling mode. In between, every
`check_s` one cheap fingerprint of the scope (newest message id, digests of the job and agent rows)
is compared with the fingerprint of the state the stream has built; a full snapshot (reason
"mismatch") follows only if they differ, so a lost notification, a retention purge or a bug costs
at most one check interval, and a matching board costs one small query per interval. A consumer
applies deltas with apply_delta, which refuses a gap in `gen` (then it waits for the next snapshot
or restarts the stream).
"""
from __future__ import annotations

import dataclasses
import datetime as _dt
import hashlib
import json
import time
from dataclasses import dataclass, field

from swarm.board import BoardUnavailable, open_snapshot_board  # noqa: F401  (re-exported)

MAX_JOBS = 50
MAX_AGENTS = 200
MAX_BLOCKERS = 100
MAX_MESSAGES = 200
DEFAULT_MESSAGES = 20
VERSION = 1


class GapError(Exception):
    """A delta that does not continue the state it is applied to (a lost line)."""


@dataclass(frozen=True)
class Scope:
    """What a snapshot covers: the named jobs plus the jobs shown for each named session; with
    neither, every active or paused job. messages: how many recent messages per job (clamped)."""
    jobs: tuple = ()
    sessions: tuple = ()
    messages: int = DEFAULT_MESSAGES
    recent_minutes: int | None = 10   # finished agents older than this are only counted (hidden)

    def __post_init__(self):
        object.__setattr__(self, "jobs", tuple(dict.fromkeys(self.jobs)))
        object.__setattr__(self, "sessions", tuple(dict.fromkeys(self.sessions)))
        object.__setattr__(self, "messages", max(0, min(int(self.messages), MAX_MESSAGES)))

    def as_dict(self) -> dict:
        return {"jobs": list(self.jobs), "sessions": list(self.sessions), "messages": self.messages}


# ---------------------------------------------------------------------------- JSON rows

def _plain(value):
    if isinstance(value, _dt.datetime):
        return (value if value.tzinfo is None else value.astimezone(_dt.timezone.utc)).isoformat()
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    return value


def row(obj) -> dict:
    """A board record as a JSON-ready dict (datetimes as UTC ISO strings)."""
    return _plain(dataclasses.asdict(obj))


def dumps(obj) -> str:
    return json.dumps(obj, separators=(",", ":"), default=str)


def _job_key(j: dict):
    return (j.get("activated_at") or j.get("created_at") or "", j["job"])


def _agent_key(a: dict):
    return (a["job"], a.get("ended_at") is not None, a.get("joined_at") or "", a["agent_key"])


# ---------------------------------------------------------------------------- reading the board

@dataclass
class Fetched:
    """One read of the board for a Scope, as JSON-ready rows. after: the message cursor it was
    read from (0: the newest `scope.messages` per job, a full read)."""
    now: str
    jobs: list
    agents: list
    hidden: dict
    blockers: list
    messages: list
    after: int
    mode: str = "push"
    degraded: str | None = None

    @property
    def full(self) -> bool:
        return self.after == 0


def _scope_jobs(board, scope: Scope) -> list:
    found: dict = {}
    if scope.jobs or scope.sessions:
        for name in scope.jobs:
            j = board.job_status(name)
            if j is not None:
                found.setdefault(j.job, j)
        for session in scope.sessions:
            for j in board.session_shown_jobs(session):
                found.setdefault(j.job, j)
    else:
        for j in board.jobs():
            found.setdefault(j.job, j)
        for j in board.jobs(True):
            if j.status == "paused":
                found.setdefault(j.job, j)
    return list(found.values())


def read_view(board, scope: Scope, after: int = 0) -> Fetched:
    """Read the view of `scope`. after > 0: messages with id > after only (the incremental
    read); the jobs, agents and open blockers are always read whole (they are small)."""
    if hasattr(board, "watch_snapshot"):   # Postgres: the whole view in one statement
        view = board.watch_snapshot(None, None, scope.recent_minutes, scope.messages,
                                    jobs=scope.jobs, sessions=scope.sessions, after_id=after)
        return Fetched(_plain(view.now()), [row(j) for j in view.job_rows], [row(a) for a in view.agent_rows],
                       {k: int(v) for k, v in view.hidden.items()}, [row(b) for b in view.blocker_rows],
                       [row(m) for m in view.message_rows], after, board.change_mode, board.degraded)
    jobs = _scope_jobs(board, scope)
    agents, hidden, blockers, messages = [], {}, [], []
    for j in jobs:
        visible, older = board.watch_agents(j.job, scope.recent_minutes)
        agents += visible
        if older:
            hidden[j.job] = older
        blockers += board.blockers(j.job)
        if scope.messages:
            if after:
                got = board.messages_after(after, j.job)[-scope.messages:]
            else:
                got = board.recent_messages(scope.messages, j.job)
            messages += got
    return Fetched(_plain(board.now()), [row(j) for j in jobs], [row(a) for a in agents], hidden,
                   [row(b) for b in blockers], [row(m) for m in messages], after, board.change_mode, board.degraded)


# ---------------------------------------------------------------------------- state and deltas

def _trim(messages: dict, per_job: int) -> None:
    """Keep the newest `per_job` messages of each job (the rule of a full read)."""
    by_job: dict = {}
    for mid in sorted(messages):
        by_job.setdefault(messages[mid]["job"], []).append(mid)
    for ids in by_job.values():
        for mid in ids[:-per_job] if per_job else ids:
            del messages[mid]


class ViewState:
    """What a consumer of the stream holds, kept on the producing side to compute deltas."""

    def __init__(self, scope: Scope):
        self.scope, self.gen = scope, 0
        self._clear()

    def _clear(self) -> None:
        self.jobs, self.agents, self.blockers, self.messages = {}, {}, {}, {}
        self.hidden, self.truncated, self.cursor, self.now = {}, [], 0, None

    # -- bounded, canonical content of one read
    def _canon(self, f: Fetched):
        truncated = []
        jobs = sorted(f.jobs, key=_job_key)
        if len(jobs) > MAX_JOBS:
            jobs, truncated = jobs[:MAX_JOBS], ["jobs"]
        names = {j["job"] for j in jobs}

        def per_job(rows, key, cap, label):
            out, count = [], {}
            for r in sorted((r for r in rows if r["job"] in names), key=key):
                count[r["job"]] = count.get(r["job"], 0) + 1
                if count[r["job"]] <= cap:
                    out.append(r)
                elif label not in truncated:
                    truncated.append(label)
            return out
        agents = per_job(f.agents, _agent_key, MAX_AGENTS, "agents")
        blockers = per_job(f.blockers, lambda b: b["id"], MAX_BLOCKERS, "blockers")
        hidden = {k: v for k, v in f.hidden.items() if k in names}
        messages = [m for m in f.messages if m["job"] in names]
        return jobs, agents, blockers, hidden, messages, sorted(truncated)

    def snapshot(self, reason: str | None = None) -> dict:
        out = {"type": "snapshot", "v": VERSION, "gen": self.gen, "now": self.now,
               "scope": self.scope.as_dict(),
               "jobs": sorted(self.jobs.values(), key=_job_key),
               "agents": sorted(self.agents.values(), key=_agent_key),
               "hidden": dict(sorted(self.hidden.items())),
               "blockers": sorted(self.blockers.values(), key=lambda b: b["id"]),
               "messages": [self.messages[i] for i in sorted(self.messages)],
               "cursor": self.cursor, "truncated": list(self.truncated)}
        if reason:
            out["reason"] = reason
        return out

    def reset(self, f: Fetched, reason: str = "initial") -> dict:
        """Replace the state with a full read; the snapshot line to send (gen advances)."""
        jobs, agents, blockers, hidden, messages, truncated = self._canon(f)
        self._clear()
        self.jobs = {j["job"]: j for j in jobs}
        self.agents = {(a["job"], a["agent_key"]): a for a in agents}
        self.blockers = {b["id"]: b for b in blockers}
        self.hidden, self.truncated, self.now = hidden, truncated, f.now
        self.messages = {m["id"]: m for m in messages}
        _trim(self.messages, self.scope.messages)
        self.cursor = max(self.messages, default=0)
        self.gen += 1
        return self.snapshot(reason)

    def diff(self, f: Fetched, refetch_full=None) -> dict | None:
        """Fold a read into the state; the delta line to send, None if nothing changed. A job
        that was not in the state before (its older messages are not in an incremental read):
        `refetch_full()` (a full read) is used instead."""
        if f.after and refetch_full is not None and {j["job"] for j in f.jobs} - set(self.jobs):
            f = refetch_full()
        jobs, agents, blockers, hidden, messages, truncated = self._canon(f)
        names = {j["job"] for j in jobs}
        d_jobs = {"upsert": [j for j in jobs if self.jobs.get(j["job"]) != j],
                  "remove": sorted(set(self.jobs) - names)}
        akeys = {(a["job"], a["agent_key"]) for a in agents}
        d_agents = {"upsert": [a for a in agents if self.agents.get((a["job"], a["agent_key"])) != a],
                    "remove": sorted([list(k) for k in set(self.agents) - akeys])}
        bids = {b["id"] for b in blockers}
        d_blockers = {"upsert": [b for b in blockers if self.blockers.get(b["id"]) != b],
                      "remove": sorted(set(self.blockers) - bids)}
        new_msgs = [m for m in messages if m["id"] not in self.messages]
        gone = {i for i, m in self.messages.items() if m["job"] not in names}
        if f.full:
            gone |= set(self.messages) - {m["id"] for m in messages}
        changed = bool(d_jobs["upsert"] or d_jobs["remove"] or d_agents["upsert"] or d_agents["remove"]
                       or d_blockers["upsert"] or d_blockers["remove"] or new_msgs or gone
                       or hidden != self.hidden or truncated != self.truncated)
        if not changed:
            return None
        self.jobs = {j["job"]: j for j in jobs}
        self.agents = {(a["job"], a["agent_key"]): a for a in agents}
        self.blockers = {b["id"]: b for b in blockers}
        for i in gone:
            self.messages.pop(i, None)
        for m in new_msgs:
            self.messages[m["id"]] = m
        _trim(self.messages, self.scope.messages)
        sent = [m for m in new_msgs if m["id"] in self.messages]
        self.cursor = max([self.cursor, *(m["id"] for m in new_msgs)])
        self.hidden, self.truncated, self.now = hidden, truncated, f.now
        self.gen += 1
        return {"type": "delta", "gen": self.gen, "now": f.now, "jobs": d_jobs, "agents": d_agents,
                "blockers": d_blockers, "hidden": hidden, "truncated": truncated, "messages": sent,
                "messages_remove": sorted(gone), "cursor": self.cursor}


def apply_delta(base: dict, delta: dict) -> dict:
    """The consumer's reducer: `base` (a snapshot dict) with one delta applied; a new dict.
    Raises GapError if the delta is not the next one."""
    if delta["gen"] != base["gen"] + 1:
        raise GapError(f"delta {delta['gen']} does not follow {base['gen']}")
    jobs = {j["job"]: j for j in base["jobs"]}
    for name in delta["jobs"].get("remove", ()):
        jobs.pop(name, None)
    jobs.update({j["job"]: j for j in delta["jobs"].get("upsert", ())})
    agents = {(a["job"], a["agent_key"]): a for a in base["agents"]}
    for key in delta["agents"].get("remove", ()):
        agents.pop(tuple(key), None)
    agents.update({(a["job"], a["agent_key"]): a for a in delta["agents"].get("upsert", ())})
    blockers = {b["id"]: b for b in base["blockers"]}
    for bid in delta["blockers"].get("remove", ()):
        blockers.pop(bid, None)
    blockers.update({b["id"]: b for b in delta["blockers"].get("upsert", ())})
    messages = {m["id"]: m for m in base["messages"]}
    for mid in delta.get("messages_remove", ()):
        messages.pop(mid, None)
    messages.update({m["id"]: m for m in delta.get("messages", ())})
    _trim(messages, base["scope"]["messages"])
    out = {**base, "gen": delta["gen"], "now": delta.get("now", base.get("now")),
           "jobs": sorted(jobs.values(), key=_job_key), "agents": sorted(agents.values(), key=_agent_key),
           "hidden": dict(sorted(delta["hidden"].items())) if "hidden" in delta else base["hidden"],
           "blockers": sorted(blockers.values(), key=lambda b: b["id"]),
           "messages": [messages[i] for i in sorted(messages)],
           "cursor": max(base["cursor"], delta.get("cursor", 0)),
           "truncated": delta.get("truncated", base.get("truncated", []))}
    out.pop("reason", None)
    return out


def snapshot_once(board, scope: Scope) -> dict:
    """One full snapshot (what `swarm snapshot --json` prints)."""
    state = ViewState(scope)
    out = state.reset(read_view(board, scope, 0), "request")
    out["mode"] = board.change_mode
    out["degraded"] = board.degraded
    return out


# ---------------------------------------------------------------------------- fingerprints

_EPOCH = _dt.datetime(1970, 1, 1, tzinfo=_dt.timezone.utc)


def _usec(value) -> str:
    """A timestamp (ISO string or datetime) as epoch microseconds, '' for none: what the SQL
    digest of PostgresBoard.scope_fingerprint builds with extract(epoch ...) * 1000000."""
    if value is None:
        return ""
    if isinstance(value, str):
        value = _dt.datetime.fromisoformat(value)
    if value.tzinfo is None:
        value = value.replace(tzinfo=_dt.timezone.utc)
    return str((value - _EPOCH) // _dt.timedelta(microseconds=1))


def _digest(lines: list) -> tuple:
    lines = sorted(lines)   # code point order == the SQL's COLLATE "C" on UTF-8
    return len(lines), hashlib.md5(",".join(lines).encode()).hexdigest() if lines else ""


def fingerprint_of_rows(jobs, agents, max_id: int, statuses: bool = True) -> tuple:
    """(newest message id, job count, job digest, agent count, agent digest) of JSON rows, the
    same value PostgresBoard.scope_fingerprint computes server side for the same view."""
    jc, jd = _digest([f"{j['job']}|{j['status']}|{j['agents']}|{j['messages']}|{_usec(j.get('last_activity_at'))}"
                      for j in jobs])
    def shown(a) -> str:
        return a["status"] if statuses or a["status"] in ("completed", "left") else "live"
    ac, ad = _digest([f"{a['job']}|{a['agent_key']}|{shown(a)}|{1 if a.get('ended_at') else 0}|{a['tool_calls']}"
                      f"|{_usec(a.get('last_contact_at'))}" for a in agents])
    return (max_id, jc, jd, ac, ad)


def fingerprint_of_state(state: "ViewState") -> tuple | None:
    """The fingerprint of what a consumer holds; None when the state was cut (caps), because the
    server's digest covers rows it does not hold."""
    if state.truncated:
        return None
    return fingerprint_of_rows(state.jobs.values(), state.agents.values(), max(state.messages, default=0) if state.scope.messages else 0)


def remote_fingerprint(board, scope: Scope) -> tuple:
    """(fingerprint, fetched): the board's fingerprint of `scope`. Postgres computes it in one
    cheap statement (fetched is None). Other backends have no digest: they read the view in full
    (local and cheap) and the read is returned so a mismatch needs no second one."""
    if hasattr(board, "scope_fingerprint"):
        return tuple(board.scope_fingerprint(scope.jobs, scope.sessions, scope.recent_minutes,
                                             bool(scope.messages))), None
    f = read_view(board, scope, 0)
    return fingerprint_of_rows(f.jobs, f.agents, max((m["id"] for m in f.messages), default=0)), f


# ---------------------------------------------------------------------------- the stream

BACKOFF = (1.0, 2.0, 4.0, 8.0, 15.0)


@dataclass
class Follower:
    open_board: object
    scope: Scope
    emit: object
    check_s: float
    keepalive_s: float
    poll_s: float
    clock: object
    sleep: object
    stop: object
    state: ViewState = field(init=False)
    degraded: bool = False
    last_emit: float = 0.0
    last_check: float = 0.0
    reason: str = "initial"

    def __post_init__(self):
        self.state = ViewState(self.scope)

    # -- lines
    def send(self, line: dict) -> None:
        self.last_emit = self.clock()
        self.emit(line)

    def say_degraded(self, flag: bool, mode: str | None, reason: str | None = None) -> None:
        if flag == self.degraded:
            return
        self.degraded = flag
        line = {"type": "degraded", "degraded": flag, "mode": mode, "gen": self.state.gen}
        if reason:
            line["reason"] = reason
        self.send(line)

    def full(self, board, reason: str) -> None:
        self.full_from(read_view(board, self.scope, 0), reason)

    def full_from(self, fetched, reason: str) -> None:
        self.send(self.state.reset(fetched, reason))
        self.last_check = self.clock()

    def check(self, board) -> None:
        """The backstop, every check_s: compare the board's fingerprint of the scope with the
        state's; a full read only if they differ (or the state was cut, so it cannot be compared)."""
        mine = fingerprint_of_state(self.state)
        theirs, fetched = remote_fingerprint(board, self.scope)
        self.last_check = self.clock()
        if mine is None or mine != theirs:
            self.full_from(fetched or read_view(board, self.scope, 0), "mismatch")

    def incremental(self, board) -> None:
        f = read_view(board, self.scope, self.state.cursor)
        delta = self.state.diff(f, lambda: read_view(board, self.scope, 0))
        if delta is not None:
            self.send(delta)

    # -- one connection's life
    def session(self, board) -> None:
        board.subscribe()   # LISTEN first: a change after this is a wake-up, never lost
        mode = board.change_mode
        self.say_degraded(mode == "degraded", mode)
        self.full(board, self.reason)
        self.reason = "resync"
        while not self.stop():
            now = self.clock()
            if mode == "push":
                timeout = min(self.keepalive_s - (now - self.last_emit), self.check_s - (now - self.last_check))
                timeout = min(max(timeout, 0.05), self.keepalive_s)
            else:
                timeout = self.poll_s
            woke = board.wait_for_change(timeout)
            now = self.clock()
            now_mode = board.change_mode
            if now_mode != mode:
                was, mode = mode, now_mode
                if was == "degraded":   # LISTEN (or the primary) is back: the notifications we missed
                    self.say_degraded(False, mode)   # are not recoverable, so resync in full
                    self.full(board, "resync")
                    continue
                self.say_degraded(mode == "degraded", mode, "listen unavailable")
                woke = True
            if now - self.last_check >= self.check_s:
                if woke or mode != "push":
                    self.incremental(board)
                self.check(board)
            elif woke or mode != "push":
                self.incremental(board)
            if now - self.last_emit >= self.keepalive_s:
                self.send({"type": "ping", "gen": self.state.gen,
                           "at": _dt.datetime.now(_dt.timezone.utc).isoformat()})

    def run(self) -> None:
        attempt = 0
        while not self.stop():
            try:
                board = self.open_board()
            except BoardUnavailable as exc:
                self.say_degraded(True, None, f"unavailable: {exc}")
                self.sleep(BACKOFF[min(attempt, len(BACKOFF) - 1)])
                attempt += 1
                continue
            attempt = 0
            try:
                with board:
                    self.session(board)
                return
            except BoardUnavailable as exc:
                self.say_degraded(True, None, f"disconnected: {exc}")
                self.reason = "reconnect"
                self.sleep(BACKOFF[0])


def follow(open_board, scope: Scope, emit, *, check_s: float = 60.0, keepalive_s: float = 15.0,
           poll_s: float = 2.0, clock=time.monotonic, sleep=time.sleep, stop=lambda: False) -> None:
    """Stream the view of `scope` through emit(line_dict) until stop() is true (or forever).
    open_board() returns a snapshot board (board.open_snapshot_board) or raises BoardUnavailable;
    the stream reconnects by itself, with back-off, and says so (a "degraded" line)."""
    Follower(open_board, scope, emit, check_s, keepalive_s, poll_s, clock, sleep, stop).run()
