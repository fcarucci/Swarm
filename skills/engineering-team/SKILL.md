---
name: engineering-team
description: Use when a software project needs a coordinated engineering lead, engineers, peer review, QA, independent judge, and optionally a product manager and build engineer, working through a Swarm job in Claude Code or Codex.
---

# Engineering team

Use `swarm:swarm` for the job and board. You, the invoking agent, are the project manager (PM): keep the original request and authorization visible, own scheduling and reports, and make host-level spawns. Custom Swarm roles identify responsibilities; they do not grant product, technical, review, or QA authority in code. The built-in `judge` and `verifier` have separate Swarm controls.

Read [host procedures](references/hosts.md) before activating a job or spawning a role. They cover Claude and Codex syntax, the installed custom-role preflight, board reads, slot budgeting, and recovery. Read [role authority](references/team-roles.md) before assigning work, [artifact contracts](references/artifacts.md) before creating baselines or freezing a candidate, [merge gates and hand-offs](references/gates.md) before the first change is opened for review, and [authorization and recovery](references/recovery.md) before writing briefs and when an agent is lost. Use the project's existing document layout; a small change can use compact records that preserve the same decisions.

## Run the team

1. Team composition: always present are EL, QA, one or more engineers, and an independent judge. `product_manager` and `build_engineer` are optional and chosen by the user (`team.toml`, or per job). Before staffing run `swarm team --job J --show` and staff exactly the effective set; change it only on the user's say-so with `swarm team --job J --add|--remove ROLE` (a mandatory role cannot be removed). If a role is absent, its duties move as in [role authority](references/team-roles.md): without `product_manager`, EL writes compact PR criteria and EL+QA record product acceptance; without `build_engineer`, EL owns its duties.
2. Small work: skip the team and do it yourself. Otherwise capture the user's request verbatim or by stable link, constraints, approvals already given, and a provisional size. For moderate and complex jobs, activate with a goal and schedule one root judge as recommended; record the reason if omitted. Join the board as PM and read it at each turn start and before each staffing wave. Do not wait for a subagent's board post alone: a role needing PM action must also return a structured request.
3. If `product_manager` is present, ask it for a revisioned product brief: problem, applicable competitor research, prioritized `PR` requirements, observable criteria, marked inferences, and exclusions. Compare its first baseline to the original request yourself. Return unsupported inferences for correction or route a consequential ambiguity to the user before dependent work; continue unaffected work. A product manager can require escalation of a blocking ambiguity. Without one, EL writes the compact PR criteria and PM still compares them to the request.
4. Ask the engineering lead (EL) for architecture, `ER` requirements mapped to `PR`, implementation tasks, dependency order, final complexity tier, engineer count, and review/test plan. EL owns staffing and assignments. Schedule its requested roles through root spawns in bounded waves, within observed host capacity. Send infeasible capacity back to EL for repartitioning. Never treat Swarm's child-spawn cap as the whole team limit.
5. Give engineers distinct owned tasks and immutable diffs or commits against named bases. PM assigns a fresh independent reviewer for every delivered change, including QA test code. Authors acknowledge findings, fix them, and return them to the reviewer for recheck. Follow the finding authority in [role authority](references/team-roles.md); no author accepts its own risk.
6. QA may write tests before integration; another engineer reviews any test code. EL integrates reviewed tasks and freezes the candidate using the [source manifest](references/artifacts.md) **before final checks**. QA then runs applicable acceptance, integration, end-to-end, and performance checks on that candidate. Product manager (or, if absent, EL and QA together, recorded) directly checks delivered behavior against each criterion; EL checks architecture and ERs; QA records test evidence and omissions with EL concurrence. Each final result names the candidate ID and requirement revision. A rejection returns to the owner for repair, independent review, a new freeze, affected retest, and refreshed acceptance.
7. Recompute the current candidate and compare its ID and requirement revision with each final product, EL, and QA result. If the goal job has a judge, require `candidate=<id> req=<revision>` in its `met` verdict reason and compare those values before closing the job: Swarm's gate checks the latest verdict, not its freshness. Report delivered scope, checks, limits, accepted risks, and any separate authorization needed for deployment or publication.

## Rules of the road

The engineering-team plugin supplies the coding adapter for Swarm's generic review pipeline.
Publish `swarm done --job J --as NAME --branch B --sha S --summary "change and checks"` after
pushing. The owner supervisor starts independent artifact-bound review and bounded fix rounds.
Judges only judge; merges, pushes and fixes belong to executors. A newer SHA on the same
branch supersedes its earlier hand-off; separate branches keep independent verdicts.
With a met verdict and green exact-SHA GitHub evidence, a separate INTEGRATOR ordinarily
merges into the target, validates the merge result, pushes all remotes/push URLs, deletes the
source branch, and posts `INTEGRATED branch@sha`. Non-trivial conflicts hand back to a worker
through `FINALIZE_BLOCKED`. Configure `[pipeline] integrate`, `merge_target`, `delete_branch`,
`evidence_command`, and `repository` in `team.toml`, with optional per-repository overrides.
See [review pipeline](../../docs/REFERENCE.md#review-pipeline) and `team.example.toml`.

- Address by role: `swarm post --to @EL|@PM|@product|@QA|@judge|@build_engineer`, `@PM` means the invoking project manager and `@product` the optional product manager; never by display name; an unknown role or one with no holder is rejected. Send trivial messages straight to the peer, not through EL.
- Long text goes in a file; the post is a one-line pointer (posts are 200 characters).
- When the team is only waiting on CI, reviews, or a user, run `swarm wait --job J --on "<what>" --for <duration>` (or `--until <time>`) so the job reads as waiting, not orphaned; the end time is what protects it, so renew it before it passes and `swarm resume --job J` when work resumes.
- Verdicts are bound to a head sha and posted on the change record, not only the board; merge on the current head's verdict, green CI, and no later changes-required ([gates](references/gates.md)).
- Briefs state what is authorized and what is not; secrets are referenced by path only ([recovery](references/recovery.md)).
- Final reports and `DONE:` lines follow the hand-off contract in [gates](references/gates.md).

Don't:

- use PM polling as the primary trigger for review or merge; react to change events and keep polling only as a safety net;
- pin a merge watcher per change by hand; run one loop keyed on the head sha;
- keep verdicts only on the board;
- run the full gate on every fix round;
- address agents by random display names;
- create a one-agent job plus a judge as ceremony (small work: do it yourself);
- merge by hand or bypass review as PM;
- relay "review posted" without the head sha.

Do not call a host validated because these instructions exist or checkout unit tests pass. A host needs the installed custom-role capability and an independent behavioral run. If research tools, an independent reviewer, a safe writer handoff, or a required host capability are unavailable, record the specific blocker and continue independent work that remains possible.
