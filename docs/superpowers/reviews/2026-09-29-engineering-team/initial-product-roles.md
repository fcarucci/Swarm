# Reviewer 1: product and role authority review

- **Reviewer:** Todd Flanders (swarm name), Reviewer 1, independent adversarial reviewer
- **Model:** Claude Opus 5.5 (`claude-opus-5-5`)
- **Job:** `engineering-team-plan-20260929`
- **Inputs:** `docs/superpowers/specs/2026-09-29-engineering-team-design.md` (cited as D:line), `docs/superpowers/plans/2026-09-29-engineering-team.md` (cited as P:line), `skills/swarm/SKILL.md`, `lib/swarm/roles.py`, `lib/swarm/hooks.py`, `lib/swarm/hosts/claude.py`, `config.example.toml`, and the live roster of this job.
- **Independence:** I did not coordinate with Reviewer 2 before writing this. I read the board only for context.
- **Scope:** product and role authority, whether each user requirement is actually met, missing decisions, acceptance and scope control, proportionality, and whether the plan can be executed. Host mechanics are Reviewer 2's area. I cover them only where they decide whether a user requirement can be met.

## Summary

The design has the right shape. Its role table, separated acceptance decisions, traceability IDs, revision invalidation, and "no fixed four-person team" position are sound. It fails in four places:

1. **Product scope is set and judged by the same role.** The product manager writes the requirements, sets the baseline, and accepts against it. The user normally sees none of it before the final report (R1-01).
2. **"Material" is undefined.** Several mandatory controls depend on it, including independent peer review, which the user required without qualification (R1-02). Who may disposition a finding is also left open (R1-03).
3. **Proportionality is handled only by merging artifacts.** Roles are never merged. A one-line fix still needs a PM, product manager, EL, engineer, reviewer and QA. Complexity is classified by the PM before the EL exists (R1-04, R1-05).
4. **The plan's verification relies on self-written paper walkthroughs.** The plan's host dry runs omit two readiness criteria from the design: product and QA rejection, and peer-review resolution (R1-07, R1-08).

Severity counts: 0 critical, 7 high, 9 medium, 2 low.

## Requirement fulfillment map

| # | Original user requirement | Where addressed | Verdict |
|---|---|---|---|
| U1 | PM does scheduling and reporting | D:11, D:19, D:57, D:61; P:21, P:70 | Present. Reporting cadence is left open on purpose (D:57). Acceptable. |
| U2 | Product manager writes specifications | D:20, D:35, D:46; P:56-57 | Present. The baseline is self-approved (R1-01). |
| U2a | Product manager does competitor research | D:20 "when relevant", D:35; P:25 | Weakened. The product manager decides relevance on its own. Codex swarm sessions have no network by default (R1-10). |
| U2b | Product manager writes requirements | D:35 (PR IDs) | Present |
| U2c | Product manager accepts the delivered product | D:20, D:50, D:53 | Present on paper. It can collapse into re-reading QA evidence (R1-12). The four-slot example leaves no product manager for the acceptance wave (R1-06). |
| U3 | EL owns architecture, software requirements and the implementation plan | D:21, D:36 | Present |
| U3a | EL staffs engineers by complexity | D:11, D:47; P:70 | Weakened. The PM classifies complexity at intake, before the EL is involved (D:45) (R1-05). |
| U3b | EL verifies adherence | D:21 "integration review", D:49, D:50 | Present. The EL is also absent from the example's final wave (R1-06). |
| U4 | Engineer pool implements | D:22, D:48 | Present |
| U4a | Engineers independently peer-review | D:23, D:49 | Weakened. Only "each material change" is reviewed (D:49), and "material" is undefined (R1-02). |
| U4b | Engineers accept or fix reviews | D:49 | Partly present. Who may accept a risk instead of fixing it is undefined (R1-03). |
| U5 | QA writes acceptance, e2e, performance and integration tests | D:24, D:50; P:24 | Weakened. QA "runs the tests warranted" and writes "automated tests where appropriate". QA alone decides what to omit, and nobody reviews QA's tests (R1-11). |
| U6 | Works with Claude and Codex | D:59-61; P:19, P:69, P:72 | Planned. There is no minimum Swarm version (R1-16), no Codex network prerequisite (R1-10), and no dry-run exit criteria (R1-07). |
| U7 | No fixed four-person team | D:9, D:61; P:17 | Met |

