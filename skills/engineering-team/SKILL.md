---
name: engineering-team
description: Use when a software project needs a coordinated engineering lead, engineers, peer review, QA, independent judge, and optionally a product manager and build engineer, working through a Swarm job in Claude Code or Codex.
---

# Engineering team

Use `swarm:swarm` for the job and board. The engineering lead owns technical staffing and persists executable dependencies with the [native task workflow](references/dependencies.md). Swarm reconciles those records and launches eligible work even after the lead and requester end their turns. A board promise to dispatch later is not a registered dependency. Roles identify responsibilities; they do not grant authority to change job lifecycle state.

Read [host procedures](references/hosts.md) before staffing a job. They cover the drive activation checklist, the EL brief template, Claude and Codex syntax for non-drive jobs, and recovery. Read [role authority](references/team-roles.md) before writing the lead brief, [artifact contracts](references/artifacts.md) before creating baselines or freezing a candidate, [integration gates and hand-offs](references/gates.md) before the first change is opened for review, and [authorization and recovery](references/recovery.md) before writing briefs and when a launch is lost. Use the project's existing document layout; a small change can use compact records that preserve the same decisions.

## How a goal job runs

1. Use the existing owner-authorized job and confirm its goal, workdir, runtime health and role model policy. Agents never activate, deactivate, pause, resume, wait or otherwise set job status.
2. EL records the candidate's immutable artifact reference and registers every known downstream stage with `swarm task add` before its predecessors or EL exit. Include independent review, QA, build and final verification where required. See [exact commands and brief contract](references/dependencies.md).
3. Each assigned executor reports structured exact-artifact evidence with `swarm task report`, then ends its turn. Evidence plus successful termination releases downstream tasks. A passing review alone does not complete a job with unfinished required stages.
4. Observe `swarm task list --job J` and `swarm status --job J`. Swarm derives active from actual work and idle when no agent is working; internal dependencies are task blockers. Ask the owner a specific question only when a human decision is required.
5. Report delivered scope, checks, limits and the verdict. Do not restart completed agents just to keep the job active or dispatch a stage already registered or already executed.

Use the user's role model policy explicitly. For this workflow, EL and independent judge use Astra, engineers use GPT-6.1, and QA uses Luna/Terra as available and requested. Resolve supported model identifiers through configured role mappings; report unavailable required models instead of silently substituting legacy Haiku/Sonnet/Opus defaults. An independent reviewer must not review its own change.

### Rules for every launch and brief

- Never put work trees, clones, builds or scratch repos under `/tmp` (the driver refuses such workdirs); one worktree per writer under the job's work roots.
- On the box run targeted tests only, through `/var/tmp/test-slots/test-slot -- <cmd>`; full suites run in CI.
- Wait for CI with `swarm ci wait --repo OWNER/REPO --sha <exact head> --timeout 540` in the foreground, repeated while pending; never `gh run watch`.
- Judges only judge; reviewers only review; fixes happen on writer launches.
- Briefs state what is authorized and what is not; secrets are referenced by path only ([recovery](references/recovery.md)).
- Every CLI command that changes a drive record prints `swarm-record: kind=... id=... rev=...` last; in a drive job the CLI never spools a command, it fails with an error.

## Waiting for CI

This skill never talks to a CI host. It depends on the [ci skill](../ci/SKILL.md): wait for CI ONLY with
`swarm ci wait --repo OWNER/REPO --sha <exact head> [--timeout 90m]` (exit 0 green, 1 failed, 124 timeout),
`swarm ci status` for a one-off look, never `gh run watch` or a `gh run list` loop. Pass the exact head SHA
and re-run it after every push. Run it in the foreground, never as a background command: a background wait
outlives the agent that started it. Put this line in every worker, engineer and reviewer brief:
`CI: wait for CI ONLY with swarm ci wait --repo OWNER/REPO --sha <exact head> [--timeout 90m] in the foreground; never gh run watch.`
Never write `gh run watch` into a brief, even as an example. The swarm hooks refuse a member's
`gh run watch`, `gh pr checks --watch` and gh/curl poll loops, and point the agent at the exact
`swarm ci wait` instead. A one-shot `gh run view <id> --json conclusion` stays allowed for judges.
A member's background shell commands run under `swarm bg`, in a fresh `bash -c`, so the shell's
aliases and functions are not available to them. If one is still running after its agent
finishes or the job closes, it is reaped (`swarm bg list --orphans`).

## CI host

`[ci] kind = "github"|"gitea"|"gitlab"|"none"` in `team.toml` names the host (setup, webhooks and tokens
are in the ci skill). Set `evidence_command` to the project's CI checker with `{sha}` when `swarm ci wait`
does not apply: zero exit status must mean all required checks passed on that exact rebased SHA. An absent
checker holds integration. Completing a request must preserve the approved rebased SHA and linear history.

## Jobs without a goal

Everything below applies only to a job without a goal: the invoking agent coordinates as PM, but still registers dependent work before ending a turn and never changes job status.

### Run the team (non-drive)

