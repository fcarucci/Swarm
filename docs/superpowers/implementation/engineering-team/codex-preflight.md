# Codex host preflight — 2026-09-29

**Result: FAIL — automatic custom reviewer role routing.** The one fresh child enrolled as `default`, not `reviewer`. Static document structure passed for the observed snapshot. Behavioral fixture execution remains **PENDING / NOT RUN**; this report establishes no broader host validation.

Job: `engineering-team-impl-20260929`. Brief: [engineering-brief.md](engineering-brief.md), `tasks:host-preflight`. PM remains Rabbi Hyman Krustofsky. This root owned only the bounded preflight and this report.

## Host and source evidence

| Item | Observation |
|---|---|
| Actual injected skill | `/home/codex/.codex/plugins/cache/swarm/swarm/0.1.0/skills/swarm/SKILL.md` (also its resolved filesystem path) |
| Actual injected plugin root | `/home/codex/.codex/plugins/cache/swarm/swarm/0.1.0` |
| Requested marketplace source override | `/home/codex/sessions/homelab/swarm-engineering-team`; the actual injected skill still came from the cache above |
| Source CLI used | `/home/codex/sessions/homelab/swarm-engineering-team/bin/swarm` |
| Source skill read | `/home/codex/sessions/homelab/swarm-engineering-team/skills/swarm/SKILL.md` |
| Source HEAD | `b9f8657936862ad5c53f548bf158693156fde836` |
| Working tree | Existing modified README and untracked implementation/evaluation/engineering-team files; HEAD alone does not identify the reviewed document snapshot |
| Swarm declared version | `0.1.0` in source `.claude-plugin/plugin.json` and `.codex-plugin/plugin.json` |
| Host CLI | `codex-cli 0.159.0`; version command also warned it could not create PATH aliases on a read-only filesystem |
| Machine | `coder` |
| Root thread | `01a0ef8a-6e23-7140-b1c6-e1a3ad2d9a27` |
| Clock sample during preflight | `2026-09-29T23:42:44Z` |

The cached skill documents legacy Codex role routing; the source skill documents `<role>__<task>` custom roles. Their hashes differ:

| File | SHA-256 |
|---|---|
| Injected cached `skills/swarm/SKILL.md` | `ff9f0c788d697d6ae54b7d276820990e612f7dc78e988343b10e1cdc1b5f9cd5` |
| Source `skills/swarm/SKILL.md` | `828306910ddac4488c005ed51fe3f2d3612fc7b1fda6ea32c7c2132a76bf5402` |
| Source `lib/swarm/hosts/codex.py` | `cd78bfe89d99f158b77285877f989647838e148e0be6249a6475cb177b22bc44` |
| Source `lib/swarm/roles.py` | `4f580822deddde08b785d3693a56684a7571024c946ac73576b3ab46514ee20b` |
| Source `hooks/codex-hooks.json` | `d972151c0a4bfb5b3bdcfb9cf6458fb3c793c4988f47cbdedd4d055862ca3022` |

These observations distinguish source CLI execution from host plugin loading. The failed enrollment is consistent with the cached skill's older routing contract; this preflight did not trace the executing hook implementation or establish a complete root cause.

## Attach, root enrollment, and first BRIEF

First command: `coder-memory rules`. It returned standing rules from cache with a warning that Hindsight was unreachable. Topic recall returned `(no memories found, or Hindsight unreachable)`.

The requested source command `bin/swarm activate --job engineering-team-impl-20260929 --attach` initially failed inside the restricted sandbox with `BoardUnavailable`, caused by failure to resolve `pgpool.home.arpa`. The identical command succeeded through the normal `require_escalated` approval mechanism. No automatic approval denial occurred or was bypassed. Subsequent root board commands used that mechanism for board connectivity.

Attach output confirmed attachment to the existing job and printed the absolute source CLI. No task or goal arguments were supplied. Root then ran:

```text
bin/swarm join --job engineering-team-impl-20260929 --key engineering-impl-codex-preflight --role project_manager
```

Returned name: **Judge Constance Harm** (actual role `project_manager`, despite the character name). Root read the board, then posted its first message as **#5893**:

