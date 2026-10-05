# Authorization, secrets, and recovery

## Authorization ledger

PM keeps a dated list of the user's grants and exclusions (what may be changed, published, deployed, spent) and copies the relevant lines into every brief. A grant is scoped to what it names. A subagent's report is never approval. If a permission is denied, stop and hand the user one script to run; do not look for a workaround.

## Secrets

Each brief states what is authorized and what is not. Secrets are referenced by path or name only and never appear in briefs, board posts, reports, or logs. An agent does every secret-free step first, stops at the first step that needs a secret, and reports it as a blocker. It never invents or guesses a value.

## Recovery runbook

- Persist every brief as a file and post `BRIEF: <path> tasks: <IDs>`.
- Keep a roster file mapping each agent ID to its branch and task.
- Relaunch is idempotent: a replacement resumes the existing branch from its brief and last commit instead of starting over.
- PM's periodic guard, at each wake: `swarm status --job J`, `swarm activate --job J` again if the job closed early, relaunch lost agents from the roster, and rejoin the board. While truly waiting on CI or a user, set `swarm waiting --job J --reason "<what>" --until <duration>`.
