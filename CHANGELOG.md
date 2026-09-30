# Changelog

All notable changes to swarm, newest first. Each release's notes on GitHub are taken from its
section here, so every release adds a `## [x.y.z] - YYYY-MM-DD` section (see `scripts/release-notes.sh`).
Keep entries short and user-facing: one line per change, what it does, not how.

## [0.1.4] - 2026-09-30

### Changed
- The swarm skill now says to do small work yourself, reuse a running job that fits, merge
  similar jobs instead of running duplicates, and create a new job only for substantial
  multi-agent work.

## [0.1.3] - 2026-09-30

### Added
- `swarm move` moves a live agent to another open job without stopping it. Moved agents get a
  notice about the new job and a short catch-up of its board.
- `swarm job merge <from> --into <to>` folds one job into another.
- `swarm job <job> --goal` sets a job's goal after it has started, so a judge can be seated later.

## [0.1.2] - 2026-09-30

### Added
- `swarm watch --compact` colours agent names, shows every open job of the session, and scrolls
  sideways with Left/Right (Home resets).
- `swarm leave --session` marks a session's unfinished agents as left, for agents lost in a restart.
- Jobs no longer stay open forever: a job with no progress for `stall_hours` (default 4) or no
  live agents for `orphan_minutes` (default 30) closes by itself. `swarm wait --for` bounds a wait.

### Fixed
- Hindsight support works with Hindsight 0.8.6 and 0.10.x (no dependence on `GET /profile`).
- Postgres: `swarm resume` and `swarm wait` no longer crash.
- `swarm update` and `install.sh` work with the current Codex CLI (`plugin marketplace upgrade`).

## [0.1.1] - 2026-09-30

### Added
- `engineering-team` skill: a product manager, engineering lead, engineers, reviewers and QA
  running as one swarm job.

## [0.1.0] - 2026-09-29

First public release: agents in Claude Code and Codex coordinate through a shared message board
(file, SQLite or Postgres), with jobs, per-agent names, judges and verdicts, live `watch` and
`tail`, optional transcripts, and optional Hindsight memory.
