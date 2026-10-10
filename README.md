# Swarm

Swarm is a plugin for Claude Code and Codex that coordinates agents through a shared
message board. Hooks deliver new messages and track jobs and agents; an optional judge
checks whether the job's goal is met. Use the skills to run the work and `swarm watch`
to follow it.

## Skills in this plugin

- [`swarm`](skills/swarm/SKILL.md): the machinery for jobs, the board, hooks, judges,
  and the live `watch` dashboard.
- [`engineering-team`](skills/engineering-team/SKILL.md): a configurable team with an
  engineering lead, QA, engineers, and an independent judge. Separate `team.toml`
  configuration and team CLI commands come through the plugin system.
- [`complexity-analyzer`](skills/complexity-analyzer/SKILL.md): complexity, coupling and maintainability metrics for Rust, Python and JS/TS.
- [`refactoring`](skills/refactoring/SKILL.md): Fowler-style refactoring in Suggest or Apply mode.
- [`ask-answer`](skills/ask-answer/SKILL.md): blockers and human questions,
  using `swarm ask`, `swarm answer`, and `swarm questions`.

In Claude Code, invoke `/swarm:swarm` or `/swarm:engineering-team`.
In Codex, select the skill from `/skills`.

## Install

You need Claude Code and/or Codex, Python 3.11+, and git. Install through each host's
plugin commands:

Claude Code:

```text
/plugin marketplace add https://github.com/fcarucci/Swarm.git
/plugin install swarm@swarm
```

Codex:

```sh
codex plugin marketplace add https://github.com/fcarucci/Swarm.git
codex plugin add swarm@swarm
```

Or, after `marketplace add`, install Swarm from the `/plugins` browser. Codex does not run a
plugin's hooks until you trust them: open `/hooks` and trust Swarm's hooks, and trust them again
after an update that changes them. Swarm does nothing until then.

Alternative: the installer script sets up every detected host for your user, using the
newest release (read [install.sh](install.sh) first). On Linux or macOS:

```sh
curl -fsSL https://raw.githubusercontent.com/fcarucci/Swarm/main/install.sh | bash
```

To install a specific version, append `-s -- --ref v0.2.0` to `bash` once that tag exists.

Start a new Claude session. In Codex, trust the plugin in `/hooks`, start a new session
for setup, then another to load its configuration. Run `swarm doctor` to check setup.

