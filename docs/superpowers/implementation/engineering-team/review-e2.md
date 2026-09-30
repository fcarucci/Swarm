# E2 independent review

**Decision: READY (static E2 review after recheck)** — E2-R1 fixed and independently rechecked. Host behavioral validation remains pending.

Reviewer: Richard (`engineering-impl-review-e2`), independent Codex reviewer; model family GPT-6 (exact serving variant not exposed). Job: `engineering-team-impl-20260929`. PM: Rabbi Hyman Krustofsky. Requirement revision: `product-r1`; technical scope: ER-4 and ER-5, approved plan Task 2. Review date: 2026-09-29.

## Reviewed version and boundary

Reviewed actual uncommitted additions against base/HEAD `b9f8657936862ad5c53f548bf158693156fde836`: the whole new `skills/engineering-team/references/hosts.md` (254 lines, absent from the base) and the 11-line engineering-team addition at `README.md:52–62`. Inspected `git diff b9f8657 -- README.md` and `git diff --no-index -- /dev/null skills/engineering-team/references/hosts.md`; the latter's exit 1 denotes an addition, not a failed check.

The board showed Johnny Tightlips's E2 DONE and handoff before this review. These SHA-256 values were recorded after that handoff and remained unchanged through the static checks:

| File | SHA-256 |
|---|---|
| `skills/engineering-team/references/hosts.md` | `2dacddc308d04bf59453997c970cdc141da91d486c938ed9f2997c468495353c` |
| `README.md` | `2378aba06faa83924ebd960ff2a94b35bfb0b2b2604b9c39ad1557b2853f6444` |

This decision binds only to that snapshot. An author correction needs the same reviewer's recheck. E1 files were consulted as linked interfaces, not accepted or reviewed as E2 deliverables. No implementation, install, configuration change, commit, spawn, or broad test was performed. Only this report is reviewer-owned.

## Finding

### E2-R1 — Medium — Codex task-name examples violate the exposed host contract

**Status:** fixed; same-reviewer recheck below. **Owner:** E2 author. **Requirements:** approved Task 2 step 2; PR-7; ER-4.

**Evidence:** `hosts.md:128` supplies `"task_name": "engineer__E1"`; table entries at lines 142–143 repeat `engineer__E1` and `reviewer__E1`. The actual `collaboration.spawn_agent` tool exposed to this review specifies: “Task name. Use lowercase letters, digits, and underscores.” These examples contain uppercase `E`.

The Swarm role parser is a different boundary: `lib/swarm/roles.py:30–39` accepts a valid role prefix and merely requires a nonempty suffix. A local parser check returns `engineer` and `reviewer` for these strings; that does **not** establish host-valid launch syntax. Applying the exposed host's lowercase/digit/underscore rule to the eight documented task names flags exactly these two entries. No real spawn was attempted, so this is a demonstrated contract mismatch, not a claim of an observed host rejection. The JSON is otherwise valid and its `fork_turns: "none"` field matches the exposed tool.

**Impact:** copying the primary Codex example fails the documented host input contract; the guide currently cannot claim all launch examples are executable. Source-only role checks miss it.

**Smallest fix:** replace all three occurrences with `engineer__e1` / `reviewer__e1`; add one sentence requiring the *entire* Codex task name, including its suffix, to use lowercase letters, digits, and underscores. Durable task IDs and brief filenames may remain `E1` / `task-E1.md`.

**Recheck:** parse the JSON, check every table/example task name against the exposed host rule, retain correct Swarm role resolution and `fork_turns: "none"`, and refresh the file hash. No capacity-probe spawn is necessary.

## Coverage and evidence

