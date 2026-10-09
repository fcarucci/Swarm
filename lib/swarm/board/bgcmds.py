"""Background commands (schema 24): the storage behind Board.bg_start / bg_end / bg_commands.

One row per background shell command a swarm member started under the `swarm bg` wrapper
(swarm.bg): who, what, where (host, boot id and pid namespace), the process group and the
/proc start time of its leader, and the random tag in its environment, so a reap can find
exactly its processes and never a reused pid. MemoryBgCommands serves the memory and file
backends (the FileStore's state document holds `bg_commands` and `next_bg_id`); SqlBgCommands
serves SQLite and Postgres, which differ only in placeholders and timestamps. The checks are
in base.Board; this module only stores.
"""
from __future__ import annotations

import datetime as dt

from .base import BgCommand

FIELDS = ("id, job, agent_key, agent_name, command, started_at, host, boot, pid, pgid, proc_start, tag, "
          "ended_at, exit_code, outcome, detail")


def sql_schema(pg: bool = False) -> str:
    id_type = "bigserial PRIMARY KEY" if pg else "INTEGER PRIMARY KEY AUTOINCREMENT"
    ts = "timestamptz" if pg else "TEXT"
    big = "bigint" if pg else "INTEGER"
    return f"""
CREATE TABLE IF NOT EXISTS bg_commands (
 id {id_type}, job TEXT NOT NULL, agent_key TEXT, agent_name TEXT NOT NULL, command TEXT NOT NULL,
 started_at {ts} NOT NULL, host TEXT NOT NULL, boot TEXT, pid {big}, pgid {big}, proc_start {big},
 tag TEXT, ended_at {ts}, exit_code INTEGER, outcome TEXT, detail TEXT);
CREATE INDEX IF NOT EXISTS bg_commands_running ON bg_commands (job, id) WHERE ended_at IS NULL;
"""


def _row(d: dict) -> BgCommand:
    return BgCommand(**{k: d.get(k) for k in BgCommand.__dataclass_fields__})


class MemoryBgCommands:
    def _insert_bg(self, job, agent_key, agent_name, command, host, boot, pid, pgid, proc_start, tag):
        s = self._s()
        with s.lock:
            if job not in s.jobs:
                raise ValueError(f"no such job {job!r}")
            bid = s.next_bg_id
            s.next_bg_id += 1
            s.bg_commands.append(dict(id=bid, job=job, agent_key=agent_key, agent_name=agent_name,
                                      command=command, started_at=self.now(), host=host, boot=boot, pid=pid,
                                      pgid=pgid, proc_start=proc_start, tag=tag, ended_at=None,
                                      exit_code=None, outcome=None, detail=None))
            s.touch()
            return bid

    def _end_bg(self, id, outcome, exit_code, detail, signalled_at=None):
        s = self._s()
        with s.lock:
            for r in s.bg_commands:
                if r["id"] != id:
                    continue
                if r["ended_at"] is None:
                    r.update(ended_at=self.now(), outcome=outcome, exit_code=exit_code, detail=detail)
                elif signal_exit(r, signalled_at):
                    r.update(outcome=outcome, detail=detail)
                else:
                    return False
                s.touch()
                return True
            return False

    def bg_commands(self, job=None, running=False):
        with self._s().lock:
            return [_row(r) for r in self._store.bg_commands
                    if (job is None or r["job"] == job) and (not running or r["ended_at"] is None)]


def signal_exit(r: dict, signalled_at) -> bool:
    """A row the wrapper ended because a signal killed its command, at or after `signalled_at`."""
    return (signalled_at is not None and r["outcome"] == "exited" and (r["exit_code"] or 0) >= 128
            and r["ended_at"] >= signalled_at)


def drop_old_bg(s, keep: dt.datetime) -> bool:
    """Retention: ended commands older than `keep`, and those of jobs that no longer exist."""
    before = len(s.bg_commands)
    s.bg_commands = [r for r in s.bg_commands
                     if r["job"] in s.jobs and (r["ended_at"] is None or r["ended_at"] >= keep)]
    return len(s.bg_commands) != before


class SqlBgCommands:
    """`_bg_pg`: Postgres (psycopg, %s, timestamptz) or SQLite (?, ISO text). The board class
    supplies `_event_tx(write)`: a transaction yielding the connection."""

    _bg_pg = False

    def _bq(self, c, sql, args=()):
        return c.execute(sql.replace("?", "%s") if self._bg_pg else sql, args)

    def _bg_time(self, value):
        return value if self._bg_pg or value is None else value.isoformat(timespec="microseconds")

    def _bg_row(self, r) -> BgCommand:
        v = list(r)
        if not self._bg_pg:
            for i in (5, 12):
                if v[i] is not None:
                    v[i] = dt.datetime.fromisoformat(v[i])
        return BgCommand(*v)

    def _insert_bg(self, job, agent_key, agent_name, command, host, boot, pid, pgid, proc_start, tag):
        with self._event_tx(True) as c:
            if self._bq(c, "SELECT 1 FROM jobs WHERE job=?", (job,)).fetchone() is None:
                raise ValueError(f"no such job {job!r}")
            return self._bq(
                c, "INSERT INTO bg_commands (job, agent_key, agent_name, command, started_at, host, boot, pid, "
                   "pgid, proc_start, tag) VALUES (?,?,?,?,?,?,?,?,?,?,?) RETURNING id",
                (job, agent_key, agent_name, command, self._bg_time(self.now()), host, boot, pid, pgid,
                 proc_start, tag)).fetchone()[0]

    def _end_bg(self, id, outcome, exit_code, detail, signalled_at=None):
        with self._event_tx(True) as c:
            if self._bq(c, "UPDATE bg_commands SET ended_at=?, outcome=?, exit_code=?, detail=? "
                           "WHERE id=? AND ended_at IS NULL",
                        (self._bg_time(self.now()), outcome, exit_code, detail, id)).rowcount > 0:
                return True
            if signalled_at is None:
                return False
            return self._bq(c, "UPDATE bg_commands SET outcome=?, detail=? WHERE id=? AND outcome='exited' "
                               "AND exit_code >= 128 AND ended_at >= ?",
                            (outcome, detail, id, self._bg_time(signalled_at))).rowcount > 0

    def bg_commands(self, job=None, running=False):
        sql, args = "SELECT " + FIELDS + " FROM bg_commands WHERE 1=1", []
        if job is not None:
            sql += " AND job=?"
            args.append(job)
        if running:
            sql += " AND ended_at IS NULL"
        with self._event_tx(False) as c:
            return [self._bg_row(r) for r in self._bq(c, sql + " ORDER BY id", tuple(args)).fetchall()]
