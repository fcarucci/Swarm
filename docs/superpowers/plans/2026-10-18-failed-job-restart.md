# Failed job restart plan

Spec: Francesco asked to complete hindsight-g08 or start it again. Agents record launch facts; Swarm alone changes job status.
Base: f43446a, same branch. No live job changes, deployment or pushes by implementer.

- Add regressions for fresh swarm-run queue after failure, historical queues, closed states, concurrent cancellation CAS, old handoff/attempt isolation and lifetime cap preservation.
- Add atomic failed+finished_at+queue revision guarded reopen to launch storage mixins, preserving owner limits and historical costs. Retire pre-failure queued work.
- Have pass inspect failed jobs and reopen only for fresh explicit run requests before starting. Ignore prior-run handoffs/attempts. Refuse claims on closed jobs and refuse CLI queueing for completed/cancelled/paused jobs.
- Run pass/queue contract suites across offline backends and isolated PostgreSQL when available; commit and hand off for independent review.