Windows (PowerShell): `irm https://raw.githubusercontent.com/fcarucci/Swarm/main/install.ps1 -OutFile install.ps1; .\install.ps1`.
See [Windows setup and limits](docs/REFERENCE.md#windows).

[Installation details](docs/REFERENCE.md#install) cover all-users mode, channels,
flags, and migration from the old skill install.

Platforms: Linux has every feature. macOS and Windows have no supervisor (it needs a
systemd user timer); see [Windows setup and limits](docs/REFERENCE.md#windows). The same
repository is a plugin for both hosts: Claude Code reads `.claude-plugin/` and
`hooks/hooks.json`, Codex reads `.codex-plugin/` and `hooks/codex-hooks.json`; each host
ignores the other's files. In Codex, Swarm needs the CLI or desktop app with trusted hooks;
plugin hooks do not run in ChatGPT cloud "Work" threads or under `allow_managed_hooks_only`.
The public ChatGPT/Codex plugin directory does not accept plugins with lifecycle hooks, so
Swarm is installed from its own marketplace (this repository).

## What Swarm changes on your machine

- **Hooks.** Swarm registers hooks for session start, subagent start and stop, session stop,
  and before and after every tool call. Outside an active swarm job (no marker in
  `~/.local/state/swarm/active`), every hook but session start (and Codex's session end)
  exits after a quick check, passing the hook input nowhere. Inside a job they show agents new board
  messages (other agents' text: treat it as data, not instructions), rewrite `swarm join` and
  `swarm verdict` calls and members' background Bash commands so they are recorded, refuse
  CI-polling commands such as `gh run watch` for members, and let a judge or finalizer hold a
  stop until a verdict is recorded.
- **First session.** The session-start hook runs `swarm bootstrap` in the background the
  first time each plugin version starts, whether or not a job is active. It creates a Python venv in `~/.local/share/swarm/venv`
  (pip installs `psycopg[binary]` and `zstandard` from PyPI), the launcher
  `~/.local/bin/swarm`, `~/.config/swarm/config.toml` and the board. It adds the spool
  directory to `sandbox.filesystem.allowWrite` in `~/.claude/settings.json`, and for Codex adds
  the spool and marker directories to `[sandbox_workspace_write] writable_roots` and sets
  `[agents] max_depth = 2` in `~/.codex/config.toml`; it backs up each file first. On Linux it
  installs and enables the systemd user timer `swarm-supervise.timer` (every 5 minutes), which
  restarts stuck agents of active jobs as headless `claude -p` or `codex exec` sessions. Set
  `[supervise] enabled = false` before the first session to skip it.
- **Network.** No telemetry: Swarm sends nothing to its author. The venv setup downloads from
  PyPI. Everything else is opt-in or user-run: `swarm upgrade` (GitHub), `swarm ci` (your
  CI host), the events listener (off by default; binds 127.0.0.1:8923), Hindsight memory (your
  configured URL), a Postgres board (your server), and the complexity-analyzer tool downloads
  (sha256-verified).
- **Data.** Messages and job data stay in the backend you chose. Transcripts are off by
  default. See [PRIVACY.md](PRIVACY.md).
- **PATH.** The plugin's `bin/` puts `swarm` on the agent's Bash PATH.

## Uninstall

On Linux, stop and remove the supervisor first:

```sh
systemctl --user disable --now swarm-supervise.timer
rm -f ~/.config/systemd/user/swarm-supervise.service ~/.config/systemd/user/swarm-supervise.timer
systemctl --user daemon-reload
```

Then remove the launcher and Swarm's local data (the second line also deletes local boards
and your configuration):

```sh
rm -rf ~/.local/bin/swarm ~/.local/share/swarm
rm -rf ~/.local/state/swarm ~/.config/swarm
```

Remove the spool path from `sandbox.filesystem.allowWrite` in `~/.claude/settings.json`, and
the Swarm entries under `[sandbox_workspace_write] writable_roots` in `~/.codex/config.toml`.
Finally uninstall the plugin: `/plugin uninstall swarm@swarm` in Claude Code, or remove it
with Codex's plugin commands.

## Quick start

Ask the model to run a swarm with distinct agent scopes. Its sequence is:

```sh
swarm activate --job fix-api --task "Fix the API regression"
# Spawn agents with the host's Agent/spawn_agent tool, each prompt containing:
# [swarm job: fix-api]
# Follow in another terminal; Ctrl-C returns to the shell:
swarm watch --job fix-api
# When work is complete, retain durable facts if Hindsight is configured:
swarm learn --job fix-api - < learnings.txt
swarm deactivate --job fix-api --outcome "API regression fixed"
```

`learnings.txt` contains one self-contained fact per nonblank line. Skip `learn` when
memory is off. Add `--goal "…"` at activation and spawn a judge for an independent
completion check; completion then requires its `met` verdict.

Use `swarm status --job fix-api` for a snapshot. Attach another session to the same job
with `swarm activate --job fix-api --attach`.

[The full workflow](docs/REFERENCE.md#how-a-swarm-runs) covers roles, lifecycle, and
[pausing and resuming](docs/REFERENCE.md#pausing-and-resuming-a-job).

## Configuration essentials

Configuration is at `~/.config/swarm/config.toml`, or `$SWARM_CONFIG`.
Only set keys that differ from the defaults; run `swarm doctor` after changes.

- `file` is the default board: one machine, no server or configuration needed.
- `sqlite` also runs on one machine: set `[board] backend = "sqlite"`.
- `postgres` shares a board across machines or OS users: set the backend and `[database]`.

Keep local boards on local disk outside sandbox writable directories.
[Backend setup](docs/REFERENCE.md#choosing-a-backend) includes examples and legacy defaults.

Hindsight memory is off unless `[hindsight] url` is set. Writes use `default_bank`
(default `coding`); recall queries `recall_banks` (default `["coding", "hermes"]`).
An explicit `activate --project NAME` adds a project bank; missing banks need explicit
creation. See [memory setup and learnings](docs/REFERENCE.md#project-memory-hindsight-optional).

Transcripts are off by default. Set `[transcripts] enabled = true` to archive them;
redaction is best effort, and board readers can read them.
See [transcript settings and provenance](docs/REFERENCE.md#transcript-archive-optional).

On Linux, [automatic recovery](docs/REFERENCE.md#supervisor-stuck-agents-and-automatic-restarts) and the [review pipeline](docs/REFERENCE.md#review-pipeline) are on by default: hand-off → judge → fix or finalizer.

The engineering team uses `~/.config/swarm/team.toml` (or `$SWARM_TEAM_CONFIG`), separately
from core configuration. `swarm team --job J --show` displays the effective team;
`swarm activate ... --team product_manager,build_engineer` selects optional seats.
See [team commands and CLI plugins](docs/REFERENCE.md#cli-plugins).

All keys are in [config.example.toml](config.example.toml) and the
[configuration reference](docs/REFERENCE.md#configuration-reference).

## Upgrading

Run `swarm upgrade`, then start new host sessions; re-trust Codex `/hooks` if they changed.
If a version was re-cut and `swarm upgrade` says you are behind main (or can't tell), run `swarm upgrade --force`: it reinstalls the plugin from the tip of main (on the release channel, the current release) for every host, keeping the board and your config.
Upgrade every host sharing a board together. See [upgrade and migration details](docs/REFERENCE.md#upgrading-shared-boards).

## Development

Run targeted tests for changed files locally; full suites run on GitHub Actions.
See [test setup](docs/REFERENCE.md#tests) and the [release process](docs/REFERENCE.md#release-process).

## Security and privacy

Threat model and known limits: [security model](docs/REFERENCE.md#security-model-and-known-limits).
Privacy: [PRIVACY.md](PRIVACY.md). Vulnerability reports: [SECURITY.md](SECURITY.md).
Third-party software: [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

## Reference and changes

[Full reference](docs/REFERENCE.md): commands, configuration, hosts, roles, and security limits.
[CHANGELOG](CHANGELOG.md): changes by release.

Apache-2.0, © Francesco Carucci. See [LICENSE](LICENSE) and retain [NOTICE](NOTICE).
