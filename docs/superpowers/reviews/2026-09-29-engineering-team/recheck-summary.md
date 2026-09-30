# Claude recheck: consolidated summary

- Job: `engineering-team-plan-20260929`
- Coordinator: Plopper, Claude Opus 5.5 (`claude-opus-5-5`)
- Reviewers: the same two original reviewers, each resumed in its existing conversation. No extra reviewers were spawned.
  - Todd Flanders (R1, product and roles): `reviews/recheck-1-product-roles.md`
  - Uter Zorker (R2, mechanics): `reviews/recheck-2-mechanics.md`
  - Both are Claude Opus 5.5.
- Inputs reviewed: both reviewers ran `sha256sum` independently, and the hashes match `reviews/recheck-inputs.sha256`.
  ```
  77f7d5b59c45a20ee423ab2fccd2b24e2ac913ecd0957195eb7fe53761cbf31c  docs/superpowers/specs/2026-09-29-engineering-team-design.md
  c6f092e21d6a8e933384601dacebdd09d98cceca7c9a00e09e6294f38999028d  docs/superpowers/plans/2026-09-29-engineering-team.md
  ```
- Constraints applied, as the user set them:
  - no blanket approval stage;
  - capacity is declared (the Codex parent declares four), otherwise a conservative budget is used;
  - no promise of blind review;
  - same-model review is allowed;
  - tiny mode is PM/product/EL + author + reviewer/QA;
  - behavioral tests of the future skill remain pending.

## Verdict

**READY for implementation as a PLAN** (both reviewers, independently). There are no new blockers.

- R1: 16 of 18 findings resolved; R1-09, R1-14 and R1-16 are resolved with small gaps.
- R2: 13 of 15 findings resolved; R2-06 and R2-07 each have a wording issue left.

This is not a claim that the skill works: every host behavioral evaluation is still future work under plan Task 3.

## Outstanding issues (recommended edits before plan Tasks 1–2 are written; none blocks the plan)

### O-1 (medium; R1 G-1 and R2 N1, found independently): the candidate manifest conflicts with compact artifacts

D:53 hashes requirement files and excludes only evaluation and acceptance record paths. D:42, D:47 and P:26 allow one compact artifact that embeds traceability and acceptance. In tiny mode, recording acceptance therefore changes the candidate ID and reopens the gates it records, which is the recursive invalidation D:53 exists to prevent.

The two reviewers' texts agree in substance. Combined wording:

- D:53, replacing "Its ID is a recorded SHA-256 manifest ... does not recursively change the candidate.":
  > At freeze, the EL records the explicit list of deliverable paths (code, tests, skill Markdown, configuration) and its ID is a SHA-256 manifest of those sorted relative paths and file hashes, whether or not the project uses Git; Git commit IDs are recorded as provenance when available. Requirements are bound by their revision (`req=<rev>`), not by the manifest. All team records (request, product, engineering, traceability, review/test, acceptance, status and evaluation records, including a compact combined artifact) live outside that list, so recording evidence or a signoff never changes the candidate.
- P:59: replace "all deliverable code, tests, skill Markdown, configuration, and requirements" with "the EL's frozen deliverable path list (requirements are bound by revision, not by the manifest)". Replace "Exclude evidence-only evaluation/acceptance record edits from that content identity." with "Keep every team record, including a compact combined artifact, outside that list."

### O-2 (medium; R2 N2): "verify actual slot availability" cannot be done, and a Codex team can requeue indefinitely

P:20 and P:70 promise that the PM checks real availability. D:75 assumes no close API. No Codex close tool appears in the snapshot fixtures, and whether a finished Codex child frees its slot is unverified.

- P:20 and P:70, replace "PM verifies actual host slot availability before scheduling another invocation" with:
  > PM counts active invocations against the declared or conservative budget, and counts a returned agent as still occupying a slot unless the host shows it released.
- Append to D:75:
  > Record whether the declared limit includes the root session. If a required fresh independent context cannot be obtained after returned agents are stopped or closed by a supported host mechanism, the PM reports this as a blocker with the exact decision needed (for example a new session, the other host, or a user-approved reduced separation) instead of requeueing indefinitely.

### O-3 (medium; R1 G-3, partly noted by R2 under R2-11): the role preflight can falsely stop the run

D:73 and P:68 say that after the first role spawn, `swarm who` must show the custom role. Two things break that check:

- On Claude the role is set at the child's first tool call (`lib/swarm/hooks.py:325-338`), so an immediate check can see the host agent type.
- `swarm who` lists only active agents (`skills/swarm/SKILL.md:397`, verified by the coordinator), so a bounded invocation that has already returned can vanish from the list.

The coordinator verified that `status --job J --all-agents` exists (`lib/swarm/cli.py:2051`; `SKILL.md:387`). Replacement for D:73 and P:68:

> after the first role invocation has posted its `BRIEF:` line, `swarm status --job J --all-agents` shows that agent with its custom role (not its host agent type)

This is still unverified on the installed runtime; the Task 3 enrolment check must confirm it. R2 adds that the `634aece` source check may not be possible on a plugin cache that is not a git checkout. The observed-role check is the one that counts.

### O-4 (low; R1 G-2, optional): tiny-mode inferences are self-checked

In tiny mode the PM holds the product role, so the baseline check at D:13 and D:58 is a self-check. This is acceptable under the user's constraints. Optional addition at the end of D:63:

> The report lists each marked inference that was not user-approved.

### O-5 (low; R1-09 remainder): the "author assigned as reviewer" injection is missing

Review focus item 2 (P:34) has no matching injection in the scenario list at P:82-83. Add a staged hand-off that lists the author as reviewer, with the expected result that the PM refuses and reassigns.

### O-6 (low; coordinator, provenance only)

`reviews/author-dispositions.md` links to `initial-product-roles.md`, `initial-mechanics.md` and `initial-coordinator-summary.md`. None of those exist in this snapshot, where the originals are `reviewer-1-product-roles.md`, `reviewer-2-mechanics.md` and `review-coordinator-summary.md`. Fix the links, or add the renamed copies, if the dispositions file is kept.

## Disagreements

- There is no verdict disagreement.
- R2 marked R2-11 resolved, while R1 flagged the preflight timing as a medium defect (G-3). The coordinator sides with R1, because the "who lists active agents only" behavior is confirmed in `SKILL.md:397`.
- R1's and R2's fixes for O-1 differ only in form: exclude all team records, versus an explicit deliverable path list. The combined text above uses both.
- The remedies R2 checked against source did not invent runtime behavior: `fork_turns: "none"`, resume/respawn, the stale-verdict hash in the verdict reason (about 90 characters, within the 200 limit), and stop-before-reassign.
