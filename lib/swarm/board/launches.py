"""Named launch queues for memory, file, SQLite and Postgres.

Queueing is an upsert in every state. Claims serialize per job (plus an optional host cap),
never per checkout. Legacy rows are read only during schema-26 migration, then dropped.
"""
from __future__ import annotations

import contextlib
import datetime as _dt
import getpass
import re

from swarm import compat
from .base import LAUNCH_STATES, LAUNCH_HARNESSES, Launch, RESTART_COLS, Restart

LAUNCH_COLS = ("id, job, name, role, harness, model, workdir, brief, state, attempts, "
               "last_outcome, queued_by, revision, agent_key, session_id, updated_at, launch_id")
LEGACY_NAME = "se" "at"
LEGACY_TABLE = LEGACY_NAME + "s"  # schema-25 table, retained only for migration
STATE_FIELDS = ("agent_key", "session_id", "last_outcome")
_NAME = re.compile(r"[a-z][a-z0-9_~-]{0,63}")


def check_launch(name, role, harness, workdir, brief, queued_by):
    if not isinstance(name, str) or _NAME.fullmatch(name) is None:
        raise ValueError("launch name must be 1-64 lowercase letters, digits, _, - or ~, starting with a letter")
    if harness not in LAUNCH_HARNESSES:
        raise ValueError("launch harness must be claude or codex")
    for what, value in (("role", role), ("workdir", workdir), ("brief", brief), ("queued_by", queued_by)):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"launch {what} must be nonempty")


def _check_state(state, fields, current=None):
    if state == current and set(fields) <= {"session_id", "agent_key"}:
        return
    if state not in ("done", "failed") and not (state == "queued" and
            fields.get("last_outcome") in ("halted", "interrupted by pause", "interrupted by driver restart", "blocked", "driver-error")):
        raise ValueError("only queue_launch or claim_launch may queue or start a launch")
    if set(fields) - set(STATE_FIELDS):
        raise ValueError("unknown launch settlement field")


def migrated_launch(row):
    """Convert a legacy row without retaining the old lifecycle or request metadata."""
    out = {k: row.get(k) for k in (c.strip() for c in LAUNCH_COLS.split(","))}
    out.update(name=row.get("name", row.get(LEGACY_NAME)), brief=row.get("brief", row.get("brief_path")),
               attempts=row.get("attempts", row.get("launches", 0)),
               queued_by=row.get("queued_by") or row.get("requested_by") or "requester")
    out["state"] = {"requested": "queued", "idle": "done", "released": "done",
                    "cancelled": "failed", "refused": "failed"}.get(row["state"], row["state"])
    if out["state"] == "running" and not row.get("launch_id"):
        out["state"] = "done"
    return out


class _Refused(Exception):
    pass