| Area | Static assessment and supporting evidence |
|---|---|
| Installed capability preflight | `hosts.md:8–44` requires source containing `634aece`, installed doctor checks, a useful role's local call and BRIEF before `status --all-agents`, and pending per-host behavioral evidence. This matches Task 2 step 1 / PR-7. An old installed cache is not an E2 defect when the instructions correctly require the capability and stop unsupported dispatch. Manual reviewer CLI enrollment is not host-hook validation. |
| CLI syntax | All 13 documented/example forms parsed through source `swarm.cli._parser().parse_args` without calling handlers: status, activate, join, read, post, wait, resume, verdict, deactivate, both doctor hosts, who, and attach. Parser declarations: `lib/swarm/cli.py:1954–2053`. |
| Host routing and fresh context | Claude prompt/description and job/role tags match `lib/swarm/hosts/claude.py:33–46`, `lib/swarm/hooks.py:222`, and the source Swarm skill. Codex `message`, `task_name`, encrypted-message limitation and role prefix match `lib/swarm/hosts/codex.py:317–370` and `roles.py`; lowercase host mismatch is E2-R1. Explicit `fork_turns: "none"` and same-role follow-ups match the exposed Codex tool semantics. Claude's live Agent schema is not exposed in this reviewer session; Claude review is source-based. |
| Protected roles | `hosts.md:145–158,175–181,230–231` distinguishes custom worker roles, read-only verifier, and root-scheduled goal judge; QA test writers use custom roles. Source `roles.py:6`, `hooks.py:375–411,804–822`, and `board/base.py:1088–1094` support the protected-role and verdict claims. “No implementation” for judge remains a workflow instruction, not a claim that every judge write is code-blocked. |
| Capacity and nonrefundable child caps | `hosts.md:160–186` counts persistent threads until host evidence releases them, separates Swarm quiet completion from host slots, uses root dispatch, and forbids member probes. `hooks.py:359–411` reserves admitted attempts before host launch; `board/base.py:1080–1086` explicitly preserves job counts when agents depart or are purged. Root hook handling is separate at `hooks.py:1404–1411`. The exposed interrupt tool stops a turn and keeps the agent available; no close tool is exposed. The guide correctly assumes neither thread destruction nor slot release. |
| Polling and direct return | `hosts.md:46–102` explicitly joins PM, drains unread board pages at turn start/before waves/after returns, and requires bounded structured direct returns including blockers. `cli.py:2604–2608` exposes remaining unread pages; `hooks.py:1404–1411` keeps root handling separate from member board injection. Staffing requests cannot depend solely on an unread board post. |
| Fencing and recovery | `hosts.md:212–226` requires confirmed stop before shared claim transfer, rejects silence/interrupt request/stale row as proof, and blocks or isolates when fencing is unavailable. Partial changes stay unreviewed; outage recovery reconciles queued posts before shared dispatch/acceptance. Matches approved design recovery and PR-9. No actual writer was stopped in this review. |
| Acceptance identity | `hosts.md:193–199,228–254` sequences integration/freeze before separate QA/product/EL acceptances and judge, requires current manifest/revision comparison, reopens affected task reviews and all final gates on deliverable/requirement change, and exempts evidence-only edits. `cli.py:2262–2271` checks only latest `met`; `board/base.py:1088–1094` stores freeform reason without a candidate binding. E1 owns the linked manifest construction details. |
| README / ER-5 | The actual new paragraph describes discovery, bounded scheduling, independent review, distinct acceptance, and workflow-only custom labels; it links the skill and host guide and separates documentation checks from host validation. No scoped README finding. |

## Verification limits and handoff

Focused read-only checks used `PYTHONPATH=lib python3 -B` with inline `json`, `shlex`, `re`, `pathlib` and the source CLI/role parser. Results: 13/13 CLI argument forms parsed; JSON fields were `fork_turns`, `message`, `task_name`; all eight prefixes resolved to expected Swarm roles; six of eight unique task names satisfy the exposed host lowercase rule (E2-R1); all six local link occurrences in E2 content resolved; no trailing whitespace in the new host guide. `git diff --check b9f8657 -- README.md` passed. The first attempt used unavailable `python`; rerun with `python3` produced the results above. No package installation was needed.

These checks establish static document/interface evidence only. No installed hook enrollment, Claude/Codex behavioral fixture, capacity-release experiment, supervisor replacement, or end-to-end completion was exercised. Both-host behavioral readiness remains with independent evaluation and final acceptance owners.

**Initial PM handoff:** route E2-R1 to Johnny Tightlips, then return the corrected hash/diff to Richard for recheck. The initial snapshot was CHANGES REQUIRED; ER-5 had no scoped defect. This review is not product/EL/QA acceptance or a judge verdict.

## Same-reviewer recheck

Johnny Tightlips posted the completed fix and explicit hash handoff before recheck. Current reviewed `hosts.md` SHA-256: `97e99b962851462ec11361b6dc04a5bae61362e942a51e1f1cdf25e7a0cb79f7`. README remains `2378aba06faa83924ebd960ff2a94b35bfb0b2b2604b9c39ad1557b2853f6444`. These supersede the initial snapshot for the READY decision; the initial evidence above preserves the finding history.

Independently verified the exact change by reversing the three example substitutions and the added whole-name rule in memory, then comparing SHA-256 with the original reviewed file: exact match. Thus no other host-guide changes accompanied this correction. All 10 concrete role-prefixed task-name occurrences found in the revised guide satisfy the exposed host lowercase/digit/underscore rule and resolve through the source Swarm role parser. The JSON still uses `fork_turns: "none"`; all 13 CLI forms still parse; no trailing whitespace; README hash unchanged. No spawn or command handler was executed by these checks.

