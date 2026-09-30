# PM board identity correction

**Finding:** E2-R2 (high), reported 2026-09-29. `hosts.md` used the fixed PM key
`orchestrator` for separate root sessions. Independent Claude roots F01 and F02 both
followed the example; both raw root transcripts show `--key orchestrator`, and the PM
row moved from F01 to F02. Source confirms the cause: `MemoryBoard.allocate_name` looks
up rows by global `agent_key`; for an active row it updates `job` and returns that row's
name. The key is not job-scoped.

**Fix:** the host guide now requires each root key to include job plus root host/session
identity. If session ID is unavailable, PM generates and persists a UUID and exact key.
Same-session resume reloads that exact key; independent attached sessions get their own
key. It explicitly forbids a fixed generic PM key and reusing another session's key or
joining it on another job. The guide also requires authorship/ownership records to use
job plus host agent ID/key, then resolve the current display name after resume before
directed handoff. Display names are reusable and can change when a completed agent
resumes while its former name is occupied. Ambiguous key-to-name mappings block the
directed handoff instead of guessing.

**Targeted checks:** source inspection of `lib/swarm/board/base.py:1025-1052` and
`lib/swarm/board/memory.py:411-444` confirms global key lookup, job reassignment for an
active row, and name replacement when the old display name is held. Inspection of
`lib/swarm/hooks.py:591-604` confirms the internal roster tracks agent keys while the
rendered roster displays names; `lib/swarm/cli.py:2611-2617` confirms `who` omits keys.
`git diff --check` and targeted text/source consistency checks passed. No runtime
behavior was rerun; the reported F01/F02 traces are the observed failure evidence.

**Recheck:** returned to the same independent reviewer Richard. Fresh affected Claude
root scenarios must use distinct PM keys and confirm correct job ownership before
acceptance. Existing frozen fixture executors remain unchanged; their original frozen
plugin does not include this revision.
