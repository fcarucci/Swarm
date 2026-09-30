# Reviewer 2: Host mechanics, scheduling, recovery, and evaluation review

- Reviewer: **Uter Zorker** (swarm job `engineering-team-plan-20260929`, Claude subagent)
- Model: Claude Opus 5.5 (`claude-opus-5-5`)
- Inputs: `docs/superpowers/specs/2026-09-29-engineering-team-design.md` ("design"), `docs/superpowers/plans/2026-09-29-engineering-team.md` ("plan"). I checked them against the snapshot source (`lib/swarm/hooks.py`, `lib/swarm/hosts/{claude,codex}.py`, `lib/swarm/roles.py`, `skills/swarm/SKILL.md`, `docs/REFERENCE.md`, `config.example.toml`) and against the Swarm plugin installed on this host (`~/.claude/plugins/cache/swarm/swarm/0.1.0`).
- Independence: I wrote this without reading or coordinating with Reviewer 1's report.
- Scope: I did not change any source, the design, or the plan. I wrote only this file.

"Verified" means I read it in the snapshot source or docs, or saw it on this job's live board. "Unverified" means neither the snapshot nor this session shows it.

## Summary

The documents describe the roles and gates well. On mechanics, though, they assume a long-lived team: a PM that hears board posts, an EL that waits across waves, authors that hold a slot while review happens, a readable "available slots" number, and hashes that bind evidence to a candidate. Current Swarm and the two hosts do not provide these. Swarm's source and docs also rule out, or leave undefined, several of them. The evaluations are mostly written and graded by the implementer. Four findings would break a real run: R2-01, R2-02, R2-03, and R2-05.

| ID | Severity | One line |
|---|---|---|
| R2-01 | high | PM (orchestrator) never receives board posts. An EL staffing request posted to the board and waited on deadlocks. |
| R2-02 | high | Roles are modeled as long-lived. Authors and the EL holding slots while waiting for a reviewer or PM causes a slot deadlock. |
| R2-03 | high | Codex `spawn_agent` forks the full parent history by default, so the "independent" reviewer, QA, product acceptor, and judge inherit PM/EL/author context. |
| R2-04 | medium | Peer review is not independent in other ways too: the reviewer assigner is unspecified, the board and memory feed author reasoning to the reviewer, and all roles get the same model by default. |
| R2-05 | high | Neither the judge `met` verdict nor `VERIFIED` is bound to a candidate. The completion gate passes on a stale verdict. |
| R2-06 | high | "Candidate commit or tree hash" and "affected" signoffs are undefined for parallel engineers in a shared or non-git tree. The result is livelock or silent staleness. |
| R2-07 | high | "Check actual available concurrent slots" cannot be done. Neither host exposes the number in the snapshot, and member spawn probes burn the non-refundable cap. |
| R2-08 | medium | EL-owned nested staffing is fragile: 2 spawns per agent, 4 per job, non-refundable, and depth-2 children cannot spawn. It should be the exception, not the default. |
| R2-09 | medium | Crash reassignment has no fencing when the supervisor is off (the default, and the setting in this job). An "idle/dead" agent can still be editing. |
| R2-10 | medium | A Codex replacement is briefed with the task name and first post only. The encrypted role brief is lost. |
| R2-11 | medium | Custom-role enrolment does not work on this host's installed runtime. The readiness criterion and "existing tests are sufficient" do not hold without a version preflight. |
| R2-12 | high | The evaluations are self-certifying: paper traces by the implementer are called behavioral, and the failure scenarios have no induction method. |
| R2-13 | medium | Codex `followup_task` reuse keeps the original role label and full context, so the author can be "reused" as its own reviewer or QA. |
| R2-14 | low | A PM turn ending without `swarm wait` lets the job auto-close. Reactivating resets the spawn counts and clears the verdict. |
| R2-15 | low | The validator path is Codex-user specific (`/home/codex/...`). |

---

## Findings

### R2-01 (high): The PM never sees board posts. An EL that posts a staffing request and waits deadlocks.