1. Team composition: always present are EL, QA, one or more engineers, and an independent judge. `product_manager` and `build_engineer` are optional and chosen by the user (`team.toml`, or per job). Before staffing run `swarm team --job J --show` and staff exactly the effective set; change it only on the user's say-so with `swarm team --job J --add|--remove ROLE` (a mandatory role cannot be removed). If a role is absent, its duties move as in [role authority](references/team-roles.md): without `product_manager`, EL writes compact PR criteria and EL+QA record product acceptance; without `build_engineer`, EL owns its duties.
2. Small work: skip the team and do it yourself. Otherwise capture the user's request verbatim or by stable link, constraints, approvals already given, and a provisional size. For moderate and complex jobs, use an owner-authorized goal job and register its independent judge; record the reason if omitted. Join the board as PM and read it at each turn start and before each staffing wave. Do not wait for a subagent's board post alone: a role needing PM action must also return a structured request.
3. If `product_manager` is present, ask it for a revisioned product brief: problem, applicable competitor research, prioritized `PR` requirements, observable criteria, marked inferences, and exclusions. Compare its first baseline to the original request yourself. Return unsupported inferences for correction or route a consequential ambiguity to the user before dependent work; continue unaffected work. A product manager can require escalation of a blocking ambiguity. Without one, EL writes the compact PR criteria and PM still compares them to the request.
4. Ask the engineering lead (EL) for architecture, `ER` requirements mapped to `PR`, implementation tasks, dependency order, final complexity tier, engineer count, and review/test plan. EL owns staffing and assignments. Schedule its requested roles through root spawns in bounded waves, within observed host capacity. Send infeasible capacity back to EL for repartitioning. Never treat Swarm's child-spawn cap as the whole team limit. Tag every spawn with its launch title, `[swarm title: EL]`, `PM`, `Product`, `QA`, `Judge`, `Build`, `Reviewer`, `Verifier`, `Eng` (an engineer may add a short scope, `Eng: board view`); the invoking PM sets its own with `swarm join --title PM` or `swarm title`. `swarm team --job J --show` lists them. Titles are display labels on the board, separate from roles and addressing. EL keeps them current: when an engineer's scope changes, run `swarm title --job J --as "<name>" "Eng: <new scope>"`.
5. Give engineers distinct owned tasks and immutable diffs or commits against named bases. PM assigns a fresh independent reviewer for every delivered change, including QA test code. Authors acknowledge findings, fix them, and return them to the reviewer for recheck. Follow the finding authority in [role authority](references/team-roles.md); no author accepts its own risk.
6. QA may write tests before integration; another engineer reviews any test code. EL integrates reviewed tasks and freezes the candidate using the [source manifest](references/artifacts.md) **before final checks**. QA then runs applicable acceptance, integration, end-to-end, and performance checks on that candidate. Product manager (or, if absent, EL and QA together, recorded) directly checks delivered behavior against each criterion; EL checks architecture and ERs; QA records test evidence and omissions with EL concurrence. Each final result names the candidate ID and requirement revision. A rejection returns to the owner for repair, independent review, a new freeze, affected retest, and refreshed acceptance.
7. Recompute the current candidate and compare its ID and requirement revision with each final product, EL, and QA result. If the goal job has a judge, require `candidate=<id> req=<revision>` in its `met` verdict reason and compare those values before reporting completion: Swarm's gate checks the latest verdict, not its freshness. Report delivered scope, checks, limits, accepted risks, and any separate authorization needed for deployment or publication.

### Rules of the road

The engineering-team plugin supplies the coding adapter for Swarm's generic review pipeline.
Publish `swarm done --job J --as NAME --branch B --sha S --summary "change and checks"` after
pushing. The owner supervisor starts independent artifact-bound review and bounded fix rounds.
A hook-registered judge (spawned with `[swarm role: judge]` or `[swarm title: Judge]`) is launched by its hooks and records `swarm verdict --job J --artifact branch@sha met --reason "evidence"`
(or `not_met --reason "missing" --next "fix brief"`; a long report goes in `--details -` on stdin, never in a file: judges cannot write files, and `swarm verdict show --job J` prints it). **Always pass `--artifact` for the exact
hand-off inspected.** A leading `branch@sha:` in the reason is accepted for compatibility.
A judge run outside the board (a one-off `codex exec`, a person) must still record its result with
`swarm verdict --job J --as NAME --artifact REF met|not_met ...` (`swarm join --job J --key K --judge`
first; a judge with hooks never joins and never passes `--as`: its hooks add its own name); otherwise the goal stays unmet and the job waits until the stale-waiting sweep closes it. Swarm derives completion only after all required stages and finalization are complete; agents never close the job.
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
- Keep CI, review and build dependencies in registered tasks. Report their precise blockers; never change job status to represent internal waiting.
- Verdicts are bound to a head sha and posted on the change record, not only the board; integrate on the current head's verdict, green CI, and no later changes-required ([gates](references/gates.md)).
- Briefs state what is authorized and what is not; secrets are referenced by path only ([recovery](references/recovery.md)).
- Final reports and `DONE:` lines follow the hand-off contract in [gates](references/gates.md).

### Don't (non-drive)

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

### PM procedure per event

The PM does not poll. Keep `swarm event wait --job J --to @pm --json` armed as a harness-tracked
background command; when it returns an event, act, `swarm event ack --job J ID`, then re-arm
it at once. CI events (raised by the `ci` skill, see its SKILL.md) and `BRANCH READY ...` board posts; the dedupe key is `kind:N@sha`, so an
event is never raised twice for the same head. Full steps: [PM event procedure](references/pm-events.md).

- `NEEDS-REVIEW N@sha`: dispatch a fresh independent reviewer from the review template and record the dispatch.
- `BRANCH-READY branch@sha`: start review, as for NEEDS-REVIEW.
- `REVIEW-CHANGES N@sha`: send the findings back to the coder.
- `CI-FAILED N@sha <contexts>`: read the failing logs, diagnose, then send it back to the coder with the cause.
- `CI-GREEN sha`: the exact head is green; if a verdict is pending, nothing to wait for any more; `swarm ci wait` returns at once.
- `READY-TO-LAND N@sha`: land per `[land] strategy`, verify the landed tree equals the reviewed head's tree, close the PR and its issue.

`[land] strategy` in `team.toml` is per project: `rebase-ff` (default; rebase, then fast-forward, no
squash and no merge commit) or `squash-ff` (one squashed commit, then fast-forward). Never force-push
the target branch.
