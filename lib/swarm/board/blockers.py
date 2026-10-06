"""Durable generic blockers. Mutations serialize on the job, including close guards.

The SQL implementation shares the lifecycle on SQLite and Postgres; only their transaction,
placeholder and timestamp adapters differ. FileBoard uses MemoryBlockers under its file lock.
"""

from __future__ import annotations
import dataclasses
import datetime as dt
import re
from .base import Blocker, BlockerEvent, CapExceeded, normalize_message

FIELDS = "id, job, kind, waiting_on, reason, until, default_value, state, created_by, created_at, resolved_by, resolved_how, resolved_at"
EVENT_FIELDS = "id, blocker, at, actor, event, detail"


def check_blocker(kind, waiting_on, reason):
    if not isinstance(kind, str) or re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", kind) is None:
        raise ValueError("blocker kind must be 1-64 lowercase letters, digits, _ or -")
    if not isinstance(waiting_on, str) or not waiting_on.strip():
        raise ValueError("blocker waiting_on must be nonempty")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("blocker reason must be nonempty")


def protects(blocker, now):
    """Waits require a future deadline; decisions addressed to people protect until resolved."""
    bounded = blocker.until is not None and blocker.until > now
    if blocker.kind == "wait":
        return bounded
    return blocker.kind == "question" or blocker.waiting_on != "external" or bounded


def rollup(js, rows, now):
    if not rows or js.status not in ("active", "paused"):
        return dataclasses.replace(js, open_blockers=0, protected_blockers=0)
    first = rows[0]
    return dataclasses.replace(
        js,
        waiting_on=first.reason,
        waiting_since=first.created_at,
        waiting_until=first.until,
        open_blockers=len(rows),
        protected_blockers=sum(protects(b, now) for b in rows),
    )


class BlockerLifecycle:
    def set_waiting(self, job, on, until=None):
        """Replace the job-level wait; resume resolves only waits, never plugin blockers."""
        with self._blocker_lock(job):
            js = self.job_status(job)
            if js is None or js.status != "active":
                return False
            for b in self.blockers(job):
                if b.kind == "wait":
                    self.resolve_blocker(b.id, "resumed", actor="swarm")
            if on is not None:
                self.open_blocker(
                    job, "wait", "external", on, until=until, default_value="", created_by="swarm"
                )
            self._refresh_waiting(job)
            return True

    def expire_blockers(self, now):
        """Existing sweep entry point. Default application and overdue are durable and once-only."""
        for js in self.jobs(True):
            for b in self.blockers(js.job):
                # Legacy waits pause with the job; decision deadlines continue.
                if b.kind == "wait" and js.status != "active":
                    continue
                if b.until is None or b.until > now:
                    continue
                if b.default_value is None:
                    self._overdue_blocker(b.id)
                elif self.resolve_blocker(b.id, b.default_value, actor="swarm", state="expired"):
                    notice = f"Blocker {b.id} expired: {b.default_value or b.reason}"
                    if js.status == "paused":
                        # Bookkeeping can settle a decision while agent work stays paused.
                        # Keep the normal writer/cap so notices remain excluded from activity.
                        text, _ = normalize_message(notice, self.message_cap())
                        try:
                            self._insert_message(b.job, "swarm", text, None, None)
                        except CapExceeded as exc:
                            text, _ = normalize_message(text, exc.cap)
                            self._insert_message(b.job, "swarm", text, None, None)
                    else:
                        self.post(b.job, "swarm", notice)
                    registry = getattr(self, "plugin_registry", None)
                    if registry:
                        registry.blocker_expired(self, b)

    def _emit_blocker_event(self, event):
        from swarm.fastpath import changed
        blocker = self.blocker(event.blocker)
        if blocker:
            changed(blocker.job)
        registry = getattr(self, "plugin_registry", None)
        if registry:
            registry.blocker_event(self, event)


