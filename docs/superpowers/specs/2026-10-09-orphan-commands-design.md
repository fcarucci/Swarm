# Orphaned background commands and CI waits (swarm-orphans)

Incident: an engineer waited for CI with `gh run watch <id> --interval 60` in a background Bash
call. After it handed back, its background command kept its agent "running" in the
orchestrating Claude Code session for about 14 h, long after the job was closed. Nothing
steered it to `swarm ci wait`, and nothing noticed the command outliving its agent.

## A. Steer CI waits to `swarm ci wait`

PreToolUse, members only (agents with a board row; the orchestrator is never gated), on both
hosts (it is a plain deny). `swarm.shellguard.ci_wait(command)` takes the command line apart
best effort, in the shellguard style. Text in single quotes and plain text in double quotes is
data, but `$(...)` and backticks inside double quotes are code (`while [ "$(gh run view ...)" ...`).
It splits into simple commands on `; & | && || ( ) { } $( \`` and newlines, and drops prefixes
(`VAR=x`, `env`, `sudo`, `nohup`, `time`, `timeout N`, `command`, `exec`, `do`, `then`, `else`,
`watch -n N`). It refuses:

- `gh run watch` (anywhere);
- `gh pr checks ... --watch`;
- a poll inside a loop (between `while`/`until`/`for`/`select` and its `done`, the loop condition
  included, with `!` stripped), or under the `watch` program; a loop elsewhere on the line, a
  poll piped into a loop, and here-document bodies do not count.
  A poll is `gh run view`, `gh run list`, `gh api <path>` where the path names CI runs
  (`/runs`, `/check-runs`, `/check-suites`, `/status`, `/statuses`, `actions/`), or curl/wget
  of such a URL (Gitea's `/api/v1/.../actions/...` and commit status).

A one-shot `gh run view <id> --json conclusion` stays allowed: judges use it to verify. The
refusal gives the exact replacement: `swarm ci wait --repo R --sha S`, plus `swarm ci status`
for one look. R comes from the command's `-R/--repo`, else the cwd's git remote. S comes from
the cwd's HEAD. When either can't be found, it is shown as a placeholder. If the `ci` plugin is
not loaded (absent or disabled), the message says so and does not name a missing command. The
shell fast path in bin/swarm-hook can skip Python for a leased worker, so it now always takes
the Python path for payloads that mention `gh`, `/runs`, `actions/` or `run_in_background`.

## B. Track background commands per agent

Verified on Claude Code 2.1.295 with a logging hook (`claude -p`, Bash with
`run_in_background: true`): PreToolUse gets `tool_input = {command, description,
run_in_background: true}`. An `updatedInput` with a new `command` is honoured for background
calls: the rewritten command ran and its output landed in the task's output file. PostToolUse
then gets `tool_response.backgroundTaskId`.

So on Claude Code, a member's background Bash call is rewritten to:

    '<plugin>/bin/swarm' bg --job J --key AGENT_ID --as NAME -- '<original command>'

This composes with the existing `join --key` / `verdict --as` rewrite, and a call that is
already `swarm bg` is not wrapped again. `swarm bg` (the wrapper):

1. starts `bash -c <command>` (`/bin/sh` without bash) in a new process group (`setpgid`). Its
   environment carries `SWARM_BG_TAG=<random token>`, and it inherits cwd, environment and
   stdio, so the output still goes to the harness's task file. The environment is the caller's:
   bin/swarm records what it changes (`_SWARM_PRE_<VAR>`, `_SWARM_LAUNCHER`) and the wrapper
   restores it. The shell is fresh, so the agent shell's aliases and functions are not there;
2. reads the child's start time (/proc/<pid>/stat field 22, clock ticks since boot), then
   records the row on the board: job, agent key and name, the command (env assignments dropped,
   one line, at most 500 chars, credentials redacted), start time, host (node name), boot id
   plus pid-namespace inode, pid, pgid (= pid), start ticks, tag;
3. forwards TERM/INT/HUP/QUIT to the group, waits, records the exit code, and exits with it
   (128+N for a signal).

The command always runs: a board that can't be reached (a sandbox) leaves it unrecorded, with
one stderr line. The row is written after the child starts, so it never delays the command.

**Schema 24**, the `bg_commands` table on every backend, idempotent (`CREATE TABLE IF NOT
EXISTS`; file and memory stores keep a `bg_commands` list):

    id, job, agent_key, agent_name, command, started_at, host, boot, pid, pgid, proc_start,
    tag, ended_at, exit_code, outcome, detail

`outcome` is NULL while running, then `exited`, `reaped` (TERM sufficed), `killed` (KILL needed)
or `gone` (not running any more, found by reap: pid reused, rebooted, vanished). Rows are purged
with the board's retention, like restarts.

**Codex gap (documented, not faked):** Codex has no "background" flag at PreToolUse. A
long-running `exec_command`/`unified_exec` session is owned by the Codex process and is only
known to be long-running after it yields. Codex also doesn't take shell rewrites here
(`supports_shell_rewrite` is False). Codex background commands are not tracked; a Codex
agent can still run `swarm bg ...` itself. There is no harness-task-id fallback on Claude
Code either: the rewrite works there.

## C. Detect and stop orphans

A row is **running** while `ended_at` is NULL. It is **orphaned** when it is running and its
agent's row is completed, left or dead (or gone), or its job is no longer active or paused.

`swarm bg list [--job J] [--orphans]` and `swarm bg reap [--job J] [--agent KEY] [--grace S]
[--dry-run]`. Reap, for each orphaned row (or, with `--agent`, that agent's running rows once
the agent is finished):

- **Host check.** A row of another host is never touched. Same host but another boot id means
  the machine rebooted, so the row is marked `gone` and nothing is signalled. Same boot but
  another pid namespace is left alone.
- **Members.** The group's members are the processes whose pgid is the recorded one, whose
  start ticks are at least the recorded start, and whose environment holds the row's
  `SWARM_BG_TAG`. This is the supervisor runner's pattern for its replacement sessions, and
  it is reused from `swarm.supervisor.runner`. A reused pid or pgid has no such member, and
  other users' processes are unreadable, so they are never members. Each signal goes through
  a pidfd opened first and re-checked, so a pid recycled between the check and the signal
  can't receive it. With no pidfd support (or no /proc: macOS, Windows) nothing is signalled,
  and the row is reported as unverifiable.
- **Signals.** TERM to every member of every orphan. Wait up to 10 s for all to be gone. KILL
  whatever is left. Record `reaped`/`killed` with a detail ("2 processes, TERM"). A row with
  no members is recorded `gone`, with the leader's start-time mismatch noted when that is why.

It runs automatically from:

- **the supervisor pass**, every job's orphans on this host;
- **`swarm deactivate`**, that job's, inline;
- **auto-close** (`sweep_jobs`), the closed jobs'. It runs in a detached `swarm bg reap --job J`
  process, so no hook waits 10 s, and only when that job has running rows on this host;
- **SubagentStop**, that agent's own running rows. It runs in a detached `swarm bg reap --agent
  KEY --grace 30`, which waits 30 s and re-checks that the agent is still finished before
  signalling.

`swarm status --job J` and `swarm watch` show `background N running, M orphaned` when N > 0.

## D. Doctor

`orphaned bg commands`: WARN with the count and `swarm bg reap` as the fix when this host has
orphaned rows, OK otherwise. A board that can't be reached is left to the board check.

## Never

- Kill a process the wrapper did not record.
- Kill on another host, boot or pid namespace.
- Kill without a /proc start-time and tag match.
- Block a hook for the 10 s grace.

## Round 2: the forwarder's secret

`gh webhook forward` (cli/gh-webhook v0.2.0) takes the webhook secret only as `--secret VALUE`. Its
`--help` lists no file, env or stdin form, and its flags are plain pflag, which never reads the
environment.

**Threat model.** Three OS users share this box and its /proc (no `hidepid`).

- Every user can read every process's command line. A value in argv is a value published to all
  of them, so no secret may be in any argv.
- Every user can open a TCP connection to 127.0.0.1. Binding to loopback alone authenticates nobody.
- A URL path token would be in `--url`, which is argv again, so it is no better.
- A unix socket with peer credentials would be ideal, but gh-webhook only posts to an http:// URL.
- The listener's own OS user can already read `secret_file`. Same-uid processes are inside the
  trust boundary either way.

**Choice.** The forwarder runs with no secret, so GitHub signs nothing; the websocket to GitHub is
TLS-authenticated by gh's token. It posts to `/github-forward`, a *local route*
(`register_event_source(local_routes=...)`). The listener serves a local route only to a loopback
peer whose socket's owner uid, as the kernel's socket table (/proc/net/tcp, tcp6) records it, is
the listener's own uid. The table shows the client end of the connection (peer address and port
-> listener address and port). Another user cannot fake that uid column. On a host without
/proc the owner can't be told, so local routes always get 403 there. The signed `/github`
route keeps the HMAC check for a real repo webhook.

**Logging.** The listener logs only the source, the status and a count. The helper supervisor
logs the helper key and exit code, never argv. The health file has no argv. A test checks that
no helper argv or env contains the secret value.

## Round 3: what the local route accepts, and when CI is green

**Local route, tightened.** A request to a local route is refused before its body is read
unless all of these hold:

1. **No proxy headers** (`X-Forwarded-*`, `Forwarded`, `Via`, `X-Real-IP`, `CF-Connecting-IP`,
   `True-Client-IP`). A reverse proxy or tunnel running as the listener's own uid connects from
   loopback with that uid; it must never turn the route into a public unsigned endpoint.
2. **The right path token.** The path is `<route>/<token>`. The token is random for each listener
   start (`secrets.token_urlsafe(24)`) and is written to the supervisor's private dir (0600,
   `events-local-token`) before any helper starts. The ci plugin reads it to build the forwarder's
   `--url`. Local users can read the token in that argv, but item 3 stops them. A remote sender
   behind a proxy can't know it.
3. **An ESTABLISHED socket of this uid.** The client socket's row in /proc/net/tcp{,6} must be
   state 01, with a non-zero inode, owned by the listener's uid. A client that sends and closes
   at once leaves a FIN_WAIT/TIME_WAIT row with uid 0 and inode 0. That row is never read as an
   owner; otherwise a root listener would have accepted anyone.

The docs say never to put a proxy in front of local routes. A public repo webhook goes to the
signed `/github` route.

**CI-GREEN is confirmed.** `PushState.ci_state` only knows the runs whose completion webhooks
arrived. With two workflows, or several check suites, on one commit, the first success looked
like "all green". A success is now only a trigger. Before posting CI-GREEN or READY-TO-LAND, the
github source asks the CI host once through the shared `swarm ci` poller (`confirmer`: its cache
when that is final and came from the host, otherwise one forced call that ignores the 60 s
floor). The answer is one of:

- green: post the events;
- failed: post CI-FAILED;
- anything else: remember the sha (`unconfirmed`). The next completion for that commit, or the
  source's poll, confirms it later (`confirm_pending`).

CI-FAILED needs no confirmation: one failure fails the commit. `ci wait` trusts CI-GREEN events
because they are now confirmed at the source.
