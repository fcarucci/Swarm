# Engineering-team plan review resolution

## Provenance and scope

- Swarm job: `engineering-team-plan-20260929`; planning and Claude review share this board.
- Initial draft: commit `50017bb`; Swarm source includes custom-role support from `634aece`.
- Independent initial reviewers: Todd Flanders and Uter Zorker, both Claude Opus 5.5, coordinated by Plopper in Claude session `4369955e-72b0-4ce3-abf9-518efb5703eb`.
- Original reports: [product and roles](initial-product-roles.md), [host mechanics](initial-mechanics.md), [coordinator summary](initial-coordinator-summary.md). Their line references describe the initial draft and are preserved verbatim.
- Scope is the design and implementation plan. No engineering-team skill has been implemented, installed, released, or behaviorally validated by this task.
- Status: complete. Both independent Claude rechecks returned READY; the Claude coordinator confirmed all final corrections with no unresolved blockers.

## Decisions for revision

| Findings | Disposition and required correction |
|---|---|
| R1-01, R1-13, R1-15 | PM checks the product baseline against the original request and preserves an explicit request-level trace. Record inferences and route unresolved consequential ambiguity, exclusions, and scope disputes to the user. Reject a blanket extra approval step for every moderate job: existing authorization remains effective. A product manager's blocking ambiguity cannot be silently waived by PM. |
| R1-02, R1-03 | Independently review every delivered change, including tests, with depth proportional to risk. Define finding disposition authority and distinguish an accepted risk from a fix. An unresolved high/critical finding requires an explicit user decision; lower findings need EL and reviewer agreement. |
| R1-04, R1-05 | State compact and full role allocations, preserving author/reviewer separation. PM sizing is provisional; EL controls engineering complexity, staffing, and escalation to a fuller process. Compact mode does not pretend all role responsibilities disappeared. |
| R1-06, R2-01, R2-02, R2-08 | Use bounded invocations and an explicit acceptance wave. PM manually reads the board. Requests requiring PM action are mirrored on the board and returned through the host's result channel. PM normally executes root spawns while EL owns staffing decisions. No role holds a slot while waiting for another role to be spawned. |
| R1-07, R1-08, R1-09, R2-12 | Paper walkthroughs are design checks. Register inputs, failure injections, expected outcomes, and negative controls before runs. Use an independent executor and separate grader. Include peer repair, QA rejection/retest, product rejection/reroute, staffing handoff, stale verdict, and recovery scenarios. Pending/unavailable runs are never passes; a reviewed plan is not a tested skill. |
| R1-10 | Check actual research tools and access. Distinguish justified non-applicability from unavailable research. Product/PM record research omissions and limitations. Do not assume every Codex host lacks network or web tools. |
| R1-11, R1-12 | QA writes or extends missing applicable tests and gets independent review of test code. EL concurs on omitted categories; product/EL own appropriate baseline performance thresholds. Product acceptance records direct observation separately from reliance on QA evidence. |
| R1-14, R2-05, R2-06 | Define immutable task evidence and a frozen integrated candidate, with a content-manifest fallback for non-git work. Explicitly exclude only evidence records from candidate identity to avoid recursive invalidation. Bind signoffs and judge reasons to candidate and requirement revision; PM verifies freshness before closing. The current Swarm completion gate does not detect stale verdicts. |
| R1-16, R2-11 | Add capability/source preflight and observed role enrollment. Version `0.1.0` alone does not identify whether custom-role support is installed. Source tests do not demonstrate installed hook behavior. No automatic install or configuration mutation is implied. |
| R1-17, R1-18, R2-15 | Replace the inaccessible account-specific validator path with explicit portable checks; include all relevant host role examples. |
| R2-03, R2-04, R2-13 | Fresh Codex context (`fork_turns: none`) for independent checking; PM assigns reviewers; record reviewer identity/model and forbid self-review, including through follow-up reuse. Shared board context is intentional and prevents a claim of blind review. Different models/hosts can improve diversity but are optional, not a prerequisite for either host to work alone. |
| R2-07 | Use a host limit when actually declared (this session declares four concurrent agents). Otherwise budget conservatively and record unknown capacity. Do not invent a universal slot-query API, or probe capacity through non-refundable member spawn calls. A returned agent and a freed host slot are distinct facts. |
| R2-09, R2-10, R2-14 | Confirm an old writer has stopped before transferring ownership. Preserve brief paths in first board posts so replacements can reconstruct their task. Do not treat board idle/dead as proof of a stopped process. Record waits before yielding with work remaining; reopening requires state reconciliation. |

## Recheck

The same reviewers independently returned **READY**: [product and roles](recheck-product-roles.md), [mechanics](recheck-mechanics.md), and [coordinator summary](recheck-summary.md). These reports preserve the exact input hashes and remaining nonblocking observations.

Final corrections before handoff:

- **O-1 / G-1 / N1:** At freeze, split compact mutable results from immutable baseline files. Hash the explicit deliverable path list, including requirements; keep every evidence-only record outside it. This resolves recursive invalidation while retaining a content check on requirements, rather than relying solely on a manually maintained revision label.
- **O-2 / N2:** Count returned agents as occupying capacity unless release is observed. Record whether the budget includes root, and stop repeated dispatch when an independent context cannot be obtained; report the needed fresh-session or host route.
- **O-3 / G-3:** Check enrollment only after the invocation posts its first `BRIEF:`, using `status --all-agents` so a completed invocation remains visible.
- **O-4 / G-2:** List inferred requirements that were not user-approved in the final report.
- **O-5:** Explicitly inject a self-review assignment in the future evaluation and require PM to refuse it.
- **O-6:** This was a review-snapshot naming mismatch. The retained reports use the names linked above; local relative-link verification passes. The final snapshot includes this actual directory layout.

The Claude coordinator verified the final document hashes and confirmed O-1 through O-6 resolved with no blockers: [final confirmation](final-confirmation.md). This plan has passed review; skill implementation and behavioral evaluations remain future work.
