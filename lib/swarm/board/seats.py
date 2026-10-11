"""Drive seats, drive locks and the atomic launch claim (schema 25, spec §3.4 and §4.5).

MemorySeats works on the MemoryStore (the memory and file backends, under the store lock).
SqlSeats runs the same statements on SQLite and Postgres: placeholders and timestamps are adapted
the way blockers.py does it. A claim is one transaction. The host-wide serialisation of the overlap
check is the Postgres advisory lock `swarm-wt:<host>`, SQLite's write lock (BEGIN IMMEDIATE) and the
store lock for memory and file.
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import getpass
import re

from swarm import compat
from .base import SEAT_CLOSED, SEAT_COMPLEXITY, SEAT_HARNESSES, SEAT_STATES, DriveLock, RESTART_COLS, Restart, Seat
from ..supervisor.worktree import roots_overlap

SEAT_COLS = ("id, job, generation, seat, role, title, harness, model, workdir, brief_path, task_ref, complexity, "
             "requested_by, requested_at, state, agent_key, session_id, revision, launches, last_outcome, "
             "wake_reason, wake_watermark, updated_at")
LOCK_COLS = "key, holder, job, generation, launch_id, host, expires_at, path"
# The fields a request sets (and compares with its row): a repeat with the same ones is a no-op.
REQUEST_FIELDS = ("role", "harness", "model", "workdir", "brief_path", "title", "task_ref", "complexity",
                  "requested_by")
# What set_seat_state may change besides the state (agent and session the seat runs, wake bookkeeping).
STATE_FIELDS = ("agent_key", "session_id", "last_outcome", "wake_reason", "wake_watermark")
# A claim may start a seat that is waiting for its first session (requested) or resuming (idle).
LAUNCHABLE = ("requested", "idle")
_SEAT_NAME = re.compile(r"[a-z][a-z0-9_~-]{0,63}")   # `~` names a successor: lead~1


def check_seat(seat, generation, role, harness, workdir, brief_path, complexity, requested_by):
    if not isinstance(seat, str) or _SEAT_NAME.fullmatch(seat) is None:
        raise ValueError("seat name must be 1-64 lowercase letters, digits, _, - or ~, starting with a letter")
    if not isinstance(generation, int) or generation < 1:
        raise ValueError("seat generation must be a positive integer")
    for what, value in (("role", role), ("workdir", workdir), ("brief_path", brief_path),
                        ("requested_by", requested_by)):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"seat {what} must be nonempty")
    if harness not in SEAT_HARNESSES:
        raise ValueError(f"seat harness must be one of {', '.join(SEAT_HARNESSES)}")
    if complexity not in SEAT_COMPLEXITY:
        raise ValueError(f"seat complexity must be one of {', '.join(SEAT_COMPLEXITY)}")


def check_lock_keys(locks, lock_paths):
    """Every key once, and a `wt:` key with the worktree root it stands for."""
    if len(set(locks)) != len(locks):
        raise ValueError("a claim names each drive lock once")
    for key in locks:
        if not isinstance(key, str) or not key:
            raise ValueError("a drive lock key must be a nonempty string")
        if key.startswith("wt:") and not lock_paths.get(key):
            raise ValueError(f"worktree lock {key} needs its root path")


def wt_paths_overlap(new_paths, live_paths) -> bool:
    """Whether any new worktree root overlaps a live one, or another new one."""
    for i, a in enumerate(new_paths):
        if any(roots_overlap(a, b) for b in live_paths) or any(roots_overlap(a, b) for b in new_paths[i + 1:]):
            return True
    return False


def request_fields(**kw) -> dict:
    return {k: kw.get(k) for k in REQUEST_FIELDS}


def _check_state(state, fields):
    if state not in SEAT_STATES:
        raise ValueError(f"seat state must be one of {', '.join(SEAT_STATES)}")
    unknown = set(fields) - set(STATE_FIELDS)
    if unknown:
        raise ValueError(f"not a seat field: {', '.join(sorted(unknown))}")


class _Refused(Exception):
    """A claim conflict found inside its transaction: the transaction rolls back, nothing is kept."""


class MemorySeats:
    """Seats, drive locks and the launch claim over the shared MemoryStore."""

    def request_seat(self, job, generation, seat, *, role, harness, model, workdir, brief_path, title=None,
                     task_ref=None, complexity="standard", requested_by):
        check_seat(seat, generation, role, harness, workdir, brief_path, complexity, requested_by)
        fields = dict(role=role, harness=harness, model=model, workdir=workdir, brief_path=brief_path,
                      title=title, task_ref=task_ref, complexity=complexity, requested_by=requested_by)
        s = self._s()
        with s.lock:
            if job not in s.jobs:
                raise ValueError(f"{job} is not a job")
            now = self.now()
            row = next((r for r in s.seats if r["job"] == job and r["generation"] == generation
                        and r["seat"] == seat), None)
            if row is None:
                row = dict(id=s.next_seat_id, job=job, generation=generation, seat=seat, **fields,
                           requested_at=now, state="requested", agent_key=None, session_id=None, revision=1,
                           launches=0, last_outcome=None, wake_reason=None, wake_watermark=None, updated_at=now)
                s.next_seat_id += 1
                s.seats.append(row)
                s.touch()
            elif all(row[k] == v for k, v in fields.items()):
                pass                                    # the same request: the same row and revision
            elif row["state"] == "requested":
                row.update(fields, revision=row["revision"] + 1, updated_at=now)
                s.touch()
            else:
                raise ValueError(f"seat is {row['state']}: release it first")
            return Seat(**row)

    def seats(self, job, generation=None, include_closed=True):
        s = self._s()
        with s.lock:
            return [Seat(**r) for r in s.seats if r["job"] == job
                    and (generation is None or r["generation"] == generation)
                    and (include_closed or r["state"] not in SEAT_CLOSED)]

    def seat(self, job, generation, seat):
        s = self._s()
        with s.lock:
            row = next((r for r in s.seats if r["job"] == job and r["generation"] == generation
                        and r["seat"] == seat), None)
            return Seat(**row) if row else None

    def set_seat_state(self, seat_id, expected_revision, state, **fields):
        _check_state(state, fields)
        s = self._s()
        with s.lock:
            row = next((r for r in s.seats if r["id"] == seat_id), None)
            if row is None or row["revision"] != expected_revision:
                return None
            row.update(fields, state=state, revision=row["revision"] + 1, updated_at=self.now())
            s.touch()
            return Seat(**row)

    def claim_drive_launch(self, *, job, generation, action_id, attempt, launch_id, seat_id, seat_revision,
                           harness, reserve_usd, reserve_minutes, locks, lock_seconds, max_host_running,
                           ruling=False, lock_paths=None):
        lock_paths = dict(lock_paths or {})
        check_lock_keys(locks, lock_paths)
        if harness not in SEAT_HARNESSES:
            raise ValueError(f"seat harness must be one of {', '.join(SEAT_HARNESSES)}")
        s = self._s()
        with s.lock:
            now = self.now()
            host = compat.node()
            row = next((r for r in s.seats if r["id"] == seat_id), None)
            if (row is None or row["job"] != job or row["generation"] != generation
                    or row["revision"] != seat_revision or row["state"] not in LAUNCHABLE):
                return None
            # the host's cap on open restarts (one more for a ruling), counted under the lock
            if sum(1 for r in s.restarts if r["host"] == host and r["ended_at"] is None) >= \
                    int(max_host_running) + (1 if ruling else 0):
                return None
            # one attempt of an action once: the unique (job, action_id, attempt)
            if any(r.get("action_id") == action_id and r["job"] == job and r["attempt"] == attempt
                   for r in s.restarts):
                return None
            if any(r["old_agent_key"] == f"launch:{launch_id}" for r in s.restarts):
                return None
            if any(d["key"] in locks for d in s.drive_locks):
                return None
            live_wt = [d["path"] for d in s.drive_locks if d["host"] == host and d["key"].startswith("wt:")
                       and d["path"]]
            new_wt = [lock_paths[k] for k in sorted(locks) if k.startswith("wt:")]
            if wt_paths_overlap(new_wt, live_wt):
                return None
            expires = now + _dt.timedelta(seconds=lock_seconds)
            for key in sorted(locks):
                s.drive_locks.append(dict(key=key, holder=action_id, job=job, generation=generation,
                                          launch_id=launch_id, host=host, expires_at=expires,
                                          path=lock_paths.get(key)))
            restart = dict(id=s.next_restart_id, job=job, agent_key=action_id, attempt=attempt, at=now,
                           reason=f"drive:{action_id}", old_agent_key=f"launch:{launch_id}", new_agent_key=None,
                           harness=harness, host=host, os_user=getpass.getuser(), minutes_cap=float(reserve_minutes),
                           ended_at=None, outcome=None, action_id=action_id, launch_id=launch_id,
                           reserved_usd=float(reserve_usd), cost_usd=None, tokens_in=None, tokens_out=None)
            s.next_restart_id += 1
            s.restarts.append(restart)
            row.update(state="running", revision=row["revision"] + 1, launches=row["launches"] + 1, updated_at=now)
            s.touch()
            return Restart(**restart)

    def renew_drive_locks(self, launch_id, expires_at):
        s = self._s()
        with s.lock:
            n = 0
            for d in s.drive_locks:
                if d["launch_id"] == launch_id:
                    d["expires_at"] = expires_at
                    n += 1
            if n:
                s.touch()
            return n

    def release_drive_locks(self, launch_id):
        s = self._s()
        with s.lock:
            before = len(s.drive_locks)
            s.drive_locks[:] = [d for d in s.drive_locks if d["launch_id"] != launch_id]
            if len(s.drive_locks) != before:
                s.touch()
            return before - len(s.drive_locks)

    def drive_locks(self, prefix=""):
        s = self._s()
        with s.lock:
            return [DriveLock(**d) for d in sorted(s.drive_locks, key=lambda d: d["key"])
                    if d["key"].startswith(prefix)]


def seats_schema(pg: bool = False) -> str:
    """Schema 25 part 2: the seats and drive_locks tables, the restart columns of a claim and the
    unique (job, action_id, attempt). Runs after the base schema, so restarts exists. Postgres
    adds the restart columns here; SQLite adds them in MIGRATIONS, before this runs."""
    id_type = "bigserial PRIMARY KEY" if pg else "INTEGER PRIMARY KEY AUTOINCREMENT"
    ts = "timestamptz" if pg else "TEXT"
    big = "bigint" if pg else "INTEGER"
    real = "double precision" if pg else "REAL"
    alter = "ALTER TABLE restarts ADD COLUMN IF NOT EXISTS {} {};\n" if pg else ""
    restart_cols = "".join(alter.format(c, t) for c, t in (
        ("action_id", "text"), ("launch_id", "text"), ("reserved_usd", real), ("cost_usd", real),
        ("tokens_in", big), ("tokens_out", big)))
    return f"""
{restart_cols}CREATE UNIQUE INDEX IF NOT EXISTS restarts_action_attempt ON restarts (job, action_id, attempt)
 WHERE action_id IS NOT NULL;
