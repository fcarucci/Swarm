"""External events (schema 21): the storage behind Board.post_event / events / ack_events.

One row per (job, kind, key); an acked row stays (so a redelivered webhook is not told twice)
until retention drops it. MemoryEvents serves the memory and file backends (a FileBoard is a
MemoryBoard over a FileStore, whose state document holds `events` and `next_event_id`);
SqlEvents serves SQLite and Postgres, which differ only in placeholders and timestamps. The
lifecycle and its checks are in base.Board; this module only stores.
"""
from __future__ import annotations

import datetime as dt

from .base import Event, event_targets

FIELDS = "id, job, kind, key, to_target, text, source, created_at, acked_at, acked_by"


def sql_schema(pg: bool = False) -> str:
    id_type = "bigserial PRIMARY KEY" if pg else "INTEGER PRIMARY KEY AUTOINCREMENT"
    ts = "timestamptz" if pg else "TEXT"
    return f"""
CREATE TABLE IF NOT EXISTS events (
 id {id_type}, job TEXT NOT NULL, kind TEXT NOT NULL, key TEXT NOT NULL, to_target TEXT,
 text TEXT NOT NULL, source TEXT, created_at {ts} NOT NULL, acked_at {ts}, acked_by TEXT,
 UNIQUE (job, kind, key));
CREATE INDEX IF NOT EXISTS events_pending ON events (job, id) WHERE acked_at IS NULL;
"""


def wanted(row_to, targets) -> bool:
    return targets is None or row_to in targets


class MemoryEvents:
    def _insert_event(self, job, kind, key, text, to, source):
        s = self._s()
        with s.lock:
            if job not in s.jobs:
                raise ValueError(f"no such job {job!r}")
            for e in s.events:
                if e["job"] == job and e["kind"] == kind and e["key"] == key:
                    return e["id"], False
            row = dict(id=s.next_event_id, job=job, kind=kind, key=key, to=to, text=text, source=source,
                       created_at=self.now(), acked_at=None, acked_by=None)
            s.next_event_id += 1
            s.events.append(row)
            s.touch()
            return row["id"], True

    def events(self, job, to=None, pending=False):
        targets = event_targets(to)
        with self._s().lock:
            return [Event(**e) for e in self._store.events
                    if e["job"] == job and (not pending or e["acked_at"] is None) and wanted(e["to"], targets)]

    def _ack_events(self, job, ids, by):
        wanted_ids = set(ids)
        s = self._s()
        with s.lock:
            n = 0
            for e in s.events:
                if e["job"] == job and e["id"] in wanted_ids and e["acked_at"] is None:
                    e["acked_at"], e["acked_by"] = self.now(), by
                    n += 1
            if n:
                s.touch()
            return n

    def _events_sleep(self, seconds):
        s = self._s()
        with s.lock:
            s.changed.wait(timeout=max(0.0, min(seconds, 1.0)))


def drop_old_events(s, keep: dt.datetime) -> bool:
    """Retention: acked events older than `keep`, and events of jobs that no longer exist."""
    before = len(s.events)
    s.events = [e for e in s.events
                if e["job"] in s.jobs and (e["acked_at"] is None or e["created_at"] >= keep)]
    return len(s.events) != before


class SqlEvents:
    """`_event_pg`: Postgres (psycopg, %s, timestamptz) or SQLite (?, ISO text). The board class
    supplies `_event_tx(write)`: a transaction yielding the connection."""

    _event_pg = False

    def _ev(self, c, sql, args=()):
        return c.execute(sql.replace("?", "%s") if self._event_pg else sql, args)

    def _ev_time(self, value):
        return value if self._event_pg or value is None else value.isoformat(timespec="microseconds")

    def _ev_row(self, r):
        v = list(r)
        if not self._event_pg:
            for i in (7, 8):
                if v[i] is not None:
                    v[i] = dt.datetime.fromisoformat(v[i])
        return Event(*v)

    def _insert_event(self, job, kind, key, text, to, source):
        with self._event_tx(True) as c:
            if self._ev(c, "SELECT 1 FROM jobs WHERE job=?", (job,)).fetchone() is None:
                raise ValueError(f"no such job {job!r}")
            row = self._ev(
                c, "INSERT INTO events (job,kind,key,to_target,text,source,created_at) VALUES (?,?,?,?,?,?,?) "
                   "ON CONFLICT (job,kind,key) DO NOTHING RETURNING id",
                (job, kind, key, to, text, source, self._ev_time(self.now()))).fetchone()
            if row is not None:
                if self._event_pg:
                    c.execute("SELECT pg_notify('swarm_events', %s)", (job,))
                    c.execute("SELECT pg_notify('swarm_board', '0')")   # lets the hooks' relay drop its leases
                return row[0], True
            found = self._ev(c, "SELECT id FROM events WHERE job=? AND kind=? AND key=?", (job, kind, key)).fetchone()
            return found[0], False

    def events(self, job, to=None, pending=False):
        targets = event_targets(to)
        sql, args = "SELECT " + FIELDS + " FROM events WHERE job=?", [job]
        if pending:
            sql += " AND acked_at IS NULL"
        if targets is not None:
            named = sorted(t for t in targets if t is not None)
            parts = []
            if named:
                parts.append("to_target IN (" + ",".join("?" * len(named)) + ")")
                args += named
            if None in targets:
                parts.append("to_target IS NULL")
            if not parts:
                return []
            sql += " AND (" + " OR ".join(parts) + ")"
        with self._event_tx(False) as c:
            return [self._ev_row(r) for r in self._ev(c, sql + " ORDER BY id", tuple(args)).fetchall()]

    def _ack_events(self, job, ids, by):
        if not ids:
            return 0
        with self._event_tx(True) as c:
            marks = ",".join("?" * len(ids))
            return self._ev(
                c, f"UPDATE events SET acked_at=?, acked_by=? WHERE job=? AND acked_at IS NULL AND id IN ({marks})",
                (self._ev_time(self.now()), by, job, *ids)).rowcount
