# Scoped completion and guarded recovery

Keep explicit artifact acceptance separate from original-goal acceptance. Current unfinished native tasks must prevent automatic completion independently of handoffs. Preserve legacy unscoped verdict interpretation; never infer a historical explicit owner close was erroneous.

1. Add shared Memory/SQLite regressions reproducing artifact-MET quiet closure, unfinished tasks under goal MET, and failed task admission after erroneous auto-close.
2. Extend shared completion guards. Preserve existing candidate/freshness checks for task-bearing full-goal acceptance.
3. Add atomic backend recovery of completed/auto rows with exact close timestamp and currently proven invalid completion. Preserve run, verdict, task evidence and agent authentication; run through normal reconciliation and task admission.
4. Verify positive goal completion, historical compatibility, explicit owner closure, idempotence and concurrent changed-close/goal-verdict guards. Freeze source for independent review and QA; no install or live lifecycle mutation.
