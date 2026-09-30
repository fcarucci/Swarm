# Recheck 2: host mechanics, scheduling, recovery, and evaluation

- Reviewer: **Uter Zorker** (swarm job `engineering-team-plan-20260929`), Claude Opus 5.5 (`claude-opus-5-5`)
- Scope: my original findings R2-01..R2-15 checked against the revised design and plan and against the snapshot source. I also read `reviews/revision.diff`, `reviews/author-dispositions.md`, `reviews/reviewer-1-product-roles.md` and `reviews/review-coordinator-summary.md`. I changed no documents or source and spawned no agents.

## Input hashes (I ran `sha256sum`; both match `reviews/recheck-inputs.sha256`)

```
77f7d5b59c45a20ee423ab2fccd2b24e2ac913ecd0957195eb7fe53761cbf31c  docs/superpowers/specs/2026-09-29-engineering-team-design.md
c6f092e21d6a8e933384601dacebdd09d98cceca7c9a00e09e6294f38999028d  docs/superpowers/plans/2026-09-29-engineering-team.md
```

Below, "D" is the design and "P" is the plan; the numbers are line numbers in the revised files.

## Per-finding status

| ID | Status | Evidence |
|---|---|---|
| R2-01 PM receives no board posts | **Resolved** | D11 and D69: the PM joins with an orchestrator key and reads the board on wakeup and before each wave; a subagent returns a structured request to its parent instead of waiting on the board. P21 and P69 say the same. This matches `hooks.py:1404-1410`, where the main session gets no injection. |
| R2-02 Long-lived roles cause slot deadlock | **Resolved** | D9, D60 and P20/P70: role invocations are bounded and return control. P71 says each role is either resumed in the same role or launched fresh from artifacts. Resuming a departed member rejoins its job (`hooks.py:257-261`, `_resumed_job`). |
| R2-03 Codex fork inheritance | **Resolved** | D61, P27, P69 and P83 require `fork_turns: "none"` for independent spawns and check it during the dry run. The source agrees: `hosts/codex.py:242-244` names `"none"` as the no-fork value and `"all"` as the default, and `codex.py:278-286` reads `forked_from_id` from the child's rollout header, so the dry-run check can be done. |
| R2-04 Other review-independence gaps | **Resolved, within the user's constraints** | D23 and P23: the PM assigns the reviewer, a blind review is not promised, and same-model review is allowed. D48 records the reviewer's identity, host and model. D61 has the reviewer inspect the actual code before relying on the author's account. |
| R2-05 Stale judge verdict | **Resolved** | D63, P71 and P83: the verdict reason carries `candidate=<id> req=<rev>`, the PM compares it with the current candidate before `deactivate`, and the documents state that the Swarm gate checks only the latest `met`. This matches `REFERENCE.md:1219-1226`. The PM can read the stored reason (`cli.py:869-872`, the `swarm status --job` verdict line). P71 also notes that `VERIFIED` is not bound to a candidate. |
| R2-06 Candidate identity and "affected" | **Mostly resolved; one contradiction remains (N1)** | D53 and P59 define per-task diffs or commits, a frozen integrated candidate identified by a SHA-256 manifest, and a non-git fallback. "Affected" is now defined (D53). The rule for which paths are excluded from the manifest conflicts with the compact single-artifact option (D42, D47); see N1. |
| R2-07 Unreadable host slots / probe spawns | **Resolved in the design; the plan wording is off (N2)** | D59 and D75 use the declared budget or serial dispatch, forbid member probe spawns, treat a failed spawn as a capacity signal, and assume no close API. P17 is correct. P20 and P70 still say the PM "verifies actual host slot availability", which no source provides and which D75 itself declines to assume. |
| R2-08 Fragile nested staffing | **Resolved** | D11, D75 and P20: the PM spawns roles at the top level by default; nested helpers are rare, synchronous, and counted against non-refundable caps. |
| R2-09 No fencing before reassignment | **Resolved** | D77 requires confirming the old agent has stopped (or that the supervisor fenced it); stale board status is not enough, and with no confirmation the PM gives the replacement a separate workspace or blocks the files. P70 and P83 require the same. |
| R2-10 Codex replacement loses the brief | **Resolved** | D51 and P69 require a first post `BRIEF: <path> tasks: <IDs>`. That matches what `REFERENCE.md:1001-1003` says a Codex replacement receives (task name plus first post). |
| R2-11 Installed runtime lacks custom roles | **Resolved** | D73, P28 and P68 require a preflight: after the first role spawn, `swarm who` must show the role, otherwise stop, with no auto-install. One caution: on Claude the role appears only after the child's first tool call (`REFERENCE.md`, Custom roles), so the check must run after the child has acted. Reading the stored `634aece` source identity may not be possible for a plugin cache that isn't a git checkout; the behavioral `swarm who` check is the one that matters. |
| R2-12 Self-certifying evaluation | **Resolved** | P74-84: scenarios are preregistered before any run, a fresh executor runs them, a separate grader grades them, a negative control must fail, and the failures are actually induced (a throwaway `SWARM_CONFIG` with caps of 1, stopping an engineer, a seeded defect, a change after `met`). Unreproducible slot exhaustion is marked pending. P80 and D83 say a paper walkthrough is not behavioral evidence. |
| R2-13 Follow-up reuse keeps role and context | **Resolved** | D75, P27 and P70: follow-up reuse is allowed only in the same role, and an author never becomes its own checker. P83 notes that Swarm itself does not block such a follow-up; the PM's workflow must. |
| R2-14 Auto-close without `swarm wait` | **Resolved** | D69 and P70: the PM runs `swarm wait --on` whenever it ends a turn with work remaining and no agent running. |
| R2-15 Host-specific validator path | **Resolved** | P60 and P72 use portable frontmatter, link and syntax checks, and run the Codex validator only where it exists. |

