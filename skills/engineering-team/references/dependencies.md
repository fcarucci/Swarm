# Durable dependencies and executor evidence

Read this before EL dispatches a candidate's review, QA, build or finalization stages.
Register the chain before predecessors end their turns. An EL/PM message promising to
start the build later is not executable state. The scheduler must have the next action
while the requester and every agent are quiet.

## Register the exact candidate

Use an immutable artifact such as `branch@<full SHA>`, one generation for its chain,
and a stable name for each stage. Resolve the actual configured role model identifiers;
this workflow uses Astra EL/judge, GPT-6.1 engineers and Luna/Terra QA.

```sh
swarm task add --job J --name qa --generation 1 --artifact REF --role qa --host codex --model QA_MODEL --workdir DIR --brief 'Run acceptance checks for REF; report exact evidence.'
swarm task add --job J --name source-review --generation 1 --artifact REF --role reviewer --host codex --model REVIEW_MODEL --workdir DIR --brief 'Independently review REF; report findings and evidence.'
swarm task add --job J --name build --generation 1 --artifact REF --depends qa --depends source-review --role engineer --host codex --model ENGINEER_MODEL --workdir DIR --brief 'Build only REF after both gates pass; verify archive identity and report evidence.'
swarm task list --job J
```

Replace every placeholder and brief with real checks and authorized actions. `--brief`
is text, not a filename; a longer brief can point to a durable reviewed artifact. Each
stage reports for its own name, generation and exact artifact. Dependencies refer to
stage names within the same generation. Different artifact evidence never grants a gate.

Registration is idempotent for the same specification and does not rerun a running or
finished task. A conflicting specification needs a new generation. A new candidate needs
new exact-artifact evidence; never reuse old QA/review results by changing a label.

## Existing work and completed work

For an already-running authorized agent, add `--reporter 'CURRENT_AGENT_NAME'` to its
stage registration and use that agent's actual role. This is an external evidence stage:
Swarm does not launch it. Resolve its current enrolled identity before registration.
Only that assigned live agent can attest the result, and its turn must end before the
dependency releases. The requester must not impersonate the agent or report on its behalf.

Do not restart a completed engineer, reviewer or build merely to create activity or
manufacture a task report. Preserve its existing evidence. If a fresh gate is necessary,
register a distinct bounded verification task that inspects the already-produced artifact;
it must not repeat the original build or other external action. Unknown archive identity,
missing provenance or inaccessible results are concrete blockers, not passing evidence.

## Executor brief and report

Every task brief includes this contract, with actual values:

> Work only on task NAME, generation N, artifact REF and the authorized scope. Read
> prerequisites and verify artifact identity before execution. Perform CHECKS. Before
> ending your turn, record the result using the command below. Report failed with the
> actual failed check or missing input; do not report passed from another agent's prose.
> Do not activate, deactivate, pause, resume, wait or otherwise set job status. Do not
> dispatch a successor already registered in the chain. Then end the turn normally.

```sh
swarm task report --job J --name NAME --generation N --artifact REF --outcome passed --reason 'Exact checks, results and durable evidence pointer'
# On failure, use --outcome failed with the concrete check/input that failed.
```

The reporter must be the assigned executor; `--as NAME` is only for its own correctly
enrolled identity where the host requires it. First evidence is immutable. Do not post
optimistic evidence and plan to replace it later. Structured evidence and successful
runner termination are both required; external reporters require observed turn/session end.

A board DONE message can summarize the report but cannot replace it. A judge verdict
applies only to its artifact and cannot complete unfinished required build/finalization work.

## Before EL ends a turn

Inspect `swarm task list --job J` and establish that every remaining authorized action
has a persisted stage, executable brief, exact artifact, role/model and prerequisites.
Check listener reconciliation health using the installed runtime's supported diagnostics.
No further PM tool call should be needed to release a ready task. If health or permission
is missing, record that precise blocker; do not claim unattended execution is validated.

Internal dependencies remain task blockers. Ask a specific owner question only when a
human decision is needed. Swarm calculates job state: actual running work means active;
all-idle agents do not become active because of a board post or an unfinished task.
