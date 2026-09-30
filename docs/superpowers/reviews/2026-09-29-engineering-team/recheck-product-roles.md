# Recheck 1: product and role authority (revised design and plan)

- **Reviewer:** Todd Flanders (swarm), Reviewer 1
- **Model:** Claude Opus 5.5 (`claude-opus-5-5`)
- **Job:** `engineering-team-plan-20260929`
- **Inputs:** `reviews/revision.diff`, `reviews/author-dispositions.md`, `reviews/recheck-inputs.sha256`, `reviews/review-coordinator-summary.md`, the revised design (D:line) and plan (P:line), and `skills/swarm/SKILL.md` and `lib/swarm/hooks.py` for the preflight check.
- **Applied the user constraints on this recheck:**
  - No blanket approval gate.
  - Capacity is declared, or else a conservative budget is used.
  - No promise of blind review.
  - Same-model review is allowed.
  - Tiny mode is PM/product/EL + author + reviewer/QA.
  - Future behavior tests stay pending.

## Input hashes (sha256sum run by me; both match recheck-inputs.sha256)

```
77f7d5b59c45a20ee423ab2fccd2b24e2ac913ecd0957195eb7fe53761cbf31c  docs/superpowers/specs/2026-09-29-engineering-team-design.md
c6f092e21d6a8e933384601dacebdd09d98cceca7c9a00e09e6294f38999028d  docs/superpowers/plans/2026-09-29-engineering-team.md
```

## Status of the original findings

| ID | Status | Evidence in the revised documents |
|---|---|---|
| R1-01 baseline self-approved | Resolved | The PM independently checks the first baseline, every inference and every exclusion against the original request before EL planning (D:13, D:58). The product manager can force the PM to escalate a blocking ambiguity (D:13, D:30, D:58). There is no blanket user gate, which fits the user's constraint. Small leftover gap: G-2 (tiny mode). |
| R1-02 "material" undefined | Resolved | Every delivered change, including QA test code, gets a reviewer the PM assigns (D:21, D:23, D:61; P:23, P:58). Review depth follows the EL's recorded risk class (P:58). Rollback notes are scoped by data, operational or interface impact (D:46). The judge is recommended for moderate and complex work (D:36). |
| R1-03 disposition authority | Resolved | Unfixed critical or high findings need the user via the PM, with the EL's recommendation. Medium and low findings need the EL's disposition plus the reviewer's concurrence. Scope-changing findings go to the user. The author never dispositions its own finding (D:61; P:58). |
| R1-04 proportionality | Resolved | The tier table sets tiny work at three distinct contexts (D:17-21). The author/reviewer and author/product-acceptor separations never change (D:19-21; P:24). This matches the user's tiny-mode constraint. |
| R1-05 complexity authority | Resolved | The PM's sizing is provisional; the EL decides the final tier and can upgrade it (D:19, D:57, D:59; P:57). |
| R1-06 continuity and acceptance wave | Resolved | Role invocations are bounded, and artifacts must be enough for a fresh invocation to continue (D:42). The example schedule now includes a final PM + product + EL wave (D:75). The resume-or-fresh choice is documented (P:71). |
| R1-07 paper walkthrough treated as behavior | Resolved | Walkthroughs are labeled design checks only (D:83; P:60, P:80). Scenarios are preregistered and committed before execution (P:80). A fresh executor runs them, a separate grader grades them, and the author cannot grade (P:81). "Pending" is never "working" (D:83; P:84). |
| R1-08 missing readiness scenarios | Resolved | D:83 and P:82-83 now cover peer-review repair, QA rejection and retest, product rejection, staffing handoff, a changed PR or candidate, and loss of an engineer. |
| R1-09 failure injections unspecified | Resolved, with one gap | Injections are defined: a throwaway `SWARM_CONFIG` with caps of 1, a verified host stop, a PR change after `met`, and an evidence-only edit (P:83). Negative controls are required (D:83; P:80). Gap: review focus item 2, "author assigned as reviewer" (P:34), has no injection in P:82-83. That is low severity and not blocking. |
| R1-10 research and network | Resolved | Available tools are preflighted instead of assumed. `not applicable` (with PM acknowledgment) is kept distinct from `not performed` (blocked), and both are reported to the user (D:45; P:26, P:59, P:68). |
| R1-11 QA tests | Resolved | QA writes or extends the tests (D:34, D:62; P:25). The EL concurs on omitted categories. Product and EL own the thresholds at baseline. QA test code gets independent review, including in tiny mode (D:62; P:57). |
| R1-12 product acceptance vs QA | Resolved | The product manager exercises each user-visible criterion on the frozen candidate and records the method and result. Any reliance on QA evidence is named with a reason (D:49, D:62; P:57). |
| R1-13 escalation | Resolved | Exclusion or deferral needs the user before dependent implementation (D:47). Dispute ownership is assigned: technical disputes to the EL, product-fit disputes to the product manager, scope disputes to the user (D:65; P:58). A `not_met` verdict returns the named gap to its owner (D:63; P:58). |
| R1-14 candidate identity | Mostly resolved; one contradiction remains | D:53 and P:59 add immutable task diffs, a frozen integrated manifest, and an exclusion for evidence records. The exclusion is defined by path, which fails for the compact single artifact, and it does not cover the review/test and traceability records. See G-1. |
| R1-15 check against the original request | Resolved | Task 1 Step 5 marks each of U1-U7 (P:60). Task 3 Step 1 preregisters the U1-U7 trace (P:80). Final verification checks against U1-U7 (P:88), and the final report traces every original requirement (D:83). |
| R1-16 version and preflight | Resolved, with a timing defect | The capability floor is commit `634aece`, a runtime role check was added, and nothing is auto-installed (D:73; P:28, P:68). The check as written can read the wrong moment or miss a finished agent. See G-3. |
| R1-17 validator path | Resolved | Portable frontmatter, link and `git diff --check` checks, with the Codex validator used only where it is accessible (P:60, P:72). |
| R1-18 Codex task names | Resolved | All role examples are listed, and the built-in roles are marked (D:71; P:69). |

