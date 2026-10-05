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
     same six events with `--host codex`), nothing is written to `~/.claude/settings.json`:
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
`${CLAUDE_PLUGIN_ROOT}/bin/swarm watch [--job J] [--interval 2]` is a full-screen live dashboard:
- the jobs table;
- the agents table for each active job (or just `--job J`, with its task): active agents, plus
  finished ones (completed/left/dead) that ended or were last seen within
  `watch_recent_minutes` (default 10); a dim line counts the older ones hidden;
- the latest messages in whatever height is left.

It redraws the moment an agent, job or message changes (on Postgres, triggers NOTIFY
`swarm_state` and `swarm_board`; SQLite and file boards are polled every 0.1 s), and at least every `--interval` seconds so idle and dead appear as time passes.
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
| `completed` | `SubagentStop` fired: the agent finished |
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
   jobs can share one project. Default: the job name.
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
   `swarm deactivate --job <job> [--status completed|cancelled|failed] [--outcome "<summary>"]`.
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
| `deactivate --job J [--status S] [--outcome O] [--force]` | switch the board off and close the job; `completed` needs the judge's `met` verdict when the job has a goal, unless `--force` (recorded). On a job that is already closed (e.g. auto-closed) it replaces the status and outcome |
| `verdict --job J --as NAME met\|not_met "reason"` | the job's judge only: record the verdict on its goal and post it on the board (queued like `post` when the board is unreachable; a non-judge is refused) |
| `wait --job J --on "<what>" [--for DURATION\|--until TIME]` / `resume --job J` | mark an open job as waiting for something (shown as `waiting` with the reason; `--for 90m` or `--until 17:30` bounds it, and a bounded wait that has not ended protects the job from auto-close) / working again (an agent joining does this too) |
| `pause --job J [--reason TEXT]` / `resume --job J [--host H]` | pause a whole job (nobody can join or post; every agent and its final transcript are saved) / resume it on this or another machine: the agents come back under their own names from the transcripts on the board |
| `status [--all] [--no-color]` / `status --job J [--all-agents]` | jobs overview / one job's details and agent table, with each agent's HOST and MODEL (older finished agents hidden unless `--all-agents`). With `[transcripts] enabled`: a `transcripts:` footer (stored and raw size, ratio, limits, jobs, oldest) / a `transcripts` line and a STORED column per agent |
| `watch [--job J] [--interval S] [--no-color]` | live full-screen dashboard of jobs, agents and messages |
| `tail [--job J] [-n N] [--interval S] [--no-agents] [--no-color]` | follow the board live (messages plus join/leave) |
| `job J [--description D] [--goal G\|-]` | create a job, or update its description; `--goal` sets or replaces the goal of an open job after activation (a changed goal clears the old verdict; the marker gets the goal flag; prints the judge tag line when no judge is seated) |
| `job merge FROM --into TO` | merge two open jobs: FROM's active agents move to TO (live, keeping names), FROM's goal is appended to TO's, FROM closes `completed` with outcome `merged into TO`. TO keeps its judge; FROM's judge becomes a normal member (the command says so, so you can stop it). Refused for the same job, or a closed FROM or TO |
| `move (--as NAME \| --key K) --to J` | move one live agent to another open job, without stopping it. Its next tool call shows a moved notice (job description, task, goal, roster) plus the new job's recent messages, once; posts made with its old `--job` land on its new job. A judge's seat is dropped. Refused for a closed or missing job |
| `join --job J --key K [--role R] [--judge\|--verifier]` | allocate or return the unique name for agent key K; `--judge`/`--verifier` give that seat to an agent without the swarm's hooks (e.g. a one-off `codex exec` judge), which reads with `read --key K` and posts, and records verdicts, through the CLI |
| `post --job J --as NAME [--to NAME\|@ROLE] "message"` | post (whitespace collapsed; capped at `message_max_chars`); `--to @EL\|@PM\|@QA\|@judge\|@<role>` goes to the current holders of that seat, and an unknown name, an empty seat or an author outside the job is refused |
| `config board.message_max_chars [N] [--save]` | print the board's message cap, or set it to N (50-4000). When a user says "make board messages 500 characters", run `swarm config board.message_max_chars 500` (add `--save` to keep it in the config file too) |