## Findings

### R1-01 (High): The product baseline is written, approved and accepted by the same role, and the user never sees the inferred requirements

**Where:** D:13 ("reasonably inferred requirements"), D:20, D:35, D:46 ("The product manager marks the version passed to EL as the baseline"), D:50; P:22 ("cannot redefine the request by signing off its own revision").

**Problem:** The product manager writes the PRs, marks them as the baseline, and later accepts the build against them. Nobody checks the baseline against the request record. The PM asks the user only when a choice "cannot be safely inferred" (D:45-46), and the PM alone decides that. P:22 bans signing off "its own revision", but the first baseline is exactly that and is not covered. If the product manager and PM disagree about whether to ask the user, there is no tie-break.

**Scenario:** The user asks for "CSV export of the orders list". The product manager infers PR-3, "export current page only, max 1,000 rows", as a reasonable inference. The EL builds it, QA tests it, and the product manager accepts. The user wanted every order for accounting. The mismatch surfaces only in the final report, after all the implementation work, and no rule was broken along the way.

**Smallest correction:**
- Add a baseline gate before technical design. The PM, or the judge on a goal job, checks the product brief against the request record.
- The brief lists every inference separately.
- For moderate and complex work, send the user one consolidated confirmation of the material inferences while unaffected work continues.
- For tiny work, list the inferences in the final report as "inferred, not user-approved".
- Let the product manager require escalation to the user. The PM cannot veto it, only schedule it.

### R1-02 (High): "Material" is undefined, yet it decides whether independent peer review, the judge and rollback notes apply

**Where:** D:49 ("An engineer who did not author the change reviews each **material** change"), D:26 (judge "recommended for material jobs"), D:36 ("rollback/recovery notes for material changes"), D:51 ("material peer findings are closed"), D:23 ("material findings"). The plan repeats the term without defining it (P:56).

**Problem:** The user required independent peer review with no qualifier. The design lets unnamed parties skip it for "non-material" changes. The author or the PM (who is under schedule pressure) will classify changes. The same word also decides whether a job gets a judge and which findings must close.

**Scenario:** An engineer marks a "small refactor" of an auth check as non-material. No reviewer is scheduled. The completion check at D:51 passes because no material findings exist.

**Smallest correction:**
- Every delivered change gets an independent review, and review depth scales with risk.
- Define "material" once, with criteria such as user-visible behavior, security or data, public interfaces, migrations, or more than one component.
- The EL, not the author, classifies each change and records the classification in the work item.

### R1-03 (High): Nobody is named to disposition findings, and "accepted risk" contradicts "blocks until resolved"

**Where:** D:23 (reviewer approves once material findings are "resolved **or explicitly recorded as accepted risk**"), D:49 ("fixes it or requests a documented risk decision" and "A critical or high finding blocks technical acceptance until resolved"); P:56 ("State who may disposition findings", deferred to the implementer).

**Problem:** D:23 lets a material finding close as accepted risk. D:49 says critical and high findings block until resolved, and it does not say whether accepted risk counts as resolved. Neither line says who may make the risk decision. The plan hands this core authority decision to whoever writes `team-roles.md`.

**Scenario:** A reviewer files a high finding, "SQL built by string concatenation". The author requests a risk decision. The EL, pressed for time, records it as accepted risk. Technical acceptance passes, and the user never learns that a high-severity defect shipped.