## New or remaining issues (neither blocks the plan)

### N1 (medium): The manifest exclusion conflicts with the compact artifact option

D53 excludes only "evaluation and acceptance record paths" from the candidate manifest, but includes "requirement files". D42 and D47 allow a small change to use "one concise plan or issue comment" holding several records, with the traceability table "embedded in the plan". Review and test records (D48), status reports and traceability tables are neither clearly included nor clearly excluded.

**Scenario:** in tiny mode, the product brief and the traceability/acceptance table share one Markdown file. Recording product acceptance changes that file, which changes the manifest ID. Under D53, that invalidates the final QA, product, EL and judge results it was meant to record. The result is either a loop that never settles or the PM ignoring the rule.

P83 already tests that an evidence-only edit does not reopen gates, so this is falsifiable. The contract is still ambiguous as written.

**Exact text proposed.** In D53, replace "Evaluation and acceptance record paths alone are excluded so recording a signoff does not recursively change the candidate." with:

> "At freeze, the EL records the explicit list of deliverable paths that form the manifest. Review, test, traceability, acceptance, status and evaluation records must live in files outside that list, even in compact mode, so recording evidence never changes the candidate. The requirement revision is tracked by its `req=<rev>` ID, not by hashing a file that also holds signoffs."

Mirror this in P59, replacing "Exclude evidence-only evaluation/acceptance record edits from that content identity." with "Compute the manifest over the EL's frozen deliverable path list; keep every evidence record outside it."

### N2 (medium): "Verify actual slot availability" can't be done, and a Codex session could run out of fresh slots

P20 says "PM verifies actual host slot availability before scheduling another invocation" and P70 says "checks actual host slot availability". But D75 says "No close API is assumed". Neither the snapshot nor its Codex fixtures show a Codex close tool; the fixtures show `spawn_agent` and `wait_agent` only, so whether a returned Codex child frees its slot is unverified.

**Scenario:** a Codex session with four declared slots. Returned children may still hold their slots, and fresh contexts are required for reviewer, QA and product acceptance (D61). The PM spawns product, EL and engineer; the next fresh reviewer spawn fails. Under D75 a failure is "a capacity signal and the task is requeued", so the same spawn is requeued forever and the team stalls without saying so.

**Exact text proposed.**

- In P20, replace "PM verifies actual host slot availability before scheduling another invocation" with: "PM counts active invocations against the declared or conservative budget, and counts a returned agent as still occupying a slot unless the host shows it released."
- Make the same substitution in P70.
- Append to D75: "Record whether the declared limit includes the root session. If a required fresh independent context cannot be obtained after returned agents are stopped or closed by a supported host mechanism, the PM reports this as a blocker with the exact decision needed (for example a new session, the other host, or a user-approved reduced separation) instead of requeueing indefinitely."

### Other checks that passed

- The verdict reason format fits Swarm: a 64-hex SHA-256 plus `req=<rev>` is about 90 characters, within the 200-character board limit.
- The fresh judge path is consistent with the source. A departed judge frees the seat; a fresh judge must be spawned by the PM at the top level, because a spawned subagent can't be a judge (`hooks.py:400-402`).
- Resuming or respawning roles, fencing, and the declared-budget approach invent no runtime behavior beyond what the source shows. D81 lists the remaining limits honestly.

## Verdict

**READY** for implementation as a plan. R2-01..R2-15 are resolved, except that R2-06 and R2-07 each leave a wording contradiction (N1 and N2 above). Both are small text edits, and the plan's Task 3 already has scenarios that would expose both issues (P83: an evidence-only edit, and slot exhaustion marked pending). I recommend applying the proposed text before implementation starts. No new blocker found.
