# Engineering brief: `swarm:engineering-team`

**Owner:** engineering lead (Hans Moleman)
**Revision:** 1, 2026-09-29
**Base:** `b9f8657` on `feature/engineering-team`
**Status:** implementation staffing request; PR mapping uses proposed `product-r1` and awaits PM comparison with the original request before candidate freeze.

## Architecture and boundary

This is a Markdown instruction skill layered on the existing Swarm plugin. `skills/engineering-team/SKILL.md` is a short entrypoint; `references/team-roles.md` defines authority and handoffs, `references/artifacts.md` defines durable records and candidate identity, and `references/hosts.md` defines executable Claude/Codex procedures. `README.md` advertises the skill. The skill schedules agents through the host and board; it adds no runtime scheduler, board schema, enforcement, or model configuration. Only Swarm's existing judge/verifier controls and spawn limits are enforced by code. Role signoffs are workflow evidence checked by the PM.

The PM owns activation, explicit board join/read, root spawns, status, and user communication. The EL owns technical staffing, task division, integration, and technical acceptance. The product manager owns the PR baseline and product acceptance; QA owns test evidence. The PM preserves the user's scope authority. Reviewers are assigned by the PM from a context distinct from each change's author. The current installed Codex plugin cache is `136c99a` and lacks `lib/swarm/roles.py`; this session's custom-role agents appear as `default`. The source worktree contains custom roles from `634aece`. This source/cache difference is an evaluation preflight finding, not evidence of successful role enrolment. Dedicated source-backed Claude and Codex sessions are required for host evaluation. Do not change the global plugin installation as part of this work.

## Technical requirements

These ERs implement the approved design and map to `product-r1` in [the product baseline](product-baseline.md). The PM must confirm that baseline against the original U1–U7 request before the mapping is final.

| ID | Requirement | PR trace | Planned owner |
|---|---|---|---|
| ER-1 | Entrypoint invokes the PM-led product baseline, EL staffing, implementation, independent review, QA, product/EL/QA acceptance, and judge gate in order. | PR-1, PR-2, PR-3, PR-4, PR-5, PR-8 | E1 |
| ER-2 | Role contract states dynamic tiny/moderate/complex staffing, distinct review/acceptance contexts, finding dispositions, and PM escalation of user scope decisions. | PR-2, PR-3, PR-4, PR-5, PR-6, PR-9 | E1 |
| ER-3 | Artifact contract preserves request history, versioned PR/ER trace, per-task diff and review evidence, QA applicability, and a frozen SHA-256 manifest of every deliverable including requirement files. Mutable evidence is outside the manifest. | PR-2, PR-4, PR-5, PR-8 | E1 |
| ER-4 | Host guide gives exact Claude/Codex role routing, PM board procedure, source-capability preflight, bounded waves, slot and nonrefundable child-cap accounting, stop/reassignment recovery, and stale-candidate gates. | PR-1, PR-6, PR-7, PR-8, PR-9 | E2 |
| ER-5 | README describes the new skill and says custom role labels alone do not enforce the delivery process. | PR-7 | E2 |
| ER-6 | Independent evaluation preregisters positive and failing fixtures, then records fresh executors and separate graders on both hosts. Missing or unsupported host runs remain pending. | PR-7, PR-10 | Evaluation owner, assigned after draft freeze |

## Staffing and task interfaces

**Tier: moderate. Request two engineers**, in separate bounded invocations; two independent deliverable streams exist, but `SKILL.md` is a shared interface. The PM should assign E1 and E2 distinct agents, schedule around actual host slots, and send an infeasible capacity request back to the EL. Concurrent work is possible on nonoverlapping files; E2 waits for E1's entrypoint before editing its links. A four-member child-spawn cap is not a four-person team cap; root PM spawns are the default. There is no capacity-probe spawn.

