# Changelog

All notable changes to swarm, newest first. Each release's notes on GitHub are taken from its
section here, so every release adds a `## [x.y.z] - YYYY-MM-DD` section (see `scripts/release-notes.sh`).
Keep entries short and user-facing: one line per change, what it does, not how.

## [Unreleased]
- **A judged head never parks a job on a person.** "Same head handed off twice", "head already judged" and the old per-generation judge cap are now a scheduled wait (5 to 30 min backoff) shown as `waiting on: lead or coordinator`; every queued launch (a person's `swarm run`, the lead's engineers, reviewers, integrators) still runs, only re-judging the same head is withheld; an unresolved hand-off (`head unknown`) never counts as a repeat; goal-hold watermarks left by older drivers are released on the first pass. The only remaining person wait is a lead's BLOCKED that names a person.

- Fix driver.log portability checks on Windows and macOS; normalize log newlines and keep rotation bounded.

- Fix Windows portability of the F03 driver cache and release tests while preserving POSIX permission checks.

- **A ruling never cancels running engineers or the lead.** It waits for them (a visible hold), and a ledger-only ruling is cancelled while engineers are queued or running.
- `swarm watch --compact` rolls finished and lost agents into one WORKERS summary per job, keeps live agents with tool/launch and last-post age, and avoids clock-only redraws.

## [0.3.3] - 2026-10-16

### Removed
- **Seats are gone.** Drive jobs run named launches (`swarm run --job J --name N --role R --brief B`), concurrency is per job, agents share a workdir, roles are prompt text; the seat vocabulary is renamed to launches everywhere (the credential jail remains).

### Fixed (drive: jobs no longer get stuck)
- An interrupted or exhausted launch is re-queued; a judge that ends without a verdict is re-queued and then escalated (`DRIVER-ESCALATE`), never closed "mechanical: unreachable" (F03).
- A re-queue restarts the attempt counter, a reopen starts a new generation and queues the lead, and every skipped launch says why (`DRIVER-SKIP`) (F03b).
- The lead's `swarm done` is recognised as a hand-off; a ruling `not_met` keeps the job active and re-queues the lead; the judge Stop hook no longer blocks on a false "board unavailable"; the codex command is configurable and refused up front when missing (F03c).
- A driver step exception escalates instead of scheduling a ruling; tracebacks go to `driver.log`; hand-offs without an artifact are tolerated (F03d).
- A job with no landing destination completes on `met`; `met` on unlanded work is never unreachable; a person can always complete a met job (F03e).
- Budget, session, wall-clock and attempt exhaustion escalate and hold for a person instead of ruling unreachable; an unchanged head is never re-ruled (F03f).
- The learnings hold survives a budget hold; launch homes carry the standing-rules cache (F03g).
- A same-sha hand-off is "no progress", the goal judge runs once per head, and a hand-off no longer cancels the lead's engineers (F03h).
- `swarm pause`/`swarm resume` keep the drive ledger consistent; stale resume handles are dropped; the judge backoff holds only the judge (F03i).
- Seeded cache directories are 0700 and repaired; fix rounds never target the judge; the per-head judge cap covers fix rounds (F03j).
- Ruling holds are distinct from budget holds, stale ruling deadlines clear, and engineers run after a per-head ruling (F03k).
- A `met` verdict clears every hold; every hold is visible as `waiting on:` in `swarm status`; `settle_halt` saves a fresh ledger (F03l).