class MemoryBlockers(BlockerLifecycle):
    def _blocker_lock(self, job):
        return self._s().lock

    def _refresh_waiting(self, job):
        self._blocker_sync(job)

    def _blocker_sync(self, job):
        s = self._store
        rows = [b for b in s.blockers if b["job"] == job and b["state"] == "open"]
        first = rows[0] if rows else {}
        if job not in s.jobs:
            s.touch()
            return
        s.jobs[job].update(
            waiting_on=first.get("reason"),
            waiting_since=first.get("created_at"),
            waiting_until=first.get("until"),
        )
        s.touch()

    def _blocker_event(self, id, actor, event, detail):
        s = self._store
        row = dict(
            id=s.next_blocker_event_id,
            blocker=id,
            at=self.now(),
            actor=actor,
            event=event,
            detail=detail,
        )
        s.next_blocker_event_id += 1
        s.blocker_events.append(row)
        return BlockerEvent(**row)

    def blockers(self, job=None, include_closed=False):
        with self._s().lock:
            return [
                Blocker(**b)
                for b in self._store.blockers
                if (job is None or b["job"] == job) and (include_closed or b["state"] == "open")
            ]

    def blocker(self, id):
        with self._s().lock:
            return next((Blocker(**b) for b in self._store.blockers if b["id"] == id), None)

    def blocker_events(self, id):
        with self._s().lock:
            return [BlockerEvent(**e) for e in self._store.blocker_events if e["blocker"] == id]

    def open_blocker(
        self, job, kind, waiting_on, reason, until=None, default_value=None, created_by=None
    ):
        check_blocker(kind, waiting_on, reason)
        with self._s().lock:
            j = self._store.jobs.get(job)
            if not j or j["status"] != "active":
                raise ValueError(f"{job} is not an open job")
            s = self._store
            row = dict(
                id=s.next_blocker_id,
                job=job,
                kind=kind,
                waiting_on=waiting_on,
                reason=reason,
                until=until,
                default_value=default_value,
                state="open",
                created_by=created_by,
                created_at=self.now(),
                resolved_by=None,
                resolved_how=None,
                resolved_at=None,
            )
            s.next_blocker_id += 1
            s.blockers.append(row)
            event = self._blocker_event(row["id"], created_by, "opened", reason)
            self._blocker_sync(job)
            result = Blocker(**row)
        self._emit_blocker_event(event)
        return result

    def _change_blocker(self, id, how, actor, event, state=None):
        with self._s().lock:
            row = next((b for b in self._store.blockers if b["id"] == id), None)
            if row is None:
                return False
            if event == "reopened":
                if row["state"] == "open":
                    return False
                j = self._store.jobs.get(row["job"])
                if not j or j["status"] != "active":
                    raise ValueError("cannot reopen a blocker on a closed job")
                row.update(state="open", resolved_by=None, resolved_how=None, resolved_at=None)
            else:
                if row["state"] != "open":
                    return False
                if event == "overdue" and any(
                    e["blocker"] == id and e["event"] == "overdue"
                    for e in self._store.blocker_events
                ):
                    return False
                if state:
                    row.update(
                        state=state, resolved_by=actor, resolved_how=how, resolved_at=self.now()
                    )
            e = self._blocker_event(id, actor, event, how)
            self._blocker_sync(row["job"])
        self._emit_blocker_event(e)
        return True

    def resolve_blocker(self, id, how=None, actor=None, state="resolved"):
        if state not in ("resolved", "expired"):
            raise ValueError("resolution state must be resolved or expired")
        return self._change_blocker(id, how, actor, state, state)

    def reopen_blocker(self, id, detail=None, actor=None):
        return self._change_blocker(id, detail, actor, "reopened")

    def comment_blocker(self, id, text, actor=None):
        # Comments also belong to resolved blockers; history is never edited.
        with self._s().lock:
            row = next((b for b in self._store.blockers if b["id"] == id), None)
            if row is None:
                return False
            e = self._blocker_event(id, actor, "commented", text)
            self._store.touch()
        self._emit_blocker_event(e)
        return True

    def _overdue_blocker(self, id):
        return self._change_blocker(id, None, "swarm", "overdue")


