# PM event procedure

Events reach the PM through `swarm event wait --job J --to @pm`; they are never read from the
board by a model. An event carries `kind`, `key` (`kind:N@sha`) and a one-line text. Always
`swarm event ack --job J ID` once it is handled, then re-arm the wait. An event naming a sha that
is no longer the PR's head is stale: ack it and do nothing.

## NEEDS-REVIEW N@sha (and BRANCH-READY branch@sha)

1. Check the head is still `sha` (`gh pr view N` or the Gitea API). If not, ack and stop.
2. Dispatch an independent reviewer (never the author) from the review template: PR number, the
   FULL head sha, the base, the requirement or issue link, what to run, and the exact verdict line
   to post on the PR and on the board: `REVIEW #N @ <full-sha>: VERIFIED|CHANGES REQUIRED <why>`.
3. Record the dispatch with a board post `REVIEW-DISPATCHED N@sha <reviewer>`; the key is the dedupe,
   there is no separate file to keep. For BRANCH-READY with no PR yet, open the PR first.

## REVIEW-CHANGES N@sha

Send the findings (the verdict text on the PR) to the coder. The fix is a new head: a new
NEEDS-REVIEW follows, and the old verdict is stale.

## CI-FAILED N@sha <contexts>

Read the failing contexts' logs. Diagnose: a real failure, a flake (rerun once), or an
infrastructure fault. Send the cause and the failing contexts back to the coder; never fix it as PM.

## READY-TO-LAND N@sha

The event means a VERIFIED verdict and green CI agree on this exact head. Re-check both, then land
per the project's `[land] strategy` (team.toml):

- `rebase-ff` (default): rebase the branch onto the target locally and fast-forward the target to
  it. No squash, no merge commit. If rebasing changes the sha, the old verdict and CI do not cover it:
  push, get a new verdict and green CI on the new sha.
- `squash-ff`: squash the branch into one commit on the latest target locally, then fast-forward.

Never force-push the target branch. After landing: verify `git rev-parse <landed>^{tree}` equals
`git rev-parse <reviewed-head>^{tree}`; if they differ, stop and report. Then close the PR and the
issue it fixes, delete the branch, and ack the event.