| Task | Engineer scope and exclusive files | Dependency and handoff | Focused verification |
|---|---|---|---|
| E1 — role/artifact contract | Create `skills/engineering-team/SKILL.md`, `references/team-roles.md`, `references/artifacts.md`. Implement approved plan Task 1, including frontmatter and stable relative links. Own the entrypoint until E1 handoff. | Consume design, approved plan, product baseline, and original U1–U7 request. Post immutable task diff or commit against `b9f8657`, requirement revision, coverage notes, and remaining questions. Release `SKILL.md` to E2 explicitly. | Frontmatter parse/name/description; resolve local links; review U1–U7 and ER-1..3 against text; `git diff --check`. Static checks prove document shape only. |
| E2 — host guide/discovery | Create `references/hosts.md`; after E1 release, add host reference to `SKILL.md`; modify `README.md`. Implement approved plan Task 2. | Consume E1 contract and current `skills/swarm/SKILL.md`/CLI syntax. Coordinate shared entrypoint edit with E1 on board. Post immutable task diff or commit, host command audit, and the installed/source capability caveat. | Check documented CLI syntax and host examples against source skill; frontmatter/link check after edit; `git diff --check`. Do not claim a host dry run from text checks. |
| EV — behavioral evaluation | Create `docs/superpowers/evaluations/2026-09-29-engineering-team.md` after draft skill and frozen expectations. Distinct executor/grader contexts on Claude and Codex. | PM schedules outside E1/E2 author contexts. Preregister fixtures and negative controls before running; record source identity, raw board/artifact/transcript links, scenario verdicts, and pending limitations. | Execute the approved Task 3 scenarios; a missing host is pending. Runtime defects become separately scoped fixes; rerun affected cases and one representative success. |

No engineer should modify another owner's files without a board handoff. Any QA-authored evaluation code or test fixture change needs an independent engineer reviewer. A reviewer also checks each E1/E2 delivered change, including shared entrypoint edits, from the actual diff and requirements. The author fixes findings; the same reviewer rechecks. The EL integrates reviewed work and checks the final diff. PM assigns reviewers and QA, not the authors.

## Dependency and acceptance sequence

1. PM records the original request and cross-checks the product manager's PR baseline and inferences. Map each ER above to PR IDs; unresolved scope changes go to the user through PM.
2. E1 delivers the entrypoint and role/artifact contract. Independent review covers its task diff. E2 then owns the host link and guide, with its own independent review. Conflicts or shared-file edits are serialized on the board.
3. EL integrates reviewed tasks, records explicit sorted deliverable paths and SHA-256 file hashes, and freezes the manifest ID and requirement revision. Immutable requirement baselines belong in the manifest; mutable review, test, acceptance, traceability-result, status, and evaluation records do not. A changed deliverable refreshes the ID and all final gates; a linked PR revision also reopens affected task reviews.
4. QA checks applicable acceptance/integration behavior in fresh host scenarios. End-to-end host evaluation is applicable because the skill's core outcome is orchestration across hosts. Performance testing is applicable only if a product or technical requirement declares a threshold before measurement; otherwise record the omission and EL concurrence. A Markdown keyword/static test cannot substitute for host behavior.
5. Product manager directly checks each user-facing criterion on the frozen candidate or states the exact reason for relying on QA evidence. EL checks ER-1..6 and architecture; QA signs test evidence. A distinct judge gives `met` only against `candidate=<id> req=<revision>`. PM checks those values against current files before completion.

**Technical acceptance:** all ERs map to approved PRs; E1/E2 diffs and any QA code have independent review with findings resolved or authorized dispositions; frontmatter, links, source command syntax, and focused checks pass; the frozen manifest includes all operative skill and requirement files; Claude and Codex evaluations are reported separately from documentation status; no unrun scenario is called passing. Both-host readiness requires source-capable role enrolment and the core positive/negative scenarios on each host. A plugin release or global installation is a separate action.

**Recovery:** this skill writes documentation only and requires no data migration or service restart. Revert its unmerged branch changes to recover. If an agent stops mid-edit, PM confirms the host stop, transfers file claim, and briefs replacement from these durable artifacts and the partial diff. A board outage blocks shared claims and gated acceptance until coordination resumes.