### Added
- **Codex through an argv wrapper (`[harness.codex] wrapper = true`).** `command` is then a wrapper prefix (for example `ssh root@node pct exec 118 -- runuser -u codex -- codex`) that runs Codex as another OS user: the launch argv is the wrapper plus Codex's own arguments, the brief goes in on stdin, and the launch is not put in swarm's jail (the remote user is the trust boundary; the log notes it once and `swarm doctor` warns).
- **`swarm await --job J`: the orchestrator's wake-up.** It blocks until something needs the orchestrating session and prints one line per item (kind, key, age, action), then exits: an open question or blocker (`answer Q492`), a post addressed to the orchestrator or its role, a BRANCH READY, INFRA REQUEST, VERDICT or ORDER post, or a job state change. `--once` prints the open items without blocking; `--timeout S` exits with no items after S seconds; `--poll S` (default 5) sets the check interval; `--remind S` (default 60) reports an open item again once it was last reported S seconds ago, so a dropped wake-up comes back on the next call. Items collapse by key (one per question, per branch@sha) and stay open until resolved, not merely read. The await keeps its own cursor per (job, session) in the board's metadata, so it never consumes the messages a `read` or another await would see.
- **The orchestrator is woken for its own job.** An orchestrating session that activated a non-drive job is told on each tool call, until it runs one, to start `swarm await --job J` as a background command; one await runs per session and job, and it is restarted after each exit. `swarm doctor` shows `orchestrator wake: armed` or `NOT ARMED` per owned job.
- **PM-STALL: a job never silently waits on its orchestrator.** A blocker or a message addressed to the orchestrator (or its role, `@PM`) that stays unhandled for `[job] orchestrator_stall_minutes` (default 5, 0 = off) gets a `PM-STALL <item> age=<m>` board post, once, then again every that many minutes while it is unhandled. `[job] notify_on_stall = true` also tells the person through `[notify]` (`push_url`, or `telegram_token` and `telegram_chat`). `swarm status` and `swarm watch` show `waiting on ORCHESTRATOR since <age>`, distinct from a wait on a person.
- **Seat continuity.** `swarm activate` on a closed job puts the reopening session back on the board in its previous role (for example project_manager) under its previous name. `swarm post --to @PM` (or any `@role`) with nobody in the seat is queued instead of refused and delivered to the next holder that joins. `swarm join --judge` and the spawn and seat-request paths refuse a second judge with `judge seat held by <name> (joined <age> ago)`, and the not-met brief names a judge that still holds the seat.
- **Stale blockers are flagged.** `swarm blockers` marks an open blocker `POSSIBLY STALE` when its subject is resolved: its PR landed or merged, or a later message names the same branch with another sha. `blocker resolve` takes several ids in one call (nothing is resolved if one id is unknown).
- **`swarm answer` works for the orchestrating session without a join.** The session that owns an open job (its activation marker is bound to it) answers as its previous board name, else as `orchestrator`, instead of failing with "no active board identity".
- **Addressed posts are delivered whole.** A `--to` post longer than the board's message cap keeps its head on the board and puts the full text in a file under the swarm data dir (`~/.local/share/swarm/posts/<job>/`), named in the message. Broadcasts keep the cap.

### Fixed
- **No wake nudge for closed or foreign jobs.** `orchestrator wake NOT ARMED` (hook and `swarm doctor`) names only active jobs the session still owns on the board, and `swarm await --job J` on a completed, failed or cancelled job exits at once with `job J is <status>: nothing to await`.
- **A person can complete a job whose goal is met without `--force`.** `swarm deactivate --status completed` compares the met verdict's `REF@SHA` candidate with the latest hand-off (a bare SHA, `REF@SHA` or `report:<path>@<blob>`) by head, and counts only a verdict of the current generation; the refused learnings step now lists the exact `swarm learn` and `--no-learn` commands.
- `swarm status --job J` says why a drive job is not progressing (`why not progressing: <launch>: <reason>` per queued launch, `why not progressing: hold <kind>: <reason>` per hold), and `swarm doctor` flags an active drive job with no running or queued launch and no hold as stalled.