CREATE TABLE IF NOT EXISTS seats (
 id {id_type}, job TEXT NOT NULL REFERENCES jobs(job) ON DELETE CASCADE, generation {big} NOT NULL,
 seat TEXT NOT NULL, role TEXT NOT NULL, title TEXT, harness TEXT NOT NULL CHECK (harness IN ('claude', 'codex')),
 model TEXT, workdir TEXT NOT NULL, brief_path TEXT NOT NULL, task_ref TEXT,
 complexity TEXT NOT NULL DEFAULT 'standard' CHECK (complexity IN ('simple', 'standard', 'complex')),
 requested_by TEXT NOT NULL, requested_at {ts} NOT NULL,
 state TEXT NOT NULL CHECK (state IN ('requested', 'running', 'idle', 'released', 'failed', 'cancelled', 'refused')),
 agent_key TEXT, session_id TEXT, revision {big} NOT NULL DEFAULT 1, launches {big} NOT NULL DEFAULT 0,
 last_outcome TEXT, wake_reason TEXT, wake_watermark {big}, updated_at {ts} NOT NULL,
 UNIQUE (job, generation, seat));
CREATE TABLE IF NOT EXISTS drive_locks (
 key TEXT PRIMARY KEY, holder TEXT NOT NULL, job TEXT NOT NULL, generation {big} NOT NULL,
 launch_id TEXT NOT NULL, host TEXT NOT NULL, expires_at {ts} NOT NULL, path TEXT);