**Smallest correction:** Decide this in the design:
- The author never dispositions its own finding.
- Medium and low findings can be dispositioned by the EL, with the reviewer confirming.
- Leaving a critical or high finding unfixed requires a user decision via the PM, with the EL's recommendation, and must be listed in the final report.
- The reviewer rechecks every fix.

### R1-04 (High): Proportionality only merges artifacts, never roles, so tiny tasks still need five or six agents

**Where:** D:9 ("A small task may use one engineer and one independent reviewer in sequence"), D:17-26 (every row mandatory), D:32, D:45, D:50-51, D:67; P:25 ("A small change may use one compact artifact").

**Problem:** D:9 suggests a tiny change needs only an engineer and a reviewer. But D:51 still requires product manager acceptance of every PR, EL acceptance of every ER, QA evidence, and closed peer findings. That means an invoker/PM, product manager, EL, engineer, reviewer and QA. The design never says which roles may be combined in one agent, or which separations are non-negotiable. Implementers will either spawn five or six agents to fix a typo (overkill) or quietly merge roles without rules (the controls disappear). For large work, it does not say when to add a second reviewer, a QA per component, or an EL per subsystem.

**Scenario:** "Fix the off-by-one in pagination" costs six agent contexts and five handoffs, or the invoker quietly does product, EL and QA work with no record of which separation it gave up.

**Smallest correction:** Add a sizing table to the design and to `team-roles.md` with, per tier, the minimum number of distinct agents, which roles may be combined, and the separations that never change.
- **Tiny:** the invoker combines PM, product and EL in a compact brief. One engineer. One independent reviewer who also runs QA checks. Product acceptance is by the invoker, who is not the author.
- **Moderate and complex:** all roles are distinct. For complex work, say when to add a reviewer or QA per component.
- **Invariant separations:** the author is never the reviewer, and the author never gives product acceptance.

### R1-05 (Medium): The PM classifies complexity before the EL exists, which conflicts with "EL staffs by complexity"

**Where:** D:45 ("PM records whether the task is tiny, moderate, or complex ... then selects the lightest useful artifact set"), D:47, D:11.

**Problem:** The user gave staffing-by-complexity to the EL. D:45 has the PM classify complexity at intake, before any product brief or architecture. That classification then limits the process (the artifact set), and the EL has no stated way to overturn it.

**Scenario:** The PM calls "add SSO login" moderate from the request text. The EL later finds it touches three services and session storage. The lightweight artifact set is already fixed, and the design gives no route back.

**Smallest correction:**
- The PM makes a provisional classification for intake only.
- The EL confirms or overrides it in the engineering brief, and that confirmation fixes staffing and review depth.
- An upgrade triggers the fuller artifact set.

### R1-06 (High): Product manager and EL continuity across waves is unspecified, and the example schedule drops both before acceptance

**Where:** D:61 ("PM + product + EL, then PM + EL + engineers, then PM + QA + reviewer"), D:49 ("The EL reviews integrated behavior after peer reviews"), D:50 (product manager acceptance after QA), D:63 (replacement only on failure); P:70 (mentions `followup_task` only for reuse).

**Problem:**
- The illustrated final wave has no product manager and no EL, yet product acceptance (D:50) and EL integration review (D:49) happen at the end.
- Nothing says whether acceptance roles are resumed (Claude `SendMessage` to an existing agent, Codex `followup_task`) or re-spawned fresh.
- A fresh agent's acceptance rests only on artifacts, which the design does not require to be complete enough for that.

Either the illustration is wrong, or acceptance is done by an agent that never saw the product reasoning, or it is skipped.

**Scenario:** On a four-slot host, the PM follows the example. The QA wave finishes, and no product manager is running. The PM, under time pressure, "confirms" product acceptance from the QA report. That is exactly what D:53 forbids.

**Smallest correction:**
- Add an acceptance wave to the illustration (PM + product + EL).
- State the continuity rule per host: resume the same agent where possible; otherwise re-spawn with the request record, product brief and evidence as the only context.
- Require every acceptance decision to be reproducible from artifacts alone.

