---
name: engineering-team
description: Use when a software project needs a coordinated engineering lead, engineers, peer review, QA, independent judge, and optionally a product manager and build engineer, working through a Swarm job in Claude Code or Codex.
---

# Engineering team

Use `swarm:swarm` for the job and board. You, the invoking agent, are the project manager (PM): keep the original request and authorization visible, own scheduling and reports, and make host-level spawns. Custom Swarm roles identify responsibilities; they do not grant product, technical, review, or QA authority in code. The built-in `judge` and `verifier` have separate Swarm controls.

Read [host procedures](references/hosts.md) before activating a job or spawning a role. They cover Claude and Codex syntax, the installed custom-role preflight, board reads, slot budgeting, and recovery. Read [role authority](references/team-roles.md) before assigning work, [artifact contracts](references/artifacts.md) before creating baselines or freezing a candidate, [integration gates and hand-offs](references/gates.md) before the first change is opened for review, and [authorization and recovery](references/recovery.md) before writing briefs and when an agent is lost. Use the project's existing document layout; a small change can use compact records that preserve the same decisions.

## Run the team

1. Team composition: always present are EL, QA, one or more engineers, and an independent judge. `product_manager` and `build_engineer` are optional and chosen by the user (`team.toml`, or per job). Before staffing run `swarm team --job J --show` and staff exactly the effective set; change it only on the user's say-so with `swarm team --job J --add|--remove ROLE` (a mandatory role cannot be removed). If a role is absent, its duties move as in [role authority](references/team-roles.md): without `product_manager`, EL writes compact PR criteria and EL+QA record product acceptance; without `build_engineer`, EL owns its duties.
2. Small work: skip the team and do it yourself. Otherwise capture the user's request verbatim or by stable link, constraints, approvals already given, and a provisional size. For moderate and complex jobs, activate with a goal and schedule one root judge as recommended; record the reason if omitted. Join the board as PM and read it at each turn start and before each staffing wave. Do not wait for a subagent's board post alone: a role needing PM action must also return a structured request.
3. If `product_manager` is present, ask it for a revisioned product brief: problem, applicable competitor research, prioritized `PR` requirements, observable criteria, marked inferences, and exclusions. Compare its first baseline to the original request yourself. Return unsupported inferences for correction or route a consequential ambiguity to the user before dependent work; continue unaffected work. A product manager can require escalation of a blocking ambiguity. Without one, EL writes the compact PR criteria and PM still compares them to the request.
4. Ask the engineering lead (EL) for architecture, `ER` requirements mapped to `PR`, implementation tasks, dependency order, final complexity tier, engineer count, and review/test plan. EL owns staffing and assignments. Schedule its requested roles through root spawns in bounded waves, within observed host capacity. Send infeasible capacity back to EL for repartitioning. Never treat Swarm's child-spawn cap as the whole team limit. Tag every spawn with its seat title, `[swarm title: EL]`, `PM`, `Product`, `QA`, `Judge`, `Build`, `Reviewer`, `Verifier`, `Eng` (an engineer may add a short scope, `Eng: board view`); the invoking PM sets its own with `swarm join --title PM` or `swarm title`. `swarm team --job J --show` lists them. Titles are display labels on the board, separate from roles and addressing. EL keeps them current: when an engineer's scope changes, run `swarm title --job J --as "<name>" "Eng: <new scope>"`.
5. Give engineers distinct owned tasks and immutable diffs or commits against named bases. PM assigns a fresh independent reviewer for every delivered change, including QA test code. Authors acknowledge findings, fix them, and return them to the reviewer for recheck. Follow the finding authority in [role authority](references/team-roles.md); no author accepts its own risk.
6. QA may write tests before integration; another engineer reviews any test code. EL integrates reviewed tasks and freezes the candidate using the [source manifest](references/artifacts.md) **before final checks**. QA then runs applicable acceptance, integration, end-to-end, and performance checks on that candidate. Product manager (or, if absent, EL and QA together, recorded) directly checks delivered behavior against each criterion; EL checks architecture and ERs; QA records test evidence and omissions with EL concurrence. Each final result names the candidate ID and requirement revision. A rejection returns to the owner for repair, independent review, a new freeze, affected retest, and refreshed acceptance.
7. Recompute the current candidate and compare its ID and requirement revision with each final product, EL, and QA result. If the goal job has a judge, require `candidate=<id> req=<revision>` in its `met` verdict reason and compare those values before closing the job: Swarm's gate checks the latest verdict, not its freshness. Report delivered scope, checks, limits, accepted risks, and any separate authorization needed for deployment or publication.