class MemoryLaunches:
    def queue_launch(self, job, name, *, role, harness, model, workdir, brief, queued_by):
        check_launch(name, role, harness, workdir, brief, queued_by)
        s = self._s()
        with s.lock:
            if job not in s.jobs:
                raise ValueError(f"{job} is not a job")
            now = self.now()
            fields = dict(role=role, harness=harness, model=model, workdir=workdir, brief=brief,
                          queued_by=queued_by, state="queued", last_outcome=None, updated_at=now,
                          session_id=None, agent_key=None)
            row = next((r for r in s.launches if (r["job"], r["name"]) ==
                        (job, name)), None)
            if row is None:
                row = dict(id=s.next_launch_id, job=job, name=name, **fields,
                           revision=1, attempts=0, launch_id=None)
                s.next_launch_id += 1
                s.launches.append(row)
            else:
                row.update(fields, revision=row["revision"] + 1)
            s.touch()
            return Launch(**row)

    def launches(self, job, include_closed=True):
        s = self._s()
        with s.lock:
            return [Launch(**r) for r in s.launches if r["job"] == job
                    and (include_closed or r["state"] in ("queued", "running"))]

    def get_launch(self, job, name):
        return next((r for r in self.launches(job) if r.name == name), None)

    def set_launch_state(self, launch_row_id, expected_revision, state, *, job, **fields):
        s = self._s()
        with s.lock:
            row = next((r for r in s.launches if r["id"] == launch_row_id and r["job"] == job), None)
            if row is None or row["revision"] != expected_revision:
                return None
            _check_state(state, fields, row["state"])
            if row["state"] == state and all(row[k] == v for k, v in fields.items()):
                return None
            if row["state"] == "running" and any(r["launch_id"] == row["launch_id"] and
                    r["ended_at"] is None for r in s.restarts):
                raise ValueError("launch is still open: end it first")
            if state == "queued" and row["state"] == "running":
                row["attempts"] = max(0, row["attempts"] - 1)
            row.update(fields, state=state, revision=row["revision"] + 1, updated_at=self.now())
            s.touch()
            return Launch(**row)

    def claim_launch(self, *, job, action_id, attempt, launch_id, launch_row_id, queue_version,
                           harness, reserve_usd, reserve_minutes, max_host_running=None,
                           max_job_running=4, ruling=False):
        s = self._s()
        with s.lock:
            row = next((r for r in s.launches if r["id"] == launch_row_id), None)
            if (row is None or row["job"] != job
                    or row["revision"] != queue_version or row["state"] != "queued"):
                return None
            if any(r["launch_id"] == row["launch_id"] and r["ended_at"] is None for r in s.restarts):
                return None
            host = compat.node()
            live = [r for r in s.restarts if r["ended_at"] is None]
            if sum(r["job"] == job for r in live) >= int(max_job_running):
                return None
            if max_host_running is not None and sum(r["host"] == host for r in live) >= int(max_host_running):
                return None
            if any((r["job"], r.get("action_id"), r["attempt"]) == (job, action_id, attempt)
                   or r.get("launch_id") == launch_id for r in s.restarts):
                return None
            now = self.now()
            restart = dict(id=s.next_restart_id, job=job, agent_key=action_id, attempt=attempt, at=now,
                           reason=f"drive:{action_id}", old_agent_key=f"launch:{launch_id}", new_agent_key=None,
                           harness=harness, host=host, os_user=getpass.getuser(), minutes_cap=float(reserve_minutes),
                           ended_at=None, outcome=None, action_id=action_id, launch_id=launch_id,
                           reserved_usd=float(reserve_usd), cost_usd=None, tokens_in=None, tokens_out=None)
            s.next_restart_id += 1
            s.restarts.append(restart)
            row.update(state="running", revision=row["revision"] + 1, attempts=row["attempts"] + 1,
                       updated_at=now, launch_id=launch_id)
            s.touch()
            return Restart(**restart)


def launches_schema(pg=False):
    real = "double precision" if pg else "REAL"
    big = "bigint" if pg else "INTEGER"
    alter = "ALTER TABLE restarts ADD COLUMN IF NOT EXISTS {} {};\n" if pg else ""
    restart_cols = "".join(alter.format(c, t) for c, t in (
        ("action_id", "text"), ("launch_id", "text"), ("reserved_usd", real), ("cost_usd", real),
        ("tokens_in", big), ("tokens_out", big)))
    return f"""
{restart_cols}
CREATE UNIQUE INDEX IF NOT EXISTS restarts_action_attempt ON restarts (job, action_id, attempt)
 WHERE action_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS restarts_launch_id ON restarts (launch_id) WHERE launch_id IS NOT NULL;
{launches_table(pg, "IF NOT EXISTS launches")}
"""


def launches_table(pg, name):
    id_type = "bigserial PRIMARY KEY" if pg else "INTEGER PRIMARY KEY AUTOINCREMENT"
    ts = "timestamptz" if pg else "TEXT"
    big = "bigint" if pg else "INTEGER"
    return f"""CREATE TABLE {name} (
 id {id_type}, job TEXT NOT NULL REFERENCES jobs(job) ON DELETE CASCADE,
 name TEXT NOT NULL, role TEXT NOT NULL, harness TEXT NOT NULL CHECK (harness IN ('claude', 'codex')),
 model TEXT, workdir TEXT NOT NULL, brief TEXT NOT NULL,
 state TEXT NOT NULL CHECK (state IN ('queued', 'running', 'done', 'failed')),
 attempts {big} NOT NULL DEFAULT 0, last_outcome TEXT, queued_by TEXT NOT NULL,
 revision {big} NOT NULL DEFAULT 1, agent_key TEXT, session_id TEXT, updated_at {ts} NOT NULL, launch_id TEXT,
 UNIQUE (job, name));"""


def _drop_launch_generation(c, pg):
    """Boards from before the drive purge have launches.generation NOT NULL and UNIQUE (job, generation, name);
    rebuild the table without it, keeping the newest row per (job, name)."""
    if pg:
        old = c.execute("SELECT 1 FROM information_schema.columns WHERE table_name='launches' "
                        "AND column_name='generation' AND table_schema=current_schema()").fetchone()
    else:
        old = any(r[1] == "generation" for r in c.execute("PRAGMA table_info(launches)").fetchall())
    if not old:
        return
    c.execute("ALTER TABLE launches RENAME TO launches_gen")
    c.execute(launches_table(pg, "launches"))
    c.execute(f"INSERT INTO launches ({LAUNCH_COLS}) SELECT {LAUNCH_COLS} FROM launches_gen "
              "WHERE id IN (SELECT MAX(id) FROM launches_gen GROUP BY job, name)")
    c.execute("DROP TABLE launches_gen")
    if pg:
        c.execute("SELECT setval(pg_get_serial_sequence('launches', 'id'), "
                  "COALESCE((SELECT MAX(id) FROM launches), 1), EXISTS(SELECT 1 FROM launches))")