### R1-07 (High): The plan treats self-written paper walkthroughs as behavioral verification

**Where:** P:58 ("Walk the contract through five scenarios. Record a paper trace ... Correct ambiguities in the three skill files"), P:59 ("A validator pass checks syntax only; **the walkthrough checks behavior**"), P:72 ("Record inputs, expected next action and gate state, and observed output"). D:69 itself says "scenario review rather than claims of new runtime enforcement".

**Problem:** The same implementer writes the skill, writes the expected behavior, and judges the trace. "Correct ambiguities" has no exit criterion. P:59 calls this behavioral checking, which it is not. For host dry runs, P:72 does not require expected outcomes to be written before the run or pass/fail to be judged by anyone other than the author. P:72 and P:78 let a host result be "pending", while P:80 lets the feature be reported as "documented" anyway. The plan never says whether a pending Codex result blocks the commit.

**Scenario:** The implementer writes `team-roles.md`, then writes a trace in which the PM "reopens signoffs" because the text says so, and records it as passing. No agent ever ran it. The skill ships as "implemented" with Codex pending.

**Smallest correction:**
- Relabel the paper traces as design review.
- Write each scenario's expected gate states in the evaluation file before any run.
- Have an agent that did not write the skill execute the dry runs and record pass/fail against those expectations.
- State explicitly that the skill may be committed as a draft, but may not be described as working on a host whose dry run is pending.

### R1-08 (High): The plan's dry runs omit the design's readiness criteria for product and QA rejection and peer-review resolution

**Where:** D:69 readiness requires: custom roles enroll correctly; an over-capacity staffing request becomes a schedule; **peer review results in a resolved finding**; **QA and the product manager can reject and reroute a candidate**; a changed PR invalidates acceptance; the final report points to records. The plan tests:
- P:58 (paper only): small change, multi-service, rejected peer review, changed requirement, performance without threshold.
- P:72 (host): slot exhaustion, spawn cap, changed requirements, dead engineer, self-review.
- P:78: one "realistic request", with no rejection injected.

**Problem:** No host run covers product manager rejection or QA rejection and rerouting. Peer-review resolution appears only on paper. The design's readiness criteria cannot be satisfied by executing the plan as written.

**Smallest correction:**
- Make the host dry-run list the union of D:69 and P:72.
- Seed the sample work with a known defect, which should produce peer-review and QA rejection.
- Add an acceptance criterion the first candidate deliberately misses, which should produce product rejection.
- Record the expected reroute and the observed reroute for each.

### R1-09 (Medium): Failure-injection methods for the dry runs are unspecified, so the scenarios cannot be falsified

**Where:** P:72 ("slot exhaustion, exhausted child-spawn cap, changed requirements, dead engineer with partial edits, and self-review").

**Problem:** The plan never says how to cause a dead engineer (stop the agent? `swarm leave`?), exhaust slots on a host whose limit is unknown, or trigger self-review. If self-review is "tested" by telling the PM to assign it, the test only checks whether the model obeys that prompt. Without a defined injection and a defined pass signal, any output can be read as a pass.

**Smallest correction:** For each scenario, state:
- the injection, for example a temporary config with `max_per_job = 1` (config.example.toml:120), stopping the engineer's agent mid-task, or a staged hand-off where the author is listed as reviewer;
- the pass signal, for example the PM refuses and posts a reassignment, or the replacement verifies the partial diff before editing.

### R1-10 (Medium): Competitor research is weakened to optional, and Codex swarm sessions have no network by default

**Where:** D:20 ("competitor research when relevant"), D:35 (`not applicable`), P:25. `skills/swarm/SKILL.md:339-345` states that the plugin's Codex setup grants "no network access", with opt-in via `[codex] network_access = true` for `codex -p swarm` sessions only.

