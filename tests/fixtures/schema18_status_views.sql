-- Main c206c89 schema 18 views (before per-job LATERAL rollups).

DROP VIEW IF EXISTS job_status;
DROP VIEW IF EXISTS agent_status;
CREATE VIEW agent_status AS
SELECT a.job, a.name, CASE WHEN a.judge THEN 'judge' WHEN a.verifier THEN 'verifier' ELSE a.role END AS role,
       CASE
         WHEN a.state IN ('completed', 'left', 'dead') THEN a.state
         WHEN a.current_tool IS NOT NULL
              AND a.tool_started_at > now() - make_interval(mins => {tool_timeout}) THEN 'running'
         WHEN a.last_seen < now() - make_interval(mins => {dead}) THEN 'dead'
         WHEN a.last_seen < now() - make_interval(mins => {idle}) THEN 'idle'
         ELSE a.state
       END AS status,
       a.current_tool, a.tool_calls,
       COALESCE(mc.messages, 0::bigint) AS messages,
       a.joined_at, a.last_seen AS last_contact_at, a.last_post_at, a.left_at AS ended_at,
       a.host, a.agent_key, a.harness, a.model, a.os_user, a.left_reason, a.resume_of
  FROM agents a
  LEFT JOIN LATERAL (
    -- Count only this job/name/incarnation via messages_job_agent_created_at.
    -- Names can be reused after departure; keep the joined_at boundary.
    SELECT count(*) AS messages FROM messages m
     WHERE m.job = a.job AND m.agent_name = a.name AND m.created_at >= a.joined_at
  ) mc ON true;

DROP VIEW IF EXISTS job_status;
CREATE VIEW job_status AS
SELECT j.job, j.status, j.description, j.task, j.outcome, j.created_by, j.session_id,
       j.created_at, j.activated_at, j.finished_at,
       COALESCE(s.agents, 0::bigint) AS agents,
       COALESCE(s.started, 0::bigint) AS started,
       COALESCE(s.running, 0::bigint) AS running,
       COALESCE(s.idle, 0::bigint) AS idle,
       COALESCE(s.completed, 0::bigint) AS completed,
       COALESCE(s.dead_or_left, 0::bigint) AS dead_or_left,
       COALESCE(m.messages, 0::bigint) AS messages,
       greatest(s.last_contact_at, m.last_post_at) AS last_activity_at,
       j.project, j.goal, j.verdict, j.verdict_reason, j.verdict_by, j.verdict_at, j.completion_forced,
       (SELECT a.name FROM agents a WHERE a.job = j.job AND a.judge AND a.left_at IS NULL) AS judge,
       j.waiting_on, j.waiting_since, j.closed_by, j.supervise, j.verdict_next,
       j.max_hours, j.waiting_until,
       COALESCE((SELECT json_agg(row_to_json(b) ORDER BY b.id) FROM blockers b
                 WHERE b.job=j.job AND b.state='open'), '[]'::json) AS blockers,
       (SELECT count(*) FROM blockers b WHERE b.job=j.job AND b.state='open') AS open_blockers,
       (SELECT count(*) FROM blockers b WHERE b.job=j.job AND b.state='open'
         AND (b.protection_rule='always' OR (b.protection_rule='addressed' AND b.waiting_on <> 'external')
              OR b.until>now())) AS protected_blockers,
       -- what `status` shows (base.derive_job_status): closed jobs their status, open ones waiting /
       -- active / waiting (goal not met: a goal without a met verdict and nobody started, running
       -- or idle; no sweep closes it) / idle
       CASE WHEN j.status <> 'active' THEN j.status
            WHEN EXISTS (SELECT 1 FROM blockers b WHERE b.job=j.job AND b.state='open') THEN 'waiting'
            WHEN COALESCE(j.waiting_on, '') <> '' THEN 'waiting'
            WHEN COALESCE(s.started, 0) + COALESCE(s.running, 0) > 0 THEN 'active'
            WHEN COALESCE(j.goal, '') <> '' AND j.verdict IS DISTINCT FROM 'met'
                 AND COALESCE(s.idle, 0) = 0 THEN 'waiting (goal not met)'
            WHEN COALESCE(greatest(s.last_contact_at,
                                   m.last_post_at),
                          j.activated_at, j.created_at) > now() - make_interval(mins => {idle}) THEN 'active'
            ELSE 'idle'
       END AS shown_status
  FROM jobs j
  LEFT JOIN (
    -- Job listings need no per-agent message counts, including for closed jobs.
    SELECT a.job, sum(a.n)::bigint AS agents,
           COALESCE(sum(a.n) FILTER (WHERE a.status = 'started'), 0)::bigint AS started,
           COALESCE(sum(a.n) FILTER (WHERE a.status = 'running'), 0)::bigint AS running,
           COALESCE(sum(a.n) FILTER (WHERE a.status = 'idle'), 0)::bigint AS idle,
           COALESCE(sum(a.n) FILTER (WHERE a.status = 'completed'), 0)::bigint AS completed,
           COALESCE(sum(a.n) FILTER (WHERE a.status IN ('dead', 'left')), 0)::bigint AS dead_or_left,
           max(a.last_contact_at) AS last_contact_at
      FROM (
        -- Classify once per agent, rather than once per counter. Terminal states
        -- return immediately, so old jobs do not evaluate live-agent thresholds.
        SELECT a.job, CASE
         WHEN a.state IN ('completed', 'left', 'dead') THEN a.state
         WHEN a.current_tool IS NOT NULL
              AND a.tool_started_at > now() - make_interval(mins => {tool_timeout}) THEN 'running'
         WHEN a.last_seen < now() - make_interval(mins => {dead}) THEN 'dead'
         WHEN a.last_seen < now() - make_interval(mins => {idle}) THEN 'idle'
         ELSE a.state
       END AS status, count(*) AS n,
               max(a.last_seen) AS last_contact_at
          FROM agents a
         GROUP BY a.job, status
      ) a
     GROUP BY a.job
  ) s ON s.job = j.job
  LEFT JOIN (SELECT job, count(*) AS messages, max(created_at) FILTER
                 (WHERE NOT (agent_name = 'swarm' AND message LIKE 'Blocker % expired:%')) AS last_post_at
               FROM messages GROUP BY job) m ON m.job = j.job;
