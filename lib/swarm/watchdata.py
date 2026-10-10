"""A complete PostgreSQL watch draw, fetched in one statement and rendered locally."""
import time
from dataclasses import fields, replace
from datetime import datetime
from swarm.board.base import AgentStatus, JobStatus, Message, Restart, Blocker, parse_job_data


def rows(cls, data):
    names = {f.name for f in fields(cls)}
    result = []
    for row in data or []:
        values = {k: v for k,v in row.items() if k in names}
        for key, value in values.items():
            if value is not None and (key.endswith('_at') or key in ('at','last_contact_at','waiting_since','waiting_until','until')):
                values[key] = datetime.fromisoformat(value) if isinstance(value,str) else value
        result.append(cls(**values))
    return result


class SnapshotBoard:
    def __init__(self, board, row):
        self.cfg, self.degraded = board.cfg, board.degraded
        self.change_mode = getattr(board, "change_mode", "push")
        self.board_cfg = board.cfg["board"]
        self.stamp = row[0]
        self.job_rows = rows(JobStatus,row[1])
        self.agent_rows = rows(AgentStatus,row[2])
        self.message_rows = rows(Message,row[3])
        self.restart_rows = rows(Restart,row[4])
        self.hidden, self.checks = row[5] or {}, row[6] or {}
        self.blocker_rows = rows(Blocker, row[7]) if len(row) > 7 else []
        self.plugin_data = row[8] or {} if len(row) > 8 else {}
        # Each idle or dead agent's newest post, taken in the same statement (agentview.last_post).
        self._last_posts = {(m.job, m.agent_name): m for m in rows(Message, row[9] if len(row) > 9 else [])}
        from swarm.review import pipeline_status
        self.job_rows = [replace(job, **pipeline_status(self.plugin_data.get(job.job)))
                         for job in self.job_rows]

    def now(self):
        return self.stamp

    def max_message_id(self):
        return max((m.id for m in self.message_rows), default=0)

    def new_jobs(self, base):
        """Names of the jobs here that `base` does not have."""
        return {j.job for j in self.job_rows} - {j.job for j in base.job_rows}

    def merged(self, base, limit):
        """This snapshot (read incrementally: only messages past base's newest) with base's
        older messages: what a full read would show. Messages of jobs no longer in scope go; each
        job keeps its newest `limit`. Jobs, agents and the rest are this read's, whole."""
        names = {j.job for j in self.job_rows}
        have = {m.id for m in self.message_rows}
        keep = [m for m in base.message_rows if m.job in names and m.id not in have]
        merged, per_job = [], {}
        for m in sorted(keep + self.message_rows, key=lambda m: m.id, reverse=True):
            per_job[m.job] = per_job.get(m.job, 0) + 1
            if per_job[m.job] <= limit:
                merged.append(m)
        self.message_rows = merged[::-1]
        return self

    def jobs(self, include_closed=False):
        return [j for j in self.job_rows if include_closed or j.status == 'active']

    def session_jobs(self, session):
        return [j for j in self.job_rows if j.session_id == session]

    def session_shown_jobs(self, session):
        return self.session_jobs(session)

    def job_status(self, job):
        return next((j for j in self.job_rows if j.job == job),None)

    def agents(self, job, include_departed=True):
        return [a for a in self.agent_rows if a.job == job and (include_departed or a.ended_at is None)]

    def watch_agents(self, job, recent_minutes):
        return self.agents(job), self.hidden.get(job,0)

    def restarts(self, job=None, **kwargs):
        return [r for r in self.restart_rows if job is None or r.job == job]

    def verification_counts(self, job):
        return tuple(self.checks.get(job,(0,0)))

    def recent_messages(self, limit, job=None, active_jobs_only=False):
        active = {j.job for j in self.job_rows if j.status == 'active'}
        messages = [m for m in self.message_rows if (job is None or m.job == job)
                    and (not active_jobs_only or m.job in active)]
        return messages[-limit:] if limit > 0 else []

    def blockers(self, job=None, include_closed=False):
        return [b for b in self.blocker_rows if (job is None or b.job == job)
                and (include_closed or b.state == 'open')]

    def last_post(self, job, agent_name):
        return self._last_posts.get((job, agent_name))

    def last_posts(self, job, names):
        return {n: m for n in dict.fromkeys(names) if (m := self._last_posts.get((job, n))) is not None}

    def job_data(self, job):
        return parse_job_data(self.plugin_data.get(job))


def age_agent(a, now, board_cfg):
    """`a` as it is at `now`, though it was read earlier: started/running/idle can only become idle
    or dead as time passes (base.derive_agent_status, from the same [board] thresholds). An agent
    inside a tool call keeps its status (its tool_started_at is not part of the row), finished
    agents never change."""
    from swarm.board.base import derive_agent_status
    if a.status in ("completed", "left", "dead") or (a.status in ("started", "running") and a.current_tool is not None):
        return a
    status = derive_agent_status(a.status, None, None, a.last_contact_at, now,
                                 idle_minutes=board_cfg.get("idle_minutes", 5),
                                 dead_minutes=board_cfg.get("dead_minutes", 30),
                                 tool_timeout_minutes=board_cfg.get("tool_timeout_minutes", 60))
    return a if status == a.status else replace(a, status=status)


class SnapshotSource:
    """The watch's reads of a Postgres board: a full snapshot, an incremental one merged into the
    last, and the fingerprint check that decides whether a full one is needed.

    full: the whole view (one statement). incremental: jobs, agents, open blockers whole, but only
    the messages past the last one held; merged into it (a job that is new to the scope: full).
    check: one cheap statement, the fingerprint of the scope against the held snapshot's; a full
    read only if they differ. Time-derived statuses are left out of it: the watch ages them
    itself (age_agent)."""

    def __init__(self, board, job, session, limit, clock=time.monotonic, check_s=60.0):
        self.board, self.job, self.session, self.limit = board, job, session, limit
        self.clock, self.check_s = clock, check_s
        self.base, self.recent, self.last_check = None, None, 0.0
        self.counts = {"full": 0, "incremental": 0, "check": 0}

    def _full(self, recent):
        self.counts["full"] += 1
        self.base = self.board.watch_snapshot(self.job, self.session, recent, self.limit)
        self.recent, self.last_check = recent, self.clock()
        return self.base

    def _matches(self, recent) -> bool:
        from swarm.snapshot import fingerprint_of_rows, row
        self.counts["check"] += 1
        self.last_check = self.clock()
        b = self.base
        mine = fingerprint_of_rows([row(j) for j in b.job_rows], [row(a) for a in b.agent_rows],
                                   b.max_message_id(), statuses=False)
        jobs = (self.job,) if self.job else ()
        sessions = (self.session,) if self.session and not self.job else ()
        return mine == tuple(self.board.scope_fingerprint(jobs, sessions, recent, True, False))

    def read(self, recent, kind):
        """The snapshot to draw from: None when kind == "check" and nothing differs."""
        if kind == "full" or self.base is None or recent != self.recent:
            return self._full(recent)
        if kind == "incremental":
            self.counts["incremental"] += 1
            fresh = self.board.watch_snapshot(self.job, self.session, recent, self.limit,
                                              after_id=self.base.max_message_id())
            if fresh.new_jobs(self.base):
                return self._full(recent)
            self.base = fresh.merged(self.base, self.limit)
            if self.clock() - self.last_check >= self.check_s and not self._matches(recent):
                return self._full(recent)
            return self.base
        return None if self._matches(recent) else self._full(recent)