**Problem:** The user listed competitor research as a product manager duty. The design lets the product manager decide on its own that research is irrelevant, and nobody reviews that choice. On Codex, a product manager may be unable to do web research at all. The design and plan never mention this, so "not applicable" and "not possible" become indistinguishable.

**Smallest correction:**
- `hosts.md` states the network prerequisite for research.
- The product brief separates `not applicable (reason)` from `not performed (no network/tool)`.
- Both appear in the final report so the user can decide.
- For user-facing features, skipping research needs the PM's acknowledgment, not just the product manager's choice.

### R1-11 (Medium): QA writing tests is weakened to optional, QA alone decides what to omit, and QA's tests are never reviewed

**Where:** D:24 ("automated tests where appropriate"), D:50 ("QA runs the tests warranted ... states omitted test types and why"); P:24 ("records which ... apply, with a reason for omissions and a threshold for any performance claim").

**Problems:**
- **The user asked QA to write the tests.** The design allows QA to only run existing tests.
- **Only QA decides omissions.** Omitted test types are "stated", but nobody concurs.
- **Performance thresholds have no owner.** QA "asks for one" late (P:34), instead of the threshold being set in the PR/ER baseline.
- **QA-written tests are not reviewed.** QA code gets no review, so a wrong acceptance test can pass a wrong build and product acceptance may lean on it (R1-12).

**Smallest correction:**
- The EL concurs on omitted test types, and omissions go in the acceptance record and final report.
- The product manager (user-facing) or EL (technical) sets performance thresholds at baseline.
- An engineer or the EL, not the QA author, reviews QA-written tests.

### R1-12 (Medium): Product acceptance can collapse into re-reading QA's evidence

**Where:** D:50 ("Product manager checks the built result against every PR acceptance criterion, including the user-visible behavior"), D:53 ("These three results are separate").

**Problem:** Nothing requires the product manager to exercise the candidate. In practice an agent will cite QA's green run. The separation in D:53 then exists in name only, and a QA test that encodes a misread criterion passes both gates.

**Smallest correction:**
- The acceptance record marks each criterion either "observed directly (method, candidate hash)" or "relied on QA evidence (ID)".
- Every user-visible criterion must be observed directly unless the PM records why that was not possible.

### R1-13 (Medium): There is no escalation path for PR exclusion, infeasibility, role disagreement, or a `not_met` verdict

**Where:** D:37 (a PR maps to a task "or **deliberate exclusion**", with no approver), D:50 ("Changed behavior or scope goes back to product manager"; the product manager cannot change scope, per D:13), D:51 (deferral needs the user, checked only at completion), D:53; P:71.

**Problems:**
- The EL can record a PR as excluded during planning. That only becomes a problem at the completion check, after all the work.
- When the EL finds a PR technically infeasible, the path (EL, then product manager, then PM, then user) is not written down.
- A disagreement between the product manager and the EL, or between the product manager and the judge, has no tie-break.
- After a judge `not_met`, nobody is named to route the rework.

**Smallest correction:** Add a short escalation rule:
- Any PR exclusion or deferral goes to the user via the PM before implementation starts.
- The EL decides technical disputes; product-fit disputes go to the product manager, and scope disputes to the user.
- After `not_met`, the PM routes the rework to the owner of the gap named in the verdict and reopens the affected signoffs.

### R1-14 (Medium): Binding evidence to a candidate hash conflicts with versioned artifacts and a shared worktree

**Where:** D:32 (artifacts kept "in version control when they are useful"), D:38-39 (records bind to "exact candidate commit or tree hash"), D:51 ("A code or requirement revision invalidates each affected review, test, and signoff"), D:48 (several engineers, ownership "through the board").

**Problems:**
- **Recording acceptance changes the candidate.** Committing the acceptance record changes the tree hash, so the accepted hash is never the delivered hash. A literal reading re-opens signoffs forever, and a loose reading erases the rule.
- **Parallel engineers have no per-task hash.** In one shared checkout with uncommitted edits, there is no hash to bind a task review to.