### Changed
- **Memory: no periodic recall, no timed reminder.** An agent gets one start recall, scoped to its job's subject tags and topic; after that it asks with `swarm recall "<query>"`, which searches every subject (`--tags`/`--bank` narrow it). Migration: `[hindsight] recall_minutes` and `remember_nudge_minutes` are gone; configs that still have them keep working (the keys are ignored), delete them.
- **Memory tags are subjects, never ids.** A job carries subject tags (`swarm activate --tags swarm,astro-loom`; `swarm job tags --job J --add/--remove/--set`, any time, posted on the board); `swarm remember` and `swarm learn` stamp exactly those (plus `--tags`), read at the moment they run. The job, agent, session and host go into metadata only; `job:`/`agent:` tags are no longer written. Memories already stored are not rewritten.
- **Learnings are mandatory.** An agent cannot stop normally until it stored a memory or declared none (`swarm remember --none "<reason>"`): the stop is blocked once. Agents that end abnormally are marked unharvested, with no LLM call. `swarm deactivate` is refused until every agent is harvested and something was stored (`swarm learn --job J --harvest`, then `swarm learn ... -` or `--none`); a person may waive it with `--no-learn "<reason>"`. Drive jobs run the step through a `learner` seat before they close.

## [0.3.2] - 2026-10-13

Same code as the withdrawn 0.3.0 and 0.3.1, re-cut so the release files exist: Swarm installs no supervise timer. Run `swarm upgrade --force` if you installed 0.3.0 or 0.3.1.

## [0.3.1] - 2026-10-13

Supersedes 0.3.0, which shipped the supervise timer. Run `swarm upgrade --force` if you installed 0.3.0.

### Changed
- **Breaking:** Swarm installs no system timer of any kind (no systemd timer or service unit, no launchd agent, no Task Scheduler task). `swarm supervise enable` and `swarm supervise disable` are removed; `swarm supervise` stays a one-shot pass run by hand, and drive jobs are driven by the event-driven session driver (`swarm drive run` is the manual loop). `swarm bootstrap` and `swarm upgrade` (and `swarm init`) remove a `swarm-supervise.timer`/`.service` left by an earlier build and print one line saying so; `swarm doctor` fails while one is there ("legacy swarm-supervise timer found; run swarm upgrade to remove it") and no longer prints timer or last-run lines. A config without `[supervise] enabled` is off (it used to count as on for configs that predated the opt-in), and drive seats are no longer cancelled by `[supervise] enabled = false`.

## [0.3.0] - 2026-10-12

### Changed
- **Breaking:** `swarm activate --goal` starts a drive job (the supervisor drives it to MET, UNREACHABLE or CANCELLED); `--no-drive --reason TEXT` keeps the old behaviour. No timer is needed: while the orchestrating session is open, its event-driven session driver runs the job (Linux, macOS and Windows). In a drive job the activating session's Agent-tool spawns are refused: staff with `swarm seat request`.
- In a goal job, `swarm ask` needs `--default` (and gets `--expires` from `[drive] question_expiry` when not given); an open question no longer freezes the whole job for the supervisor: only the asking seat waits.
- Setup is opt-in on a new machine. Installing the plugin no longer starts a detached `swarm bootstrap` at the first session: SessionStart prints `[swarm] not set up: run swarm init` and does nothing else. `swarm init` is the explicit setup (config, launcher, board; `--host claude|codex`) and prints a summary. By default it no longer edits `~/.claude/settings.json` (`sandbox.filesystem.allowWrite`) or `~/.codex/config.toml` (`writable_roots`, `[agents] max_depth`): it prints the exact lines, and only `swarm init --apply-settings` edits them (backup kept).
- The choice is stored: `~/.local/share/swarm/host/settings-consent` holds `yes` or `no`. `swarm init --apply-settings` (and the installers, through `SWARM_APPLY_SETTINGS=1`) store `yes`; a plain `swarm init` on a new machine stores `no`. The detached bootstrap, `swarm bootstrap` and `swarm upgrade` follow the stored answer (`swarm bootstrap --apply-settings` / `--no-apply-settings` override it for one run), so the config and launcher that `swarm init` creates never turn into consent.
- Existing installs keep working with no action: a machine with `~/.config/swarm/config.toml`, the `~/.local/bin/swarm` launcher, or a bootstrap stamp of any version and no stored answer counts as `yes` (stored on first use), so the detached bootstrap at a plugin version change, `swarm upgrade` and `swarm init` keep their settings upkeep. Migration for anyone who wants today's behaviour on a new machine: `swarm init --apply-settings` (or `swarm bootstrap --host claude|codex --apply-settings`); to opt an existing install out, write `no` into the consent file. A new machine's SessionStart notice writes nothing (no host directory or cache) until `swarm init` runs.
- The supervisor (stuck-agent restarts and the review pipeline's automatic hand-offs) is now opt-in: a new install leaves `[supervise] enabled` off and installs no systemd timer. New configs (from config.example.toml) say `enabled = false` explicitly. **Existing installs keep working with no action**: a config written before this change has no `enabled` key and stays on, on every host (Linux timer passes, and stuck-agent closing during sweeps on macOS and hosts without systemd); `swarm doctor` suggests making that explicit. One-command migration to make it permanent: `swarm supervise enable`.
- The venv's dependencies are pinned to exact versions with sha256 hashes (`requirements.txt`: zstandard; `requirements-postgres.txt`: psycopg, psycopg-binary and their markers' dependencies) and installed with `pip --require-hashes --only-binary=:all:`. The launchers check for Python 3.11+ before creating the venv. psycopg is installed only for a Postgres board or when the venv already has it. The launchers decide "Postgres board" by parsing the config like `swarm` does (`lib/swarm/pgwant.py`, run with the system python3; `--config PATH` and `SWARM_CONFIG` are honoured): `backend = "postgres"` in any TOML spelling, or a non-empty `[database]` section with no backend set; the shipped `config.example.toml` (backend `file`) is not Postgres. A Postgres board without psycopg now says what to run instead of raising ImportError, and a failed pip install says that installs are hash-checked wheels only. **Existing installs need no action**: the first `swarm` command after the upgrade rebuilds the venv once with the pinned versions and keeps psycopg.

