# Changelog

All notable changes to swarm, newest first. Each release's notes on GitHub are taken from its
section here, so every release adds a `## [x.y.z] - YYYY-MM-DD` section (see `scripts/release-notes.sh`).
Keep entries short and user-facing: one line per change, what it does, not how.

## [0.1.5] - 2026-09-30

### Added
- The Postgres `host` can be a list (a cluster): writes follow the primary across a switchover.
- `status`, `who`, `read --peek`, `doctor`, `watch` and `tail` keep working from a standby when no
  primary is up, and say so; `watch` and `tail` poll where `LISTEN` is unavailable.
- `watch` and `tail` reconnect with backoff instead of exiting.

## [0.1.4] - 2026-09-30

### Changed
- The swarm skill now favours doing small work yourself and reusing or merging jobs; new jobs are
  for substantial multi-agent work.

## [0.1.3] - 2026-09-30

### Added
- `swarm move` moves a live agent to another open job without stopping it.
- `swarm job merge <from> --into <to>` folds one job into another.
- `swarm job <job> --goal` sets a job's goal after it has started.

## [0.1.2] - 2026-09-30

### Added
- `swarm watch --compact`: coloured agent names, all open jobs, sideways scrolling.
- `swarm leave --session` marks a lost session's unfinished agents as left.
- Idle jobs close themselves: after `stall_hours` (default 4) without progress, or
  `orphan_minutes` (default 30) with no live agents. `swarm wait --for` bounds a wait.

### Fixed
- Hindsight works with 0.8.6 and 0.10.x.
- Postgres: `swarm resume` and `swarm wait` no longer crash.
- `swarm update` and `install.sh` work with the current Codex CLI.

## [0.1.1] - 2026-09-29

### Added
- `engineering-team` skill: a product manager, lead, engineers, reviewers and QA as one swarm job.

## [0.1.0] - 2026-09-29

First public release: agents in Claude Code and Codex coordinate through a shared message board
(file, SQLite or Postgres), with jobs, judges and verdicts, live `watch` and `tail`, optional
transcripts, and optional Hindsight memory.
