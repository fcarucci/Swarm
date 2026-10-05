# Host orchestration and recovery

Use with the [role contract](team-roles.md), [artifact contract](artifacts.md), and
[Swarm CLI/host reference](../../swarm/SKILL.md). The invoking agent is PM. EL decides
technical staffing and assignments; PM owns activation, board polling, root dispatch,
and user reports. A `project_manager` delegate does bounded planning/reporting and
returns to PM; its label grants no host-only powers.

## 1. Check the installed host before dispatch

Record these in the job's evidence record, separately for Claude and Codex:

- Installed plugin path, version, source commit or package identity, and host version.
  The minimum released version is `0.1.1`, which packages this skill and custom-role
  support. A source checkout may be used when it contains the same changes. Verify
  the actual loaded source and role enrollment; a cached `0.1.0` plugin does not
  expose this skill or route custom roles. Inspect the installed hook source, not just a
  development checkout; a source CLI's manual
  `join --role engineer` does not prove the installed hooks route roles.
- Run the installed plugin's `bin/swarm doctor --host claude` or `--host codex`.
  Check configured board/backend, connectivity, hook trust, and session setup. Record
  failures; do not silently install/update the plugin or change configuration.
- Read the actual host tool schema and declared slot limit from session context or
  configuration. Record whether the limit includes PM/root and whether completed
  persistent threads still count. If unknown, count root conservatively and dispatch
  serially. Check fresh-context and lifecycle tools before relying on them.
- Record available research tools and actual network access. The Codex Swarm sandbox
  may lack network; web tools may differ from shell access. Relevant research blocked
  by access is `not performed: <actual blocker>`, not `not applicable`. Product records
  either omission with its reason, PM acknowledges it and reports it to the user.
  Never invent findings or relax sandbox settings to conceal a research limitation.

After setup below, use the first safe, useful role invocation for enrollment: it must
make a local tool call and post `BRIEF: <durable path> tasks: <IDs>` before PM checks:

```sh
swarm status --job J --all-agents
```

Confirm that invocation's row has its custom role, even if it has already returned.
Hooks may only resolve enrollment at the first tool call. An immediate check after
spawn, or active-only `swarm who`, can falsely report failure. If the row still shows
the host agent type (for example `default`) instead of the requested role, stop
role-dependent dispatch and report an unsupported installed runtime and the needed
source update. Missing enrollment is unresolved, not passing. Do not work around it
with a manually relabeled row and claim host validation. On Codex also confirm the
dry run used `fork_turns: "none"`. Static frontmatter/link/diff checks and checkout
tests prove neither installed enrollment nor host workflow behavior. Keep each host's
independent behavioral evaluation `pending` until it is actually observed and graded.

## 2. Activate and keep PM on the board

Below, replace `swarm` with the absolute `swarm command:` printed by activation and
replace example job `J`, paths, IDs, names, and goal text with the real values.
Claude initially uses `${CLAUDE_PLUGIN_ROOT}/bin/swarm`; Codex uses
`<plugin root>/bin/swarm` (three directories above this reference).

```sh
# Optional: add --team product_manager,build_engineer to override the default composition from team.toml
swarm activate --job J --description "Bounded engineering delivery" --task "Brief: docs/team/request.md" --goal "Meet the recorded requirements with independent review and product, EL, and QA acceptance"
# Set once for this job and root host session; record the exact key in the PM run record.
PM_KEY="pm:<job>:<root-host>:<root-session-id>"
swarm join --job J --key "$PM_KEY"
swarm read --job J --key "$PM_KEY"
```

`agent_key` is a global identity on the board, not scoped by job. A second job or
attached root session therefore needs a different key. Include the job and root
host/session identity in every PM key. If the host provides no root session ID, generate
a UUID once (for example, `python3 -c 'import uuid; print(uuid.uuid4())'`), include it
in the key, and persist both the UUID and exact key in the PM run record before joining.
Never use a fixed generic key such as `orchestrator`; never reuse a different session's
key or join that key under another job. On a resume in the same root host session, load
and reuse its recorded key exactly; do not generate another. Every independent attached
session gets a new key of its own. These rules prevent a join from moving the existing
global agent row to another job.

Use the current name returned by this PM key's `join` for PM posts. The main session
gets no member hook messages: explicitly `read` at each turn start, before every staffing
wave, and after direct returns; drain any `more unread` pages. Reply to directed requests.
Before a directed handoff, identify the recipient by its host agent ID/key in the durable
ownership record, then resolve that identity's current name in this job's active roster.
Names are reusable display labels: a completed agent that resumes can receive a different
name if its former one is held by another active agent. `swarm who --job J` shows active
names but omits agent keys, so it is not by itself proof that a historical name belongs
to the same agent. Use the latest identity-to-name mapping established by the resumed
agent's current enrollment/board post and the host invocation record. If the mapping is
ambiguous, hold the `--to` handoff until the agent posts its current identity; do not
guess from an old name. Never treat display name alone as stable author identity. Record
authorship and ownership by job plus host agent ID/key, and refresh the current display
name after each resume before addressing or attributing work.

