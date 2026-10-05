# Changelog

All notable changes to swarm, newest first. Each release's notes on GitHub are taken from its
section here, so every release adds a `## [x.y.z] - YYYY-MM-DD` section (see `scripts/release-notes.sh`).
Keep entries short and user-facing: one line per change, what it does, not how.

## [0.1.18] - 2026-10-05

### Added
- Role addressing: `swarm post --to @EL|@PM|@QA|@judge|@<role>` goes to whoever holds that seat on the job now. An unknown recipient, a seat nobody holds, or an author who is not an agent of `--job` is refused with an error and nothing is stored (it used to be stored for nobody to read). `post` also takes `--key` instead of `--as`. See [Addressing a role](docs/REFERENCE.md#addressing-a-role).
- `swarm wait` takes `--until` (a duration, a time of day such as `17:30`, or a date and time) as well as `--for`. A bounded wait that has not ended now protects a job from the stall limits (including `goal_stall_hours`) as well as the orphan rule, and its end is shown in `status` and `watch`; an ended wait counts as progress. A board read by the orchestrating session counts as contact for liveness.
- CLI plugins: core discovers command plugins (a plugins directory next to the config, `$SWARM_PLUGIN_PATH`, `skills/*/swarm_plugin.py`, `swarm.plugins` entry points), `swarm plugins` lists them and any load error, and a broken plugin never breaks a core command. `[plugins] disabled` skips plugins by name. Documented in `docs/PLUGINS.md`.
- Engineering-team plugin (ships with the skill): `swarm team --job J [--show|--add ROLE|--remove ROLE]`, `swarm activate --team product_manager,build_engineer`, and a `team` line in `status --job J`. Always present: engineering lead, QA, engineers, judge (not removable). Optional: `product_manager` (on by default), `build_engineer` (new, off by default), reviewer, verifier. The default composition is read from `team.toml` (`$SWARM_TEAM_CONFIG`, else next to `config.toml`; a missing file means the defaults; template `team.example.toml`).
- engineering-team skill: head-bound verdicts, gating on the merge result, scoped fix rounds, goals as checkable queries, a hand-off contract, a secrets protocol, an authorization ledger, a recovery runbook, a "don't" list, and the team composition rules (who carries the duties of an absent product manager or build engineer).

### Changed
- Schema 16 (applied by the automatic schema upgrade on first use by the new version): `jobs.plugin_data`, a small JSON object of per-job settings that plugins keep with a job (`Board.job_data` / `set_job_data`). A plain `ADD COLUMN`.

## [0.1.17] - 2026-10-05

### Added
- The board message length is configurable: one cap per board, stored in the board so every client and host agrees (`[board] message_max_chars` only seeds a new board). Read it with `swarm config board.message_max_chars`, change it online with `swarm config board.message_max_chars 500` (50 to 4000; `--save` also writes the config file). Lowering it never cuts or refuses messages already stored; it applies to new posts. The start hook shows the live cap.

### Changed
- Schema 15: the cap moves from the `varchar(N)` column / table CHECK into the board (Postgres: `text` column, a replaceable `NOT VALID` check and a `board_meta` row, converted online in one quick, retried `ALTER`; SQLite: a trigger and a `board_meta` table). Boards keep the cap they enforced and every message.

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