class SqlBlockers(BlockerLifecycle):
    def _blocker_execute(self, c, sql, args=()):
        return c.execute(sql.replace("?", "%s") if self._blocker_pg else sql, args)

    def _blocker_time(self, value):
        if value is None or self._blocker_pg:
            return value
        return value.isoformat()

    def _blocker_row(self, row, event=False):
        if row is None:
            return None
        values = list(row)
        if not self._blocker_pg:
            for i in ((2,) if event else (5, 9, 12)):
                if values[i] is not None:
                    values[i] = dt.datetime.fromisoformat(values[i])
        return (BlockerEvent if event else Blocker)(*values)

    def _blocker_lock(self, job):
        import contextlib

        @contextlib.contextmanager
        def lock():
            if self._blocker_pg:
                with self._conn.transaction():
                    self._conn.execute("SELECT job FROM jobs WHERE job=%s FOR UPDATE", (job,))
                    yield self._conn
            else:
                with self._tx() as c:
                    yield c

        return lock()

    def _refresh_waiting(self, job):
        with self._blocker_lock(job) as c:
            self._blocker_sync(c, job)

    def _blocker_sync(self, c, job):
        r = self._blocker_execute(
            c,
            "SELECT reason, created_at, until FROM blockers WHERE job=? AND state='open' ORDER BY id LIMIT 1",
            (job,),
        ).fetchone()
        self._blocker_execute(
            c,
            "UPDATE jobs SET waiting_on=?,waiting_since=?,waiting_until=? WHERE job=?",
            (*(r or (None, None, None)), job),
        )

    def _blocker_event(self, c, id, actor, event, detail):
        row = self._blocker_execute(
            c,
            "INSERT INTO blocker_events (blocker,at,actor,event,detail) VALUES (?,?,?,?,?) RETURNING "
            + EVENT_FIELDS,
            (id, self._blocker_time(self.now()), actor, event, detail),
        ).fetchone()
        return self._blocker_row(row, True)

    def blockers(self, job=None, include_closed=False):
        c = self._conn
        clauses = []
        args = []
        if job is not None:
            clauses.append("job=?")
            args.append(job)
        if not include_closed:
            clauses.append("state='open'")
        sql = (
            "SELECT "
            + FIELDS
            + " FROM blockers"
            + (" WHERE " + " AND ".join(clauses) if clauses else "")
            + " ORDER BY id"
        )
        return [self._blocker_row(r) for r in self._blocker_execute(c, sql, tuple(args)).fetchall()]

    def blocker(self, id):
        return self._blocker_row(
            self._blocker_execute(
                self._conn, "SELECT " + FIELDS + " FROM blockers WHERE id=?", (id,)
            ).fetchone()
        )

    def blocker_events(self, id):
        return [
            self._blocker_row(r, True)
            for r in self._blocker_execute(
                self._conn,
                "SELECT " + EVENT_FIELDS + " FROM blocker_events WHERE blocker=? ORDER BY id",
                (id,),
            ).fetchall()
        ]

    def open_blocker(
        self, job, kind, waiting_on, reason, until=None, default_value=None, created_by=None
    ):
        check_blocker(kind, waiting_on, reason)
        with self._blocker_lock(job) as c:
            j = self._blocker_execute(c, "SELECT status FROM jobs WHERE job=?", (job,)).fetchone()
            if not j or j[0] != "active":
                raise ValueError(f"{job} is not an open job")
            row = self._blocker_execute(
                c,
                "INSERT INTO blockers (job,kind,waiting_on,reason,until,default_value,state,created_by,created_at) VALUES (?,?,?,?,?,?,'open',?,?) RETURNING "
                + FIELDS,
                (
                    job,
                    kind,
                    waiting_on,
                    reason,
                    self._blocker_time(until),
                    default_value,
                    created_by,
                    self._blocker_time(self.now()),
                ),
            ).fetchone()
            result = self._blocker_row(row)
            e = self._blocker_event(c, result.id, created_by, "opened", reason)
            self._blocker_sync(c, job)
        self._emit_blocker_event(e)
        return result

    def _change_blocker(self, id, how, actor, event, state=None):
        row = self.blocker(id)
        if row is None:
            return False
        with self._blocker_lock(row.job) as c:
            row = self.blocker(id)
            if event == "reopened":
                if row.state == "open":
                    return False
                j = self._blocker_execute(
                    c, "SELECT status FROM jobs WHERE job=?", (row.job,)
                ).fetchone()
                if not j or j[0] != "active":
                    raise ValueError("cannot reopen a blocker on a closed job")
                self._blocker_execute(
                    c,
                    "UPDATE blockers SET state='open',resolved_by=NULL,resolved_how=NULL,resolved_at=NULL WHERE id=?",
                    (id,),
                )
            else:
                if row.state != "open":
                    return False
                if (
                    event == "overdue"
                    and self._blocker_execute(
                        c, "SELECT 1 FROM blocker_events WHERE blocker=? AND event='overdue'", (id,)
                    ).fetchone()
                ):
                    return False
                if state:
                    self._blocker_execute(
                        c,
                        "UPDATE blockers SET state=?,resolved_by=?,resolved_how=?,resolved_at=? WHERE id=?",
                        (state, actor, how, self._blocker_time(self.now()), id),
                    )
            e = self._blocker_event(c, id, actor, event, how)
            self._blocker_sync(c, row.job)
        self._emit_blocker_event(e)
        return True

    resolve_blocker = MemoryBlockers.resolve_blocker
    reopen_blocker = MemoryBlockers.reopen_blocker
    _overdue_blocker = MemoryBlockers._overdue_blocker

    def comment_blocker(self, id, text, actor=None):
        row = self.blocker(id)
        if row is None:
            return False
        with self._blocker_lock(row.job) as c:
            e = self._blocker_event(c, id, actor, "commented", text)
            self._blocker_sync(c, row.job)
        self._emit_blocker_event(e)
        return True


