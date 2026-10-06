"""A complete PostgreSQL watch draw, fetched in one statement and rendered locally."""
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
        self.board_cfg = board.cfg["board"]
        self.stamp = row[0]
        self.job_rows = rows(JobStatus,row[1])
        self.agent_rows = rows(AgentStatus,row[2])
        self.message_rows = rows(Message,row[3])
        self.restart_rows = rows(Restart,row[4])
        self.hidden, self.checks = row[5] or {}, row[6] or {}
        self.blocker_rows = rows(Blocker, row[7]) if len(row) > 7 else []
        self.plugin_data = row[8] or {} if len(row) > 8 else {}
        from swarm.review import pipeline_status
        self.job_rows = [replace(job, **pipeline_status(self.plugin_data.get(job.job)))
                         for job in self.job_rows]

    def now(self):
        return self.stamp

    def jobs(self, include_closed=False):
        return [j for j in self.job_rows if include_closed or j.status == 'active']

    def session_jobs(self, session):
        return [j for j in self.job_rows if j.session_id == session]

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

    def job_data(self, job):
        return parse_job_data(self.plugin_data.get(job))
