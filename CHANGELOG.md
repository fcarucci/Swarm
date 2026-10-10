# Changelog

All notable changes to swarm, newest first. Each release's notes on GitHub are taken from its
section here, so every release adds a `## [x.y.z] - YYYY-MM-DD` section (see `scripts/release-notes.sh`).
Keep entries short and user-facing: one line per change, what it does, not how.

## [Unreleased]

### Changed
- Setup is opt-in on a new machine. Installing the plugin no longer starts a detached `swarm bootstrap` at the first session: SessionStart prints `[swarm] not set up: run swarm init` and does nothing else. `swarm init` is the explicit setup (config, launcher, board; `--host claude|codex`) and prints a summary. By default it no longer edits `~/.claude/settings.json` (`sandbox.filesystem.allowWrite`) or `~/.codex/config.toml` (`writable_roots`, `[agents] max_depth`): it prints the exact lines, and only `swarm init --apply-settings` edits them (backup kept).
- The choice is stored: `~/.local/share/swarm/host/settings-consent` holds `yes` or `no`. `swarm init --apply-settings` (and the installers, through `SWARM_APPLY_SETTINGS=1`) store `yes`; a plain `swarm init` on a new machine stores `no`. The detached bootstrap, `swarm bootstrap` and `swarm upgrade` follow the stored answer (`swarm bootstrap --apply-settings` / `--no-apply-settings` override it for one run), so the config and launcher that `swarm init` creates never turn into consent.
- Existing installs keep working with no action: a machine with `~/.config/swarm/config.toml`, the `~/.local/bin/swarm` launcher, or a bootstrap stamp of any version and no stored answer counts as `yes` (stored on first use), so the detached bootstrap at a plugin version change, `swarm upgrade` and `swarm init` keep their settings upkeep. Migration for anyone who wants today's behaviour on a new machine: `swarm init --apply-settings` (or `swarm bootstrap --host claude|codex --apply-settings`); to opt an existing install out, write `no` into the consent file. A new machine's SessionStart notice writes nothing (no host directory or cache) until `swarm init` runs.

### Added
- `swarm verdict --details -|PATH`: a judge records its full Markdown report (evidence, numbered defects, fix list; at most 64 KiB) with a met or not_met verdict, bound to the same `--artifact`, instead of writing a `verdict.md` that judges are not allowed to create. `swarm verdict show --job J [--artifact REF]` prints it, `status --job J` shows `details: N lines`, and the fix worker's brief points to it. Needs schema 23 (nullable `jobs.verdict_details`; the board upgrades itself on first use).
- Background commands of agents are tracked (schema 24, the `bg_commands` table; the board upgrades itself on first use). A swarm member's background Bash call in Claude Code now runs under `swarm bg`, which records it on the board with its process group, start time and host, and records its exit code. The command keeps its output, exit code and environment, but it now runs in a fresh `bash -c`, without Claude Code's shell snapshot: the agent shell's aliases and functions are not available to it. Codex can't be wrapped: its long-running commands are not tracked (documented).
- `swarm bg list [--job J] [--orphans] [--all]` and `swarm bg reap [--job J] [--agent KEY] [--dry-run]`. A background command still running after its agent finished or its job closed is an orphan. Reap stops it with SIGTERM, then SIGKILL after 10 s. It only signals processes it can prove are the recorded command's (this host, boot and pid namespace, start time, tag), so a reused pid or another host's process is never touched. The supervisor pass, `deactivate`, auto-close and an agent's SubagentStop (after 30 s) reap by themselves.
- `swarm status --job` and `swarm watch` show `background N running, M orphaned`; `swarm doctor` warns about this host's orphaned background commands.