## Rules of the road

The engineering-team plugin supplies the coding adapter for Swarm's generic review pipeline.
Publish `swarm done --job J --as NAME --branch B --sha S --summary "change and checks"` after
pushing. The owner supervisor starts independent artifact-bound review and bounded fix rounds.
A hook-registered judge (spawned with `[swarm role: judge]` or `[swarm title: Judge]`) is seated by its hooks and records `swarm verdict --job J --artifact branch@sha met --reason "evidence"`
(or `not_met --reason "missing" --next "fix brief"`). **Always pass `--artifact` for the exact
hand-off inspected.** A leading `branch@sha:` in the reason is accepted for compatibility.
A judge run outside the board (a one-off `codex exec`, a person) must still record its result with
`swarm verdict --job J --as NAME --artifact REF met|not_met ...` (`swarm join --job J --key K --judge`
first; a judge with hooks never joins and never passes `--as`: its hooks add its own name); otherwise the goal stays unmet and the job waits until the stale-waiting sweep closes it. Close
the job as soon as the verdict is `met`.
Judges only judge; integration, pushes and fixes belong to executors. A newer SHA on the same
branch supersedes its earlier hand-off; separate branches keep independent verdicts.
A separate INTEGRATOR rebases the branch onto the latest target locally with `git rebase`,
then uses fast-forward/push of the rebased branch. Open a Merge Request / Pull Request where
supported as the review and CI vehicle; without a CI host/request, push the rebased branch directly.
A changed SHA needs a new hand-off and judge verdict. Require a met verdict and the project's
CI green on the exact rebased SHA before advancing the target. Push all remotes/push URLs,
delete the source branch after successful integration, and post `INTEGRATED branch@sha`.
Non-trivial conflicts hand back to a worker through `FINALIZE_BLOCKED`. Configure `[pipeline]
integrate`, `target_branch`, `delete_branch` and a separate `[ci]` adapter in `team.toml`,
with optional per-repository overrides. Select the CI host explicitly; commands belong to its adapter.
See [review pipeline](../../docs/REFERENCE.md#review-pipeline) and `team.example.toml`.

- Address by role: `swarm post --to @EL|@PM|@product|@QA|@judge|@build_engineer`, `@PM` means the invoking project manager and `@product` the optional product manager; never by display name; an unknown role or one with no holder is rejected. Send trivial messages straight to the peer, not through EL.
- Long text goes in a file; the post is a one-line pointer (posts are 200 characters).
- When the team is only waiting on CI, reviews, or a user, run `swarm wait --job J --on "<what>" --for <duration>` (or `--until <time>`) so the job reads as waiting, not orphaned; the end time is what protects it, so renew it before it passes and `swarm resume --job J` when work resumes.
- Verdicts are bound to a head sha and posted on the change record, not only the board; integrate on the current head's verdict, green CI, and no later changes-required ([gates](references/gates.md)).
- Briefs state what is authorized and what is not; secrets are referenced by path only ([recovery](references/recovery.md)).
- Final reports and `DONE:` lines follow the hand-off contract in [gates](references/gates.md).

## Waiting for CI

Agents never watch CI with `gh`. Every agent on a box that ran its own `gh run watch` (3 s polling) burned the
whole 5000/h GitHub API budget in minutes. **wait for CI ONLY with `swarm ci wait --repo OWNER/REPO --sha <exact head> [--timeout 90m]` (exit 0 green, 1 failed with the failing jobs, 124 timeout); `swarm ci status` for a one-off look. Never `gh run watch`, never a `gh run list` loop.**
`swarm ci` is budget-safe: one shared cache and one poller per box (`~/.cache/swarm/ci/OWNER/REPO/SHA.json`), at most
one CI host call per repo per 60 s across all agents, backoff 60/120/300 s on queued runs, 10 min when under 500 calls
remain, and a fall back to the public API on a 403 (it says so). When the event core is installed and a CI event
(`CI-FAILED`, `READY-TO-LAND`) for that exact SHA arrives, the wait returns from the event without polling.
The CI host comes from `[ci] kind` (`github` or `gitea`; for Gitea set `api_url` and `token_file`, a path).
Pass the exact head SHA of the change (never a branch name), and re-run it with the new SHA after every push.
Put this rule in **every worker, engineer and reviewer brief** and in every spawn prompt that mentions CI:
`CI: wait for CI ONLY with swarm ci wait --repo OWNER/REPO --sha <exact head> [--timeout 90m] (exit 0 green, 1 failed with the failing jobs, 124 timeout); swarm ci status for a one-off look. Never gh run watch, never a gh run list loop.`

Don't:

- watch CI with `gh run watch`, a `gh run list` loop or any direct `gh`/API polling: use `swarm ci wait`;
- use PM polling as the primary trigger for review or integration; react to change events and keep polling only as a safety net;
- pin an integration watcher per change by hand; run one loop keyed on the head sha;
- keep verdicts only on the board;
- run the full gate on every fix round;
- address agents by random display names;
- create a one-agent job plus a judge as ceremony (small work: do it yourself);
- integrate by hand or bypass review as PM;
- relay "review posted" without the head sha.

Do not call a host validated because these instructions exist or checkout unit tests pass. A host needs the installed custom-role capability and an independent behavioral run. If research tools, an independent reviewer, a safe writer handoff, or a required host capability are unavailable, record the specific blocker and continue independent work that remains possible.


## CI adapters

`[ci] kind = "github"|"gitea"|"gitlab"|"none"` selects the adapter explicitly; the default
is `none`. Set `evidence_command` to the project's CI checker with `{sha}`; zero exit status
must mean all required checks passed on that exact rebased SHA, never a latest-branch run.
GitHub has a built-in `gh run list --commit SHA` checker and uses `gh pr create/view` for PRs.
Gitea uses `tea`/API for MRs/PRs and configured CI checks; GitLab uses `glab`/API for MRs and
configured CI checks. `none` uses a configured project CI command and direct pushes.
An absent CI checker holds integration. Request completion must preserve the approved rebased
SHA and linear history. Commands and authentication belong to the adapter, never generic core.

## PM procedure per event

The PM does not poll. Keep `swarm event wait --job J --to @pm --json` armed as a harness-tracked
background command; when it returns an event, act, `swarm event ack --job J ID`, then re-arm
it at once. CI events come from the `ci` plugin's `gitea` or `github` event source (enabled by `[ci.gitea]` /
`[ci.github]` in the swarm config, never assumed) and from `BRANCH READY ...` board posts; the dedupe key is `kind:N@sha`, so an
event is never raised twice for the same head. Full steps: [PM event procedure](references/pm-events.md).

- `NEEDS-REVIEW N@sha`: dispatch a fresh independent reviewer from the review template and record the dispatch.
- `BRANCH-READY branch@sha`: start review, as for NEEDS-REVIEW.
- `REVIEW-CHANGES N@sha`: send the findings back to the coder.
- `CI-FAILED N@sha <contexts>`: read the failing logs, diagnose, then send it back to the coder with the cause.
- `READY-TO-LAND N@sha`: land per `[land] strategy`, verify the landed tree equals the reviewed head's tree, close the PR and its issue.

`[land] strategy` in `team.toml` is per project: `rebase-ff` (default; rebase, then fast-forward, no
squash and no merge commit) or `squash-ff` (one squashed commit, then fast-forward). Never force-push
the target branch.