Use `status --all-agents` for job history. Broadcast file claims/findings; address
requests and handoffs with `--to` only after resolving the current name:

```sh
swarm post --job J --as "PM name" --to "EL name" "Capacity: one next invocation; return revised staffing in docs/team/engineering.md"
```

Claude can route multiple jobs by prompt tags. Codex allows one job per session;
prompt tags cannot select another job. A configured second session/host sharing the
board may use `swarm activate --job J --attach` to attach to an already active job
without reopening it. That session uses its own durable PM key, built from the same job
and its own host/session identity; never transfer or reuse the first session's key. Agree
one PM scheduler; do not create competing ownership or dispatch loops.

## 3. Give every invocation a bounded, durable brief

Include job, PM name, absolute CLI path, repository/workspace, role, task IDs, durable
brief path, requirement revision, inputs, exclusive files, checks, and return condition.
Its first board post is `BRIEF: <durable path> tasks: <IDs>` using the hook-assigned
name; make a local tool call so enrollment can resolve. Read the brief and board,
claim files before edits, and post findings and handoffs. Keep board posts within the
board's message cap (default 200 characters; `swarm config board.message_max_chars`); put details in artifacts.

Every invocation returns directly to PM after its bounded deliverable, including on
a blocker. Post the request to PM on the board **and** return this structure:

```text
action: review | assign | fix | test | accept | resolve_blocker
task_id: E1
artifact_path: docs/team/task-E1.md
blocker: none | exact missing capability/decision
result: changed paths, immutable task diff/base, checks and limitations
```

An EL staffing request must return; do not leave EL waiting indefinitely for an unread
board message. Review briefs supply requirements, immutable diff/base, and reproducible
checks. Reviewers inspect those sources independently before treating author narrative
as proof. Shared board history remains visible: a fresh context is not a blind review.

### Claude Code

PM calls the host's `Agent` tool with `prompt` and a bounded `description`; use a fresh
agent for each independent role. Put both tags on their own lines in **every** prompt:

```text
[swarm job: J]
[swarm role: engineer]
Task E1. Read docs/team/task-E1.md. PM: <exact board name>.
Use <absolute swarm command>. First board post: BRIEF: docs/team/task-E1.md tasks: E1
Own only the files named in that brief. Finish the bounded task, post a handoff,
and return action, task_id, artifact_path, blocker, and result to PM.
```

Change the role tag per seat: `engineering_lead`, `product_manager`, `build_engineer`, `qa`, `engineer`, `reviewer`, `verifier`, `judge`. `swarm post --to @EL` and the other role addresses resolve to the agents holding that tag now, so the tag must be exact; an orchestrating agent that should receive `@PM` when there is no product manager joins with `[swarm role: project_manager]`. The tag determines
the Swarm role; the host's agent type does not substitute for it. A member's exceptional
synchronous helper also needs `[swarm spawn: <why strictly needed>]` and the job tag;
meet configured `min_justification_chars` and all child caps.

### Codex

PM calls `spawn_agent` with a role-prefixed task name and a fresh context, for example:

```json
{
  "task_name": "engineer__e1",
  "fork_turns": "none",
  "message": "Task E1; read docs/team/task-E1.md. PM: <exact board name>. Use <absolute swarm command>. First board post: BRIEF: docs/team/task-E1.md tasks: E1. Own only brief files. Return action, task_id, artifact_path, blocker, and result directly after this bounded task."
}
```

Codex hooks cannot read the encrypted child message's tags. All children join the
session job, and `task_name` selects the role:

| Task name example | Responsibility / enforcement |
|---|---|
| `project_manager__report` | Bounded PM delegate; custom worker metadata |
| `product_manager__spec` | Product baseline and later product acceptance; custom |
| `build_engineer__gate` | Optional: build/CI gating and scoped fix rounds; custom |
| `engineering_lead__plan` | Technical plan/staffing and later technical acceptance; custom |
| `engineer__e1` | Assigned implementation; custom |
| `reviewer__e1` | PM-assigned independent review; custom, not read-only |
| `qa__acceptance` | Independent QA and relevant test authoring; custom |
| `verifier__check` | Built-in read-only claim checker; no spawns |
| `judge__final` | Built-in verdict authority; no implementation or spawns |

Custom roles have ordinary worker permissions, not enforced review/acceptance powers.
Role identifiers are 1–64 lowercase letters/digits/underscores, start with a letter,
and contain no `__`; the task suffix is nonempty. The host requires the entire
`task_name` to use lowercase letters, digits, and underscores; use `engineer__e1`
while retaining task ID `E1` in the brief. Reserve built-in `judge` and `verifier`
for their actual duties. A test-writing QA/reviewer needs a custom role, not `verifier`.

Require `fork_turns: "none"` for fresh independent launches, especially reviewer, QA,
product acceptance, verifier, and judge. If unavailable, report the missing capability.
`followup_task` preserves the old role/context: it can resume an engineer's fixes or a
reviewer's recheck, not turn an author into its independent reviewer, QA, or product
acceptor. A same-role product manager or EL may return for acceptance if independence
is preserved; a newly launched replacement reads the artifacts in a fresh context.