**Claim.** Design l.61: "A lead who has exhausted its nested allowance sends the staffing request to PM for a root-level spawn." Design l.47 and plan l.70 describe the EL→PM request/response flow. Design l.57: PM "records the wait on the Swarm board ... and resumes when the dependency clears."

**Reality (verified).** `lib/swarm/hooks.py:1404-1410`: when a hook call has no `agent_id` (the main or orchestrating session), the hook only touches the "seen" file and rewrites spawn models, then `return 0`. It never injects board messages. The orchestrator must `swarm join --key orchestrator` and poll `swarm read` explicitly (`skills/swarm/SKILL.md:283-286`). Nothing wakes an idle orchestrator when a post arrives. In Claude, the PM wakes only when a background agent completes.

**Scenario.** PM spawns EL in the background and ends its turn to wait. EL has used its 2 spawns (`max_per_agent = 2`), posts "need 2 more engineers" on the board, and keeps polling for them. The PM is never told. It stays idle until EL finishes, and EL will not finish because it is waiting for engineers. The PM wakes only when EL gives up or its context runs out.

**Smallest correction.** Put this rule in the skill (hosts.md and team-roles.md): **no subagent ever blocks on a PM action.** A request that needs the PM (staffing, spawn, user question, scope decision) goes into the agent's **final result**, and the agent ends its turn. The board post is only a mirror. The PM runs `swarm read` at every wake-up and before every wave. Add an evaluation scenario: the EL needs more engineers than it can spawn, and the check is that the PM receives the request through the EL's return value.

### R2-02 (high): Roles are modeled as long-lived. Slot deadlock between author, reviewer, and EL.

**Claim.** Design l.61 example: "PM + product + EL, then PM + EL + engineers, then PM + QA + reviewer". Here the EL persists across waves. Design l.49: the author "acknowledges each finding, fixes it ... and returns the change for reviewer recheck". Design l.48: "EL integrates in dependency order". The design assumes the EL, the author, and the reviewer stay alive through a multi-round loop.

**Reality.** On Claude, `SubagentStop` is final (`lib/swarm/hosts/claude.py:37`, `stop_is_final = True`): once an agent returns, it is completed. In this environment, blocking foreground `sleep` is refused by the Bash tool, so an agent cannot cheaply idle-wait for a peer. That is an observation of this session's tool policy, not Swarm source. An agent that stays alive to wait keeps its host slot for the whole time.

**Scenario (4-slot host).** Slots: PM, EL, engineer A, engineer B. A finishes and waits (alive) for a reviewer so it can fix findings. The PM planned a separate reviewer C, but no slot is free. B is still implementing and will also wait alive for its own reviewer. The EL waits alive to integrate after reviews. No one releases a slot, so C never starts. Cross-review (A reviews B, B reviews A) avoids this only if both are polling the board at the right time, and the design does not require that.

