# Changelog

All notable changes to swarm, newest first. Each release's notes on GitHub are taken from its
section here, so every release adds a `## [x.y.z] - YYYY-MM-DD` section (see `scripts/release-notes.sh`).
Keep entries short and user-facing: one line per change, what it does, not how.

## [0.1.12] - 2026-10-02

### Added
- `swarm pause --job J [--reason ...]` pauses a job: every agent's transcript is checkpointed to the board with a resume manifest (name, role, host, model, read cursor, last tool, task), live agents are stopped, and joins and posts on the job are refused with a clear message while hooks stay safe no-ops.
- `swarm resume --job J [--host H] [--workdir D] [--only NAME...] [--dry-run] [--retry]` re-creates each agent on this machine from the transcript stored on the board (same names, roles and cursors, board history intact) and tells it it was paused and resumed. Claude Code resumes natively; Codex resumes experimentally or gets a briefing. `--retry` redoes agents a previous resume failed.
- Board schema 12: `paused` job status and a `job_pauses` table (run `swarm migrate`; Postgres, SQLite and file boards).

### Limits
- No cross-harness resume (a Claude transcript on Codex or the reverse gets a briefing instead); subagent trees are not rebuilt (they return as independent sessions); redacted secrets stay redacted; files and git state are not part of the pause.

## [0.1.11] - 2026-10-01

### Changed
- `swarm upgrade` always runs `migrate --force`: active jobs on this machine are listed as a warning and no longer block the upgrade.

## [0.1.10] - 2026-10-01

### Fixed
- The `~/.local/bin/swarm` launcher now falls back to the newest installed swarm plugin (Claude or Codex)
  when the plugin folder it points at is gone, instead of failing every command until bootstrap reruns.
- `swarm update` is now `swarm upgrade`; `update` still works as an alias.

## [0.1.9] - 2026-10-01

### Fixed
- `swarm update` no longer fails with `No module named 'swarm.cli'` after the host replaced the plugin
  version it was running from.

## [0.1.8] - 2026-10-01

### Fixed
- A job marked `swarm wait` no longer has the orchestrator told to spawn the next round after a not met
  verdict; `swarm resume` (or an agent joining) turns the reminder back on.

## [0.1.7] - 2026-09-30

### Fixed
- `swarm update` recovers when Codex's swarm marketplace points at a deleted local folder, or is no longer
  registered: it adds the public source instead of failing.

## [0.1.6] - 2026-09-30

### Fixed
- Commands that write fail with one clear line (exit 1), not a traceback, when no primary is reachable.
- A dead host in a Postgres host list costs at most a few seconds before the next is tried.

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
