# Claude host preflight: engineering-team-impl-20260929

**Date:** 2026-09-29 (16:44–16:46 local)
**Owner:** Krusty the Clown (board key `engineering-impl-claude-preflight`, role `project_manager`). This seat owns only this preflight. The PM is still Rabbi Hyman Krustofsky.
**Scope:** a source-backed Claude host preflight. It is not the behavioral fixture suite.
**Result:** **PASS**. Claude custom-role routing works from this source snapshot. Behavioral host evaluation has not run yet, so it is **PENDING**.

## Host and loaded plugin

| item | observed |
|---|---|
| Host | Claude Code `2.1.285` at `/home/claude/.local/share/claude/versions/2.1.285`, model `claude-opus-5-5` |
| Launch | `claude --print --permission-mode auto --model claude-opus-5-5 --plugin-dir /home/claude/sessions/engineering-team-impl-20260929-claude-preflight ...` |
| Loaded plugin root | `/home/claude/sessions/engineering-team-impl-20260929-claude-preflight` (this snapshot) |
| Plugin manifest | `swarm` `0.1.0` (`.claude-plugin/plugin.json`) |
| Source revision | The snapshot is not a git checkout and has no revision stamp. The brief names `b9f8657` on `feature/engineering-team` as its base. The snapshot contains `lib/swarm/roles.py` (custom roles). Identity is pinned by the hashes below. |

How I know the snapshot root is the one loaded:
- `PATH` includes `<snapshot>/bin`.
- The skill list has `swarm:engineering-team`. Only this snapshot ships that skill; the installed cache has only `skills/swarm`.
- The SessionStart hook stamp `~/.local/share/swarm/host/hooks-ran-claude-0.1.0-4128733326` has the time 16:44. Its key is `cksum` of the snapshot path (4128733326). The cached install's key is 1566491539, and its stamp was last touched at 16:38, before this session started.
- I read `skills/swarm/SKILL.md` from the snapshot.

Pinned sha256 values for the snapshot:
- `bin/swarm` `125249db9eb442993c6a4bea5d82004cf528ddd094513d39f4e4bce4fceb41c7`
- `bin/swarm-hook` `6969d5bc0110d65bcb1a152f412926f906e026fb746aaea5cbab0a37d1229bbd`
- `hooks/hooks.json` `0225c3d8c1c7a707067e23388806d9ffc7706083eafe50cf51508143d272ca5c`
- `skills/swarm/SKILL.md` `828306910ddac4488c005ed51fe3f2d3612fc7b1fda6ea32c7c2132a76bf5402`
- `skills/engineering-team/SKILL.md` `8b46edac65821ee355d142d1e187d0549d3998f0fc805d4d54223655386ab880`

Environment notes. I made no changes for any of these.
- The user-scope plugin `swarm@swarm` 0.1.0 is also installed and enabled. It lives at `~/.claude/plugins/cache/swarm/swarm/0.1.0`, `gitCommitSha fad58747`, installed at 16:36:33. It lacks `skills/engineering-team`. In this session `--plugin-dir` took precedence: the snapshot's skills and hooks were used. I did not touch the global install.
- The plugin's own SessionStart bootstrap repointed the launcher `~/.local/bin/swarm` to this snapshot ("launcher changed" in `bootstrap.log` at 16:44). The plugin did this at session start; no command I ran did it. Its automatic `migrate` step was refused because other jobs are active.
- No `~/.claude/CLAUDE.md` exists and the project memory directory is empty, so no stored memory rules applied.

## Attach and join
- `bin/swarm activate --job engineering-team-impl-20260929 --attach` → "attached this session…", tag `[swarm job: engineering-team-impl-20260929]`, rc 0.
- `bin/swarm join --job … --key engineering-impl-claude-preflight --role project_manager` → `Krusty the Clown`, rc 0.
- I read the board. My first post was BRIEF #5917: `BRIEF: docs/superpowers/implementation/engineering-team/engineering-brief.md tasks:claude-host-preflight. …`

## Spawn and role evidence
I spawned exactly one fresh `Agent` child (general-purpose). Its prompt carried `[swarm job: engineering-team-impl-20260929]` and `[swarm role: reviewer]`, each on its own line. The prompt had no join or set_role instruction.

Row from `bin/swarm status --job engineering-team-impl-20260929 --all-agents`:

```
Capital City Goofball   reviewer         claude  opus-5-5   completed  8      2     42s ago  5s ago        30.8 KB
```

- **Role:** `reviewer`, routed automatically from the prompt tag. **Host:** `claude`. **Model:** `opus-5-5`. **Status:** `completed`, 8 calls, transcript stored.
- **Name:** the hook injected it: `[swarm] You are **Capital City Goofball**, a member of the swarm working on job "engineering-team-impl-20260929".`
- **First post, BRIEF #5923:** `BRIEF: docs/superpowers/implementation/engineering-team/engineering-brief.md tasks:claude-host-preflight child reviewer static check.` It was posted through the source CLI.
- **Second post, DONE #5924:** the static result.
- **Minor observation, not a failure:** the child said the injected context gave its name but did not state its own role, and the injected roster listed only the other agents. The role shows correctly in `status`, but the child learned it only from its prompt tag.

## Static outcome (from the child, re-checked against the hash above)
`skills/engineering-team/SKILL.md` at sha256 `8b46edac…ab880`:
- The frontmatter is on lines 1–4 and has only the keys `name` and `description`. `name: engineering-team` matches the directory name.
- It has 5 relative links to 3 files: `references/hosts.md`, `references/team-roles.md` and `references/artifacts.md`. All three exist.
- **Caveat:** this snapshot's SKILL.md does not match the E1-corrected hash Luann Van Houten posted at 16:45 (`0af2f0a3…8448`). The static result applies to this snapshot only, not to the candidate after the E1 fixes.

## Verdict
| check | result |
|---|---|
| Snapshot plugin root loaded (skills and hooks) | PASS |
| Attach and join through the source CLI | PASS |
| Custom `reviewer` role routed automatically on Claude | PASS |
| Host and model recorded (`claude` / `opus-5-5`) | PASS |
| Child's first post is BRIEF | PASS (#5923) |
| Static frontmatter and links | PASS (snapshot hash only; candidate after E1 fixes not checked) |
| Behavioral host evaluation | PENDING (outside this preflight) |

I made no runtime, config, install, trust or service changes and no commits. The shared job was not deactivated.

## PM provenance note

The Claude auto-mode classifier returned no verdict for the report Write and declined it. The session returned the proposed report as its tool-denial payload; the PM preserved it here through a separate authorized workspace write. The session did not save the original file. Its statement about absent memory files does not establish that `coder-memory rules` was run; the PM has loaded and followed standing rules. The role/host/model row is independently corroborated by the shared board. Source-session bootstrap repointed the Claude launcher automatically; this is a reported side effect, not a claim that the launch was globally side-effect free.
