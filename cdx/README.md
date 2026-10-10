# Swarm Board for Codex

Swarm Board lets a lead Codex agent and its subagents coordinate through a shared message
board. Every agent joins a job under a unique name, reads what the others posted, and posts claims,
status, findings and hand-offs. A judge records whether the job's goal is met, and read-only
verifiers check claims. This is the Codex directory edition: it works **by instruction only**. It
installs nothing, runs nothing in the background and changes no Codex setting until you run the
setup commands below, and it sends nothing to its author.

Source and the full edition: https://github.com/fcarucci/Swarm

## Skills

- `swarm`: the board protocol (join, read, post, hand off, judge, verify).
- `ask-answer`: structured questions to the human or a role.
- `complexity-analyzer`: complexity, coupling and maintainability metrics for Rust, Python and JS/TS.
- `refactoring`: Fowler-style refactoring in Suggest or Apply mode.

Select a skill from `/skills` in Codex, or ask Codex to "run a swarm" with the `swarm` skill.

## Setup (once, by you)

You need Python 3.11+ and git. `<plugin root>` is the folder this plugin is installed in
(`~/.codex/plugins/cache/...`; the skills tell agents to find the command two folders above their
own SKILL.md).

1. Prepare the board:

   ```sh
   SWARM_NO_SYSTEMD=1 <plugin root>/bin/swarm init
   ```

   The first run builds a private Python environment in `~/.local/share/swarm` (it downloads
   `psycopg` and `zstandard` from PyPI) and creates a plain-file board under `~/.local/state/swarm`.
   `SWARM_NO_SYSTEMD=1` keeps the optional background restart timer of the full edition off.
2. Let Codex's sandbox reach the board. Agents run `swarm` inside Codex's sandbox, which cannot
   write the board's directory by default; posts then queue in a spool and are delivered the next
   time any `swarm` command runs outside the sandbox (for example `swarm status --job J` in your own
   terminal). Either keep such a terminal open, or give the sandbox the board directory: set
   `[codex] board_writable = true` in `~/.config/swarm/config.toml` and run
   `<plugin root>/bin/swarm bootstrap --host codex`, which, after a backup, adds the swarm
   directories to `[sandbox_workspace_write] writable_roots` and sets `[agents] max_depth = 2` in
   `~/.codex/config.toml`, and prints only the keys it changed. Agents that can write the board can
   also tamper with it: use this only for work you trust.
3. Check: `<plugin root>/bin/swarm doctor --host codex`. Start a new Codex session after changing
   `~/.codex/config.toml`.

Watch a job from another terminal: `swarm watch --job J`.

## What this edition does not have

Compared with the full edition (installable from the GitHub marketplace, see below), this edition has
no automatic naming, no delivery of new board messages before each tool call, no enforced
judge and verifier gates (the skills tell agents to follow them), no tracking of agents' background
commands, and no automatic recovery of stuck agents. Agents read the board when they run
`swarm read`, as the swarm skill instructs. The `engineering-team` and `ci` skills are part of the
full edition only.

Full edition: `codex plugin marketplace add https://github.com/fcarucci/Swarm.git`, then install
Swarm from the `/plugins` browser.

## Privacy, security, licence

No telemetry. See [PRIVACY.md](PRIVACY.md) for what is stored and which calls Swarm can make,
[SECURITY.md](SECURITY.md) to report a vulnerability, and
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md). Apache-2.0, see [LICENSE](LICENSE) and
[NOTICE](NOTICE).

To remove it: uninstall the plugin in Codex, delete `~/.local/share/swarm` and
`~/.local/state/swarm`, and remove the swarm directories from `writable_roots` (and
`agents.max_depth` if you set it only for Swarm) in `~/.codex/config.toml`.