## Remaining issues (none block the plan)

### G-1 (Medium): Path-based evidence exclusion contradicts compact artifacts and leaves some records hashed

D:53 excludes only "Evaluation and acceptance record paths", and the manifest includes "requirement" files. Two other rules collide with that:
- D:42 and P:26 allow one compact artifact holding several records for small work.
- D:47 embeds the traceability record, including the "product acceptance result", in the plan. D:48 records reviews and tests after the freeze.

If requirements and acceptance share a file, or if QA writes its test record after the freeze, then recording a result changes the candidate ID. That is exactly the recursive invalidation D:53 is meant to prevent. Excluding the whole file instead drops the requirement text from the identity. Tiny mode, which the user requires, is the main case that hits this.

The remedy is sound in intent, but its exclusion rule cannot be applied as written. Proposed replacement for the fourth and fifth sentences of D:53 (from "Its ID is a recorded SHA-256 manifest ..." through "... does not recursively change the candidate."):

> Its ID is a recorded SHA-256 manifest of sorted relative paths and file hashes for every deliverable code, test, skill Markdown, and configuration file, whether or not the project uses Git; Git commit IDs are recorded as provenance when available. Requirements are bound separately by their requirement revision (`req=<rev>`), not by the manifest. All team records (request, product, engineering, traceability, review/test, acceptance, status and evaluation records, including a compact combined artifact) are excluded from the manifest, so recording evidence or a signoff never changes the candidate.

The same change is needed in P:59: replace "all deliverable code, tests, skill Markdown, configuration, and requirements" with "all deliverable code, tests, skill Markdown, and configuration (requirements are bound by revision, not by the manifest)". Also replace "evidence-only evaluation/acceptance record edits" with "all team record files, including a compact combined artifact".

### G-2 (Low): In tiny mode the PM checks its own baseline, so no one checks the inferences

In tiny mode the PM takes the product role (D:19), so the "independent" baseline check (D:13, D:58) is a self-check. This is acceptable under the user's constraint, and I do not ask for a user gate. The user should still be able to see what was inferred. Proposed addition to the end of D:63:

> The report lists each marked inference that was not user-approved.

### G-3 (Medium): The role preflight can give a false negative and stop the run wrongly

D:73 and P:68 say that after the first role spawn, `swarm who` must show the custom role. Otherwise the run stops. Two problems with that:
- **Timing.** On Claude, the role is recorded at the child's first tool call, not at SubagentStart (`lib/swarm/hooks.py:325-338`, `_verify_route`; `hooks.py:983` names the agent by `agent_type` until then). A PM that checks right after a background spawn can still see the agent type.
- **Finished agents drop off `who`.** `swarm who` lists only active agents (`skills/swarm/SKILL.md:397`). A bounded foreground invocation that has already returned may not appear at all.

This contradicts the bounded-invocation model of D:11 and D:60. Proposed replacement for P:68's "a first role spawn followed by `swarm who` showing that role" (and the matching sentence in D:73):

> after the first role invocation has posted its `BRIEF:` line, `swarm status --job J --all-agents` shows that agent with its custom role (not its host agent type)

This is unverified on the installed runtime, and the Task 3 role-enrolment smoke check (P:84) should confirm it.

## New blockers

None. I found no remaining conflict among the PM, product manager, EL and user over authority, and none with the user's constraints. Checking the two documents against each other found no conflicts beyond G-1 and G-3.

## Verdict

**READY** for implementation as a plan. I recommend applying G-1 and G-3 (exact text above) before Task 1 Step 4 and Task 2 Step 1 are written. If they are not applied, Task 1's static check (P:60) should record them as open items. G-2 is optional. Behavioral readiness of the future skill remains pending under Task 3, as intended.