def migrate_launches(c, pg=False):
    """One transactional copy from pre-26 storage, then the generation column's removal. Repeated setup is harmless."""
    if pg:
        exists = c.execute(f"SELECT to_regclass('{LEGACY_TABLE}')").fetchone()[0] is not None
    else:
        exists = c.execute(f"SELECT 1 FROM sqlite_master WHERE type='table' AND name='{LEGACY_TABLE}'").fetchone() is not None
    if exists:
        _copy_legacy_launches(c, pg)
    _drop_launch_generation(c, pg)


def _copy_legacy_launches(c, pg):
    cols = LAUNCH_COLS.replace("name,", LEGACY_NAME + ",").replace("brief,", "brief_path,")
    cols = cols.replace("attempts,", "launches,").replace("queued_by", "requested_by")
    expr = cols.replace("state,", "CASE state WHEN 'requested' THEN 'queued' WHEN 'idle' THEN 'done' "
                        "WHEN 'released' THEN 'done' WHEN 'refused' THEN 'failed' "
                        "WHEN 'cancelled' THEN 'failed' WHEN 'running' THEN "
                        "CASE WHEN launch_id IS NULL THEN 'done' ELSE 'running' END ELSE state END,")
    c.execute(f"INSERT INTO launches ({LAUNCH_COLS}) SELECT {expr} FROM {LEGACY_TABLE} WHERE true ON CONFLICT DO NOTHING")
    c.execute(f"DROP TABLE {LEGACY_TABLE}")
    if pg:
        c.execute("SELECT setval(pg_get_serial_sequence('launches', 'id'), "
                  "COALESCE((SELECT MAX(id) FROM launches), 1), EXISTS(SELECT 1 FROM launches))")


