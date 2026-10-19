"""Read-only job context and time ageing shared by status and watch."""

def job_context(board, j, now) -> str:
    """Exact registered reasons for a job without working agents; never infer a human wait."""
    from swarm.cli import _job_status_word
    shown = _job_status_word(board, j, now)
    if shown == 'active' and j.status != 'active':
        return f"current agents working; prior {j.status}: {j.outcome or 'terminal lifecycle record'}"
    if shown not in ("waiting", "idle"):
        from swarm.cli import _waiting_word
        return _waiting_word(j, now)
    blockers = board.blockers(j.job)
    relevant = [b for b in blockers if b.kind == "question"] if shown == "waiting" else blockers
    reasons = [(f"Q{b.id}" if b.kind == "question" else b.kind)
               + f" -> {b.waiting_on}: {b.reason}"
               + (f" · until {b.until.astimezone().strftime('%H:%M')}" if b.until else "") for b in relevant]
    if shown == "idle":
        from swarm import dependencies
        data = board.job_data(j.job)
        error = data.get('task.reconciler_error')
        if error:
            reasons.append('reconciler: ' + str(error))
        tasks = dependencies.describe(board, j.job)
        reasons += [f"task {t['name']} ({t['artifact']}): {t['reason'] or t['state']}"
                    for t in tasks if t['state'] != 'done']
        if data.get('task.blocker') and all(t['state'] == 'done' for t in tasks):
            reasons.append(str(data['task.blocker']))
        named = {t['name'] for t in tasks}
        reasons += [(f"queued launch: {r.name}" if r.state == 'queued'
                     else f"launch {r.name}: awaiting runner completion")
                    for r in board.launches(j.job, include_closed=False) if r.name not in named]
    return "; ".join(reasons) or "no agents working; no pending work registered"



def age_job(job, recorded, now, cfg):
    """Age visible agents into the job counters while retaining hidden finished totals."""
    import dataclasses
    from swarm.board.base import AgentStatus
    from swarm.watchdata import age_agent
    known = {}
    for value in recorded:
        values = value[0] if isinstance(value, tuple) and value and isinstance(value[0], list) else value
        if isinstance(values, list):
            for agent in values:
                if isinstance(agent, AgentStatus) and agent.job == job.job:
                    known[agent.agent_key] = agent
    changes = {}
    for agent in known.values():
        aged = age_agent(agent, now, cfg)
        if agent.status != aged.status:
            for status, delta in ((agent.status, -1), (aged.status, 1)):
                field = "dead_or_left" if status in ("dead", "left") else status
                changes[field] = changes.get(field, 0) + delta
    return dataclasses.replace(job, **{key: getattr(job, key) + delta for key, delta in changes.items()})
