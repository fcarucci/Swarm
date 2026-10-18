# Hook/reconciler incident: evidence and acceptance boundary

This record separates observed live facts from isolated test evidence. It is not a
claim that the requested live handoff has passed.

## Observed live facts

- At approximately 09:54 local time, process 4127094 ran the native `events serve
  --reconcile` command with the user's configured board. Its heartbeat advanced and
  reported ready. The process began at 09:49:22.
- The dependency engineer read its runtime environment: PYTHONPATH pointed at the
  deleted 0.3.3 installation while the installed plugin was 0.3.4. Existing readiness
  checked config, stamp, heartbeat and state, not loaded-runtime identity.
- Three pending PlateSolve events were Mac performance messages addressed to `@el`.
  They were not task evidence. The diagnostic's global pending-event count nevertheless
  demanded an orchestrator waiter. Six external dependency stages were blocked on
  missing evidence; that is a valid gate, not proof of failed dispatch.
- The recent hook error log contained repeated parse warnings for old Oct 15 transcripts.
  No matching old terminal-hook event identifier or pass/reconciler error was established.
  Do not fabricate an original hook ID or call this a reproduced historical dispatch failure.
- Source `hooks.run_hook` discarded a returned error from `dependencies.wake` on terminal
  events, while logging thrown exceptions. This can silently discard a startup/runtime
  mismatch blocker; the task command already persists the corresponding error string.
- Root attempted native creation of a dedicated proof job. The agent lifecycle guard
  refused it and no state changed. No identity, environment or authorization bypass is allowed.

## Scoped repairs and ownership

Reverend Lovejoy owns target-aware diagnostics and terminal-hook returned-error
observability, plus isolated terminal-to-reconciler-to-claim proof. Lewis Clark owns
immutable loaded-runtime identity and guarded native listener replacement. Hank Scorpio
owns snapshot native-task APIs and context fingerprint recovery. Duffman independently
reviews listener/diagnostic/proof changes; Mayor Quimby reviews snapshot repairs.

Pending role messages remain visible; do not auto-ack them or manufacture task evidence.
A task must report from its assigned live identity before its terminal event. Its report,
artifact, generation and successful terminal state jointly gate the successor. No existing
PlateSolve build is relaunched for this investigation.

## Evidence required before acceptance

For each isolated test, record its test name, exact source SHA and backend/config scope.
Record hook event type and fixture identifier, task row/generation, claim/restart identity,
artifact, configured model and resulting state. If the launcher is fake, label it simulated;
no real spawned model agent exists in that proof. Exercise returned-error and thrown-error
paths, missing/wrong evidence, duplicate terminal events and subsequent listener ticks.
No subsequent orchestrator turn may be necessary to dispatch the eligible stage.

For a later owner-created live proof, record the real hook identifier if available, task
row/generation, restart/claim ID, actual spawned agent key and model, immutable artifact,
state transitions and timestamps. A missing identifier stays explicitly unavailable.
A fresh targeted verifier may inspect an existing artifact; it must not rebuild old Mac
artifacts or import a fabricated PASS. Keep owner activation denial separate from code results.

Before runtime replacement, independent review must approve native process identity,
exclusive lock ownership, PID reuse protection and exact config/runtime matching. After
replacement, require a new ready heartbeat for the expected runtime and only one listener.
A supervised service and hard-kill recovery are separate operational evidence from a
successful detached listener tick. Full suites run through GitHub Actions; local checks
remain targeted. Installed behavior is not certified by isolated tests alone.