### Fixed
- `swarm ci wait` and `swarm ci status` no longer return a cached failure for a commit that was re-run: a failure is re-checked with the CI host (within the 60 s floor) and a newer run attempt, or a run still queued or in progress, reads as running until its result is in. A cached green stays final.
- CI's Postgres service job pulls `postgres:16` from `public.ecr.aws/docker/library` instead of Docker Hub, which rate-limited anonymous pulls.
- A subagent resumed through SendMessage keeps its name and role. A subagent that was already running when its job was activated (so its hooks did not adopt it) and runs `swarm join --key X` now joins under its own agent id, as hook-enrolled members already did; under a made-up key, a resumed agent found no row of its own and was named afresh.
- A subagent's `swarm join` in a command that also joins another job of its session rewrites only its own job's join; the other join is left unchanged.
- A subagent that runs `swarm join --key orchestrator` is refused: only the main session is the orchestrator.
- A subagent's `swarm join` with any key starting with `orchestrator` (`orchestrator-2`, `orchestrator_x`) is refused too; a supervisor replacement that restarts the coordinator may still join as the orchestrator.
- Pool names that read like a seat (`Judge Constance Harm`, `Judge Snyder`, ...) are no longer given to agents: a worker named "Judge ..." looked like the job's judge to itself and to everyone else.
- A role that is not an `@role` token (a Claude agent type such as `general-purpose`) no longer makes every tool call log `invalid event target` in hook-errors.log.
- `swarm upgrade` on the main channel compares the installed commit with the tip of main and reinstalls when they differ, even when the version string is unchanged (it used to print `ok 0.2.3 -> 0.2.3` and install nothing without `--force`). Where the host records no commit, the upgrade records the one it installed. The release channel keeps version semantics.
- Bootstrap's bytecode prune runs only on a cache directory that passes the launchers' check (this user's, no group or other access, no symlink, no ACL) and, resolved, lies strictly inside the home directory (the home directory and its ancestors are refused by inode, whatever the spelling: `//home/u`, `/proc/self/root/...`, a symlinked parent), and deletes only interpreter cache files (`*.cpython-NNN.pyc`) whose source is gone and directories it emptied. A `SWARM_PYCACHE` pointed at `$HOME` used to lose orphan `.pyc` files and every empty directory there.
- CI-GREEN and READY-TO-LAND no longer fire while some of a commit's CI is still running. One workflow (or check suite) finishing green used to be enough when a commit had several, and `ci wait`/`ci status` then reported green early. A completion webhook is now only a trigger: the whole commit is confirmed with one call through the shared `swarm ci` poller before either event is posted. A commit still running is confirmed later by the source's poll. CI-FAILED is still immediate.
- Local event routes (the forwarder's `/github-forward`) refuse more:
  - a client that sent and closed at once (its TIME_WAIT/FIN_WAIT row carries uid 0, which a listener running as root read as its own; only an ESTABLISHED socket with a live inode counts now, checked before the body is read);
  - any request with proxy headers (`X-Forwarded-*`, `Forwarded`, `Via`, `X-Real-IP`, ...);
  - any request without the random token each listener start adds to the route path (kept in the supervisor's private dir, read by the ci plugin for the forwarder's URL).

  Never put a reverse proxy or tunnel in front of local routes; the docs no longer suggest a proxy at `[events] bind`.
- The CI-wait check refuses `$(gh run watch ...)` or backticks in an unquoted here-document body, which bash runs. Quoted bodies stay allowed.
- The webhook secret no longer appears on any command line. `gh webhook forward` (gh-webhook v0.2.0 takes the secret only as `--secret VALUE`, which every OS user can read in /proc) now runs without one and delivers to a new local route, `/github-forward`. The listener serves that route only to a loopback peer owned by its own OS user, checked in the kernel's socket table (Linux; refused elsewhere). Event sources can register such `local_routes`. Rotate a secret that was used with an earlier version.
- `swarm ci status` agrees with `swarm ci wait`: a CI event that ends a wait (or that `status` itself finds) is written to the shared CI cache, so `status` no longer reports a stale "running" for a SHA that is green.
- `swarm event ack` and `swarm blocker` without `--as`, run from a session that orchestrates a job, are recorded as `orchestrator`. They were recorded under whichever subagent of that session the board listed first, because subagents share the session id.
- The events listener's helper (`gh webhook forward`) re-reads its source's config and credentials at every restart. The restart wait stays capped at 5 min, and the `forwarder-down` alert says when the next try is. A fixed credential was already retried within 5 min, but nothing said so, so the listener looked stuck.
- The events listener and supervisor runners that `swarm supervise` starts no longer run `python -B`. They use the same bytecode cache as `bin/swarm`, or write no bytecode, so they no longer recompile everything on each start.
- Agents no longer wait for CI with `gh run watch` or a polling loop. The hooks refuse a member's `gh run watch`, `gh pr checks --watch` and `gh`/curl CI poll loops, and give the exact `swarm ci wait --repo R --sha S` to run instead (repo and head read from the work dir), or say that the ci plugin isn't installed. A one-shot `gh run view <id> --json conclusion` is still allowed. The engineering-team and swarm skills tell orchestrators to never put `gh run watch` in a brief and to run `swarm ci wait` in the foreground.
- The judge instructions and the judge gate's refusal now tell a judge to put its report in `--details` (a heredoc body is no longer mistaken for a file write by the shell guard), so a verdict's `--reason` no longer points at a file that was never written.

## [0.2.3] - 2026-10-08

### Changed
- Faster commands: the launchers (`bin/swarm`, `bin/swarm-hook`) now keep Python bytecode in `~/.local/share/swarm/pyc` (a private directory of yours; `SWARM_PYCACHE` overrides it; if it is a symlink, shared or has an ACL nothing is cached or written) instead of recompiling about 1 MB of source on every run, which took 0.2 s of each command and of each hook that reaches Python. `swarm who` drops from about 0.45 s to 0.17 s. Bootstrap removes cached bytecode of plugin versions and modules that are gone.
- `swarm status` no longer takes 2-5 s on a large board. The auto-close sweep it runs read every message of a job up to six times to look for hand-offs, wrote the transcript retry state once per missing transcript (an fsync each) and read each idle agent's last post in its own query; hand-offs now come from one filtered query, the retry state is written once per sweep, last posts are fetched in one query and expiring blockers in one query for the whole board. About 3.2 s to 0.7 s on the live board.
- Schema 22 (applied automatically on first use, on every backend): `agents.message_count`, the messages an agent posted to its job since it joined. The `agent_status` view and the watch snapshot read it instead of counting messages per agent on every read (on Postgres and SQLite triggers keep it exact whichever client posts, so hosts need not upgrade in lockstep; the file and memory stores count it at post time). `agent_status` for a 713-agent job goes from 26 ms to 1 ms and the watch counts join no longer scans messages.
- Postgres: the watch snapshot reads each idle agent's last post with an index lookup per agent instead of scanning every message of those agents (32 ms to 3 ms).
- Postgres: `messages` and `agents` vacuum and analyze after 2% to 5% of their rows change instead of 20%, so the visibility map stays fresh and index-only scans stop fetching heap pages (set by schema setup, idempotent).

## [0.2.2] - 2026-10-08

### Added
- Agent titles: an optional short label per agent (at most 60 characters, such as `EL`, `PM`, `Eng: board view`), separate from the role and display only. Set it with a `[swarm title: ...]` line in a subagent's prompt, `swarm join --title` or `swarm title --job J (--as NAME | --key K) "text"` (empty text clears it); `who`, `status` and `watch` show it (a TITLE field in `who`, a TITLE column in the agents table when any agent has one, `Name (title)` in compact `watch`), board messages do not. A supervisor replacement or resumed agent keeps its title. Codex spawn messages are encrypted, so a Codex agent uses `swarm title`.
- The engineering-team plugin tags every seat with its title (`EL`, `PM`, `Product`, `QA`, `Judge`, `Build`, `Reviewer`, `Verifier`, `Eng`, optionally `Eng: scope`), including the review pipeline's judge, fix worker and integrator; `swarm team --show` lists them and the skill tells the EL to keep them current.
- External events (schema 21): `swarm event post|list|ack|wait` record "something happened outside the board" for the orchestrator, a role or an agent. Posts are idempotent per job, kind and key; `event wait` blocks until one is pending (Postgres `LISTEN`, a one-second poll elsewhere; exit 124 on timeout). Pending events show in the hook context, a compact line each, until acked. Python API: `Board.post_event`, `events`, `pending_events`, `ack_events`, `wait_event`.
- `swarm events serve`: an HTTP listener for external event sources that plugins register with `api.register_event_source` (routes, `verify` before parse, `handle`, optional `poll`). Signature checked on the raw body, body size capped, nothing of a request logged. Off by default (`[events] enabled`); `swarm supervise` keeps it running.
- Event sources can declare helper processes (e.g. `gh webhook forward`): the listener keeps them running with backoff and a `forwarder-down` alert; source polls have a 600 s floor (`[events] min_poll_interval_s`) unless the source's config lowers it.
- Safety-net checks in the supervisor pass and `swarm events check`: listener up, orchestrator `event wait` armed, job stalled, events or messages for the orchestrator unacked too long. One `SWARM-ALERT` per job; an optional Haiku summary uses `[events] model`, never the orchestrator's.
- CI event sources (`ci` plugin, no core change; configured under `[ci.github]` / `[ci.gitea]` in `config.toml`, a `CI-GREEN` event per green commit): `gitea` and `github` register through the event-source interface and raise `NEEDS-REVIEW`, `REVIEW-CHANGES`, `CI-FAILED` and `READY-TO-LAND` (verdict and CI success must agree on the exact current head sha; key `kind:N@sha`); `BRANCH READY` board posts become `BRANCH-READY` events. The GitHub source is callback-first: it works from pushed `workflow_run`, `check_suite`, `pull_request` and review webhooks with no API call (delivered by a supervised `gh webhook forward` when `[ci.github] forward = true`); API polling is a fallback no faster than every 600 s. Gitea CI is polled from the commit status. The PM's per-event procedure is in the skill. `[land] strategy = "rebase-ff" | "squash-ff"` in `team.toml` (default `rebase-ff`) sets how a PR lands.
- `swarm ci status|wait --repo OWNER/REPO --sha SHA` (new `ci` plugin, skills/ci): the CI state of an exact SHA that every agent on a box shares, so agents stop running `gh run watch` and exhausting the GitHub API budget. One cached poller per box, at most one CI host call per repo per 60 s (backoff 60/120/300 s on queued runs, 10 min under 500 calls left, public-API fallback on a 403), and a CI event for the SHA ends the wait without polling. `wait` exits 0 green, 1 failed (failing jobs and log tail), 124 on timeout. GitHub and Gitea (`[ci] kind`). The engineering-team skill, its briefs and `swarm team --show` tell agents to use it.
- New `ci` plugin (`skills/ci`): everything that talks to a CI or repo host. `swarm ci status|wait` and the shared poller live there; core Swarm and engineering-team do not.
- Model policy per role in `team.toml`: `[models.claude]` / `[models.codex]` map `watcher`, `reviewer`, `engineer`, `qa`, `judge`, `engineering_lead`, `product_manager` to a model, and spawns pick it from config (config.toml's `[models]` wins; a role left out gets the session default).

### Changed
- Schema 20 (applied automatically on first use, on every backend): a nullable `agents.title` and a last `title` column of the `agent_status` view. Boards stay readable by the previous release, which shows no titles; as always, upgrade every host sharing a board together. `swarm who` gains a TITLE field after the role (empty without one).
- engineering-team is now process only (PM, reviewer and integrator steps per CI event, `[land] strategy`, models per role, seat titles). It waits with `swarm ci wait`, reacts to the `ci` skill's events (including `CI-GREEN`), and makes no CI host call of its own; the integrator's evidence check uses `swarm ci wait`. A test fails if the retired word returns outside the rename line.
- The old `[forge]` name is gone, with no alias: the `team.toml` section is `[ci]` (`[repositories."p".ci]` for overrides), the `swarm ci --kind` option replaces the old host flag, and event-source state files, modules and docs say CI host. `swarm upgrade` and `swarm init` rewrite an existing `[forge]` section to `[ci]` once, keeping `team.toml.bak` next to it and saying so.
- CI: tag and release-branch pushes no longer re-run the full test matrix (the Release workflow tests the tag).

### Fixed
- The launchers (`bin/swarm`, `bin/swarm.cmd` via `lib/swarm/winlaunch.py`) reinstall requirements when `psycopg` or `zstandard` is missing from the venv, even when the requirements stamp matches. Before, swarm crashed on import until the venv was deleted by hand.
- Judges can record a verdict again with no `join` and no `--as`. A judge spawned with `[swarm title: Judge]` was seated as a plain worker, and the shell fast path then skipped the hook that rewrites `swarm join --key` to the caller's own identity, so `join --judge` made a second identity that `--as` could not (rightly) use. Now `[swarm title: Judge]` seats the judge like `[swarm role: judge]` (a live judge is never displaced; a finished or dead one is replaced), `swarm join`/`swarm verdict` calls always reach the hook, and `swarm verdict` with no `--as` runs as the caller. `swarm join` without `--key` says so and points judges at `swarm verdict --job J met ...`.
- `swarm watch` no longer crashes with `AttributeError: 'SnapshotBoard' object has no attribute 'last_post'` (0.2.1, PostgreSQL boards): the watch snapshot now carries each idle or dead agent's last post, fetched in the same single statement.
- `swarm upgrade --force` reinstalls the plugin from the tip of main (or the current release) even when the version is unchanged, and reports the installed commit before and after; without it, a main-channel install that is behind the tip at the same version says so instead of "up to date".

## [0.2.1] - 2026-10-08

### Added
- Stale waiting jobs: a job waiting for any reason (a goal's verdict included) with no live agent and no activity for `[job] stale_waiting_minutes` (120) gets one board notice, and at `[job] stale_waiting_close_minutes` (240) is auto-closed (`completed` with a met verdict, else `failed`).

### Fixed
- The board view no longer misleads. `who`, `status` and `watch` show an agent that posted DONE/VERIFIED/FAILED, recorded a met verdict, or went quiet before the goal was met as `finished` (not `dead` or `idle`); `dead` is kept for agents silent while they still owed work. An idle agent shows `waiting` (and on what) when the job has a wait or blocker or its last post says it waits. A CLI member joined as `orchestrator` (the role defaults from a key named `orchestrator...`) shows as `standby`/`away` and is not counted as a worker. `status --job` and `watch` add an `agents` line (N working, N waiting, N finished, N lost), the job list a WORKERS column.
- One agent, one row: when a hook-registered Claude subagent runs `swarm join --key X`, its hook rewrites the call to use the agent's own key, so it no longer appears twice (an idle ghost beside the running agent).
- `swarm deactivate` no longer asks for learnings again after `swarm learn --job J` recorded them. A pipeline job whose accepted artifacts await finalization still refuses until FINALIZED/INTEGRATED is posted; the refusal says so. A stale waiting job with a met verdict but unfinalized accepted work closes `failed`.
- A new judge takes the judge seat itself when the previous judge has completed or died (a fix round after `not_met`), through the hook, `join --judge` and the pipeline, so it records `swarm verdict` under its own name; a live judge is never displaced and `--as` another agent is still refused.
- `swarm verdict` no longer asks for `swarm learn`; the learnings reminder comes when the job closes (`deactivate`).

### Changed
- The skill text says a judge run outside the board must record `swarm verdict`, and that the job is closed as soon as the goal is met.

## [0.2.0] - 2026-10-07

### Added
- The `engineering-team` skill and plugin: a product manager, engineering lead, engineers, QA and judge on one job, with optional build engineer, reviewer and verifier. `swarm team --job J [--show|--add ROLE|--remove ROLE]` and `swarm activate --team ...` set the team; defaults come from `team.toml`.
- The `ask-answer` plugin: structured questions to the human, a role or an agent, with answers, comments and corrections, a questions pane in `watch`, and pending human questions in the orchestrator's context. Optional `[notify] on_question` command.
- Automatic review pipeline: a worker's hand-off starts a judge, `not_met` starts a bounded fix round, `met` starts a finalizer, with no orchestrator session needed.
- The `refactoring` and `complexity-analyzer` skills.
- Generic blockers (schema 18): `swarm blockers`, `swarm blocker resolve` and `swarm blocker comment`; several decisions can wait independently on one job, with deadlines and overdue flags.
- Automatic recovery of crashed agents and orphaned coordinators, with backoff, a 24-hour cap and a `GAVE UP` notice. On by default; `swarm init`/`upgrade` installs a 5-minute user timer; `enabled = false` turns it off.
- Role addressing: `swarm post --to @EL|@PM|@QA|@judge|@<role>` reaches whoever holds that seat now. An unknown recipient or seat is refused instead of stored for nobody. `post` also takes `--key` instead of `--as`.
- `swarm wait --until` (a duration, a time of day or a date and time). A bounded wait protects the job from stall limits and is shown in `status` and `watch`.
- CLI plugins: `swarm plugins` lists discovered command plugins and load errors; a broken plugin never breaks a core command; `[plugins] disabled` skips plugins. See `docs/PLUGINS.md`.

### Fixed
- Waiting and paused jobs show as waiting/paused in `watch` and `status`, not active.
- Codex agents stay active between turns and complete only when the session ends.
- Orchestrator reminders stay silent while agents work and repeat only after the orphan interval.
- Queued role-addressed posts are validated on delivery; a refused post tells its author why.
- `@PM` reaches the project manager (or orchestrator); `@product` reaches the optional product manager.
- File plugins with unsafe ownership, world or foreign-group write access, or symlinks are refused and listed; `swarm plugins` reports discovery failures.
- Postgres setup rebuilds missing board metadata without losing messages; old message-width upgrades keep dependent status views.
- The test suite works as the `codex` user and with `NO_COLOR` set, and no longer depends on machine speed.
- Review and completion ignore pre-pipeline hand-offs, superseded branch revisions and malformed DONE posts; merged or deleted branches count as integrated without launching judges.
- Verdicts infer an omitted artifact from a leading `branch@sha:` reason; judge instructions prominently show `--artifact`.
- `swarm who` shows waiting jobs whose goal is not met; the orphan-rule comment excludes waiting and paused jobs.

### Changed
- Session watches show only open jobs (or the last finished one), coalesce notification bursts and redraw from cached snapshots.
- `wait` and `resume` use blockers; upgrades keep existing waits and plugin data. `status` and `watch` list open blockers.
- Schemas 16-19 (applied automatically on first use): per-job plugin data, per-job PostgreSQL message counts, blockers and event history, and faster multi-job status totals.
- Review pipeline integration is ci-agnostic: rebase locally, MR/PR where supported; GitHub is a CI adapter, not assumed.

## [0.1.17] - 2026-10-05

### Added
- The board message length is configurable: one cap per board, stored in the board so every client and host agrees (`[board] message_max_chars` only seeds a new board). Read it with `swarm config board.message_max_chars`, change it online with `swarm config board.message_max_chars 500` (50 to 4000; `--save` also writes the config file). Lowering it never cuts or refuses messages already stored; it applies to new posts. The start hook shows the live cap.

### Changed
- Schema 15: the cap moves from the `varchar(N)` column / table CHECK into the board (Postgres: `text` column, a replaceable `NOT VALID` check and a `board_meta` row, converted online in one quick, retried `ALTER`; SQLite: a trigger and a `board_meta` table). Boards keep the cap they enforced and every message.
- Linux tool hooks skip Python while the board is unchanged and contact is not due (`[hook] hook_min_interval_s`, default 15 seconds). Shared message notifications invalidate read stamps; notifier failures fall back to cursor reads. Start, stop and session-stop hooks keep their behavior.
- `watch` defaults to 10-second periodic refreshes (`[board] watch_interval_s`); notifications and snapshot misses coalesce for `watch_min_redraw_s` (default 2 seconds), including compact panes. Keys still render immediately from cached data.
- PostgreSQL watch snapshots use one statement with grouped message counts. Schema 15 replaces correlated status counts and adds message/agent indexes (automatic migration).
- Hindsight writes default to the existing `coding` bank (`default_bank`), rather than creating a bank per job; missing banks require explicit `--create-bank`.
- Recall uses `recall_banks` (default `coding` and `hermes`) plus an explicit project bank, with deduplication, bounded whole-fact caches and isolated bank failures.
- Job completion instructions require distilled learnings in the best-matching existing bank; `swarm learn` retains them with provenance and `--list-banks` lists choices.
- `swarm deactivate --delete-bank` deletes an explicit project bank only after successful learning retention elsewhere.

### Fixed
- Postgres schema setup allows cumulative lock waits within a separate 60s query budget and preserves the original connection failure when cleanup fails.
- Timing tests use controlled clocks and synchronization, with isolated fixtures and hook spool assertions independent of IO time; test-only stress tooling helps reproduce scheduling failures.

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