class SqlLaunches:
    _launch_pg = False

    def _sx(self, sql):
        return sql.replace("?", "%s") if self._launch_pg else sql

    def _launch_time(self, value):
        """A timestamp as the backend stores it (Postgres: the datetime; SQLite: its text form)."""
        if value is None or self._launch_pg:
            return value
        from .sqlite import _ts
        return _ts(value)

    def _launch_time_out(self, value):
        if value is None or self._launch_pg or isinstance(value, _dt.datetime):
            return value
        return _dt.datetime.fromisoformat(value)   # the text _ts wrote

    def _launch_obj(self, cls, cols, row):
        names = [c.strip() for c in cols.split(",")]
        d = dict(zip(names, row))
        for f in ("updated_at", "expires_at"):
            if f in d:
                d[f] = self._launch_time_out(d[f])
        return cls(**d)

    @contextlib.contextmanager
    def _launch_tx(self):
        """One write transaction. SQLite: BEGIN IMMEDIATE (or the enclosing one, under a savepoint so a
        refused claim undoes its own writes); Postgres: a transaction, or a savepoint inside one."""
        if self._launch_pg:
            with self._conn.transaction():
                yield self._conn
            return
        with self._tx() as c:
            c.execute("SAVEPOINT drive_launches")
            try:
                yield c
            except BaseException:
                c.execute("ROLLBACK TO drive_launches")
                c.execute("RELEASE drive_launches")
                raise
            c.execute("RELEASE drive_launches")

    def _exec(self, c, sql, args=()):
        return c.execute(self._sx(sql), args)

    def queue_launch(self, job, name, *, role, harness, model, workdir, brief, queued_by):
        check_launch(name, role, harness, workdir, brief, queued_by)
        with self._launch_tx() as c:
            now = self._launch_time(self.now())
            row = self._exec(c,
                "INSERT INTO launches (job, name, role, harness, model, workdir, brief, "
                "queued_by, state, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?) "
                "ON CONFLICT (job, name) DO UPDATE SET role=excluded.role, harness=excluded.harness, "
                "model=excluded.model, workdir=excluded.workdir, brief=excluded.brief, "
                "queued_by=excluded.queued_by, state='queued', last_outcome=NULL, session_id=NULL, agent_key=NULL, "
                "revision=launches.revision+1, updated_at=excluded.updated_at "
                f"RETURNING {LAUNCH_COLS}",
                (job, name, role, harness, model, workdir, brief, queued_by, now)).fetchone()
            return self._launch_obj(Launch, LAUNCH_COLS, row)

    def launches(self, job, include_closed=True):
        sql, args = f"SELECT {LAUNCH_COLS} FROM launches WHERE job = ?", [job]
        if not include_closed:
            sql += " AND state IN ('queued', 'running')"
        return [self._launch_obj(Launch, LAUNCH_COLS, r) for r in
                self._exec(self._conn, sql + " ORDER BY id", args).fetchall()]

    def get_launch(self, job, name):
        row = self._exec(self._conn, f"SELECT {LAUNCH_COLS} FROM launches WHERE job=? AND name=?",
                         (job, name)).fetchone()
        return self._launch_obj(Launch, LAUNCH_COLS, row) if row else None

    def set_launch_state(self, launch_row_id, expected_revision, state, *, job, **fields):
        with self._launch_tx() as c:
            row = self._exec(c, f"SELECT {LAUNCH_COLS} FROM launches WHERE id=? AND job=?",
                             (launch_row_id, job)).fetchone()
            if row is None:
                return None
            current = self._launch_obj(Launch, LAUNCH_COLS, row)
            _check_state(state, fields, current.state)
            if current.revision != expected_revision or (current.state == state and
                    all(getattr(current, k) == v for k, v in fields.items())):
                return None
            if current.state == "running" and self._exec(c,
                    "SELECT 1 FROM restarts WHERE launch_id=? AND ended_at IS NULL",
                    (current.launch_id,)).fetchone():
                raise ValueError("launch is still open: end it first")
            assignments = "".join(f", {k}=?" for k in fields)
            if state == "queued" and current.state == "running":
                assignments += ", attempts=CASE WHEN attempts>0 THEN attempts-1 ELSE 0 END"
            row = self._exec(c, "UPDATE launches SET state=?, revision=revision+1, updated_at=?" + assignments +
                             f" WHERE id=? AND job=? AND revision=? RETURNING {LAUNCH_COLS}",
                             (state, self._launch_time(self.now()), *fields.values(), launch_row_id, job,
                              expected_revision)).fetchone()
            return self._launch_obj(Launch, LAUNCH_COLS, row) if row else None

    def claim_launch(self, *, job, action_id, attempt, launch_id, launch_row_id, queue_version,
                           harness, reserve_usd, reserve_minutes, max_host_running=None,
                           max_job_running=4, ruling=False):
        try:
            with self._launch_tx() as c:
                host = compat.node()
                if self._launch_pg:
                    for key in ("restarts:" + job, "restarts-host:" + host):
                        c.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (key,))
                moved = self._exec(c,
                    "UPDATE launches SET state='running', revision=revision+1, attempts=attempts+1, "
                    "updated_at=?, launch_id=? WHERE id=? AND job=? AND revision=? "
                    "AND state='queued' AND NOT EXISTS (SELECT 1 FROM restarts WHERE "
                    "restarts.launch_id=launches.launch_id AND ended_at IS NULL) RETURNING id",
                    (self._launch_time(self.now()), launch_id, launch_row_id, job, queue_version)).fetchone()
                if moved is None:
                    raise _Refused("launch moved or previous attempt still running")
                n = self._exec(c, "SELECT count(*) FROM restarts WHERE job=? AND ended_at IS NULL",
                               (job,)).fetchone()[0]
                if n >= int(max_job_running):
                    raise _Refused("job cap")
                if max_host_running is not None:
                    n = self._exec(c, "SELECT count(*) FROM restarts WHERE host=? AND ended_at IS NULL",
                                   (host,)).fetchone()[0]
                    if n >= int(max_host_running):
                        raise _Refused("host cap")
                row = self._exec(c,
                    "INSERT INTO restarts (job, agent_key, attempt, at, reason, old_agent_key, harness, host, "
                    "os_user, minutes_cap, action_id, launch_id, reserved_usd) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING "
                    f"RETURNING {RESTART_COLS}",
                    (job, action_id, attempt, self._launch_time(self.now()), f"drive:{action_id}", f"launch:{launch_id}",
                     harness, host, getpass.getuser(), float(reserve_minutes), action_id, launch_id,
                     float(reserve_usd))).fetchone()
                if row is None:
                    raise _Refused("attempt recorded")
                return self._restart_row(row)
        except _Refused:
            return None

    def _restart_row(self, row):
        r = list(row)
        for i in (4, 12):   # at, ended_at
            if r[i] is not None and not self._launch_pg:
                r[i] = self._launch_time_out(r[i])
        return Restart(*r)

