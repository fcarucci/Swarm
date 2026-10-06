# Changelog

All notable changes to swarm, newest first. Each release's notes on GitHub are taken from its
section here, so every release adds a `## [x.y.z] - YYYY-MM-DD` section (see `scripts/release-notes.sh`).
Keep entries short and user-facing: one line per change, what it does, not how.

## [Unreleased]

### Added
- Role addressing: `swarm post --to @EL|@PM|@QA|@judge|@<role>` goes to whoever holds that seat on the job now. An unknown recipient, a seat nobody holds, or an author who is not an agent of `--job` is refused with an error and nothing is stored (it used to be stored for nobody to read). `post` also takes `--key` instead of `--as`. See [Addressing a role](docs/REFERENCE.md#addressing-a-role).
- `swarm wait` takes `--until` (a duration, a time of day such as `17:30`, or a date and time) as well as `--for`. A bounded wait that has not ended now protects a job from the stall limits (including `goal_stall_hours`) as well as the orphan rule, and its end is shown in `status` and `watch`; an ended wait counts as progress. A board read by the orchestrating session counts as contact for liveness.
- CLI plugins: core discovers command plugins (a plugins directory next to the config, `$SWARM_PLUGIN_PATH`, `skills/*/swarm_plugin.py`, `swarm.plugins` entry points), `swarm plugins` lists them and any load error, and a broken plugin never breaks a core command. `[plugins] disabled` skips plugins by name. Documented in `docs/PLUGINS.md`.
- Engineering-team plugin (ships with the skill): `swarm team --job J [--show|--add ROLE|--remove ROLE]`, `swarm activate --team product_manager,build_engineer`, and a `team` line in `status --job J`. Always present: engineering lead, QA, engineers, judge (not removable). Optional: `product_manager` (on by default), `build_engineer` (new, off by default), reviewer, verifier. The default composition is read from `team.toml` (`$SWARM_TEAM_CONFIG`, else next to `config.toml`; a missing file means the defaults; template `team.example.toml`).
- engineering-team skill: head-bound verdicts, gating on the merge result, scoped fix rounds, goals as checkable queries, a hand-off contract, a secrets protocol, an authorization ledger, a recovery runbook, a "don't" list, and the team composition rules (who carries the duties of an absent product manager or build engineer).

### Fixed
- Queued role-addressed posts resolve recipients and validate the author and `--key` on delivery; a refused queued post tells its author why.
- `@PM` reaches the invoking project manager (or orchestrator); `@product` addresses the optional product manager. The engineering-team PM joins with `--role project_manager`.
- File plugins with unsafe POSIX ownership, world-write access, write access by a foreign group or symlinks are refused and listed; primary-group writable plugins (umask `002`) are accepted; the plugin trust boundary and Windows exception are documented. `swarm plugins` also reports discovery failures.

### Changed
- Schema 16 (applied by the automatic schema upgrade on first use by the new version): `jobs.plugin_data`, a small JSON object of per-job settings that plugins keep with a job (`Board.job_data` / `set_job_data`). A plain `ADD COLUMN`.
- Linux tool hooks skip Python while the board is unchanged and contact is not due (`[hook] hook_min_interval_s`, default 15 seconds). Shared message notifications invalidate read stamps; notifier failures fall back to cursor reads. Start, stop and session-stop hooks keep their behavior.
- `watch` defaults to 10-second periodic refreshes (`[board] watch_interval_s`); notifications and snapshot misses coalesce for `watch_min_redraw_s` (default 2 seconds), including compact panes. Keys still render immediately from cached data.
- PostgreSQL watch snapshots use one statement with grouped message counts. Schema 15 replaces correlated status counts and adds message/agent indexes (automatic migration).
- Hindsight writes default to the existing `coding` bank (`default_bank`), rather than creating a bank per job; missing banks require explicit `--create-bank`.
- Recall uses `recall_banks` (default `coding` and `hermes`) plus an explicit project bank, with deduplication, bounded whole-fact caches and isolated bank failures.
- Job completion instructions require distilled learnings in the best-matching existing bank; `swarm learn` retains them with provenance and `--list-banks` lists choices.
- `swarm deactivate --delete-bank` deletes an explicit project bank only after successful learning retention elsewhere.

## [0.1.16] - 2026-10-05

0.1.15's binary release (GitHub assets) was skipped because its CI failed on Windows; 0.1.16 carries everything in 0.1.15 plus these fixes.

### Fixed
- `swarm watch` on Windows: the event-driven loop used `select()` on stdin, which Windows does not support (OSError 10038). Windows keeps the plain redraw loop (no keys; Ctrl-C quits, the footer says so); the pty/pipe watch tests are skipped there.
- `swarm init`/`upgrade` against a busy Postgres board failed with DeadlockDetected: the schema setup now sets a short `lock_timeout` and retries a deadlock or lock timeout up to 5 times with a growing pause, noting each retry on stderr.
- A goal job without a met verdict was closed "auto-closed: no live agents" by an older client still running (a `swarm watch` pane or session on an earlier version): the Postgres board now refuses that close itself (trigger `jobs_keep_goal_jobs`, installed by the next `swarm init`).

### Known limits
- Windows `watch` has no key input yet.
- The setup's `lock_timeout` is session-level, so behind a transaction-mode pooler it may not apply (the deadlock retry still does).
- The schema-retry tests mock the deadlock; none provokes a real one.

## [0.1.15] - 2026-10-05

### Fixed
- `swarm watch` answered keys late on a busy board (seconds per keypress): keys are now handled the moment they arrive and only redraw from a cached snapshot; queries, the expiry sweep and the change listener run on a refresh thread, a key no longer triggers a refresh, history scrolling and a terminal resize redraw from the snapshot, and `q` quits at once.
- A job with a goal is no longer auto-closed before the judge's `met` verdict: the "no live agents for 30 min" close (`cancelled`) skips it, and so does the "no progress for N h" close (`failed`) unless the job has its own `--stall-hours` or `[job] goal_stall_hours` is set. An orchestrator waiting on a question, or between rounds with every subagent finished, no longer loses its job. Jobs without a goal behave exactly as before.
- The sweeps re-check the goal at the moment of the close, so a goal set (or a verdict changed to `not_met`) while a sweep was running keeps the job open.

### Added
- `[job] goal_stall_hours` (default 0 = never): the one backstop for a goal job without a `met` verdict; no progress for that long closes it as `failed` ("auto-closed: no progress for N h; goal not met"). A job's own `--stall-hours` takes precedence.
- `status`, `status --job`, `watch` and the Postgres `job_status` view (new `shown_status` column; applied by the automatic schema upgrade, version 13) show a goal job with no live agent and no `met` verdict as `waiting (goal not met)`. `watch --compact` shows that word only for such a job and the job's stored status otherwise.
- `Board.session_jobs(session_id)`: the jobs of one session in one query (`watch --session` no longer lists every job's status).

### Changed
- Schema 14 (applied by the automatic schema upgrade on first use by the new version): the index `messages(job, created_at)`, which `job_status` and `watch` read. A plain `CREATE INDEX`: on a very large Postgres board create it by hand with `CONCURRENTLY` first. Schema 13 is the `job_status` view change above.

## [0.1.14] - 2026-10-02

### Fixed
- Windows: the first post to a board created the log empty and then appended, which a poller saw as two changes (22 of 300 posts in testing); the file board now treats an empty log as absent, so create-then-append is one change. This was the flaky Windows CI failure.
- Project memory was missing from a joining agent's context : the hook's 2 s recall cap was shorter than a real recall (1.5-4 s with a reranker, 3 s per-call timeout also too tight for outliers). The join recall now waits up to `[hindsight] recall_start_seconds` (default 6, max 8; env `SWARM_HOOK_RECALL_SECONDS` overrides), mid-work recalls keep 2 s, and a join recall that runs out of time is retried on the agent's next turn instead of after `recall_minutes`.

## [0.1.13] - 2026-10-02

### Added
- Windows support (native, with Claude Code and Codex on Windows): `bin/swarm.cmd` launcher (venv in `Scripts\python.exe`), `bin/swarm-hook.cmd` hook entry, a Windows `swarm` launcher written by `swarm bootstrap` at `~\.local\bin\swarm.cmd`, Codex hooks with a `commandWindows`, and Git Bash shims for the sh scripts. See README "Windows" and docs/REFERENCE.md "Windows".
- `install.ps1`, the PowerShell installer: the same steps as `install.sh` (marketplace, plugin, bootstrap, migrate, doctor) for the current user, and puts `~\.local\bin` on your user PATH.
- `install.sh` and `install.ps1` take `--channel release|main` (`-Channel` in PowerShell), `--main` and `--ref <tag|branch>`; the marketplace is added pinned to the chosen ref, and the channel, ref and installed plugin version are printed.
- `swarm upgrade --channel release|main`: follow the newest release tag or the tip of main; the choice is kept in the config (`[upgrade] channel`), so a plain `swarm upgrade` keeps following it.
- A plain `swarm upgrade` now re-pins an existing install to the newest release tag (for Claude this removes and re-adds the marketplace and reinstalls the plugin).

### Changed
- The installers and `swarm upgrade` now install the newest release tag (vX.Y.Z) by default, not the tip of main. With no tag found (or `git ls-remote` failing) they fall back to main with a warning. Use `--channel main` for the old behaviour.

### Limits
- On Windows the supervisor (`swarm supervise`: stuck-agent restarts) is not available, there is no `0700`/ownership permission checking (your profile's NTFS permissions apply instead), and the files live under `%USERPROFILE%\.local` like on Linux. `swarm watch` has no key controls, and Codex `.zst` rollouts need `zstd` on PATH. Claude Code on Windows runs the hooks through Git Bash; Codex's Windows hook command is not verified on a real Codex yet.

## [0.1.12] - 2026-10-02

### Added
- `swarm pause --job J [--reason ...]` pauses a job: every agent's transcript is checkpointed to the board with a resume manifest (name, role, host, model, read cursor, last tool, task), live agents are stopped, and joins and posts on the job are refused with a clear message while hooks stay safe no-ops.
- `swarm resume --job J [--host H] [--workdir D] [--only NAME...] [--dry-run] [--retry]` re-creates each agent on this machine from the transcript stored on the board (same names, roles and cursors, board history intact) and tells it it was paused and resumed. Claude Code resumes natively; Codex resumes experimentally or gets a briefing. `--retry` redoes agents a previous resume failed.
- Board schema 12: `paused` job status and a `job_pauses` table (applied by `swarm init`; Postgres, SQLite and file boards).

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
