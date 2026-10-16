# Design: drive mode without seats (Francesco, 2026-10-15: "remove this seat bullshit COMPLETELY")

## What goes away
The seat concept and everything built on it: `swarm seat request|release|list|show`, seat rows with the
requested/idle/running/released/failed/refused/cancelled state machine, seat revisions, seat_request
authorization receipts (drivegate, driver_exec.authorize_seat/seat_authorized/carry_authorization,
driver_snapshot._seats receipt gate), `requested_by`, the release-before-re-request rule, dead seat names,
jail-enforced read-only roles (driver_exec.read_only_role), the per-job concurrency cap compared host-wide,
the `wt:` workdir lock that serializes agents sharing a workdir, tamper halts on goal change, launch-time
workdir refusals retried in a loop, and the lead's "staff seats" duties.

## What replaces it: launches
A drive job has a goal, a budget (cost, sessions, wall clock, per-job concurrency) and a list of LAUNCHES.
A launch is the minimal record the driver needs: `name, role, harness, model, workdir, brief, state
(queued|running|done|failed), attempts, last_outcome`. Nothing else.

- `swarm run --job J --name N --role R --brief B [--model M --harness H --workdir D]` queues a launch.
  Anyone the hooks already trust may call it: the lead, an engineer, the requester session, a person. No
  authorization dance, no receipt. Calling it again for an existing name re-queues that launch (new attempt),
  whatever its state: there is no "release", no dead names.
- The driver starts queued launches on every pass, up to the job's `max_concurrent` (this job's running
  launches only; an optional `[drive] max_host_running` is a separate host-wide knob). No workdir lock: two
  agents may share a workdir; git worktrees are the agents' business, as for humans.
- A launch that exits is `done`; one that ends refused, crashed or without a hand-off is `failed` with the
  reason posted on the board ONCE, addressed to whoever queued it, and is not retried unchanged. Re-queuing it
  is a one-line `swarm run` by the lead or the coordinator.
- Budgets are checked before EVERY start, including re-queues.
- Roles are prompt text only (prompts.py). Judges and reviewers are told they only judge; nothing in the jail
  enforces it. The jail keeps the real protections: no credentials, no push, no land, no production.
- The goal lives on the board and may change at any time (`swarm job --goal`, `job merge`); the driver never
  halts for it. Tamper handling keeps only: job row missing, drive flag cleared, supervise off, job paused.
- Workdir validation happens in `swarm run` with the launcher's exact rule (allowed root, no dot-directory,
  not home) and refuses with the reason; the driver never retries a refused workdir.
- `swarm status` shows launches as `queued/running/done/failed` with the reason; a job with queued launches
  and none running for more than one pass is a DRIVER bug and is reported as such on the board.

## Migration
- Existing seat rows map 1:1 to launches (requested->queued, idle/running->running or done, closed->failed/done).
- The lead prompt replaces "request seats, release finished ones" with "queue launches with `swarm run`".
- Hooks: `run` joins GRANT_SUBS where `seat` was; SEAT_SWARM_COMMANDS gains `run`, loses `seat`.
- Tests: every seat lifecycle test is replaced by a launch test; test_seat_isolation's pinned rule count
  updated. Regression tests for F1 (two agents, one workdir, both run), B5 (six jobs, per-job cap), D2/A6
  (reopen then first pass launches the lead), B3 (goal change, no halt), C4 (dot-dir refused at `run`).

## Acceptance
A drive job with a goal and three `swarm run` calls (lead, engineer, engineer sharing the lead's workdir) has all
three running within one pass, survives a goal change, a lead crash (re-queue by name), and a budget bump, and
ends complete when the judge rules met. No command named `seat` remains in the CLI or the skills.

Landing is a property of the job's pinned owner destinations, independent of its recipe: remote URLs select destinations and target_branch selects their target (legacy default main); target_branch alone is not a destination. With none pinned, goal or ruling MET completes after learnings, no integrator is queued, and land reports nothing to land; a legacy no-destination refusal is permanent and consumed once. With destinations pinned, MET on an unlanded candidate never becomes unreachable, including at recovery deadlines: integrator retries are bounded by max_attempts, and refused or exhausted landing uses DRIVER-ESCALATE notifications with backoff while the job stays active. A person or owning coordinator may complete a latest goal/ruling MET with their supplied outcome despite unfinalized artifacts; without MET, completion still lists outstanding finalization.

Budget and limit exhaustion holds the job waiting on a person (budget), never records mechanical unreachable. The driver posts `DRIVER-ESCALATE budget:` once and repeats launch-failed notifications every five minutes until a person raises the relevant limit with `swarm drive budget --job J KEY=VALUE` or cancels. Held passes launch nothing and consume no attempts; a sufficient budget increase resumes work on the next pass. A ruling not_met gives the lead its fix round before another ruling: an unchanged goal-ready head is ruled at most once per generation, with at most three ruling launches per generation; hitting either cap also escalates and holds. Budget-triggered ruling prompts explicitly require not_met with budget as the blocker.