## 4. Schedule against actual capacity

PM records a small queue with owner, dependency, state, next check, and host invocation
ID. EL requests engineers and review coverage; PM root-spawns bounded work in waves
and returns infeasible staffing requests to EL. Never weaken independence to fit slots.

- Track the declared host budget and actual persistent threads as well as active
  invocations. A returned/idle agent can still occupy a slot: count it until host
  evidence shows release. Serial execution does not by itself reclaim persistent
  thread capacity. Use only lifecycle/status tools exposed by this host; do not
  invent a close API or assume an interrupt destroys a thread.
- Codex `stop_quiet_minutes` (default 3) controls Swarm's completed status after a turn,
  not host capacity. `swarm leave`, job deactivation, and a stale board row also do
  not prove a host thread stopped or a slot was freed. Check supported lifecycle
  results before the next wave, and retain the evidence in the schedule.
- Root PM spawns are the default. Member helpers are exceptional synchronous work,
  subject separately to `[spawn] max_per_agent` (default 2), `max_per_job` (default 4),
  and `max_depth` (default 2). The job child budget is not total team size. Admitted
  attempts are nonrefundable, even after child exit or host launch failure. Codex
  depth also needs host support; explain helper need on the board because prompt
  justification tags are unreadable there. Judge/verifier cannot spawn; members
  cannot create the judge.
- Never use a member spawn to probe capacity. A root launch refusal is a capacity
  signal: requeue, record it, and wait for changed capacity evidence rather than
  retrying. Reuse a same-role context only when its task preserves independence.
  If a required fresh checker cannot launch and no supported lifecycle action frees
  capacity, report the blocked task and needed fresh-session/authorized-host route.

Example dependency sequence (pack waves only when observed capacity permits):

1. Product baseline → PM comparison with original request → EL plan/staffing.
2. Engineer implementations → PM-assigned independent reviews → same-author fixes
   and same-reviewer rechecks. QA may author tests in parallel on separate files;
   another engineer reviews every QA code change.
3. EL integrates and freezes the candidate manifest and requirement revision.
4. Final acceptance wave: QA executes checks, product manager exercises criteria,
   and EL checks technical requirements on that same candidate. Resume each in its
   same role or launch a fresh replacement from artifacts; preserve author/QA and
   author/product separation. Record three separate decisions, even across waves.
5. Optional verifier checks specific claims; the goal job's one judge evaluates all
   evidence. PM performs the freshness check below before completion.

If PM ends a turn with work remaining and no agent active, record the actual reason:

```sh
swarm wait --job J --on "Fresh independent reviewer capacity; E1 queued"
swarm resume --job J
```

Run `resume` when returning to work after the dependency clears, then poll the board.

## 5. Fence stopped writers and recover

Before manual reassignment, confirm the old writer has stopped through supported host
status/stop tools or observed completion. An interrupt request alone, silence, a dead
board row, or a supervisor notice is insufficient. For supervisor replacement, check
`swarm status --job J --all-agents` and confirm fencing before treating the replacement
as sole owner. Do not accidentally launch a second replacement.

Once stopped, post the claim transfer and give the replacement the request, durable
brief, requirement revision, current artifacts, last committed task state, and partial
diff. It reconstructs the task from those sources, never from its task name alone,
and treats partial edits as unreviewed. If stop cannot be confirmed, keep shared files
blocked or use an independent workspace; do not allow concurrent writers in one checkout.

On board outage, continue only isolated work with established ownership. Defer new
shared claims, board-gated spawns, and acceptance; queued/spooled posts are not delivered
coordination. After recovery, drain messages and reconcile claims and candidate state
before dispatch. Report an unrecoverable blocker with evidence and the decision needed.

## 6. Complete only on matching evidence

Use one built-in judge only for a job activated with `--goal`; PM schedules it at root.
The judge posts its verdict through the CLI under its assigned name, for example:

```sh
swarm verdict --job J --as "Judge name" met "candidate=<manifest-id> req=<revision>; evidence docs/team/acceptance.md"
```

A `not_met` verdict returns to PM for owner assignment, repair, review, and recheck.
Before completion, PM recomputes/compares the current integrated SHA-256 manifest ID
and requirement revision with product, EL, QA, and judge records using the
[artifact identity rules](artifacts.md). Swarm enforces only the latest `met`, not
its freshness; a verifier's `VERIFIED` count is likewise not candidate-bound.
Changed deliverables or requirements reopen affected task reviews and all final
QA/product/EL/judge gates. Evidence-only edits outside the manifest do not. Obtain
fresh matching decisions/verdict before proceeding; never force completion to hide gaps.

```sh
swarm deactivate --job J --status completed --outcome "Accepted candidate=<manifest-id> req=<revision>; evidence docs/team/acceptance.md"
```

Report delivered scope, checks, omissions, risks, and each host's observed/pending
status separately. Documentation correctness is not behavioral validation, and neither
authorizes plugin installation, release, merge, publication, or deployment.
