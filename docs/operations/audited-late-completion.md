# Audited completion after a historical pass failure

This route accepts genuinely completed cleanup after a native `swarm pass`
no-handoff failure. It preserves the original failed lifecycle, timestamps,
outcome, goal and notices. A validated immutable completion receipt supplies the
read-only completed projection; actual started or running work still displays
active. Scheduling and cancellation continue to use the raw lifecycle.

The exception is limited to a recorded native no-handoff failure with its native
notice and ended launch history. Manual failures, cancellation, unknown legacy
provenance and other closed states are refused. It never enables supervision,
creates an owner, changes an agent role or opens ordinary task admission.

## Preconditions

An actually enrolled final executor posts the normal `swarm done` handoff for an
immutable final candidate after the failure. The candidate should identify the
final operational manifest, requirements and all cleanup receipts. Historical
goal obligations and any explicitly approved revised requirements remain visible.
All actual work, tasks, questions, launches and background processes must settle.
An idle persistent conversation is allowed only after its authenticated current
turn has ended; an idle label cannot prove a background process stopped.

The requester must be the original interactive Codex root, established through
upstream private coordinator registration and native rollout provenance. Its
current real user response-item must authorize this request. The trusted hook
issues an exact PID, tool-call, session, command and argument bound, expiring,
single-use grant. Environment variables, agent labels, forked sessions and direct
source CLI execution cannot create authority.

## Native commands

The original root invokes these as individual native shell tool calls, allowing
the ordinary PreToolUse hook to attest them:

```text
swarm late-review --job JOB --closure CLOSURE_SHA256 --candidate IMMUTABLE_REF --req REQUIREMENTS_SHA256 --request-id ACTUAL_USER_ITEM_ID --workdir EXISTING_APPROVED_DIRECTORY
```

`CLOSURE_SHA256` is `swarm.settlement.closure(raw_job_status)['sha']`, the digest of
the exact raw failure, goal and run identity. No command changes that identity.
The workdir must already exist and pass the configured native workdir policy.
It is bound in the grant; no allowed roots or sandbox settings are expanded.

The durable reconciler admits exactly one native `gpt-6-astra` judge through the
existing claim, runner and real thread enrollment. Its private assignment is
checked at claim, launch and runtime boundaries. A stale candidate, cancellation
or lost authority prevents execution. A failed review does not retry or staff a
lead. The judge records both normal artifact acceptance and a bound full-goal
MET with zero open requirements and a full report, or records NOT_MET.

After the genuine judge executor has positively stopped and the original root
has independently verified the exact final manifest, the root invokes:

```text
swarm late-finalize --job JOB --closure CLOSURE_SHA256 --candidate IMMUTABLE_REF --req REQUIREMENTS_SHA256 --request-id ACTUAL_USER_ITEM_ID --workdir EXISTING_APPROVED_DIRECTORY --verdict-id BOUND_GOAL_VERDICT_ID --evidence FINAL_MANIFEST_SHA256
```

This separate one-use grant binds the real executor, accepted verdict and final
evidence digest. Plain `FINALIZED` text is visibility only and cannot authorize
settlement. A backend transaction validates the authenticated private intent and
appends a typed finalization receipt together with its visible pipeline message.
The reconciler then atomically appends the immutable completion receipt and
completion event after rechecking all gates. Last-turn hooks and restarted
listeners run the same reconciliation; owner polling is unnecessary.

## Persistence and refusal

Schema 29 adds dedicated SQL completion/finalization tables. Memory and file
backends store equivalent records in reserved internal board metadata preserved
by older file writers. Agent-writable job data and generic messages cannot
insert receipts. PostgreSQL snapshot, status and watch projections validate the
same exact closure and current bound verdict evidence. Fingerprints include the
completion timestamp so a missed notification is repaired by periodic refresh.
The receipt also records native handoff, launch, restart, background, blocker and
task-evidence high-water marks. A newer handoff or work obligation invalidates
its completion projection; unrelated conversation and activity heartbeats do not.
Launch revisions also invalidate acceptance after a native same-row requeue.
These marks follow supported native operations, rather than arbitrary direct
database edits to existing rows. A stale receipt cannot
be replaced by rerunning reconciliation after later work finishes.

Receipt insertion and event publication roll back together on failure. Repeated
reconciliation returns the same receipt. A changed goal, candidate, verdict,
cancellation or unresolved owned runtime prevents completion; the historical
failure remains available throughout. `late_review.pending` is diagnostic
context, never authority. There is no force flag, manual receipt-insertion
command or owner-identity fallback.