CREATE INDEX IF NOT EXISTS drive_locks_launch ON drive_locks (launch_id);
"""


class SqlSeats:
    """Seats, drive locks and the launch claim on SQLite (`_seat_pg` False) and Postgres (True)."""

    _seat_pg = False

    def _sx(self, sql):
        return sql.replace("?", "%s") if self._seat_pg else sql

    def _seat_time(self, value):
        """A timestamp as the backend stores it (Postgres: the datetime; SQLite: its text form)."""
        if value is None or self._seat_pg:
            return value
        from .sqlite import _ts
        return _ts(value)

    def _seat_time_out(self, value):
        if value is None or self._seat_pg or isinstance(value, _dt.datetime):
            return value
        return _dt.datetime.fromisoformat(value)   # the text _ts wrote

    def _seat_obj(self, cls, cols, row):
        names = [c.strip() for c in cols.split(",")]
        d = dict(zip(names, row))
        for f in ("requested_at", "updated_at", "expires_at"):
            if f in d:
                d[f] = self._seat_time_out(d[f])
        return cls(**d)

    @contextlib.contextmanager
    def _seat_tx(self):
        """One write transaction. SQLite: BEGIN IMMEDIATE (or the enclosing one, under a savepoint so a
        refused claim undoes its own writes); Postgres: a transaction, or a savepoint inside one."""
        if self._seat_pg:
            with self._conn.transaction():
                yield self._conn
            return
        with self._tx() as c:
            c.execute("SAVEPOINT drive_seats")
            try:
                yield c
            except BaseException:
                c.execute("ROLLBACK TO drive_seats")
                c.execute("RELEASE drive_seats")
                raise
            c.execute("RELEASE drive_seats")

    def _exec(self, c, sql, args=()):
        return c.execute(self._sx(sql), args)

    def request_seat(self, job, generation, seat, *, role, harness, model, workdir, brief_path, title=None,
                     task_ref=None, complexity="standard", requested_by):
        check_seat(seat, generation, role, harness, workdir, brief_path, complexity, requested_by)
        fields = dict(role=role, harness=harness, model=model, workdir=workdir, brief_path=brief_path,
                      title=title, task_ref=task_ref, complexity=complexity, requested_by=requested_by)
        with self._seat_tx() as c:
            if self._exec(c, "SELECT 1 FROM jobs WHERE job = ?", (job,)).fetchone() is None:
                raise ValueError(f"{job} is not a job")
            now = self._seat_time(self.now())
            row = self._exec(c, f"SELECT {SEAT_COLS} FROM seats WHERE job = ? AND generation = ? AND seat = ?",
                             (job, generation, seat)).fetchone()
            if row is None:
                row = self._exec(
                    c, "INSERT INTO seats (job, generation, seat, role, title, harness, model, workdir, brief_path, "
                    "task_ref, complexity, requested_by, requested_at, state, revision, launches, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'requested', 1, 0, ?) "
                    f"RETURNING {SEAT_COLS}",
                    (job, generation, seat, role, title, harness, model, workdir, brief_path, task_ref,
                     complexity, requested_by, now, now)).fetchone()
                return self._seat_obj(Seat, SEAT_COLS, row)
            current = self._seat_obj(Seat, SEAT_COLS, row)
            if all(getattr(current, k) == v for k, v in fields.items()):
                return current
            if current.state != "requested":
                raise ValueError(f"seat is {current.state}: release it first")
            row = self._exec(
                c, "UPDATE seats SET role = ?, harness = ?, model = ?, workdir = ?, brief_path = ?, title = ?, "
                "task_ref = ?, complexity = ?, requested_by = ?, revision = revision + 1, updated_at = ? "
                f"WHERE id = ? RETURNING {SEAT_COLS}",
                (*fields.values(), now, current.id)).fetchone()
            return self._seat_obj(Seat, SEAT_COLS, row)

    def seats(self, job, generation=None, include_closed=True):
        sql, args = f"SELECT {SEAT_COLS} FROM seats WHERE job = ?", [job]
        if generation is not None:
            sql += " AND generation = ?"
            args.append(generation)
        if not include_closed:
            sql += " AND state NOT IN (" + ", ".join("?" for _ in SEAT_CLOSED) + ")"
            args.extend(SEAT_CLOSED)
        rows = self._exec(self._conn, sql + " ORDER BY id", args).fetchall()
        return [self._seat_obj(Seat, SEAT_COLS, r) for r in rows]

    def seat(self, job, generation, seat):
        row = self._exec(self._conn, f"SELECT {SEAT_COLS} FROM seats WHERE job = ? AND generation = ? AND seat = ?",
                         (job, generation, seat)).fetchone()
        return self._seat_obj(Seat, SEAT_COLS, row) if row else None

    def set_seat_state(self, seat_id, expected_revision, state, **fields):
        _check_state(state, fields)
        assignments = "".join(f", {k} = ?" for k in fields)
        with self._seat_tx() as c:
            row = self._exec(
                c, f"UPDATE seats SET state = ?, revision = revision + 1, updated_at = ?{assignments} "
                f"WHERE id = ? AND revision = ? RETURNING {SEAT_COLS}",
                (state, self._seat_time(self.now()), *fields.values(), seat_id, expected_revision)).fetchone()
        return self._seat_obj(Seat, SEAT_COLS, row) if row else None

    def claim_drive_launch(self, *, job, generation, action_id, attempt, launch_id, seat_id, seat_revision,
                           harness, reserve_usd, reserve_minutes, locks, lock_seconds, max_host_running,
                           ruling=False, lock_paths=None):
        lock_paths = dict(lock_paths or {})
        check_lock_keys(locks, lock_paths)
        if harness not in SEAT_HARNESSES:
            raise ValueError(f"seat harness must be one of {', '.join(SEAT_HARNESSES)}")
        host = compat.node()
        try:
            with self._seat_tx() as c:
                if self._seat_pg:   # the host-wide overlap lock: one claim at a time per host
                    c.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("swarm-wt:" + host,))
                now = self._seat_time(self.now())
                moved = self._exec(
                    c, "UPDATE seats SET state = 'running', revision = revision + 1, launches = launches + 1, "
                    "updated_at = ? WHERE id = ? AND job = ? AND generation = ? AND revision = ? "
                    "AND state IN ('requested', 'idle') RETURNING id",
                    (now, seat_id, job, generation, seat_revision)).fetchone()
                if moved is None:
                    raise _Refused("seat moved")
                open_here = self._exec(c, "SELECT count(*) FROM restarts WHERE host = ? AND ended_at IS NULL",
                                       (host,)).fetchone()[0]
                if open_here >= int(max_host_running) + (1 if ruling else 0):
                    raise _Refused("host cap")
                live = self._exec(c, "SELECT key, path FROM drive_locks WHERE host = ?", (host,)).fetchall()
                if any(key in locks for key, _ in live):
                    raise _Refused("lock held")
                live_wt = [path for key, path in live if key.startswith("wt:") and path]
                new_wt = [lock_paths[k] for k in sorted(locks) if k.startswith("wt:")]
                if wt_paths_overlap(new_wt, live_wt):
                    raise _Refused("worktree overlap")
                expires = self._seat_time(self.now() + _dt.timedelta(seconds=lock_seconds))
                for key in sorted(locks):
                    if self._exec(
                            c, "INSERT INTO drive_locks (key, holder, job, generation, launch_id, host, expires_at, "
                            "path) VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING RETURNING key",
                            (key, action_id, job, generation, launch_id, host, expires,
                             lock_paths.get(key))).fetchone() is None:
                        raise _Refused("lock held")
                row = self._exec(
                    c, "INSERT INTO restarts (job, agent_key, attempt, at, reason, old_agent_key, harness, host, "
                    "os_user, minutes_cap, ended_at, outcome, action_id, launch_id, reserved_usd) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?) ON CONFLICT DO NOTHING "
                    f"RETURNING {RESTART_COLS}",
                    (job, action_id, attempt, now, f"drive:{action_id}", f"launch:{launch_id}", harness, host,
                     getpass.getuser(), float(reserve_minutes), action_id, launch_id,
                     float(reserve_usd))).fetchone()
                if row is None:
                    raise _Refused("attempt or launch recorded")
                return self._restart_row(row)
        except _Refused:
            return None

    def _restart_row(self, row):
        r = list(row)
        for i in (4, 12):   # at, ended_at
            if r[i] is not None and not self._seat_pg:
                r[i] = self._seat_time_out(r[i])
        return Restart(*r)

    def renew_drive_locks(self, launch_id, expires_at):
        with self._seat_tx() as c:
            return self._exec(c, "UPDATE drive_locks SET expires_at = ? WHERE launch_id = ?",
                              (self._seat_time(expires_at), launch_id)).rowcount

    def release_drive_locks(self, launch_id):
        with self._seat_tx() as c:
            return self._exec(c, "DELETE FROM drive_locks WHERE launch_id = ?", (launch_id,)).rowcount

    def drive_locks(self, prefix=""):
        rows = self._exec(self._conn, "SELECT " + LOCK_COLS + " FROM drive_locks ORDER BY key").fetchall()
        return [self._seat_obj(DriveLock, LOCK_COLS, r) for r in rows if r[0].startswith(prefix)]
