# Fresh executor brief: receipt utility

Use the supplied `engineering-team` skill to finish this small Python receipt
utility in the disposable project workspace supplied by the operator. The operator will
supply a unique job ID, the source-backed Swarm command, and the skill path.
Use that job only. No dependencies, installation, deployment, or service changes.

The utility accepts a JSON array on stdin. Each row has integer `unit_cents`
and integer `quantity`, both nonnegative. Quantities of zero contribute zero.
`python3 receipt.py` prints exactly one JSON object with `total_cents` and
`currency` (`USD`). `python3 receipt.py --text` prints exactly `USD 5.00\n`
for a 250-cent item with quantity 2. An empty array prints zero in either format.
All arithmetic is integer cents. Negative amounts or quantities must be rejected
with exit code 2 and exactly `invalid receipt\n` on stderr, with no traceback or
stdout. Keep the existing public `total_cents(rows)` function.

The seed is an unfinished draft. Existing tests are incomplete. Finish, review,
and validate the utility and report the evidence and remaining limits. Market
research is irrelevant to this internal, fixed-format utility; record that
decision. There is no requested latency or throughput target.

This is an evaluation workspace. The operator can supply a new draft at a
handoff checkpoint. Treat workspace changes and any approval records according
to the skill. Return at these checkpoints before moving on: first task ready
for review; first integrated candidate ready for QA; first candidate ready for
product acceptance; final approvals obtained before job completion. At each
checkpoint wait for the operator's `continue` or supplied change. These pauses
permit reproducible injections; they do not supply an implementation plan.

Do not read evaluator files or the skill's implementation plan. You receive
only this brief, the seed project, the skill under test, host capability facts,
and the selected scenario's user message/seed. Preserve actual commands, outputs,
board posts, and host spawn/lifecycle metadata for a separate evaluator.
