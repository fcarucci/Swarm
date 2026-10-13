# Authorization, secrets, and recovery

## Drive jobs: the driver recovers

In a drive job the supervisor driver is the recovery loop; no coordinator keeps a roster or relaunches anything.

- A lost or stuck seat is relaunched from its brief and the board history under the same seat name, within `max_attempts` and the budget; a lost lead is relaunched the same way and is woken by watermark, so no event is lost.
- A job with no responsible actor for two stall periods (2x `[supervise] timer_minutes`) raises `DRIVER-STALLED`; a bad ledger holds only that job (`DRIVER-LEDGER-BAD`), a missing recipe field `DRIVER-CONFIG`, a failed close audit `DRIVER-AUDIT`.
- A person stops a job with `swarm halt --job J [--reason TEXT]` and resumes it with `swarm unhalt --job J` (its deadlines shift by the halted time); the kill switch file `supervise.off` stops every driver on the host. `swarm deactivate` cancels (CANCELLED).
- Budgets change on a running job with `swarm drive budget --job J KEY=VALUE`; no session is stopped. A new goal or re-activation is a rollover that stops every session.
- Read state with `swarm status --job J`, `swarm seat list --job J` and `swarm doctor`; do not edit ledger files. A drive job must reach MET, UNREACHABLE or CANCELLED; if it looks stuck, `swarm halt` it and report the evidence.

## Authorization ledger

The lead brief carries the user's grants and exclusions (what may be changed, published, deployed, spent) and every seat brief copies the relevant lines. The requester keeps the dated list. A grant is scoped to what it names. A subagent's report is never approval. If a permission is denied, stop and hand the user one script to run; do not look for a workaround.

## Secrets

Each brief states what is authorized and what is not. Secrets are referenced by path or name only and never appear in briefs, board posts, reports, or logs. An agent does every secret-free step first, stops at the first step that needs a secret, and reports it as a blocker. It never invents or guesses a value. Landing credentials are pinned at activation (`--credential DEST=SOURCE`), by absolute helper path, key path or socket, never as a value.

## Non-drive jobs: recovery runbook

- Persist every brief as a file and post `BRIEF: <path> tasks: <IDs>`.
- Keep a roster file mapping each agent ID to its branch and task.
- Relaunch is idempotent: a replacement resumes the existing branch from its brief and last commit instead of starting over.
- PM's periodic guard, at each wake: `swarm status --job J`, `swarm activate --job J` again if the job closed early, relaunch lost agents from the roster, and rejoin the board. While truly waiting on CI or a user, set `swarm wait --job J --on "<what>" --for <duration>` (bounded: only a bounded wait protects the job from auto-close).