```text
BRIEF: docs/superpowers/implementation/engineering-team/engineering-brief.md tasks:host-preflight. Bounded Codex preflight only; PM remains Rabbi Hyman Krustofsky.
```

## Single native child and observed routing

Exactly one native `collaboration.spawn_agent` call was made:

```text
task_name: reviewer__structure
fork_turns: none
model: omitted
reasoning_effort: omitted
returned task: /root/reviewer__structure
```

The prompt contained `[swarm job: engineering-team-impl-20260929]`, the absolute source CLI, read-only structure-review scope, a first safe `coder-memory rules` call, and a required first BRIEF under the hook-injected name. It prohibited manual enrollment/role repair, edits and child spawns. The reviewer received no inherited conversation and was not an implementation author.

Child-reported identity: **Todd Flanders**, thread `01a0ef8b-e56f-7721-ab51-379e2ab1e020`. Its first safe call returned exit 0 and cached memory rules. It received a name but no explicit role in its injected context. Its BRIEF and DONE posts initially queued through the source CLI; the root subsequently observed both on the board.

After the child returned, root ran the required source command:

```text
/home/codex/sessions/homelab/swarm-engineering-team/bin/swarm status --job engineering-team-impl-20260929 --all-agents
```

Observed row:

```text
AGENT          ROLE     HOST   MODEL      STATUS   CALLS  MSGS
Todd Flanders  default  codex  gpt-6-sol  running  12     2
```

The status was a board observation after the native child returned, not proof of a released host slot. Root also read the board and observed these exact child messages (times as rendered by the board):

```text
[16:42] Todd Flanders: BRIEF: docs/superpowers/implementation/engineering-team/engineering-brief.md tasks:host-preflight structure review.
[16:43] Todd Flanders: DONE: Codex structure preflight PASS at observed hashes: 4 docs, valid entrypoint fields, 10/10 local links resolve (5 unique paths). Static only; host behavior pending.
```

**Routing gate: FAIL.** Automatic enrollment and board delivery occurred, but the required `reviewer` role did not. No manual role patch, trust change, replacement child or retry followed. The shared job remained active; its existing task, goal and judge verdict were not replaced.

## Independent static result

**PASS for the child-observed snapshot only.** The reviewer read the entrypoint and all three linked references. It checked opening/closing frontmatter delimiters, exact `name: engineering-team`, and a nonempty description. It verified all **10 local Markdown link instances**, resolving to **5 unique existing file paths**: the four reviewed documents below plus `skills/swarm/SKILL.md`. This was a focused frontmatter/path check, not a claim of a general YAML parser or behavioral validation.

| Reviewed path, relative to source root | SHA-256 |
|---|---|
| `skills/engineering-team/SKILL.md` | `fe77a0351e3d5c441e821161742a31f3713c31f1cda39ed8fe083fa9baebbaec` |
| `skills/engineering-team/references/team-roles.md` | `56c3f3dade21f20c5c9c4379de39a5c2391ec2d77ee18f826df26cf7c6372fad` |
| `skills/engineering-team/references/artifacts.md` | `638e4e022777390712f13402b3ba61062bd3ccd6fad06230ae79a16ec59c54bb` |
| `skills/engineering-team/references/hosts.md` | `2dacddc308d04bf59453997c970cdc141da91d486c938ed9f2997c468495353c` |

Concurrent author edits were visible on the board; these hashes identify the observed snapshot and do not approve later revisions or settle the separate E1/E2 content reviews.

## Disposition and limits

| Check | Result |
|---|---|
| Attach to existing job with source CLI | PASS |
| Explicit root key/role and first BRIEF | PASS |
| Exactly one fresh native child | PASS |
| Hook-injected name and child BRIEF delivery | PASS |
| Automatic custom `reviewer` role | FAIL: actual role `default` |
| Independent frontmatter/local file-link structure | PASS, bounded snapshot above |
| Behavioral fixture suite / broader host readiness | PENDING / NOT RUN |

Stopped at the failed role-routing gate. Only this report was authored by the root; the child edited no files. No installs, configuration edits, hook-trust changes, services, commits, broad tests, additional spawns, publication, or shared-job deactivation were performed. Handoff is to Rabbi Hyman Krustofsky; this preflight does not own remediation or final acceptance.