**E2-R1 resolution:** fixed and verified. **Final PM handoff:** READY for E2 static documentation review at the hashes above, with no open E2 findings. Carry independent host evaluation and candidate-bound product/EL/QA/judge acceptance forward separately. E1 interface findings remain with E1's reviewer and are not resolved by this E2 decision.

## E2-R2 independent recheck

**Decision: READY (static documentation recheck).** The original high-severity global PM identity collision is fixed in the reviewed `hosts.md` snapshot below. This review does not claim that updated live Claude workflows passed; the fresh affected host runs remain required before behavioral acceptance.

**Reviewed version:** actual `git diff dc3a1b0 -- skills/engineering-team/references/hosts.md`, with the E2-R2 author change confined to that guide. Current `hosts.md` SHA-256: `b8302b26642eb8236f120a4ba90372f0e047132d830837197987742fde257223`. Excluded evidence consulted (not treated as E2 deliverable): `evidence/pm-identity-fix.md`, SHA-256 `8b23f6f7c77039046efa35772908f47520ed68ce82f70fb32237244bfc16b11c`. `git diff --check dc3a1b0 -- skills/engineering-team/references/hosts.md` passed. No host launch, runtime test, or edit to E2 source was made by this reviewer.

### E2-R2 — High — global orchestrator key can move a PM row between jobs

**Disposition:** fixed by Johnny Tightlips; same-reviewer recheck passes. The original failure is preserved in the excluded evidence record: independent Claude roots F01 and F02 copied `--key orchestrator`, and the second join moved the existing PM row from F01's job to F02's. That is consistent with source, not a speculative risk. `agents.agent_key` is a global primary key in both Postgres (`lib/swarm/board/postgres.py:45–56`) and SQLite (`lib/swarm/board/sqlite.py:107–138`); the `Board.allocate_name` contract (`lib/swarm/board/base.py:1025–1044`) says an active key joined to another job is moved. `MemoryBoard.allocate_name` (`lib/swarm/board/memory.py:411–427`) implements precisely that: it indexes `agents` by key and updates the existing row's `job` when the key is active.

The corrected guide now:

- builds a root PM key from job plus root host/session identity (`hosts.md:57–73`), with a one-time UUID fallback and a requirement to persist both UUID and exact key before joining;
- requires exact recorded-key reuse when resuming the same root session, and a distinct durable identity for every independently attached root session (`hosts.md:69–73,97–102`);
- uses the current PM name returned by `join`, while keeping job/host agent key as authorship identity (`hosts.md:75–88`);
- explains that display names can be reassigned and an agent can receive a new name on resume when its old name is occupied; it notes `swarm who` omits keys and blocks a directed handoff if the current identity/name correlation is ambiguous (`hosts.md:78–91`).

These clauses are consistent with the actual interfaces. `swarm join --job J --key K` returns the assigned name (`lib/swarm/cli.py:1993–1996,2655–2665`), while the source contract and `MemoryBoard` show global-key movement. Names are unique only among active agents; the allocation contract permits a different free name when a departed agent's former name is held. `RosterEntry` internally contains both key and name (`lib/swarm/board/base.py:413–421`), but the rendered roster and `swarm who` expose display names without agent keys (`lib/swarm/hooks.py:592–604`; `lib/swarm/cli.py:2611–2617`). The guide does not pretend `who` proves historical identity: it requires current enrollment/post plus the host invocation record to establish the mapping, and explicitly defers handoff if that mapping cannot be established. That is a sound workflow gate given the exposed CLI; it does not claim a new key-lookup command exists.

**Placeholder and command clarity:** the `PM_KEY="pm:<job>:<root-host>:<root-session-id>"` line is explicitly a template: the preceding paragraph says to replace example values, and the following instructions define what the root host/session fields mean and how to replace the session component with a persisted UUID when unavailable. The actual `join` and `read` commands consistently use the assigned shell variable. Same-session reuse and attached-session uniqueness are stated as operational rules, not left to infer from the template. No contradictory fixed `orchestrator` key remains in the E2 host guide.

**E2-R2 resolution:** fixed and statically verified at `hosts.md` hash `b8302b26642eb8236f120a4ba90372f0e047132d830837197987742fde257223`. There are no open E2 findings in this review. The initial live F01/F02 collision is the RED evidence; do not describe it as fixed by a rerun. PM's requested fresh affected Claude roots must use this revision and verify separate keys, row ownership, and PM reads before behavior is accepted. This READY decision covers E2 documentation only; it is not final product/EL/QA acceptance or a judge verdict.
