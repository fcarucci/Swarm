# Autonomous dependent work and truthful session state

Date: 2026-10-18. Baseline: b0dba5842871302bfbd0872290503ccce011f284.
This is the scoped engineering contract, not a claim of completed implementation.

## Evidence and failure mechanism

Live evidence in `docs/evidence/2026-10-18-autonomy/live-before.json` establishes that PlateSolve had zero launch rows and no workdir or task-dependency keys. Its local job enrollment was true, but local owner enrollment was false. QA post 41699 at 15:55:07 recorded PASS for candidate cd12e18; EL acknowledgment 41753 at 16:05:33 states that root dispatched Bart. Thus the missing work was never registered durably, in addition to the missing autonomous terminal pass. No build is restarted during this investigation.

Source independently establishes three gaps:

- `hooks.run_hook` invokes `_pass_sweep` only for `turn`; `passcmd.throttled` uses a 60-second host stamp. A final stop cannot guarantee another scheduling pass, and a queue created during one pass may need another pass to launch.
- `passcmd._advance` closes on a matching `met` verdict before checking running launches or downstream work. A judge's approval alone therefore cannot safely represent completion of a registered build/publish chain.
- `supervisor.pipeline` already has artifact-aware judge/fix/finalize transitions, but it is a separate supervisor path. `events_listener` defaults off and refuses to serve without plugin sources. Reusing it requires an explicit internal reconciliation mode; merely sending an event is insufficient.

The pipeline also treats idle agents as live blockers and its finalizer prompt asks agents to deactivate the job. That instruction contradicts the standing prohibition on agents changing job status.

## Chosen boundary

Extend the existing durable queue and pass, using existing atomic launch claims and bounded runners. Persist dependent work before the predecessor exits. A task identity contains job/run identity, stable stage identity, and the exact artifact. Its persisted prerequisites, execution recipe, and blocker reason survive agent and listener exits. Repeated registration must converge and conflicting registration must fail visibly.

The minimal proposed representation reuses launch rows with a structured task envelope containing stage identity, exact artifact, dependency identifiers, and required evidence. Atomic registration must not overwrite a running or completed row. Ordinary enrolled jobs may register tasks with an explicit valid workdir under normal task authorization; they must not require forged supervisor/drive ownership credentials. A dedicated report route validates task, generation, artifact and permitted reporting provenance.

Readiness comes only from durable evidence for the exact artifact and declared prerequisites. An older commit's review or a different artifact's job-level verdict cannot release a build. Required failed stages hold downstream work with their concrete reason. A newer artifact supersedes older pending work without granting it the new artifact's evidence.

One scheduler owns dispatch and completion. Tasks flow through blocked, ready, claimed, and terminal outcomes using storage transactions or revision checks. Duplicate hook events and simultaneous passes launch at most one attempt. Claims recheck cancellation, lifetime costs, concurrency, and current prerequisite validity. Runner failures remain observable and retry only within existing attempt/budget bounds.

Hooks request reconciliation after terminal lifecycle writes. The existing event-listener infrastructure supplies bounded periodic recovery even after the PM and all interactive turns end. Internal scheduling must work without CI plugins and must not silently open external HTTP service. The proposed listener mode performs reconciliation only, with no HTTP bind when external events are disabled or no sources are present. A durable pending item survives a crash between persistence and wake. Listener startup/health failure is a precise operational blocker, not a false running agent.

Job completion requires all required dependent work and finalization to succeed and no actual worker to remain. A passing review is evidence for its stage, not job completion. Agents continue to be prohibited from lifecycle commands; Swarm derives state.

## Ownership

- Dependency engineer: durable registration API/storage adapters, prerequisite evaluation, pass dispatch/completion integration, listener recovery and minimal hook wake integration, pipeline prompt correction, focused scheduler tests.
- Status engineer: actual agent turn state and derived status across memory/file/SQLite/Postgres and watch snapshots. A live session is not an executing turn. Running workers imply active; only idle workers cannot imply active. Idle is an honest intermediate observation, while scheduler recovery or a precise blocker accounts for outstanding work.
- Session engineer: native owner-authorized reassignment command, storage/session markers, filtered watcher behavior and tests. Preserve job identity, activation history, evidence, queue, and lifecycle state. Do not use relabeling or recreation.
- Engineering lead: this contract, interface review and acceptance coverage. Root owns live evidence, integration, deployment decisions, and the explicitly authorized final binding of exactly loom-platesolve and loom-flythrough-plan to session 01a14f8c-7e04-7c81-ac1b-6610de7d8d0a.
- Independent QA/review/judge: validate committed implementation and adversarial scheduling cases; do not use the real PlateSolve build as a test.

Shared-file edits, especially hooks and CLI dispatch, require explicit coordination between their engineers.

## Acceptance evidence

1. Register a downstream build before QA/review finish. End every agent turn. Deliver the final exact-artifact evidence and observe one bounded runner launch without an orchestrator tool call.
2. Duplicate, reorder, or drop wake notifications, restart the listener, and crash between durable enqueue and wake. Poll reconciliation recovers exactly one eligible launch.
3. Wrong-artifact evidence, failed QA/review, and superseded candidates never launch the build. Display a specific blocker.
4. Race cancellation and competing passes against launch. No launch after cancellation wins; concurrency and lifetime budget hold.
5. A met verdict cannot complete a job whose required build/finalizer remains pending, running, or failed.
6. Completed Codex turns become idle without destroying resumability. Following turns become running. Watch and backend snapshots agree; running takes precedence over questions, while all-idle does not display active.
7. Reassign only the two named jobs using the native owner route; preserve history/status/queue and have the session watcher show exactly those two. Read-only plan phase performs no live reassignment.

## Alternatives rejected

A terminal-hook-only patch loses wakeups under throttle, crashes, and later external events. A new independent scheduler duplicates existing launch claims and budgets. The selected design retains a single dispatch authority and adds durable dependencies plus recovery to that authority.

## Workflow integration

`skills/engineering-team/SKILL.md`, its EL brief in `references/hosts.md`, and
`references/dependencies.md` require actual native registration before predecessor exit.
The API is `task add` (repeatable `--depends`, exact `--artifact`, `--generation`),
`task report` (assigned executor, immutable passed/failed evidence), and `task list`.
An already-running external seat can be declared with `--reporter`; it is not relaunched.
Completed work is never restarted solely to create a report. A distinct verifier may
inspect its existing artifact when a fresh evidence gate is necessary.

Reconciliation process survival and claim recovery are separate guarantees. A claim
lease and runner wake cannot recover a hard-killed sole listener with no future event.
Unattended hard-kill recovery needs supervision of the existing listener process, with
installed behavior independently demonstrated; hooks alone do not prove this guarantee.
