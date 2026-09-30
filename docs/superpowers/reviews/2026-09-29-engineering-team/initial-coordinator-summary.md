# Claude adversarial review: coordinator summary

- Job: `engineering-team-plan-20260929`
- Coordinator: Plopper (swarm key `claude-review-coordinator`, role `review_coordinator`), Claude Opus 5.5 (`claude-opus-5-5`)
- Reviewer 1: Todd Flanders, Claude Opus 5.5. Product and role authority, requirement fulfillment, scope, proportionality, executability. Report: `reviews/reviewer-1-product-roles.md` (R1-01..R1-18: 7 high, 9 medium, 2 low).
- Reviewer 2: Uter Zorker, Claude Opus 5.5. Claude/Codex mechanics, caps, deadlocks, recovery, independence, stale evidence, evaluations. Report: `reviews/reviewer-2-mechanics.md` (R2-01..R2-15: 7 high, 6 medium, 2 low).
- Inputs: `docs/superpowers/specs/2026-09-29-engineering-team-design.md` (D) and `docs/superpowers/plans/2026-09-29-engineering-team.md` (P). Both reviewers checked them against the snapshot source. Neither changed anything outside `reviews/`.
- Independence: the reviewers wrote their reports separately. Neither read the other's report first.
- Enrollment: the hook enrolled both reviewers automatically, but `swarm who` shows their role as `general-purpose`, not `reviewer`. The installed plugin cache (0.1.0) predates custom-role support. See R1-16 and R2-11.
- Coordinator spot checks (verified in the snapshot):
  - `lib/swarm/hooks.py:1404-1410`: the main session gets no board injection (R2-01).
  - `lib/swarm/hosts/codex.py:242-244`: `fork_turns` "all" is the default (R2-03).
  - `docs/REFERENCE.md:1224-1226`: the completion gate checks only the latest verdict (R2-05).

## Must-fix before implementation (both reviewers, or verified blocking)

| # | Issue | IDs | Smallest correction |
|---|---|---|---|
| 1 | The evaluations grade themselves. Paper traces are called behavioral checks (P:58-59), dry runs have no way to cause the failures they test, and the "independent review" names no reviewer. | R1-07, R1-09, R2-12 | Pre-register expected gate states. A fresh agent runs the scenarios using only the skill, and a separate grader compares. Cause failures for real: a temporary `SWARM_CONFIG` with caps of 1, stopping an agent, a seeded defect, a requirement change after acceptance, and a Codex fork-header check. Add a negative control. A host whose dry run is "pending" cannot be described as working. |
| 2 | The dry runs omit readiness criteria in D:69: product and QA rejection and rerouting, and resolution of peer-review findings. | R1-08 | The host dry-run list is the union of D:69 and P:72. |
| 3 | Candidate hash and "affected" are undefined. Parallel engineers share one tree, committing artifacts changes the hash, and non-git work has no hash at all. | R1-14, R2-06 | One task = one branch or worktree = one commit. The EL names the integration commit. Artifact paths are excluded from the candidate. Define "affected". Use a sha256 manifest when there is no git. |
| 4 | Swarm does not detect a stale judge `met` or `VERIFIED`. | R2-05 | The verdict reason carries `candidate=<hash> req=<rev>`. The PM compares hashes before `deactivate`. |
| 5 | The PM (orchestrator) receives no board posts, so an EL that waits on a staffing request deadlocks. | R2-01 | No subagent blocks on a PM action. Requests go in the agent's final result. The PM runs `swarm read` at every wake-up. |
| 6 | Long-lived roles cause slot deadlock, and the four-slot example drops the product manager and EL before acceptance. | R2-02, R1-06 | Each role step is a bounded invocation. Resume the agent (Claude `SendMessage`, Codex `followup_task`) or re-spawn it from artifacts. Add an acceptance wave. Each acceptance decision must be reproducible from artifacts alone. |
| 7 | Codex children fork the parent's history by default, so review, QA, acceptance and the judge are not independent. | R2-03, R2-13, R2-04 | Require `fork_turns: "none"` for role spawns (acceptance unverified; the dry run must confirm it). The PM assigns reviewers. Reviewer briefs exclude the author's narrative. Reuse never covers self-review. |
| 8 | "Check actual available slots" cannot be done. Member spawn probes burn a cap that is never refunded. | R2-07, R2-08 | Use a declared slot budget. The PM spawns all long-lived roles at root. The EL exercises its staffing authority through the plan. |
| 9 | Authority gaps: the product manager sets and accepts its own baseline, nobody is named to disposition findings, and "material" is undefined. | R1-01, R1-03, R1-02 | Add a baseline gate against the request record, with inferences listed and confirmed by the user for moderate or complex work. The author never dispositions its own finding. Leaving a high or critical finding unfixed needs the user's decision. Define "material", and have the EL classify it. |
| 10 | Proportionality: roles are never merged, so a tiny fix still needs 5-6 agents. | R1-04, R1-05 | Add a sizing table: the minimum number of agents, which roles may merge, and the invariant separations. The EL confirms the PM's provisional complexity. |

## Should-fix

- Competitor research and Codex network access: R1-10.
- QA test writing and review, and who owns thresholds: R1-11.
- Product acceptance collapsing into QA evidence: R1-12.
- Escalation paths: R1-13.
- Checking against the user request, not only the design: R1-15.
- Crash fencing without the supervisor: R2-09.
- Durable role brief for Codex replacements: R2-10.
- Minimum Swarm version and role preflight: R1-16, R2-11.
- `swarm wait` against auto-close: R2-14.
- Validator path per host: R1-17, R2-15.
- Codex task-name examples: R1-18.

## Overlaps and disagreements

- **Overlap:** R1-07/R2-12, R1-09/R2-12, R1-14/R2-06, R1-16/R2-11 and R1-17/R2-15 were found independently, which increases confidence in them.
- **Tension on tiny work (R1-04 vs R2-03 and R2-04):** R1 proposes that the invoker combine the PM, product and EL roles for tiny work and give product acceptance. R2's independence findings argue against acceptance by an agent that carries the full orchestrating context. Suggested resolution: the invariant is only that the author never reviews or accepts its own change. Tiny work may merge roles if the reviewer is a separate agent with a clean context (`fork_turns: "none"` on Codex). The authors should decide this explicitly.
- **Staffing authority (R1 vs R2-08):** there is no real conflict. R1 keeps staffing authority with the EL, as the user required. R2 moves spawn execution to the PM. Both hold if the EL decides staffing and the PM executes all root spawns.
- **Enrollment labels:** Krusty says the `general-purpose` labels are not a plan defect. Both reviewers accept that the runtime cause is an old install, but still require a version prerequisite and a preflight in the plan.
- **Unverified (dry run must measure):**
  - host concurrency caps;
  - when a Codex child frees its slot;
  - what happens to a Claude subagent's background children after it returns;
  - whether `fork_turns: "none"` is accepted;
  - worktree isolation on Codex.