# The migration leaves old columns as a compatibility projection; data is never removed.
def sql_schema(pg=False):
    id_type = "bigserial PRIMARY KEY" if pg else "INTEGER PRIMARY KEY AUTOINCREMENT"
    timestamp = "timestamptz" if pg else "TEXT"
    return f"""
CREATE TABLE IF NOT EXISTS blockers (
 id {id_type}, job TEXT NOT NULL, kind TEXT NOT NULL, waiting_on TEXT NOT NULL,
 reason TEXT NOT NULL, until {timestamp}, default_value TEXT,
 state TEXT NOT NULL CHECK(state IN ('open','resolved','expired')), created_by TEXT,
 created_at {timestamp} NOT NULL, resolved_by TEXT, resolved_how TEXT, resolved_at {timestamp});
CREATE INDEX IF NOT EXISTS blockers_job_state ON blockers(job,state,id);
CREATE TABLE IF NOT EXISTS blocker_events (
 id {id_type}, blocker BIGINT NOT NULL REFERENCES blockers(id), at {timestamp} NOT NULL,
 actor TEXT, event TEXT NOT NULL CHECK(event IN ('opened','commented','resolved','expired','overdue','reopened')),detail TEXT);
CREATE INDEX IF NOT EXISTS blocker_events_blocker ON blocker_events(blocker,id);
"""


def migrate_sql(c, pg=False):
    # The caller's existing setup transaction/retry owns these statements.
    if pg:
        c.execute("SELECT job FROM jobs WHERE waiting_on IS NOT NULL FOR UPDATE")
    c.execute(
        "INSERT INTO blockers (job,kind,waiting_on,reason,until,default_value,state,created_by,created_at) "
        "SELECT job,'wait','external',waiting_on,waiting_until,'','open','swarm',COALESCE(waiting_since,activated_at,created_at) "
        "FROM jobs j WHERE waiting_on IS NOT NULL AND NOT EXISTS (SELECT 1 FROM blockers b WHERE b.job=j.job)"
    )
    c.execute(
        "INSERT INTO blocker_events (blocker,at,actor,event,detail) SELECT id,created_at,created_by,'opened',reason "
        "FROM blockers b WHERE NOT EXISTS (SELECT 1 FROM blocker_events e WHERE e.blocker=b.id)"
    )
