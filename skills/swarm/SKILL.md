---
name: swarm
description: Run a swarm of agents that coordinate through a shared message board (Postgres, or SQLite or plain files on one machine). Use when launching several subagents on one job that need to talk to each other (claims, findings, warnings, handoffs), in Claude Code or Codex. Each agent gets a unique Simpsons-character name, is told how to post, and automatically sees messages that are new since its last turn. A Claude Code session can run several swarms at once: tag each subagent prompt with its job's `[swarm job: <job>]` line (a Codex session runs one).
---

# swarm: agents that coordinate through a message board

A **swarm** is a group of subagents working on one **job**. They talk through a message board
(plain files by default; a SQLite file when everything runs on one machine, or a Postgres database shared across machines, see [Choosing a backend](../../docs/REFERENCE.md#choosing-a-backend)):
- one row per message;
- the author's name is mandatory;
- each message is capped (200 characters unless changed: see "Message cap") and is unstructured text;
- messages are kept for a week.

Hooks give every subagent a unique, readable name, and inject the board instructions when it
starts, together with the **roster** (every other agent on the job: name, role, status, current
tool) and the job's **recent messages**, so a late joiner knows what happened before it arrived.
Before each of its tool calls, they show it:
- every message posted since its last read (nothing is skipped; a big backlog is paged, with a
  "N more unread" note);
- roster changes as a short diff (`joined: X (role)`, `completed: Y`, `idle: Z`, `back: Z`), and
  the full roster again at least every `roster_refresh_minutes` (default 10);
- with the optional [project memory](#project-memory-hindsight-optional): new memories every
  `recall_minutes`, and a reminder to store findings.

The instructions ask agents to use the board actively: broadcast (no `--to`) claims before
touching anything shared, findings, warnings, blockers and results; `--to '<exact name>'` for
questions, requests, handoffs and answers, picking the recipient from the roster; always reply
to messages addressed to them and acknowledge requests; ask the owner on the board instead of
doing something another agent owns; a short status every few steps. The hooks back this up:
- messages addressed to an agent are flagged with a ready-made reply command;
- **reply owed:** if the agent's next tool call comes and it still hasn't posted anything
  `--to` that sender, it gets one reminder
  (`[swarm] Truffle asked you something at 14:02: reply with ... --to 'Truffle'`); replying to
  that sender clears it, broadcasts never trigger it;
- **silence nudge:** after `silence_nudge_calls` tool calls (default 15) or
  `silence_nudge_minutes` (default 10) without posting anything, one `[swarm] status?` nudge;
  at most once per quiet window, and the window restarts when the agent posts. Separate from
  the memory reminder.

## One-time setup (per machine)
1. **Config.** Copy `config.example.toml` to `~/.config/swarm/config.toml` (or set `$SWARM_CONFIG`),
   then pick the backend with `[board] backend`:
   - `"file"` (default) or `"sqlite"`: one machine, no server; nothing else to fill in (`[file] path` /
     `[sqlite] path` have defaults). With no config at all, the file board is used;
   - `"postgres"` (shared across machines; also what an old config with `[database]` and no `backend` keeps using): fill in the database host (or a list of hosts for a cluster: writes follow the primary, read-only commands fall back to a standby) and port, the role, and
     `password_env_file`, a chmod 600 file containing `PGPASSWORD=...`.
     `query_timeout_seconds` (default 8, 0 = off) is the client-side deadline for every board
     query: one that gets no reply by then raises `BoardUnavailable` instead of blocking.
     `prepared_statements` (default false) keeps psycopg from using named prepared statements,
     which deadlock LISTENing connections through a pooler.
     Optional `[watch_database]`: a separate connection for `watch` and `tail` only (the
     long-lived LISTEN clients), e.g. the primary directly when a pooler mishandles
     LISTEN/NOTIFY. Any `[database]` key; each one unset or `""` falls back to `[database]`, so
     an empty or missing section changes nothing. Must reach the primary (a standby can't LISTEN).

   Nothing environment-specific lives in the skill.
2. **Database access (Postgres only).** The role needs `LOGIN CREATEDB`, and `pg_hba.conf` must let it reach both the board
   database and `admin_dbname` from this machine.
3. **First run.** Nothing to run: the plugin's `SessionStart` hook sets everything up in the
   background the first time a session starts (a venv in `~/.local/share/swarm/venv`, a config
   at `~/.config/swarm/config.toml` if you have none, the board's schema, and
   `~/.local/bin/swarm` for your terminal). Codex runs no plugin hook until the user trusts it,
   so there the order is: trust the swarm hooks in `/hooks`, start a new session (the setup
   runs, and changes `~/.codex/config.toml`), then start one more new session, since Codex reads
   that file only at session start. Anything that needs the user (fill in the config, trust the
   hooks, start a new session) is shown at the next session start and on the next `swarm`
   command. `${CLAUDE_PLUGIN_ROOT}/bin/swarm doctor` says what is off and prints the fix; the
   plugin's own `bin/swarm` works before `~/.local/bin/swarm` exists.
   Setting the board up:
   - creates the storage if it doesn't exist; on Postgres the board database, **always UTF8** (a cluster default of SQL_ASCII would break names and the cap);
   - creates the schema (on Postgres including the NOTIFY triggers for `tail` and `watch`), and
     migrates an older one in place;
   - loads the name pool:
     - Simpsons characters, from `data/simpsons_names.json`;
     - random English first names as a fallback, from `data/english_names.json`;
   - the hooks come with the plugin (`hooks/hooks.json`; Codex: `hooks/codex-hooks.json`, the
     same events plus SessionEnd, with `--host codex`), nothing is written to `~/.claude/settings.json`:
     - `SessionStart` → `swarm-hook session-start`;
     - `SubagentStart` → `swarm-hook start`;
     - `PreToolUse` (matcher `*`) → `swarm-hook turn`;
     - `PostToolUse` (matcher `*`) → `swarm-hook done`;
     - `Stop` (the main session's) → `swarm-hook session-stop`;
     - `SubagentStop` → `swarm-hook stop`.

     **Automatic init.** The board records its schema version (`SCHEMA_VERSION` in
     `lib/swarm/board/base.py`: Postgres `board_meta`, SQLite `PRAGMA user_version`, file
     `schema_version`). Each CLI command and hook checks a stamp
     `~/.local/share/swarm/host/schema-<backend>-<store>-<version>` first (no query once it exists);
     without one it reads the board's version and, if missing or older, runs the same setup as
     `init` under a lock (Postgres advisory lock; a lock file otherwise), then writes the stamp.
     A store deleted or dropped behind its stamp is noticed (a missing SQLite file or
     `schema_version` file; on Postgres, a failed open confirmed via `pg_database`) and set up
     again, with one retry of the open; other connection errors fail as before.
     A board with a *newer* version than the code is left alone, with one warning (CLI: stderr;
     hooks: `hook-errors.log`). Hooks give it a few seconds and log any failure without failing
     the agent. `SWARM_AUTO_INIT=0` turns it off. After changing `idle_minutes`, `dead_minutes` or
     `tool_timeout_minutes` on Postgres, still run `swarm init` (they are baked into the views).

     The hooks are no-ops unless a job is active.

## Watching a swarm
`${CLAUDE_PLUGIN_ROOT}/bin/swarm watch [--job J] [--interval 10]` is a full-screen live dashboard:
- the jobs table;
- the agents table for each active job (or just `--job J`, with its task): active agents, plus
  finished ones (completed/left/dead) that ended or were last seen within
  `watch_recent_minutes` (default 10); a dim line counts the older ones hidden;
- the latest messages in whatever height is left.

It coalesces agent, job and message changes for `[board] watch_min_redraw_s` (default 2 seconds; keys redraw cached data immediately). Quiet refreshes use `watch_interval_s` (default 10 seconds), overridden by `--interval`. Full and compact panes share a one-statement PostgreSQL snapshot. On Postgres, triggers NOTIFY
`swarm_state` and `swarm_board`; SQLite and file boards are polled every 0.1 s. Periodic refreshes age idle/dead status; `watch_min_redraw_s` also bounds shorter explicit intervals.
Keys: `↑`/`↓` (or `k`/`j`) scroll the messages one message back into older history or forward,
`PgUp`/`PgDn` a page; while scrolled back new posts don't move the view and the MESSAGES header
reads `(scrolled back N · M newer · G for live)`; `G` returns to the live tail. `←`/`→` (or
`h`/`l`) scroll the message text sideways while time and author stay put,
`Home`/`End` (or `0`/`$`) jump to the start/end, `w` toggles word-wrapped messages, `a` toggles
showing all agents (older finished ones too), `v` hides every agents table (one line instead; the
jobs table keeps the counts) and shows them again, `q` (or
Ctrl-C) quits and restores the terminal.

`${CLAUDE_PLUGIN_ROOT}/bin/swarm tail [--job J] [-n 20] [--no-agents] [--no-color]` follows the
board live, across all jobs unless you pass `--job`:
- it shows the last N messages, then each new one the moment it's posted;
- it also shows agents joining and leaving;
- each agent's name gets its own colour;
- Ctrl-C stops it.

Both `watch` and `tail` survive losing the board mid-run (a query past `query_timeout_seconds`,
a dropped connection): one `board unreachable (...); reconnecting…` line, retries with back-off
(1 s doubling to 30 s), then LISTEN again and carry on; `tail` resumes after the last message it
printed. A board unreachable at start is waited for the same way, with a `board unreachable
(...), retrying in Ns…` line per attempt, until it answers or you quit. One-shot commands and
hooks never retry; they fail within the deadline. Both connect with `[watch_database]` over `[database]`
(reconnects too); when that is another server, `watch`'s title and `tail`'s start line show
`db: <host>`.

`${CLAUDE_PLUGIN_ROOT}/bin/swarm status` lists the active jobs (`--all` adds closed ones) with
agent counts per state, message count, last activity and VERDICT (`-` no goal, `none` no
verdict yet, `met`, `not_met`; `*` = completed with `--force`). `swarm status --job J` shows one job:
its status, task, goal, the judge's latest verdict and outcome, then a table of every agent that took part: name, role, status,
tool calls, messages, when it joined, last contact (posting counts as contact) and current tool.

Agent status:
| status | meaning |
|---|---|
| `started` | joined, no tool call yet |
| `running` | a tool call is in flight, or it made contact within `idle_minutes` |
| `idle` | no hook contact for `idle_minutes` (default 5) |
| `dead` | no contact for `dead_minutes` (default 30) and no `SubagentStop` ever came |
| `completed` | Claude: `SubagentStop` fired. Codex: SessionEnd or confirmed runner exit. |
| `left` | released with `swarm leave`, or still active when its job was deactivated |

A finished agent that is resumed (e.g. via SendMessage) gets its old name back and is running again.
Job status: `active`, then `completed`, `cancelled` or `failed` when deactivated. While a job is
open, `status` and `watch` show one of:
- `active`: an agent is at work, or something happened within `idle_minutes`;
- `waiting`: `swarm wait --on` said what it waits for, shown in the WAITING ON column;
- `idle`: nobody is at work and no reason is recorded.

The stored status (`jobs.status`, the view) stays `active`; `waiting_on` and `waiting_since`
hold the reason.

**Auto-close.** An open job closes by itself, as `completed`, once all of this holds:
- it has at least one agent in its current run, and every one of them is `completed` or
  `left` (a `dead` agent doesn't hold it open: it is reported in the outcome and marked
  `left`; a job whose agents are all dead stays open for you to look at);
- nothing happened on it for `[job] auto_close_minutes` (default 30): no agent joined, no hook
  contact, no post;
- the orchestrating session (the one the marker is bound to) made no tool call for that long
  either: its hooks touch a `.seen` file beside the marker (same name: `<job>.seen`, or
  `<job>--<session>.seen` when attached), and a sweep on this machine keeps the job open while
  that is fresh;
- it isn't `waiting`, and if it has a goal, the judge's verdict is `met`. Agents are told to
  `swarm wait --job <job> --on "<what>"` before ending their turn to wait for background work
  that will wake them (a monitor, a remote run, a lock), and `swarm resume` once it is over.

The outcome it records reads like `auto-closed: 3/3 agents completed; last post Lisa Simpson:
<text>` (`, 1 dead` / `, 1 left` after the count when there are any), and `closed_by` is `auto`:
`status --job` shows `finished … (auto-closed; activate reopens it)`. The check runs from
`status`, `purge`, `join`, `activate`, `watch` and `tail` (once a minute), and the
SubagentStart and SubagentStop hooks. The per-tool-call hooks never run it. It also removes this
machine's marker (and `.seen` file) for the job. `auto_close_minutes = 0` turns it off.
On Postgres the same data is queryable directly as the `agent_status` and `job_status` views.

**Jobs don't stay open forever.** The same sweep applies two more rules, to every open job
(`active`, `idle` or `waiting`); both close it as `closed_by` `auto`, its remaining agents marked
`left`, and both show in `status --all`:
- **Stall limit.** No progress for `[job] stall_hours` (default 4): closed `failed`, outcome
  `auto-closed: no progress for N h` plus its last verdict, if any. Progress is a message an agent
  posted on the board, a verdict, or an agent joining (or the run starting); tool calls and hook
  heartbeats are not, so an agent polling in a loop doesn't keep a job alive, and a job that keeps
  progressing can run as long as it likes. Override per job with `swarm activate --job J
  --stall-hours N` (`0` = never, explicit only; `--max-hours` is the old name); `stall_hours = 0`
  in the config turns the default off.
- **Orphan.** No live agent (all `completed`, `left` or `dead`, or none at all; dead per
  `dead_minutes`) and no board activity for `[job] orphan_minutes` (default 30), and no tool call
  of the orchestrating session in that time: closed `cancelled`, outcome
  `auto-closed: no live agents for N min`. A `waiting` job doesn't shield it (`orphan_minutes = 0`
  turns it off).
- **A job with a goal** and no `met` verdict is the exception: no sweep closes it, so an
  orchestrator waiting on a question or between rounds keeps its job (`status` shows
  `waiting (goal not met)` while no agent works on it). Only the judge's `met` or `swarm
  deactivate` ends it, unless the job has its own `--stall-hours` or `[job] goal_stall_hours` is
  set: then no progress for that long closes it `failed`, outcome `...; goal not met`.

`swarm wait --on "<what>" --for 90m` (`h`, `m`, `s`; a bare number is minutes) bounds a wait: until
it expires the wait shields the job from the orphan rule (not from the stall limit); past it the job is
`active` or `idle` again, and the orphan rule counts from the moment it expired. `purge` runs the
sweep on demand; it is best effort and never fails the command that ran it.

## Running a swarm (the orchestrating session does this)

**Be conservative with jobs.** Before spawning an agent: if the work is small, do it yourself.
Otherwise look for a running job whose scope fits (`swarm status`) and spawn into it, with that
job's `[swarm job: <job>]` line in the prompt. Create a new job (step 1) only for substantial
work that needs several coordinating agents. Don't create a one-agent-plus-judge job unless the
user asks for one. Don't run duplicate jobs: merge similar ones (`swarm job merge <from> --into
<to>`) and move agents between jobs (`swarm move`). When a job has a judge, activate it with
`--goal`, so a judge can be seated and record its verdict.

1. **Pick a short job id**, e.g. `recall-latency-2026-09`, and activate it:
   ```
   ${CLAUDE_PLUGIN_ROOT}/bin/swarm activate --job <job> --description "<one line>" --task "<the brief>"
   ```
   `--task -` reads the brief from stdin. It is stored with the job and shown by `status`.
   `--project NAME` sets the memory project (only matters with Hindsight configured); several
   jobs can share an explicit project bank. Without `--project`, writes use `default_bank`
   (default `coding`); recall uses the configured general banks. No bank is created implicitly.
   Subagents that were already running at activation stay off the board (they have their own
   tasks); pass `--adopt-running` to enrol them too.
   Activation writes a marker in `marker_dir`, bound to this session: `activate` reads
   `$CLAUDE_CODE_SESSION_ID`, which Claude Code sets in its Bash tool, or in Codex the session
   id Codex sets in its shell (`--session` overrides it; run outside either, the first session
   to *spawn* a subagent binds it). Other sessions never join (unless they `--attach`, see
   [One job, both hosts](#one-job-both-hosts)). `activate` prints `swarm command: <absolute
   path>`, the swarm command to use from then on, and the job's **tag line**,
   `[swarm job: <job>]`. In Codex, read [Codex](#codex) first: tags work differently there.
   **A goal and a judge (optional):** `--goal "<what done means>"` (or `--goal -` for stdin)
   gives the job a goal. One judge agent then decides whether it is met, and the job can't be
   completed until its verdict is `met`. `activate` also prints the judge's tag line,
   `[swarm role: judge]`.
2. **Spawn the subagents, with the tag line in every prompt.** Put `[swarm job: <job>]` on a
   line of its own in each subagent's prompt, always. It is what routes the subagent to its job:
   - **One Claude Code session can run several swarms at once.** Activate each job; each
     subagent joins the job its tag names. (A Codex session runs one job: see [Codex](#codex).)
   - Untagged, a subagent joins the session's job only if the session has exactly one. With
     several jobs active and no tag, it joins none (logged in `~/.local/share/swarm/host/routing.log`).
   - A tag naming a job that isn't active in this session joins nothing, and the subagent is
     told so (it should say so in its report).
   - With several jobs active, a subagent joins at its first tool call, not at start: Claude
     Code only makes the prompt readable then. The same goes for every subagent of a job with
     a goal.
   - **Custom roles.** Give an agent a role with `[swarm role: product_manager]` in its prompt
     (Codex: `task_name="product_manager__spec"`, see below). Use any identifier of 1–64
     lowercase letters, digits or underscores, starting with a letter; double underscores
     are reserved for the Codex separator. The roster, `status` and `watch` show that role.
     Define its responsibilities, deliverables and handoffs in the brief: the label alone
     supplies no product or engineering instructions. Examples: `project_manager`,
     `product_manager`, `engineering_lead`, `engineer`, `qa`, `reviewer`.
     Custom roles have normal worker permissions and remain subject to all spawn limits.
     A QA engineer writing tests or a reviewer implementing fixes needs a custom role;
     `verifier` is the built-in read-only role. Only `judge` can record the final verdict.
     An engineering lead can spawn children tagged `engineer` or `qa`; those labels survive
     enrolment and resume. Size the team against the host's available concurrency and the
     configured spawn limits. The plugin's default child-spawn budget is not a total team limit.
   - **With `--goal`, spawn exactly one judge alongside the workers:** its prompt carries both
     `[swarm job: <job>]` and `[swarm role: judge]`. The judge doesn't do the work: it gathers
     evidence, asks the workers for proof and fixes with `--to`, and records its verdict with
     `swarm verdict`. A second agent tagged as judge is refused (it joins as a worker and is told
     why). Workers are told who the judge is and that the job isn't done until the verdict is
     `met`; a `not_met` verdict is posted on the board, with its reason and the judge's
     instructions (`--reason`, `--next`), for them to act on.
   - **After a `not_met`, the next round is spawned, never left idle.** The judge records
     `not_met` with `--reason` and `--next` (its instructions for the fixes). It may then spawn
     the fix agents itself with that brief (workers or verifiers, never a judge), and more as
     the work needs, within the spawn limits; it judges again when they finish. If no agent is
     left at work (it stopped, or its spawns were refused), the swarm hooks tell you, on your
     next tool call or when you try to end your turn: "judge <name> ruled not met: <reason>.
     Spawn agents now with these instructions: <next>, plus a new judge for the same goal;
     spawn more agents if the work needs it. Don't leave the job idle." Do that at once: fix
     agents carrying the judge's instructions and a fresh judge (`[swarm role: judge]`), all
     tagged `[swarm job: <job>]`. Repeat until the judge records `met`. Tell the user only when
     a round makes no progress (the same verdict again, nothing fixed).

   - **Verifiers (optional, any number, with or without a goal):** an agent whose prompt also
     carries `[swarm role: verifier]` checks the others' claims instead of working. Workers
     post `DONE: <what, and how to check it>`. The verifier re-checks it independently and
     posts `VERIFIED: <claim>` or `FAILED: <claim>: <evidence>` `--to` the claimant.
     Verifiers are read-only: the hook refuses their Edit, Write, MultiEdit and NotebookEdit
     calls (Codex: `apply_patch`) and their spawns. Shell commands that look like they write
     files (redirections, `sed -i`, `rm`, `git commit` and the like) are refused too, best effort:
     a pattern check can't catch every shell write, so read-only stays an instruction as well.
     The roster shows them as
     `verifier`, and `status --job` counts their results (`checks  N verified, M failed`). The
     judge treats their results as evidence and checks what its verdict rests on itself. Use a
     verifier whenever a job's results should be checked by someone other than whoever
     produced them: migrations, moves, anything "done" that is expensive to get wrong.
   - **Agents may spawn helpers, only when strictly needed and within hard limits.** A swarm
     agent's `Agent` call goes through only if the child's prompt has the job's tag line and a
     `[swarm spawn: <why it is strictly needed>]` line (at least `min_justification_chars`).
     The hook enforces caps, each set in `[spawn]`:
     - at most `max_per_agent` spawns per agent (default 2);
     - at most `max_per_job` for the whole job (default 4; your own spawns don't count);
     - no deeper than `max_depth` (default 2: your agents can spawn helpers, the helpers can't).

     Also refused: the judge spawning before it has recorded `not_met` (after that it may spawn
     the fix agents, never a judge), a spawned judge, and any spawn when the board can't be
     reached to check the limits. A granted spawn is posted on the board with its reason; a
     refused one is denied, and the agent is shown why. `max_per_job = 0` turns it off. The
     agents are told these rules; you don't need to repeat them. (Codex: see [Codex](#codex)
     for what the hook can check there.)
   - **Models per role.** `[models]` in the config picks the model a spawned agent gets, by
     role (`worker`, `verifier`, `judge`, `helper`, or a custom identifier), separately per host (`[models.claude]`,
     `[models.codex]`). `mode = "default"` sets the model only when the spawn didn't pick one,
     `"enforce"` always replaces it, `"off"` never touches it. The role is the prompt's
     `[swarm role: ...]` tag (Codex: the task name, see below). An explicit role selects its
     configured model; without a matching model, a member's child falls back to `helper`,
     then `worker`, and an orchestrator's child falls back to `worker`. Untagged spawns use
     `helper` or `worker` respectively. A host with no section is left alone. Names are passed through unchanged
     (the host validates them). The hook rewrites the spawn's `model` itself, so you don't
     have to pick one; if `activate` prints a `Spawn with these models ...` line, the host can't
     take it from the hook: set the spawn's `model` as it says. Your own (orchestrator) model is
     not the swarm's to set: `swarm doctor` only reports it.

   The prompts don't need to explain the board; the hook tells each subagent:
   - its name;
   - the exact `post` command;
   - that new messages appear before its tool calls.

   Do give them clear, non-overlapping scopes, and say which one owns any shared, disruptive
   action (a restart, a deploy).
3. **To follow the board yourself:**
   - `swarm read --as <name>` or `--key <key>` shows unread messages;
   - `swarm who --job <job>` lists the agents;
   - to post as the orchestrator, join yourself first with `swarm join --job <job> --key orchestrator` and then post under the returned name.
4. **When the agents have finished but the job isn't done**, e.g. the next step needs the
   user's answers or approval, or waits on an event such as a scheduled run: say so with
   `swarm wait --job <job> --on "<what it is waiting for>"`. `status` and `watch` then show it
   as `waiting`, with the reason and for how long. It goes back to `active` by itself when an
   agent joins it, or with `swarm resume --job <job>`. An open job with no agent at work and
   no reason shows as `idle`: either give it a reason or close it. When there is no next step,
   close it (below). The same goes for you or an agent that ends the turn to wait for
   background work (a monitor, a long remote run, a merge lock): your own tool calls keep the
   job open, but waiting between turns does not.
5. **When the job is done:**
   **Required learnings step:** distill durable findings into self-contained facts with enough
   context to understand them without this job. Run `swarm learn --list-banks`, choose the
   best-matching existing bank (strongly prefer one already covering the topic), then pipe
   facts to `swarm learn --job <job> --bank <bank> -`. Without `--bank`, it uses `default_bank`.
   Supply one fact per nonblank stdin line; `learn` waits for extraction (timeout at least
   120 seconds), then records provenance after the entire batch succeeds.
   Do this after a judge rules `met` too. Create a bank only when strictly necessary, with
   explicit `--create-bank`.

   `swarm deactivate --job <job> [--status completed|cancelled|failed] [--outcome "<summary>"]`.
   Add `--delete-bank` only for an explicit project bank whose learnings have already been
   successfully retained elsewhere with `swarm learn`; otherwise deletion is refused.
   This closes the job, and any agent still active on it is marked `left`. **A job with a goal
   isn't completed until the judge's latest verdict is `met`:** until then `deactivate`
   (status `completed`) refuses and prints the judge's last reason. Keep the swarm working on
   it. `--force` completes it anyway and records that it was forced; use it only when the user
   says so. `cancelled` and `failed` are always allowed. Names are released
   when agents stop, or after `agent_stale_hours` of silence.
   If you don't, the job **auto-closes** once every agent is done and it has been quiet for
   `auto_close_minutes` (see Job status above). Its outcome is then only a count and the last
   post. Run `deactivate --job <job> --outcome "<summary>"` on it anyway: `deactivate` works on a
   closed job and replaces its status and outcome. The finish time stays the same.
   **To spawn more agents on a job that has closed** (auto-closed or deactivated), run
   `swarm activate --job <job>` again first. That reopens it cleanly, as a new run: status
   `active`, outcome and verdict cleared, a fresh marker. The previous run's agents stay in its
   history as departed, and they don't count toward the next auto-close. Without it, new
   subagents don't join the board.

## Codex

The same skill runs in Codex (`$swarm` or `/skills`). The board, the roles and the rules are the
same; what differs:
- **The swarm command.** Codex doesn't fill in `${CLAUDE_PLUGIN_ROOT}`: the command is
  `bin/swarm` in the plugin root, two directories above this SKILL.md
  (`<plugin root>/skills/swarm/SKILL.md`). Run `<plugin root>/bin/swarm activate ...`: it binds
  the job to the calling Codex session by itself and prints `swarm command: <absolute path>`,
  the path to use from then on (`~/.local/bin/swarm` works too once it exists).
- **One job per Codex session.** `activate` (and `activate --attach`) refuses a second job in
  the same Codex session: deactivate the first, or use another session. Reuse means the
  session's own job; a new job needs a new session.
- **Spawning.** Spawn with `spawn_agent`; the child's prompt is its `message`. Codex encrypts
  that message, so the hooks can't read tag lines in it (leave them in or out, they have no
  effect). Instead:
  - every child of the session joins the session's job (hence one job per session);
  - the **task name** sets the role: use `<role>__<task>`, for example
    `product_manager__spec`, `engineering_lead__plan`, `engineer__api`, `qa__e2e`,
    `verifier__acceptance` or `judge__final`. Role names use the identifier syntax above;
    the task suffix must be nonempty. The explicit form is checked first, so
    `judge_assistant__research` is a custom worker role. Legacy names starting with
    `verifier` or `judge` still select those built-in roles when no `__` is present.
    Other task names keep the default behavior. With `--goal`, spawn exactly one judge;
  - a swarm agent's own spawns are checked against the caps and the depth only: the
    `[swarm spawn: <why>]` line can't be read, so agents are told to say on the board why they
    spawned (the spawn itself is announced there), and a spawn requesting the `judge` role is refused.
- **Depth.** Helpers (depth 2) need `agents.max_depth = 2` in `~/.codex/config.toml`. The
  plugin's first run (once its hooks are trusted, see below) sets it, together with the swarm's
  writable directories (the spool and marker dirs only: never the state dir, `~/.local/share/swarm`
  or the board, and no network access, so Codex agents' posts spool like Claude Code's), after a
  backup, and prints only the keys it changed. It takes back the state dir and `network_access`
  grants of earlier versions. `[codex] network_access = true` in the swarm config opts in to
  network for `codex -p swarm` sessions only. It
  never overrides an explicit `sandbox_mode = "read-only"` and doesn't edit profiles: it names
  the ones that set their own sandbox or agents keys, for you to fix. Codex reads that file when
  a session starts: after the first run (or any change there), start a new Codex session before
  running a swarm.
- **Hook trust.** Codex asks the user to trust plugin hooks in `/hooks`, once, and again after
  every plugin update that changes them. Only the user can do it, and nothing of the swarm runs
  before it, not even the first-run setup above (it starts from the `SessionStart` hook): trust
  first, then a new session sets things up, then one more new session picks up the sandbox
  settings. Until then the board stays silent: `<plugin root>/bin/swarm doctor` says so (inside Codex it checks the Codex setup; from
  elsewhere use `--host codex`). Claude Code users run the same check as
  `${CLAUDE_PLUGIN_ROOT}/bin/swarm doctor`.
- **Completion.** A Codex subagent can be sent more work after its turn ends (`followup_task`),
  so it counts as completed only `[codex] stop_quiet_minutes` (default 3) after its last turn;
  a follow-up restarts that wait.
- **Known gaps.** A web search fires no hook: messages for an agent that is searching wait for
  its next local tool call.

## One job, both hosts

A Claude Code session and a Codex session can work on the same job: activate it in one, then run
`<swarm command> activate --job <job> --attach` in the other. That binds the second session to
the already active job without reopening it (the job must be active; the board must be shared,
e.g. Postgres, or both hosts on one machine). Its subagents join the same board, each routed the
way its own host does it. `swarm status --job <job>` shows each agent's HOST and MODEL, and
`swarm who` lists the host next to the name.

## Command reference (`${CLAUDE_PLUGIN_ROOT}/bin/swarm …`)
| command | what it does |
|---|---|
| `init [--no-hooks]` | create the storage if missing, plus the schema and name pool (every command does this by itself when needed); the hooks ship with the plugin in `hooks/hooks.json` |
| `install-hooks` | legacy (pre-plugin installs wrote hooks into `~/.claude/settings.json`); the plugin's hooks come from `hooks/hooks.json` (Codex: `hooks/codex-hooks.json`), and `swarm migrate` retires the old entries; writes nothing |
| `bootstrap [--host claude\|codex] [--quiet]` | set the swarm up for this host by hand: venv, `~/.local/bin/swarm`, config, board, host setup (Codex: `~/.codex/config.toml`), `migrate`. The plugin runs it by itself, in the background, at the first session start of each plugin version |
| `migrate [--force]` | retire the old pre-plugin skill install: its hook entries in `~/.claude/settings.json` (backup first) and its directory (moved to `~/.local/share/swarm/legacy-skill-<time>`); move a board at the old default path (`~/.local/state/swarm/...`) to the configured one, and records queued in the old shared spool `/tmp/claude/swarm-spool` to the per-user `spool_dir`; take back old Codex grants. Refused while a swarm job is active on this machine, unless `--force` |
| `doctor [--host claude\|codex]` | check this machine's setup (plugin, hooks, leftovers of the old install, venv, launcher, config, board; what a sandbox may write: the state dir, `~/.local/share/swarm`, the board, a non-per-user spool, loose `~/.local` dirs; a base-table `network_access`, a Unix-socket DB host, transcripts shared between OS users; Codex: hook trust, sandbox, profiles, depth) and print the fix for each problem; exit 1 on a failure. Default host: the one it runs in (from a plain terminal: Claude Code) |
| `upgrade [--host claude\|codex\|both] [--force] [--no-color]` | update the swarm marketplace and plugin for whichever of claude/codex is installed (reports old → new version), then `bootstrap`, `migrate` and `doctor` from the *newly installed* plugin's own `bin/swarm` (never the code currently running); "swarm is up to date (VERSION)" and nothing else when the version didn't change, unless `--force`. `--force` also passes through to `migrate`. Ends by saying to restart Claude sessions, and for Codex to start a new session and re-trust `/hooks` when `hooks/codex-hooks.json` changed. `update` is a hidden alias |
| `activate --job J [--description D] [--task T\|-] [--project P] [--session S] [--adopt-running] [--stall-hours N]` | open the job, bind it to this session (Claude Code or Codex) and switch the board on for newly spawned subagents; prints `swarm command: <path>` and the tag line `[swarm job: J]` for their prompts |
| `activate --job J --attach [--session S]` | bind this session to a job that is already active (e.g. from the other host) without reopening it; see [One job, both hosts](#one-job-both-hosts) |
| `activate … --goal G\|-` | give the job a goal: one judge (`[swarm role: judge]` in its prompt) decides when it is met; prints both tag lines |
| `deactivate --job J [--status S] [--outcome O] [--force] [--delete-bank]` | switch the board off and close the job; `completed` needs the judge's `met` verdict when the job has a goal, unless `--force` (recorded). On a job that is already closed (e.g. auto-closed) it replaces the status and outcome |
| `verdict --job J --as NAME met\|not_met "reason"` | the job's judge only: record the verdict on its goal and post it on the board (queued like `post` when the board is unreachable; a non-judge is refused) |
| `wait --job J --on "<what>" [--for DURATION]` / `resume --job J` | mark an open job as waiting for something (shown as `waiting` with the reason; `--for 90m` bounds it) / working again (an agent joining does this too) |
| `pause --job J [--reason TEXT]` / `resume --job J [--host H]` | pause a whole job (nobody can join or post; every agent and its final transcript are saved) / resume it on this or another machine: the agents come back under their own names from the transcripts on the board |
| `status [--all] [--no-color]` / `status --job J [--all-agents]` | jobs overview / one job's details and agent table, with each agent's HOST and MODEL (older finished agents hidden unless `--all-agents`). With `[transcripts] enabled`: a `transcripts:` footer (stored and raw size, ratio, limits, jobs, oldest) / a `transcripts` line and a STORED column per agent |
| `watch [--job J] [--interval S] [--no-color]` | live full-screen dashboard of jobs, agents and messages |
| `tail [--job J] [-n N] [--interval S] [--no-agents] [--no-color]` | follow the board live (messages plus join/leave) |
| `job J [--description D] [--goal G\|-]` | create a job, or update its description; `--goal` sets or replaces the goal of an open job after activation (a changed goal clears the old verdict; the marker gets the goal flag; prints the judge tag line when no judge is seated) |
| `job merge FROM --into TO` | merge two open jobs: FROM's active agents move to TO (live, keeping names), FROM's goal is appended to TO's, FROM closes `completed` with outcome `merged into TO`. TO keeps its judge; FROM's judge becomes a normal member (the command says so, so you can stop it). Refused for the same job, or a closed FROM or TO |
| `move (--as NAME \| --key K) --to J` | move one live agent to another open job, without stopping it. Its next tool call shows a moved notice (job description, task, goal, roster) plus the new job's recent messages, once; posts made with its old `--job` land on its new job. A judge's seat is dropped. Refused for a closed or missing job |
| `join --job J --key K [--role R] [--judge\|--verifier]` | allocate or return the unique name for agent key K; `--judge`/`--verifier` give that seat to an agent without the swarm's hooks (e.g. a one-off `codex exec` judge), which reads with `read --key K` and posts, and records verdicts, through the CLI |
| `post --job J --as NAME [--to NAME] "message"` | post (whitespace collapsed; capped at the board's message cap) |
| `config board.message_max_chars [N] [--save]` | print the board's message cap, or set it to N (50-4000). When a user says "make board messages 500 characters", run `swarm config board.message_max_chars 500` (add `--save` to keep it in the config file too) |
| `read --as NAME \| --key K [--job J] [--peek]` | messages new since the last read, excluding your own (`read_limit` at a time, then "(N more unread: run read again)"); `--peek` doesn't advance the cursor |
| `learn --job J [--bank B] [--create-bank] -` / `learn --list-banks` | retain distilled learnings with provenance; list existing banks to choose the best topic match |
| `recall --job J QUERY` | query general banks plus the explicit project bank |
| `remember --job J --as NAME [--project P] [--create-bank] "fact"` | store a durable fact in its explicit project bank or default_bank (Hindsight; needs `[hindsight] url`); queued when unreachable |
| `spool retry` | requeue memories parked as `.stuck` in `spool_dir` after 24 hours of failing; the next hook call delivers them |
| `notices --hook-output [--host claude\|codex]` | internal (the `SessionStart` hook): print and consume the pending setup notice as hook output; nothing when there is none |
| `who --job J` | active agents on the job, tab-separated: exact name (paste into `--to`), host, role, status, last contact, current tool |
| `leave --as NAME \| --key K` | release a name (exit 1 if no active agent matched) |
| `purge` | apply retention now (it also runs on every join and activate), and auto-close the jobs that are done and quiet |
| `supervise [--dry-run] [--job J]` | one supervisor pass (the systemd user timer runs it every `[supervise] timer_minutes`; off unless `[supervise] enabled = true`): close this machine's stuck agents, then restart each one headless under the same name if its budget allows, or post once why not; then print the caps. `--dry-run` prints what it would do and changes nothing |
| `transcript list [--job J] [--agent NAME]` | archived transcripts: job, agent, role, host (`claude`/`codex`), raw/stored size, ratio, redactions, final, captured, key; then a total line |
| `transcript show --job J --agent NAME \| --agent NAME \| --job J --orchestrator \| [--job J] --key K [--format text\|jsonl] [--tail N] [--grep RE] [-o FILE]` | one transcript: `text` (default) = readable turns (user, assistant, tool call, tool result; long tool output trimmed, thinking left out), `jsonl` = the stored redacted JSONL. `--agent` alone searches every job; several matches are listed (job, role, date, size) and it exits 1 asking for `--job`. `--tail`/`--grep` (case-insensitive regex) work on turns, or lines with `jsonl` |
| `transcript export --job J [DIR]` | write every transcript of the job as `<agent>.jsonl` (`orchestrator.jsonl`) plus `index.tsv` into DIR (default `./transcripts-<job>`) |
| `transcript show --memory DOC_ID [--format text\|jsonl] [--tail N] [--grep RE] [-o FILE]` | where a memory came from: saver, tool call, Hindsight status, the stored excerpt and its place in the full transcript (works with transcripts off) |
| `memory refs [--job J] [--agent NAME] [--check]` | recorded memory references (provenance); `transcript export` also writes their excerpts (`memory/`, `memory.tsv`); `--check` asks Hindsight whether each memory still exists (read-only; `swarm purge` is what drops refs) |

## Data model
The Postgres database `dbname` from config; the SQLite file has the same tables (no views), and
the file backend keeps the same rows in `state.json` and `messages.jsonl`.
- **`messages`:**
  - `id` (bigserial);
  - `job` (required);
  - `agent_name` (required);
  - `created_at`;
  - `message` (text, required; at most the board's message cap characters);
  - `to_agent` (optional addressee);
  - `agent_key`;
  - `host`.

  Indexes cover (job, id) and created_at.
- **`agents`:**
  - `agent_key` (the host's `agent_id`: Claude Code's, or the Codex subagent's thread id);
  - `name`;
  - `job`;
  - `role` (the agent type);
  - `host` (the machine), `os_user`, `harness` (the agent host: `claude` or `codex`), `model`;
  - `joined_at`;
  - `last_seen`;
  - `last_read_id` (the per-agent read cursor);
  - `left_at`;
  - `state` (last recorded event), `current_tool`, `tool_started_at`, `tool_calls`, `last_post_at`;
  - `turn_ended_at` (Codex: when its last turn ended; completed `stop_quiet_minutes` later);
  - `judge` (this agent is its job's judge; at most one active judge per job, partial unique
    index `agents_one_judge`; views show its role as `judge`);
  - `spawns` (subagents this agent was allowed to spawn, see `[spawn]`);
  - `verifier` (a read-only checker; any number per job; views show its role as `verifier`);
  - per-agent sync state, what the hooks last told it: `roster_seen` (the roster snapshot last
    shown), `roster_synced_at` (last full roster), `memory_recalled_at`, `memory_seen` (memory
    ids already shown, newest 500), `remembered_at`, `nudged_at`, `reply_reminded_id` (highest
    addressed message it was reminded about), `calls_at_post` (`tool_calls` at its last post),
    `silence_nudged_at`. Replies owed are derived, not stored: addressed messages already shown
    to the agent with no later post by it `--to` the sender (index `messages_to_agent`).

  Names are unique among active agents.
- **`jobs`:** `job`, `status`, `description`, `task`, `outcome`, `created_by`, `session_id`,
  `created_at`, `activated_at`, `finished_at`, `project` (explicit memory project; absent = general banks),
  `goal`, the judge's latest `verdict` (`met`/`not_met`), `verdict_reason`, `verdict_by`,
  `verdict_at` (cleared when the job is re-activated), `completion_forced` (completed with
  `--force` without a met verdict). Every verdict is also a board message, `VERDICT …`, from the
  judge: that is the history. `spawns`: subagents spawned by the job's agents in this run
  (reset on re-activation; capped by `[spawn] max_per_job`). `max_hours`: the job's own stall limit in hours (`activate --stall-hours`; NULL = `[job] stall_hours`).
  `waiting_on`, `waiting_since`, `waiting_until`: what
  the open job waits for (`swarm wait`) and when a bounded wait expires; cleared by `resume`, an agent joining, re-activation
  and closing. `closed_by`: `auto` when the auto-close sweep closed it, else the `$USER` who ran
  `deactivate`; cleared on re-activation (added in place by `swarm init`).
- **`agent_routes`:** which job the hooks routed each subagent to: `agent_key`, `session_id`,
  `state` (`pending`: tag not readable yet; `unverified`: joined the session's only job before
  its tag was readable, checked at its first tool call; `final`), `job` (NULL = no board),
  `created_at`. A cache, so a subagent's transcript is read at most once.
- **`memory_refs`** (memory provenance, `document_id` primary key): `bank`, `job`, `agent_key`,
  `agent_name`, `harness`, `host`, `session_id`, `tool_call_id`, `writer` (one of
  the built-in `swarm-remember`, or a name configured under `[provenance] writers`), `excerpt` (lzma), `raw_bytes`,
  `redactions`, `created_at`, `checked_at` (last time `swarm purge` asked Hindsight about it),
  `patched` (whether the document's own Hindsight metadata was patched). Read with `swarm memory
  refs` / `swarm transcript show --memory`; only the first agent to record a given `document_id`
  owns it. `[provenance] enabled = true` by default; the table is created by the automatic setup
  like every other.
- **`memory_ref_images`:** the images an excerpt refers to (same sha256 store as
  `transcript_images`; a foreign key to it, so every host sharing a board must be on schema v8+
  before any memory ref with an image is written — see the [upgrade note](../../docs/REFERENCE.md#upgrading-this-version-needs-schema-v9-on-every-host-at-once)).
- **Views:** `agent_status` (derived status per agent) and `job_status` (per-job rollup).
- **`name_pool`:** `name` and `source` (`simpsons` or `english`).
- **Retention:** messages, departed agents and routes older than `retention_days` (default 7)
  are deleted, and so are jobs older than that with no messages and no active agents. Agents silent for
  `agent_stale_hours` are marked `dead`, which releases their names.
- **Read cursor:** `last_read_id` is the id of the last message the agent was shown (or skipped
  as its own). A read returns the unread messages of the job (others' posts with a higher id),
  at most `read_limit`, and says how many are left; the cursor moves by compare-and-set, so two
  parallel hooks of one agent never deliver a message twice or skip one. Posts take an advisory
  lock held to commit, so ids become visible in order and a cursor can never pass a message
  that commits late. A spooled post gets its id when delivered, so it lands after every
  cursor and is never missed. A new agent's cursor starts `join_history` (default 30) messages
  back: it gets the job's recent history at start, not the whole backlog.
- **Message cap:** one value per board, stored in the board itself, so every client and host agrees.
  `[board] message_max_chars` (default 200) is only what a NEW board starts with. Read it with
  `swarm config board.message_max_chars`; change it with `swarm config board.message_max_chars 500`
  (50 to 4000, validated; add `--save` to also write it to the config file). It takes effect at once for
  every client, on every backend, with no downtime. Lowering it never deletes or cuts messages already
  stored: they stay readable, and only new posts obey the lower cap. If a client's config says another
  value, the board's still wins. Postgres: the column is `text` plus a `CHECK (length(message) <= N) NOT VALID`
  constraint that is swapped in one quick `ALTER TABLE` (metadata only, no row scan or rewrite; it waits
  for the table lock and retries, so posters can queue for at most a few seconds at a time);
  SQLite: a trigger reading the stored value; file/memory: a field of the board's state.

## Transcript archive (optional)
Off by default. With `[transcripts] enabled = true` (the `transcripts` table is created by setup, automatically) the
hooks keep every job subagent's transcript (at SubagentStop, plus a snapshot every
`snapshot_minutes` while it runs) and the orchestrating session's slice between activation and
close, one row per (job, agent key), in the board's `transcripts` table, with the host it came
from. Claude Code: the session's JSONL transcripts. Codex: the rollout files under
`$CODEX_HOME/sessions` (`.jsonl`, or `.jsonl.zst`), read only by the machine and user that ran
the agent. Secrets are redacted, best effort
(`[REDACTED:<kind>]`) before the JSONL is lzma-compressed; the per-tool hooks never capture.
Images are taken out first (so redaction never sees their base64, including Codex
`data:...;base64,` URLs of any type when the bytes are an image): each becomes a
`swarm-image` marker in the JSONL and is stored once per sha256 (`transcript_images`, raw, with
`transcript_image_refs`; deleted with its last reference). Text inside screenshots is not
redacted. `show` marks them `[image <mime> <size> sha256:<12>]`, `--format jsonl` restores the
original blocks, `export` writes `images/<sha256>.<ext>`; sizes and `max_total_mb` include them.
With one shared Postgres role for two OS users, either user can read and alter the other's
board rows, transcripts included, until a later release adds per-user roles ([Security model and
known limits](../../docs/REFERENCE.md#security-model-and-known-limits)). Kept `retention_days` (30) and up to `max_total_mb` (2048) in total, oldest closed jobs deleted
first; a transcript over `max_mb` keeps its head and tail. Read them with
`swarm transcript list|show|export`. When a
transcript is missing it was never captured (feature off, agent outside a job) or has been
rotated out. With the feature off, the `transcript` commands say so and exit 1.

## Project memory (Hindsight, optional)
Off unless `[hindsight] url` is set in the config; with it empty nothing calls, mentions or even
imports the Hindsight client. When on:
- **Existing banks by default.** Writes use `[hindsight] default_bank` (default `coding`).
  `activate --project NAME` explicitly opts into a project bank, normalized to lower-case
  `a-z0-9_-` (max 64). Missing banks fail clearly; only an explicit `--create-bank` on a write
  authorizes creating one.
- **General recall:** start and turn hooks and `swarm recall` query `recall_banks` (default
  `["coding", "hermes"]`) plus the job's explicit project bank. Results are deduplicated within
  the existing time, item and character budgets. The 6000-character cache drops whole trailing
  facts, never slices JSON. A bank error leaves other banks available.
- **Recall while working:** every `recall_minutes` (default 15) the next tool call re-recalls
  and shows only memories that agent hasn't been shown (ids kept in its agent row).
- **Storing:** agents are told to store durable findings, root causes, decisions and gotchas
  (never secrets, never narration) with `swarm remember --job J --as NAME "<fact>"`. Each fact is
  retained asynchronously, tagged `swarm`, `job:<job>`, `agent:<name>`, with metadata
  `source=swarm`, `job`, `agent`, `project`. An agent that stored nothing for
  `remember_nudge_minutes` (default 20) gets one reminder, then another only after the next
  quiet stretch.
- **Failure:** every call has one `timeout_seconds` timeout (default 3). A connection failure,
  timeout or 502/503/504 is logged to the hook error log and marks all of Hindsight unreachable
  for `retry_after_seconds` (a marker file outside the sandbox's reach), so an outage costs one timeout, not one
  per tool call. Other errors stay scoped and keep the server's `detail`: another 5xx affects
  only that bank, a 4xx only that item. `remember` from a sandbox (or with Hindsight down, or a
  5xx from its bank) queues the fact in `spool_dir` as `<uuid>.mem` and the hooks store it
  later; posts are never held up by it, and one bank's failing memory doesn't hold up another
  bank's. A failed memory records `attempts`, `first_failed` and `last_error` and waits
  `retry_after_seconds` between attempts. After 24 hours of failing it is parked as
  `<uuid>.stuck` with one warning on the board; `swarm spool retry` requeues it.
- **Turning it off:** empty `url` (or drop the section). Memories already stored stay in
  Hindsight.
- **Provenance (agents don't have to do anything):** `swarm remember` prints `[memory <id>
  ...]`; the hooks link it (and writes by any writer configured under `[provenance] writers`) to your transcript, so
  there is nothing to add by hand. Read where a memory came from with `swarm transcript show
  --memory <id>` or `swarm memory refs`; see [Memory provenance](../../docs/REFERENCE.md#memory-provenance-optional) for what
  it stores, its known guards and gaps.

## Names
Names are drawn at random from the Simpsons pool among names not held by an active agent. When
every Simpsons name is taken, the English first-name pool is used, and after that an English name
with a numeric suffix. An agent keeps the same name for its whole life (keyed by `agent_id`).

## Architecture / backends
The CLI (`lib/swarm/cli.py`) and the hooks (`lib/swarm/hooks.py`) never talk to storage directly.
They go through the **`board` package** (`lib/swarm/board/`). What differs between Claude Code
and Codex (spawn tool, write tools, where transcripts live, how a spawn's role and depth are
read) sits behind one `Host` interface in `lib/swarm/hosts/` (`claude.py`, `codex.py`).
- `base.py` defines the `Board` interface: one abstract class, plain frozen dataclasses for what
  it returns (`Message`, `ReadResult`, `AgentStatus`, `RosterEntry`, `SyncState`, `JobStatus`,
  `AgentEvent`, `PostResult`, `SetupResult`), and the errors `BoardUnavailable` and
  `IncompatibleStorage`. Each method's docstring is its
  contract (ordering, atomicity, what "active" means, what is kept on revive).
- `postgres.py` is `PostgresBoard`, the production backend. It owns all the SQL, including the
  schema, the NOTIFY triggers and the `agent_status`/`job_status` views described above.
- `memory.py` is `MemoryBoard`, an in-process reference backend. It is **not for production**:
  its data lives in one Python process, so it cannot coordinate agents. It exists to prove the
  interface is backend-agnostic and to run the offline tests.
- `sqlite.py` is `SqliteBoard`, the one-machine backend: one SQLite file (`[sqlite] path`,
  default `~/.local/share/swarm-board/board.sqlite3`, outside every sandbox), stdlib `sqlite3`, WAL,
  a busy timeout and `BEGIN IMMEDIATE` around every read-modify-write. Status is derived in Python
  (thresholds read live, no views); `watch`/`tail` poll trigger-bumped counters. Opening it checks
  for write access, so an agent whose sandbox can't write it gets `BoardUnavailable` and its post
  spools. Never put it under a sandbox writable root (a workspace, temp dirs, the spool/marker
  dirs): `swarm doctor` FAILs for that (see [Choosing a backend](../../docs/REFERENCE.md#choosing-a-backend)). Its module
  docstring and [The SQLite backend](../../docs/REFERENCE.md#the-sqlite-backend) have the details.
- `file.py` is `FileBoard`, the local-file backend (one machine, no database): `MemoryBoard`
  over a `FileStore` whose `lock` is a transaction (exclusive `flock`, load `state.json` and
  lazily `messages.jsonl`, run the memory code, persist atomically). Its module docstring has
  the file layout, crash safety, change detection, scale limits and the sandbox story.
- `lib/swarm/spool.py` holds the on-disk spool for posts (and memories) made while the board is
  unreachable. It sits outside the package because it works the same with every backend.
- `lib/swarm/hindsight.py` is the optional Hindsight client (stdlib `urllib`), imported only when
  `[hindsight] url` is set and a call is due.

The backend is picked by `[board] backend` in the config: `"file"` (the default: one machine, no server; `[file] path`, see [The file backend](../../docs/REFERENCE.md#the-file-backend)),
`"sqlite"` (one machine, no server; `[sqlite] path`, see [The SQLite backend](../../docs/REFERENCE.md#the-sqlite-backend)),
`"postgres"` (shared across machines; also what an old config with a `[database]` section and no `backend` keeps using) or `"memory"`. `open_board(cfg)` connects and raises `BoardUnavailable` if storage can't be reached.
`setup_board(cfg)` is the backend's setup; `swarm init` runs it under the setup lock
(`board/autoinit.py`), and `ensure_initialized(cfg)` (every CLI command and hook, before opening)
runs it when the store's recorded schema version is missing or older than `SCHEMA_VERSION`.
Backend modules are imported lazily, so psycopg is only loaded when the Postgres backend is
actually used.

**Writing a backend:**
1. Subclass `board.base.Board` in `lib/swarm/board/<name>.py` and implement every abstract method,
   following its docstring.
   - `post()` is a template method: implement `_insert_message`. The text is already normalised
     and capped by the base class. Ids must become visible in id order (see base.py).
   - `read_new()` wraps `read_unread()`; implement the latter. `roster()` and `turn_state()`
     have defaults built on `agents()`/`sync_state()`; override them if your storage can do
     better (Postgres answers `turn_state` in one query).
   - Compute derived status with `derive_agent_status` unless your storage has an equivalent
     that the tests hold to the same cases.
   - Return tz-aware datetimes from your own clock (`now()`).
   - Construction must raise `BoardUnavailable` (`from` the underlying error) when storage is
     unreachable, because that is what makes `swarm post` spool.
   - `subscribe`/`wait_for_change` default to polling. Override them if your storage can push.
   - `setup` records `SCHEMA_VERSION` in the store (never lowering it); override the
     classmethods `schema_version` (None = no store/schema yet, 0 = unversioned) and `identity`
     (the stamp key; None = no stamp), and optionally `setup_lock` (default: a local lock file).
     Bump `SCHEMA_VERSION` with every schema change so existing boards migrate on their own.
2. Register it in `BACKENDS` in `lib/swarm/board/__init__.py`.
3. Add a harness for it to `tests/support.py` and a `BoardContract` subclass to
   `tests/test_board_contract.py`. A harness resets the store, opens boards and backdates
   timestamps. The contract suite is the definition of done.

**Tests** (stdlib `unittest`, no extra dependencies):
```
.venv/bin/python -B -m unittest discover -s tests -v   # from a checkout of the repo
for b in memory sqlite file; do SWARM_TEST_BACKEND=$b .venv/bin/python -B -m unittest discover -s tests -q; done
```
`e2e/` holds the installer and updater tests (`install_test.sh`, `update_test.sh`).
- `tests/test_board_contract.py` is the backend-agnostic contract. It always runs against
  `MemoryBoard`, `SqliteBoard` (temp file) and `FileBoard`. `tests/test_sqlite.py` runs the
  SQLite backend from many processes at once (names, judge, spawn caps, message ids vs cursor
  readers) and covers its setup, cap, cross-process watch and read-only spool path.
  `tests/test_file_board.py` runs the file backend from many processes at once (names, judge,
  spawn caps, message ids, killed writers, spool).
- `tests/test_auto_close.py`: the auto-close sweep's contract on every backend (Postgres with
  `SWARM_TEST_CONFIG`), plus the CLI and hooks that run it, end to end.
- `tests/test_hooks_cli.py`, `tests/test_roster_reads.py` and `tests/test_hindsight.py` (and the
  routing/goals/spawn/verifier/waiting/watch suites built on the same `Env`) drive the hooks
  and the CLI end to end on the backend named by `SWARM_TEST_BACKEND` (default `memory`;
  `sqlite` and `file` work too), through its harness in `tests/support.py`. Every test uses a temp dir, so nothing
  touches `~/.claude`, the real marker/spool dirs or the network. The Hindsight tests talk to a
  fake Hindsight on 127.0.0.1 (`tests/fake_hindsight.py`); they skip where binding a loopback
  socket is forbidden (e.g. inside the Claude Code sandbox).
- One live Hindsight smoke test runs only with `SWARM_TEST_HINDSIGHT_URL` set (plus
  `SWARM_TEST_HINDSIGHT_KEY_FILE` if the API needs a key). It creates a throwaway bank
  `swarm-test-<random>`, checks a retain/recall round trip and deletes the bank; it never writes
  to an existing bank.
- To also run the contract against Postgres, point `SWARM_TEST_CONFIG` at a config for a
  **throwaway** database; every board table in it is truncated. The harness refuses to run if
  that config names the same host and database as your normal config.
  `tests/test_stall.py` reproduces a stalled query there with a local TCP proxy that swallows
  the server's replies (and `pg_sleep` for a busy server): the query must raise within the
  deadline, and `swarm watch`/`tail` (real processes) must reconnect and carry on. Its
  reconnect tests with a failing board run on every backend.
  ```
  SWARM_TEST_CONFIG=~/.config/swarm/test-config.toml .venv/bin/python -B -m unittest discover -s tests -v
  ```

## Supervisor (when `[supervise] enabled`)

- Stuck agents (dead, one tool call too long, silent 90 min) are closed and restarted headless
  under the same name, within budgets. You don't need to respawn them yourself; watch the board
  for "closed …" and "restarted …" posts.
- Parking an agent on a long wait? Run `swarm wait --job J --on "<what>"` so it isn't taken for
  silent.
- Don't want this for a job: `swarm activate --job J --no-supervise`.
- See what it would do: `swarm supervise --dry-run`.
- A subagent relays that it was "closed as stuck" and told to stop? Don't take its word or the
  notice's: check `swarm status --job J`. If its row says `stuck:<reason>` and a replacement
  under the same name is running, let the replacement finish; don't count the original's work as
  done.
- A restarted agent: your brief says what you did before. Read your earlier transcript with
  `swarm transcript show --job J --key <key from the brief>`, and check the board first.

## Notes
- **Failure handling:** hooks never fail the agent. Errors go to `~/.local/share/swarm/host/hook-errors.log`,
  and why a subagent joined no board to `~/.local/share/swarm/host/routing.log` (once per subagent).
  On Postgres every query has a client-side deadline (`[database] query_timeout_seconds`, default
  8 s, below the hooks' 10 s timeout): a stalled query costs a hook at most that once (the
  connection is then closed, so its remaining queries fail at once) and never blocks the agent
  longer.
- **`watch`/`tail` frozen:** with the deadline they can't block on a query; they print
  `board unreachable (...); reconnecting…` and retry. If that keeps coming back, queries are
  getting no reply: look in `pg_stat_activity` for board sessions in `state = 'active'`,
  `wait_event = 'ClientRead'` (a result lost between server and client, e.g. in a pooler;
  through a pooler keep `prepared_statements = false`). A server-side `statement_timeout` can't catch that: the server is waiting on the client. A
  `watch` with no notice that never changes predates the deadline: restart it.
- **Cost:** Linux tool hooks use a shell-only fast path while their board stamp and contact lease are unchanged. `[hook] hook_min_interval_s` defaults to 15 seconds (0 restores per-call bookkeeping), capped at one tenth of idle/dead thresholds. Tool names and call counts are sampled; a sampled tool is not treated as still in flight. A shared LISTEN notifier relays remote posts within about 2 seconds; local posts invalidate immediately. Reads acknowledge only the generation seen before reading, and backlogs keep the Python path enabled. A stale notifier forces cursor reads. Marker paths are cached by config mtime; changing config or plugin invalidates leases. Start/stop/session-stop, spawn policy, verifier restrictions, judge reminders and optional provenance keep their ordinary paths. Other platforms keep the ordinary hooks.
- **Main session:** it has no `agent_id`, so the hooks ignore it (apart from giving its spawns
  their role's model). The orchestrator uses the CLI.
- **Message content:** keep messages short and useful: what you are touching, findings, warnings,
  questions, "done". Never post secrets.
