
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
  LEFT JOIN (
    -- Group by incarnation, not just name: names can be reused after departure.
    SELECT a.agent_key, count(*) AS messages
      FROM agents a JOIN messages m
        ON m.job = a.job AND m.agent_name = a.name AND m.created_at >= a.joined_at
     GROUP BY a.agent_key
  ) mc ON mc.agent_key = a.agent_key;

DROP VIEW IF EXISTS job_status;
CREATE VIEW job_status AS
SELECT j.job, j.status, j.description, j.task, j.outcome, j.created_by, j.session_id,
       j.created_at, j.activated_at, j.finished_at,
       count(s.agent_key)                                  AS agents,
       count(*) FILTER (WHERE s.status = 'started')        AS started,
       count(*) FILTER (WHERE s.status = 'running')        AS running,
       count(*) FILTER (WHERE s.status = 'idle')           AS idle,
       count(*) FILTER (WHERE s.status = 'completed')      AS completed,
       count(*) FILTER (WHERE s.status IN ('dead', 'left')) AS dead_or_left,
       COALESCE(m.messages, 0::bigint) AS messages,
       greatest(max(s.last_contact_at), m.last_post_at) AS last_activity_at,
       j.project, j.goal, j.verdict, j.verdict_reason, j.verdict_by, j.verdict_at, j.completion_forced,
       (SELECT a.name FROM agents a WHERE a.job = j.job AND a.judge AND a.left_at IS NULL) AS judge,
       j.waiting_on, j.waiting_since, j.closed_by, j.supervise, j.verdict_next,
       j.max_hours, j.waiting_until,
       -- what `status` shows (base.derive_job_status): closed jobs their status, open ones waiting /
       -- active / waiting (goal not met: a goal without a met verdict and nobody started, running
       -- or idle; no sweep closes it) / idle
       CASE WHEN j.status <> 'active' THEN j.status
            WHEN COALESCE(j.waiting_on, '') <> '' THEN 'waiting'
            WHEN count(*) FILTER (WHERE s.status IN ('started', 'running')) > 0 THEN 'active'
            WHEN COALESCE(j.goal, '') <> '' AND j.verdict IS DISTINCT FROM 'met'
                 AND count(*) FILTER (WHERE s.status = 'idle') = 0 THEN 'waiting (goal not met)'
            WHEN COALESCE(greatest(max(s.last_contact_at),
                                   m.last_post_at),
                          j.activated_at, j.created_at) > now() - make_interval(mins => {idle}) THEN 'active'
            ELSE 'idle'
       END AS shown_status
  FROM jobs j LEFT JOIN agent_status s ON s.job = j.job
  LEFT JOIN (SELECT job, count(*) AS messages, max(created_at) AS last_post_at
               FROM messages GROUP BY job) m ON m.job = j.job
 GROUP BY j.job, m.messages, m.last_post_at;
