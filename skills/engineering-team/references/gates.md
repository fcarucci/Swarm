# Integration gates, goals, and hand-offs

Terms are generic: the **change record** is wherever your project tracks a proposed change and its review (pull request, patch, merge request); **CI** is its automated checks; **build slots** are the limited places a build or test can run.

## Head-bound verdicts

Every review verdict is one fixed line, posted on the change record (where integration automation reads it) and on the board:

`REVIEW #<id> @ <full-sha>: VERIFIED <why>` or `REVIEW #<id> @ <full-sha>: CHANGES REQUIRED <why>`

A verdict is stale as soon as the change has a new head. Integrate only when all hold: a `VERIFIED` for the current head, green CI on that head, and no later `CHANGES REQUIRED`. Never relay "review posted" without the sha. Use one loop keyed on the head sha for all open changes instead of a hand-pinned watcher per change, and trigger it from change events; polling is only the safety net.

## Gate on the rebased result

Rebase the branch onto current main locally with `git rebase` and test that exact rebased SHA.
Use an MR/PR as the review and CI vehicle where supported; otherwise push directly.
If rebasing changes the SHA, refresh the hand-off, verdict and CI before fast-forward/pushing
the approved rebased branch. Never create merge commits, squash or force-push. If main moved, repeat the rebase and refresh
review and CI for any changed SHA. `build_engineer` runs this when present, otherwise EL.

## Scoped fix rounds

The first push gets the full gate. A fix round runs the affected tests plus lint locally, with CI running the full gate in parallel. Do not rerun the full gate for every fix round. Before integration, the head must have a green full CI run. Use build slots deliberately: measure load before adding parallel builds.

## Goals as checkable queries

Write the goal as queries anyone can re-run, for example "open tracker items = 0; CI green on the candidate sha". The judge's `met` reason cites `candidate=<sha> req=<rev>`. A recorded override is allowed only when an external judge ruled; record who, when, and why.

## Hand-off contract

Each agent's final report has these fields: task, branch, head, base, gate results, deviations, shared-file edits, blockers. It also posts one board line: `DONE: <task> head <sha>`. Shared-file edits and file claims are announced before touching them; EL grants or defers a claim and sets integration order (each branch rebases onto the latest target).