### Added
- Drive seats run in a jail (Linux): their own user, mount and pid namespaces with an allowlisted view of the person's files (the work tree and git dir, the harness login and settings read-only, the swarm venv and the tools on PATH), their own `$HOME`, `/tmp` and transcripts, and an allowlisted environment. A jailed seat has no board password: its `swarm` commands and hooks run outside the jail through its runner's broker, which stamps who the seat is and refuses commands a seat never runs (`bg`, `config`, `supervise`, ...). Only the supervisor lands. On macOS and Windows (no filesystem jail) seats are best-effort: the allowlisted environment, no git credential helper or ssh agent, a scratch HOME and the same broker; `swarm doctor` warns. A Linux host without unprivileged user namespaces falls back to best-effort too (doctor warns); `[drive] seat_isolation = "strict"` refuses seats instead wherever the jail cannot be built. Turning isolation off is a person's act (`swarm supervise seat-isolation off`). `swarm doctor` probes it ("seat isolation").
- Drive control commands: `swarm seat request|list|show|release|cancel|ack`, `swarm review`, `swarm goal-ready`, `swarm halt|unhalt`, `swarm drive repair|budget|models`; every control command prints a `swarm-record:` line, and `swarm verdict` binds its VERDICT message and judged artifact.
- Drive activation: durable claim/owner record/ledger order, pinned remotes and credentials (`--credential`), pinned requirements, per-job budgets (`--max-cost-usd`, `--ruling-reserve-usd`, `--budget`) and models (`--model ROLE=MODEL`).
- Drive seats: code a seat runs must be a literal argument the hook can scan; `source .venv/bin/activate` and `.venv/bin/pytest` are refused in drive seats (use `.venv/bin/python -m pytest`), and interpreter code arriving on stdin is refused.
- Drive hooks: host-private receipts for every agent-written control record, seat roles from the ledger, read-only judge/reviewer guardrails, refusals of agent-launched sessions and target pushes, and a supervisor kick after control commands.
- `swarm doctor` drive lines: git >= 2.40, seat isolation (a WARN where seats are best-effort), credential helpers, Claude Bash sandbox, Codex board access, per-job budget and models, DRIVER-ORPHAN / DRIVER-LEDGER-BAD jobs and goal jobs that are not autonomous.
- Drive-mode QA harness (internal): `e2e/drive_live.sh` runs a throwaway goal job on an isolated board (own HOME, throwaway Postgres, local file:// fake hub) driven only by supervisor passes, plus a chaos suite (seat/runner/supervisor kills, corrupt ledger, stale lock, long-idle reactivation, expiring question default, forged close and receipt); it skips with `pending 8a/8b/8c/11b` until the driver is integrated.
- Private git mirrors for drive mode: per-destination bare mirrors under the supervisor's private directory with pinned credentials (absolute credential helper, ssh key or agent socket) and a sanitized git environment, verbatim patch-id comparison for clean rebases, and a close-time audit that flags target commits not covered by a judged change. Internal groundwork.
- Schema 25 seats and drive locks: a `seats` table (idempotent requests, compare-and-set state, one seat per name and generation), `drive_locks` with per-destination and worktree-root leases (overlapping roots refused under a host-wide lock), and one atomic launch claim that checks the seat revision, host cap, locks and budget reservation together. Internal groundwork for drive mode.
- Schema 25: typed judge verdicts (`swarm verdict --scope artifact|goal|ruling`, `--open`, `--progress`, `--candidate`, `--req`) and a new `unreachable` verdict (with `--details`) that settles a goal, recorded on every backend and through the offline spool.
- Drive mode groundwork (internal, no user-visible change yet): an injectable clock and `SWARM_CRASH_AT` crash barriers for chaos tests, and the driver's frozen snapshot/action model with shared test fixtures; a host-private drive ledger (atomic, fsynced, fail-closed on planted files) hook receipts for agent-written control records, the driver's pure next-action rules, and one launch contract for headless seats (per-session budget, read-only judges, resume), usage capture from Claude and Codex output, and a containment check that can say "unknown".
- `swarm snapshot --json [--job J ...] [--session ID ...] [--messages N]`: the board's current view (jobs, agents, open blockers, newest messages) as one JSON line. A pure read: no setup, spool flush, sweep or bookkeeping, a read-only session (Postgres refuses any write itself), bounded output (at most 50 jobs, 200 agents and 100 blockers per job, 200 messages per job; `truncated` says what was cut).
- `swarm snapshot --follow`: the same view as an NDJSON stream for dashboards: a full snapshot, then one delta per change, `ping` keepalives, and a `{"degraded":true}` line while it can only poll (a standby, a pooler that refuses LISTEN, a lost connection), then `{"degraded":false}` and a fresh snapshot. Postgres wakes on LISTEN/NOTIFY (LISTEN before the first read, so nothing is missed) and reconnects by itself; SQLite and file boards keep polling. Every `watch_check_s` (60) a one-statement fingerprint of the scope (messages, job details, agents, open blockers) is compared with the stream's state and a full snapshot follows only on a mismatch.
- `Board.supports_subscribe` and `Board.change_mode` ("push", "poll" or "degraded"): which backends push and what holds right now.
- Plugin listing metadata and disclosure: README sections "What Swarm changes on your machine" and "Uninstall", plugin-manager install first, PRIVACY.md, SECURITY.md, THIRD_PARTY_NOTICES.md; manifest homepage, keywords and descriptions that state the side effects; a Codex `interface` block with icons, and `agents/openai.yaml` display metadata for every skill. No behaviour change.
- `swarm supervise enable` / `swarm supervise disable`: write `[supervise] enabled = true|false` in the config (keeping the rest of the file) and install and start, or stop and disable, the systemd user timer.
- `swarm uninstall [--purge] [--dry-run]`: removes what Swarm set up outside the plugin: the supervisor's units (stopped first), the `~/.local/bin` launcher Swarm wrote, `~/.local/share/swarm` (venv, caches, logs, supervisor dir, tools, legacy-skill backups), and the entries it added to Claude's `sandbox.filesystem.allowWrite` and Codex's `writable_roots` (each file backed up first). `--purge` also deletes `~/.local/state/swarm`, the default SQLite board and the config file when it is in `~/.config/swarm`. Postgres boards, custom board paths, other `~/.config/swarm` files and backups are kept. If stopping the timer fails, nothing is removed and the manual commands are printed.
- Codex directory edition: `scripts/build-codex-directory` packages the tree as `dist/swarm-codex-<version>.zip`, a plugin without hooks for OpenAI's public plugin directory (generated `swarm-team` manifest with the directory `interface` fields, instruction-only `SKILL.cdx.md` skills for `swarm` and `ask-answer`, its own README and icons under `cdx/`, an `EDITION` file the CLI reads so `activate`, `doctor` and `bootstrap` stop describing hook behaviour, and no `hooks.py`; `JOB_NAME`, `_markers` and `_log_line` moved to `swarm/jobmarkers.py`). `swarm ask`, `questions` and `answer` take `--key`, so a joined agent is identified without hooks and never taken for the human. The release workflow builds it and attaches it to the release. The full edition is unchanged.
- `swarm verdict --details -|PATH`: a judge records its full Markdown report (evidence, numbered defects, fix list; at most 64 KiB) with a met or not_met verdict, bound to the same `--artifact`, instead of writing a `verdict.md` that judges are not allowed to create. `swarm verdict show --job J [--artifact REF]` prints it, `status --job J` shows `details: N lines`, and the fix worker's brief points to it. Needs schema 23 (nullable `jobs.verdict_details`; the board upgrades itself on first use).
- Background commands of agents are tracked (schema 24, the `bg_commands` table; the board upgrades itself on first use). A swarm member's background Bash call in Claude Code now runs under `swarm bg`, which records it on the board with its process group, start time and host, and records its exit code. The command keeps its output, exit code and environment, but it now runs in a fresh `bash -c`, without Claude Code's shell snapshot: the agent shell's aliases and functions are not available to it. Codex can't be wrapped: its long-running commands are not tracked (documented).
- `swarm bg list [--job J] [--orphans] [--all]` and `swarm bg reap [--job J] [--agent KEY] [--dry-run]`. A background command still running after its agent finished or its job closed is an orphan. Reap stops it with SIGTERM, then SIGKILL after 10 s. It only signals processes it can prove are the recorded command's (this host, boot and pid namespace, start time, tag), so a reused pid or another host's process is never touched. The supervisor pass, `deactivate`, auto-close and an agent's SubagentStop (after 30 s) reap by themselves.
- `swarm status --job` and `swarm watch` show `background N running, M orphaned`; `swarm doctor` warns about this host's orphaned background commands.

### Changed
- `swarm watch` on Postgres no longer re-reads the whole view on a timer. A notification reads only the new messages and refreshes the jobs and agents; idle and dead are aged on screen from the snapshot, so a quiet board costs one cheap fingerprint statement a minute (was one ~150 KB snapshot every 10 s) and a full read happens only when the fingerprint differs, on connect, after a reconnect, or when push comes back. A watch that has fallen back to polling says so under the title. Blockers in the watch snapshot are the open ones.

### Fixed
- `[drive] credential_helpers` is wired into `swarm land`: a drive integrator can land to a destination whose credential helper is an absolute path you list there (e.g. `/home/you/.local/bin/gh`), and a refused helper's message names the key.
- `swarm doctor` no longer reports `FAIL supervise timer ?/?` from agent shells: it reads the timer through the user manager's environment (`XDG_RUNTIME_DIR`), and says "unknown (no user bus from this shell)" instead of failing when the bus is unreachable but the last pass is recent.
- The Codex directory edition ZIP ships `requirements-postgres.txt`, which its launcher reads on first run.
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
- The old `[hub]` name is gone, with no alias: the `team.toml` section is `[ci]` (`[repositories."p".ci]` for overrides), the `swarm ci --kind` option replaces the old host flag, and event-source state files, modules and docs say CI host. `swarm upgrade` and `swarm init` rewrite an existing `[hub]` section to `[ci]` once, keeping `team.toml.bak` next to it and saying so.
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
