<!-- Draft of the Unreleased "Drive mode" entries for CHANGELOG.md (Task 14). Merge by hand: "Breaking changes"
     goes under "### Changed" (keep the BREAKING prefix), "Added" under "### Added". Not part of CHANGELOG.md itself. -->

## [Unreleased]

### Breaking changes
- BREAKING: a goal implies drive. `swarm activate --goal` now hands the job to the supervisor (lead, seats, review, judge, landing); `--no-drive --reason TEXT` keeps the old coordinator-run job, and the activating session becomes the requester/observer.
- BREAKING: `--default` is required on `swarm ask` in a drive job; the driver applies it when the question expires (`question_expiry`, 4h), so no question blocks the job.
- BREAKING: a requester's Agent-tool spawns are refused in drive jobs; staffing goes through `swarm seat request` (the lead or a person), and the engineering-team and swarm skills no longer tell the requester to spawn agents.
- BREAKING: `swarm activate --goal` fails until `swarm supervise enable` has been run on the host (drive mode is refused on Windows and macOS).

### Added
- Drive mode: a goal job ends MET, UNREACHABLE (a judge verdict with reasons, or the labelled mechanical one when a budget, deadline or reserve runs out) or CANCELLED, and is never left idle waiting for a coordinator; no new always-on process, only the timer pass, its runners and the opt-in events listener.
- `swarm seat request|list|show|release|cancel`, `swarm review`, `swarm goal-ready`, `swarm halt` / `unhalt`, `swarm land` and `swarm drive budget|models`: the drive control commands; each ends with `swarm-record: kind=<kind> id=<n> rev=<n>` and never spools in a drive job.
- `[drive]` in `config.toml`: every budget is a configurable default, not a limit (`max_cost_usd` 40 USD and `ruling_reserve_usd` 5 USD shipped, `max_sessions`, `max_seats`, `max_rounds`, `no_progress_rounds`, `max_attempts`, `session_usd_cap`, `min_session_usd`, `max_session_minutes`, `max_wall_hours`, ruling and question timings, `stall_minutes`, `max_concurrent_sessions`, `codex_usd_per_mtok`); override per job with `--max-cost-usd`, `--ruling-reserve-usd`, `--budget KEY=VALUE`, or on a running job with `swarm drive budget`.
- `[drive.models]` and `--model ROLE=MODEL`: the model per drive role, shipped as haiku for engineers, workers and integrators, sonnet for reviewers and QA, opus for the judge, lead and security reviewer (`allowed_models` limits the choice); reviewers run strict gates over haiku-written changes.
- Landing through `swarm land`: rebase only (or `[land] strategy = squash-ff`), fast-forward pushes only, CI green on the exact rebased SHA, in supervisor-private mirrors with credentials pinned at activation (`--credential DEST=SOURCE`: an absolute helper path, an ssh key or an agent socket; `known_hosts` seeded); doctor checks each destination with a dry-run `git ls-remote` and needs git 2.40 or newer.
- A close-time audit of the target's history; a flagged audit (`DRIVER-AUDIT`) blocks the close and makes a ruling due, and the docs explain what it flags and how to resolve it.
- Windows UNC worktree roots are refused in drive mode (fail safe).
- Docs: a "Drive mode" section in `docs/REFERENCE.md`, `[drive]` in `config.example.toml`, a rewritten engineering-team skill (requester/observer, EL brief template, PM procedure for non-drive jobs only) and a drive section in the swarm skill; a doc test keeps every `[drive]` default documented.