**Smallest correction:**
- Define "candidate" as the code commit excluding the artifact paths, or keep artifacts out of the candidate tree.
- Require each engineer to work on its own branch or worktree and commit before review.
- The EL integrates and names the integrated candidate hash that QA and acceptance bind to.

### R1-15 (Medium): Final verification checks against the design, not the user's original request

**Where:** P:77 ("Compare every product/team design requirement against Tasks 1–2 and the delivered files").

**Problem:** The design is where the requirements were weakened (R1-02, R1-10, R1-11). Checking the skill against the design will pass those weakenings.

**Smallest correction:** The evaluation file includes a request-level trace. It maps each of U1-U7 (the table above) to skill text and host dry-run evidence, and marks any intended weakening for the user's approval.

### R1-16 (Medium): No minimum Swarm version; the installed runtime in this very job does not record custom roles

**Where:** D:59, D:69 ("custom roles enroll correctly"); P:18-19, P:72.

**Evidence:**
- `swarm who --job engineering-team-plan-20260929` shows `Todd Flanders claude general-purpose` and `Uter Zorker claude general-purpose`, although both prompts contain a `[swarm role: reviewer]` line.
- The installed plugin at `/home/claude/.claude/plugins/cache/swarm/swarm/0.1.0/lib/swarm/` has no `roles.py`. The snapshot has `lib/swarm/roles.py`, and `hooks.py:325-338` sets the role at the first tool call.
- Krusty the Clown confirmed on the board that the installed cache predates custom-role support.
- I did not verify the snapshot code's behavior at runtime.

**Problem:** Krusty treats this as expected rather than a plan defect. For users, though, the skill will run on whatever version is installed and quietly lose role labels, which the design relies on for handoffs and for model selection by role (`SKILL.md:263-267`). The plan never states a version prerequisite or a startup check.

**Smallest correction:**
- `SKILL.md` states the minimum plugin version.
- After the first spawn, the PM checks `swarm who`. If roles show the agent type instead, the PM records the degradation and uses board posts to identify roles.
- Dry runs record the plugin version.

### R1-17 (Low): The skill validator path works only on the Codex host

**Where:** P:59 (`python3 /home/codex/.codex/skills/.system/skill-creator/scripts/quick_validate.py ... (or the installed equivalent)`).

**Evidence:** From this Claude host, `ls` of that path returns "Permission denied".

**Problem:** A Claude-side implementer has no named equivalent to run.

**Smallest correction:** Name the check for each host, or specify the minimal frontmatter and link check to run.

### R1-18 (Low): The Codex task-name list omits roles the design uses

**Where:** P:69 lists `product_manager__`, `engineering_lead__`, `engineer__`, `qa__`, `judge__`. D:19 and D:23 also use `project_manager` delegates, `reviewer`, and optional `verifier`.

**Smallest correction:** Add `reviewer__...`, `project_manager__...` and `verifier__...` examples. Note that `verifier` and `judge` are built-in (`SKILL.md:329-335`).

## Non-findings (checked, and I agree with the design)

- **Spawn caps apply to members only.** `max_per_job` excludes the orchestrator's own spawns (`SKILL.md:255`), so root-level spawns by the PM are limited only by host concurrency, as D:11 and P:17 say.
- **Custom roles carry no enforcement.** D:28 and P:18 correctly say custom roles grant no permissions. Only judge and verifier are special (`lib/swarm/roles.py:1-6`; `SKILL.md:224-226`).
- **Codex role routing is correct.** Routing by task name matches `roles.from_task_name` (`lib/swarm/roles.py:28-37`).
- **No fixed four-person team is met** (D:9, D:61).

## Disagreement with authors

Krusty the Clown's board note says the `general-purpose` roster labels are "not a plan defect". I agree the runtime cause is the installed version, not the design. The missing version prerequisite and runtime check is still a plan gap (R1-16).