**Smallest correction.** Make each role step a **bounded invocation that ends and releases its slot**: implement → return; review → return; fix → a new invocation. The new invocation is either a resume of the same agent (Claude `SendMessage`, which this session's tool list describes as "continue a previously spawned agent with its context intact"; Codex `followup_task`) or a fresh agent briefed from artifacts. The PM's dependency board tracks invocations, not persistent people. The design's wave example must count slots per invocation and must not show the EL occupying a slot across waves unless it is actively working.

### R2-03 (high): Codex children inherit the parent's whole history by default. Review, QA, acceptance, and judge are not independent.

**Reality (verified in source comments; fixture-backed).** `lib/swarm/hosts/codex.py:242-244`: "A forked child's rollout (spawn_agent fork_turns other than "none"; **"all" is the default**) starts with the parent's history". The fixture `tests/fixtures/codex/0.157.1/hooks/PreToolUse-08.transcript.jsonl:9` shows a spawn with `"fork_turns": "all"`.

**Scenario.** The Codex PM spawns `qa__acceptance` and `product_manager__acceptance` with the default fork. Both start with the PM's full transcript: the EL's staffing reasoning, engineers' "DONE, all tests pass" handoffs, the PM's view that the candidate is ready. If the EL spawns `engineer__review_api` after reading engineer A's rationale, the reviewer carries that rationale. The design's independence rules (design l.49, l.53; plan l.23) are then violated by the default.

**Smallest correction.** In hosts.md, require `fork_turns: "none"` for every role spawn, and state it as mandatory for reviewer, QA, product acceptance, verifier, and judge. The evaluation must check the child's rollout header (`session_meta.payload.forked_from_id` absent; `codex.py:278-286` reads it). I have not verified that `"none"` is accepted by every Codex version the plugin supports; the dry run must confirm it.

### R2-04 (medium): Other gaps in peer-review independence.

1. **Who picks the reviewer is unspecified.** Design l.49 says "An engineer who did not author the change reviews". Plan l.56 does not say who assigns. Nothing stops the author from choosing its reviewer, or the EL who wrote the plan from being the only reviewer (design l.49 allows the EL "when qualified").
2. **The reviewer sees author reasoning automatically.** Every broadcast post is injected into every member before its tool calls (hook context of this job). The author's "DONE ... because X" rationale reaches the reviewer before it reads the diff. Hindsight memory recall can do the same when enabled.
3. **Same model by default.** `config.example.toml` `[models.claude] worker = "opus"`; custom roles fall back to `worker`. The author and reviewer are the same model with the same skill brief, so their blind spots are correlated.

**Smallest correction.** The PM assigns the reviewer, never the author. The reviewer brief contains requirement IDs, the base..head diff, and test commands, with no author narrative. The reviewer records findings before reading the author's handoff notes. The review record states the reviewer's host and model. For material changes, when both hosts are attached, the skill recommends cross-host review (Claude reviews Codex output or the reverse), as this job itself does.

### R2-05 (high): The judge verdict and VERIFIED are not bound to a candidate, so the completion gate passes on a stale `met`.

**Claim.** Design l.51: "A code or requirement revision invalidates each affected review, test, and signoff ... If a judge is present, PM supplies the evidence, waits for `met`, and uses the normal Swarm completion command." Plan l.71 is similar.

**Reality (verified).** `docs/REFERENCE.md:1219-1226`: the gate checks only that "the judge's **latest** verdict is `met`". A verdict is a string with no candidate. The `VERIFIED` count (`REFERENCE.md` Verifiers section) is also unbound.

**Scenario.** The judge records `met` at commit `abc123`. The product manager then finds a text bug, and engineer A pushes `def456`. QA reruns, but the judge is not re-asked. The PM runs `swarm deactivate --status completed`, and the gate accepts because the latest verdict is still `met`, against a candidate the judge never saw. The design's invalidation rule is a convention, but the design presents the Swarm gate as the enforcement.

**Smallest correction.** The judge brief requires the verdict reason to start with `candidate=<hash> req=<rev>`. Before `deactivate`, the PM compares the current candidate hash to the hash in the latest verdict (and in each acceptance record). On a mismatch it asks for a new verdict. State plainly that the Swarm gate does not detect staleness. Add this as an evaluation scenario (R2-12).

### R2-06 (high): "Candidate hash" and "affected" are undefined for parallel work. The result is livelock or silent staleness.

**Claim.** Design l.38-39 and l.51; plan l.57: records bind to "the exact candidate commit or tree hash", and a revision "invalidates each affected review".

**Gaps.** (a) Engineers working concurrently in one worktree have no commit per task. A dirty tree has no stable hash unless someone snapshots it. The design's only isolation is board claims of file ownership (design l.48), which is a convention. (b) Reviews of A's change were taken at tree T1. B's edits in the same worktree produce T2. Under a strict reading every review is invalidated, so reviews never converge while work continues (livelock). Under a loose reading "affected" is judged by the author, so staleness goes unnoticed. (c) This review snapshot is not a git repo (`git log` → "not a git repository"), so the documents' hash rule cannot be followed in exactly the kind of environment they were written in.

**Smallest correction.** One task = one branch or worktree = one commit range. Claude's Agent tool offers `isolation: "worktree"` in this environment; I have not verified an equivalent for Codex. Peer review binds to the task's head commit. Integration produces an integration commit. The EL reviews the merge diff, and QA, product, and judge bind to the integration commit. Define "affected": a task review is invalidated by any change to files in its diff or to requirement IDs it traces to, and any change to the integration commit invalidates QA, product, and judge results. For non-git work, use a `sha256sum` manifest of the deliverable files as the candidate ID.

### R2-07 (high): "Actual available concurrent slots" cannot be read, and probing with member spawns burns the non-refundable cap.

**Claim.** Design l.47 and l.61 ("Before each wave the PM checks the host's actual available concurrent slots"); plan l.17 and l.70 ("Read current host concurrency").

**Reality.** Nothing in the snapshot reads or exposes a host concurrency limit for either host (grep for `max_threads`/`concurren` in `lib/` finds only supervisor replacement caps). Claude Code's subagent concurrency cap and Codex's thread cap are **unverified** here. Swarm counts a member spawn before the host runs it, and "a spawn is counted even if the `Agent` call then fails", and "departed agents don't give spawns back" (`REFERENCE.md` "Agents spawning agents", items 7 and the paragraph at l.1281-1284). Orchestrator spawns are not gated or counted (`REFERENCE.md:1283-1284`).

**Scenario.** The EL tries to spawn 3 engineers to "see what fits". Suppose the host rejects the third for capacity (unverified behavior). Swarm has already counted all three spawns: the EL's 2 are consumed (the third is refused by `max_per_agent`), and the job's 4 are half gone with one engineer's worth of work lost.

**Smallest correction.** Replace "check actual slots" with: the PM uses a **declared slot budget**, which is the user's or config's value recorded in the request record, or a conservative default. Only the PM (root, uncounted) spawns speculatively. A host spawn failure is treated as a capacity signal, and the work is re-queued. The EL never probes. Mark each host's cap "unverified; measured in dry run" in hosts.md.

### R2-08 (medium): EL-owned nested staffing is fragile. Make root spawns the default.

**Reality (verified).** `config.example.toml [spawn]`: `max_per_agent = 2`, `max_per_job = 4`, `max_depth = 2` (orchestrator's agents are depth 1, "helpers can't spawn"). The counter is non-refundable and is reset only by re-activation, which also clears the verdict (`SKILL.md` step 5). Supervisor replacements "never spawn subagents" (`REFERENCE.md:1001`). A spawned subagent can't be a judge. I have not verified what happens to a Claude EL's background children after the EL returns (`stop_is_final`).

**Scenario.** The EL spawns engineers A and B, and A dies. Replacing A needs a third EL spawn, which is refused. The PM does it at root, but now the EL "owns" some engineers as children and not others. If the EL is replaced by the supervisor, it cannot spawn at all.

**Smallest correction.** State that the PM spawns every long-lived role at root: uncounted, depth 1, and able to spawn their own short helpers. The EL's staffing authority is exercised through the staffing plan (who, which task, how many). Nested spawns are only for short helpers the spawner waits for synchronously. This keeps the EL's authority (design l.11) and turns "PM executes spawns the EL cannot" from the exception into the norm.

### R2-09 (medium): Crash reassignment has no fencing without the supervisor.

**Claim.** Design l.63: "PM reassigns its explicit owned tasks after checking the board and worktree." Plan l.71 is similar.

**Reality (verified).** The supervisor is off by default (`REFERENCE.md:970`) and is off for this job (`swarm status`: "supervise off for this job"). Only a supervisor-closed agent is fenced ("each of its tool calls is refused", `REFERENCE.md:1014-1016`). Without it, `dead` means no hook contact for 30 minutes. An agent in one long tool call (up to `tool_timeout_minutes = 60`) looks idle but is alive. Board claims are posts, not locks.

**Scenario.** Engineer A runs a 40-minute integration test. The PM sees no posts, reassigns A's task to A2, and A2 starts editing the same files. A's test returns and A resumes editing. Both write the same files, and each passes review of its own diff.

**Smallest correction.** Before reassigning, stop the old agent explicitly (Claude: the host's task-stop tool, available in this environment; Codex: close or stop, unverified) or confirm its `SubagentStop`. Post a claim transfer. The replacement starts from the last committed task state (R2-06) and treats uncommitted partial edits as unreviewed input, not as work it inherited. Recommend enabling the supervisor for long jobs, and note that it brings fencing.

### R2-10 (medium): A Codex replacement loses the role brief.

**Reality (verified).** `REFERENCE.md:1001-1003`: a replacement is briefed with "the original task, taken from the first agent of the chain (Claude: its spawn prompt; **Codex: the task name and first post**)". The Codex spawn `message` is encrypted (`codex.py:1-6`), so the role brief never reaches the replacement.

**Scenario.** `qa__e2e` is replaced by the supervisor. The replacement knows it is "qa" doing "e2e" and whatever its first post said. It does not know the PR IDs, thresholds, environment, or which candidate to test.

**Smallest correction.** On both hosts, each role brief is written to a durable artifact file. The agent's **first board post** must be `BRIEF: <path> tasks: <IDs>`. The same file serves manual PM reassignment (design l.63: "A replacement reads the current artifacts").

### R2-11 (medium): Custom-role enrolment does not work on this host's installed runtime. Unit tests do not prove readiness.

**Observed (verified on this board).** Both reviewers were spawned with `[swarm role: reviewer]`, and `swarm who` shows `general-purpose` for both even after many tool calls. The installed plugin (`~/.claude/plugins/cache/swarm/swarm/0.1.0/lib/swarm/hooks.py:964`) calls `allocate_name(agent_id, job, payload.get("agent_type"))` and has no `roles.py` or `set_agent_role`. The snapshot has the feature (`lib/swarm/hooks.py:981-985`, `_verify_route` at l.330-338). Krusty says on the board that the installed cache predates commit 634aece. I accept that this is not a design defect in itself.

**Why it still matters.** Design l.69: "Tests of existing role parsing and spawn enforcement are sufficient for platform behavior." Plan readiness includes "custom roles enroll correctly". On the machine that runs this job, the unit tests pass on the snapshot while the runtime behaves differently. A dry run on an un-upgraded host would silently test the old behavior, or would "pass" by reading role names from the agents' own posts.

**Smallest correction.** The skill declares a minimum Swarm version or commit. hosts.md adds a preflight: after the first role spawn, `swarm who` must show that role, and otherwise stop and report. The evaluation file records the Swarm version on each host. Design l.69 should say the unit tests cover parsing, and the dry run covers the installed runtime.

### R2-12 (high): The evaluations are self-certifying.

**Where.**
- Plan l.58 (Task 1 Step 4): the implementer writes a "paper trace" of five scenarios and then "correct[s] ambiguities". Plan l.59: "the walkthrough checks behavior". This treats a paper walkthrough by the author as behavioral evidence.
- Plan l.31, l.32, l.34 (Review Focus 2, 3, 5): "Tested in Task 1". Task 1 contains only the paper trace.
- Plan l.72 (Task 2 Step 4): "expected next action and gate state, and observed output" are recorded by the same implementer. There is no method to *induce* slot exhaustion, cap exhaustion, or a dead engineer with partial edits.
- Plan l.78: "Independently review the skill with a realistic request" names no independent actor.
- Design l.69: "the skill's workflow examples need scenario review rather than claims of new runtime enforcement". That is fine, but scenario review by the author is not independent.

**Counterexample.** The implementer writes "self-review scenario: reviewer = engineer B, gate closed", and the trace "passes". In a real run, the Codex default fork (R2-03) and board-injected rationale (R2-04) make B's review non-independent, the stale judge verdict (R2-05) closes the job, and no step of the evaluation would detect either.

**Smallest correction.**
1. **Pre-register** each scenario's inputs and expected gate states in the evaluation file, committed *before* any run.
2. Each run is executed by a **fresh agent given only the skill** (not the plan or design) on isolated sample work. A **separate grader** compares the observed board, artifacts, and gate state to the pre-registered expectations.
3. Induce the failures behaviorally:
   - Cap exhaustion: use a temporary `SWARM_CONFIG` with `max_per_agent = 1` and `max_per_job = 1`. The refusal is source-enforced, and the check is that the PM re-queues the work.
   - Dead engineer: stop the engineer mid-edit with the host's stop tool.
   - Peer review: seed a known defect that the reviewer must report.
   - Requirement change: change a PR after acceptance, then check that the PM refuses completion and re-requests the verdict (R2-05).
   - Independence: check the Codex fork header (R2-03).
4. Include at least one **negative control** that must fail, so the grader cannot pass everything.

Slot-exhaustion induction needs a host cap that can be lowered. That is unverified for both hosts; if it cannot be induced, mark it pending instead of passed.

### R2-13 (medium): Codex follow-up reuse keeps the role label and full context.

**Claim.** Design l.61 and plan l.70: the PM "may send a follow-up to reuse an existing Codex agent".

**Reality (verified).** The Codex role comes only from the task name (`codex.py:360-363` `role_hint` from `agent_path`). A follow-up cannot change it, and the agent keeps its whole conversation. `followup_task` also restarts the quiet windows (`hooks.py:1337-1351`).

**Scenario.** Slots are tight. The PM reuses finished `engineer__api` for QA of the integrated candidate, or for "review" of the fix to its own earlier change. The roster still says `engineer`, and the agent carries its authoring context. Its "QA signoff" or "review" is self-review.

**Smallest correction.** Allowed reuse: same role, a new task, and **never** a review, QA, or acceptance of any change the agent authored or reasoned about. For a different role, spawn a new agent (fresh fork, R2-03) after the old one is closed or idle.

### R2-14 (low): A wait without `swarm wait` auto-closes the job, and reactivation resets state.

Design l.57 uses `swarm wait` only "if work spans long waits". `config.example.toml [job] auto_close_minutes = 30`: a job closes once all agents are done and it has been quiet. Re-activating then resets the spawn counts, clears the verdict, and marks earlier agents departed (`SKILL.md` step 5). **Scenario:** the PM ends its turn to wait for the user's scope decision after all agents have returned. The job auto-closes overnight, and the recovery path silently discards the judge's `not_met` history. **Fix:** whenever the PM ends a turn with no agent running and work remaining, it must run `swarm wait --on "<reason>"` (as `SKILL.md` step 4 already says).

### R2-15 (low): The validator path is host-specific.

Plan l.59 names `/home/codex/.codex/skills/.system/skill-creator/scripts/quick_validate.py`. That exists only for the Codex OS user. A Claude-side implementer cannot run it, and "(or the installed equivalent)" is not an instruction. **Fix:** name the validator by what it checks (frontmatter `name`/`description`) and give a Claude-side equivalent, or state that validation runs on the Codex host.

---

## Items checked and found sound

- Plan l.17: `max_per_job` counts child spawns, not team size, and orchestrator spawns are uncounted (`REFERENCE.md:1283-1284`). Correct.
- Plan l.18 and design l.28: only `judge` and `verifier` are enforced, and custom roles are ordinary workers (`roles.py:6`, `REFERENCE.md` Custom roles). Correct.
- Plan l.19 and design l.59: Codex prompt tags are unreadable and the role comes from `task_name` `<role>__<task>` (`roles.py:28-37`, `codex.py:326`). Correct.
- Design l.61: Codex `stop_quiet_minutes` governs Swarm status, not host slot release. Correct as far as it goes (see R2-07 on slot release being unmeasured).
- Design l.11: the PM delegate cannot own host-only spawns. This is consistent with "a spawned subagent can't be a judge" (`hooks.py:400-402`).
- Design l.63: deferring member spawns during a board outage is consistent with fail-closed member spawns (`hooks.py:1439-1443`).

## Unverified (the dry run must measure these, not assume them)

- Claude Code and Codex concurrent subagent caps, and whether the root or orchestrator counts toward them.
- Whether a Codex child's host slot frees at turn end, on an explicit close, or never within a session.
- What happens to a Claude subagent's background children after the subagent returns.
- Whether `fork_turns: "none"` is accepted by all supported Codex versions.
- A worktree isolation equivalent on Codex.
