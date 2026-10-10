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

You need Claude Code and/or Codex, Python 3.11+, and git. On Linux or macOS, install
for your user and every detected host:

```sh
curl -fsSL https://raw.githubusercontent.com/fcarucci/Swarm/main/install.sh | bash
```

The installer uses the newest release, sets up the CLI, and checks the installation.
To install a specific version, append `-s -- --ref v0.2.0` to `bash` once that tag exists.

Or install through each host's plugin commands:

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

Start a new Claude session. In Codex, trust the plugin in `/hooks`, start a new session
for setup, then another to load its configuration. Run `swarm doctor` to check setup.

Windows (PowerShell): `irm https://raw.githubusercontent.com/fcarucci/Swarm/main/install.ps1 -OutFile install.ps1; .\install.ps1`.
See [Windows setup and limits](docs/REFERENCE.md#windows).

[Installation details](docs/REFERENCE.md#install) cover all-users mode, channels,
flags, and migration from the old skill install.

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

## Codex directory edition

OpenAI's public ChatGPT/Codex plugin directory does not accept plugins with lifecycle hooks, and
Swarm's full edition is built on them. So the repository also builds a hook-free **Codex directory
edition**, packaged from this tree (never a separate branch):

```sh
scripts/build-codex-directory        # writes dist/swarm-codex-<version>.zip
```

The ZIP has no `hooks/` files and no `hooks` key, no Claude-only files (`.claude-plugin/`), no tests or
installers, a generated manifest (`swarm-team`, "Swarm Team for Codex") and its own README and
icons (`cdx/`), and an `EDITION` file that makes the CLI describe an edition without hooks (no `hooks.py` either). Its skills (`SKILL.cdx.md`
files, swapped in for `SKILL.md` by the build; the full edition's skill text is never changed) tell
agents to run `swarm join`, `swarm read` and `swarm post` themselves. It includes the `swarm`,
`ask-answer`, `complexity-analyzer` and `refactoring` skills.

Compared with the full edition it lacks automatic naming, delivery of new board messages before each
tool call, the enforced judge and verifier stop gate (the skills ask agents to follow it), tracking of
agents' background commands, automatic recovery of stuck agents, and the `engineering-team` and `ci`
skills. Setup is explicit: the user runs `swarm init` (see the edition's [README](cdx/README.md)); nothing
installs or changes a Codex setting by itself.

To get the full edition, install it from this repository's own marketplace:
`codex plugin marketplace add https://github.com/fcarucci/Swarm.git`, then install Swarm from the
`/plugins` browser and trust its hooks in `/hooks`.

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

## Reference and changes

[Full reference](docs/REFERENCE.md): commands, configuration, hosts, roles, and security limits.
[CHANGELOG](CHANGELOG.md): changes by release.

Apache-2.0, © Francesco Carucci. See [LICENSE](LICENSE) and retain [NOTICE](NOTICE).
