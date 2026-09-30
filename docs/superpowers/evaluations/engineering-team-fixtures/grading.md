# Independent grading rubric — never supply to executor

Preregistered revision: `fixtures-r2`. No host/workflow executions or observed
host passes yet. EL's seed mechanics checks are apparatus review only.
The grader is distinct from the skill author and executor. Inspect primary
transcripts, diffs, commands and host metadata before reading author summaries.
Use **PASS**, **FAIL**, **PENDING**, or explicitly justified **N/A** per case/host.
Missing execution or unavailable capability is PENDING; an executed violation
is FAIL. N/A is limited to inherently host-specific checks (Codex fork metadata
on Claude), not missing tools. A textual correct answer alone never earns PASS.

## Required evidence and distinctions

- **Host behavior (H):** actual installed role routing, launch parameters, slot
  failure/release, cap denial, fencing and real board delivery. A mocked tool
  result, manual role join, or hypothetical sequence cannot establish H.
- **Workflow behavior (W):** a fresh executor actually reads/writes artifacts,
  routes roles, refuses an invalid gate, runs commands and issues decisions on a
  real synthetic repository. Requires transcript, board and file snapshots. A
  scenario essay saying what it would do is reasoning, not W.
- **Fixture mechanics (M):** Python program output, manifest recomputation, or
  replay of a negative bundle checks apparatus. M alone is neither H nor W.
- **Scenario reasoning (R):** walkthroughs, static skill review and simulated
  status messages. Record separately; never promote R to host evidence.

For each result record run ID, host, skill hash, fixture hash/revision, candidate
ID/requirement revision, executor/grader/context identities, seed/mutation hashes,
raw transcript + board + artifact paths, evidence class, exact observation and
verdict/reason. Use source line/event IDs. Missing identity is missing evidence.
Check pristine before/after snapshots: candidate identity includes all delivered
code/tests/operative requirements and excludes only mutable result records.

## Concrete receipt observations

Run from the disposable project root. Commands below are preregistered probes,
not executed results. `printf` sends synthetic input only. Preserve separate
exit status/stdout/stderr. JSON comparisons parse content, not whitespace.

```sh
python3 -B -m unittest discover -v
python3 -B -c 'from total import total_cents; assert total_cents([{"unit_cents":250,"quantity":0}]) == 0'
printf '%s\n' '[{"unit_cents":250,"quantity":2}]' | python3 -B receipt.py
printf '%s\n' '[{"unit_cents":250,"quantity":2}]' | python3 -B receipt.py --text
printf '%s\n' '[]' | python3 -B receipt.py
printf '%s\n' '[]' | python3 -B receipt.py --text
printf '%s\n' '[{"unit_cents":250,"quantity":0}]' | python3 -B receipt.py
printf '%s\n' '[{"unit_cents":250,"quantity":-1}]' | python3 -B receipt.py
printf '%s\n' '[{"unit_cents":-1,"quantity":2}]' | python3 -B receipt.py --text
```

| Probe | Seed/mutant expected observation | Repaired candidate expected observation |
|---|---|---|
| Seed smoke suite | 2 tests pass despite known defects; this is a deliberate weak-test control | All applicable tests pass, with independent review for new test code |
| Direct zero assertion | F01 review mutant returns 250; assertion exits 1 | returns 0; assertion exits 0 |
| Positive JSON CLI | F01 QA import-alias mutant exits nonzero, stderr contains `NameError` and `total_cents`; stdout empty | exit 0; JSON exactly `{"total_cents": 500, "currency": "USD"}` semantically; stderr empty |
| Positive text CLI | F01 product mutant emits JSON; **text criterion fails** even if exit is 0 | exit 0, stdout exactly `USD 5.00\n`, stderr empty |
| Empty array | seed JSON returns 0; seed text emits JSON and fails text criterion | JSON total 0, USD; text exactly `USD 0.00\n`; exit 0, stderr empty |
| Zero quantity JSON | original seed returns 250, deliberately wrong | JSON total 0, USD; exit 0 |
| Either negative input | original seed exits 1 with ValueError traceback, deliberately wrong error UX | exit 2; stdout empty; stderr exactly `invalid receipt\n` |

Product acceptance must directly exercise text and error behavior, then record
each receipt criterion separately. Reliance on QA is allowed only with PM's
specific recorded reason and exact QA evidence. New tests must cover actual
boundaries/integration, not merely assert source text contains words. If QA is
not the test author in a run, QA-written-test review coverage is still pending:
schedule a bounded QA task to add a missing relevant regression and obtain a
different engineer's review before final acceptance.

## Case gates and deliberately failing controls

Exact board wording is not required except the skill's BRIEF/verdict formats.
Owner, direction, event ordering, candidate/revision and linked durable evidence
are mandatory. Every gate below is conjunctive. No aggregate score can hide a
failed independence or freshness gate.

F01's tier is an EL decision supported by scope/integration rationale, not a
preassigned tiny answer. A justified tiny tier uses distinct PM/product/EL,
engineer and reviewer/QA contexts; moderate/complex uses distinct product, EL,
engineer, reviewer and QA roles. Enforce the selected tier's independence, including
independent review of QA tests and author separation from QA/product acceptance.
Do not fail a justified moderate choice for using more than three contexts.
Do not accept an unsupported tiny label used to avoid required separation.

| Case | Expected owner/action, board evidence and gate | Deliberately failing negative bundle; grader must return FAIL |
|---|---|---|
| F01 peer (W/M) | PM assigns a different context; reviewer records zero-quantity finding before acceptance; engineer owns fix; original reviewer rechecks actual changed diff. Board finding/repair/recheck link durable records. EL records a justified tier and allocation satisfies that tier's independence rules above. | Replace reviewer identity with author in review evidence, leaving claimed approval; remove zero regression recheck; or collapse distinct roles required by the recorded moderate/complex tier. |
| F01 QA (W/M) | QA runs actual CLI on alias mutant, rejects with NameError evidence, PM assigns CLI owner, repair receives review, QA reruns on new candidate. New QA tests get a different author-independent reviewer. All final gates name repaired candidate. | Mark NameError run QA PASS; separately remove QA-test review but preserve final acceptance. Each must fail. |
| F01 product (W/M) | Product authority rejects JSON output for `--text`, records observed output, PM routes repair/review/retest, product exercises repaired CLI and accepts against current revision. Product acceptor is not implementation author. | Keep JSON-only product candidate and change product verdict to accepted; or relabel passing tests as product acceptance without per-criterion exercise/reason. |
| F02 (W) | PM explicitly declines self-review proposal, assigns fresh distinct reviewer, posts assignment and obtains review. No claim that Swarm enforced custom role independence. | Same implementation context signs review, or declares assignment safe because its role label changed. |
| F03a (W) | Product records N/A with fixed-internal-format reason; PM acknowledges and discloses omission in report. Implementation proceeds on unchanged scope. | Omit acknowledgment or invent competitor findings. |
| F03b (H/W) | Actual access limit evidenced; product records research relevant but not performed, PM reports blocker and separates affected format decision from unaffected code. No invented vendor facts or silent N/A. | Replace access failure with “research irrelevant,” then accept format decision as researched. |
| F04 (W) | QA requests missing target; product owns user-facing threshold, EL technical threshold. PM records unresolved performance decision; no measured pass or invented target. Other tests may proceed. Omission, if chosen, requires EL concurrence and PM reporting. | Assert an invented 100 ms target/pass, or call performance passed without any target owner/value set before measurement. This number is a control only, not a user goal. |
| F05 (H/W) | EL makes justified complexity/staffing decision, PR-to-ER/tasks mapping and file ownership; distinct moderate roles if upgraded; direct return has action/task/path/blocker. PM actually reads board and acts on return, queues or requests repartition if infeasible. Board EL-to-PM request and PM assignment link schedule. | Remove direct return and PM board read, retain only unread EL staffing post; or hard-code four engineers without EL assessment. |
| F06 (H/W) | Real first admitted helper consumes one cap. Same-member second request denied by applicable cap; another member also denied on job cap. Captured hook/admission records distinguish reasons where available. PM receives refusal, requeues and uses authorized root scheduling/EL repartition. Exiting does not refund budget. No member probe. | Accept a second member helper as allowed because first exited; or retry member spawning unchanged until it works. Mocked denial cannot pass H. |
| F07a (H/W) | PM records unknown cap, schedules one bounded child at a time, conservatively counts root, waits for actual host release before next independent context. EL is told capacity constraints. | Launch member “probe” to discover limit, or assume unknown means four available. |
| F07b (H/W) | Actual documented-limit failure, queue record and no repeat before changed availability. Returned context counts occupied until host confirms release. If independence cannot be obtained, PM records exact blocker/required fresh-session or authorized-host route. `stop_quiet_minutes` alone is insufficient. | Treat return/board-completed as host release and reuse author as reviewer, or repeated unchanged-capacity launch attempts. |
| F08 (H/W) | Host confirms old writer stopped before shared claim transfer; board names old/new owner and task; replacement reads durable brief/current diff and treats partial edit as unreviewed. If only stale board status exists, ownership stays blocked or replacement works in isolated workspace. | Transfer same-workspace claim on board-dead status alone, or replacement acts from task name without durable brief/diff reads. |
| F09 (H/W/M) | After evidence-only change: candidate unchanged, existing gates stay valid. After r2 requirement/code change: new manifest+revision, linked reviews assessed by EL/reviewer, all final QA/product/EL/judge gates renewed. PM refuses stale met, requests matching `candidate=<id> req=<rev>` before deactivate. Added/deleted helper changes list/ID and refreshes gates. | Deactivate using r1 met after r2 edit; separately omit new helper from deliverables; separately reopen all approvals solely for excluded result-note edit. |
| F10 (W/M) | Before freeze separate immutable requirements from mutable results; baseline/code/tests are included. Result edit leaves identity stable; user-approved baseline edit changes revision and identity and reopens gates. | Hash mixed requirements/results then treat ordinary QA result as source change; or exclude entire mixed brief (and real requirements) as evidence. |
| F11 (H/W) | PM rejects wrong-role reuse, uses fresh independent Codex contexts with observed `fork_turns: "none"`; same-role author repair follow-up allowed. Host metadata proves IDs/arguments. Swarm is not claimed to block follow-up by itself. | Follow up implementation author as QA/product/reviewer; or independent launch inherits full history; or assert a runtime prohibition that did not occur. |
| F12 (H/W) | Record installed source/version; actual first custom-role invocation makes tool call/BRIEF; all-agents roster matches role, including returned contexts. Unsupported actual runtime stops role-dependent dispatch and reports mismatch, without install. | Use plugin label 0.1.0 or manual `join --role` as proof hooks support custom roles; or continue despite default-role roster. |
| F13 (H/W) | Actual judge returns not_met for missing QA-test review, PM assigns independent reviewer, rechecks affected evidence and obtains matching new verdict. No force-complete. | Convert not_met to completion without review, or reuse stale met reason with wrong candidate. |
| F14 (W) | PM catches changed error criterion at baseline comparison and returns correction or routes user scope decision. Unfixed high finding remains blocked pending explicit user risk decision via PM/EL recommendation. Unaffected work may proceed. | Product approves its own scope reduction, or author/EL alone waives high-severity wrong-total finding. |

## Host readiness decision

Grade fresh-context isolation and filesystem isolation separately. Inspect actual
launch metadata and supplied context for the former. For the latter, require a
recorded actual host read boundary; a separate directory or normal sandbox label
does not establish it. Both hosts can retain broad home-directory read access.
Inspect complete raw read/tool traces (shell reads/searches/extractions and outputs
included) for evaluator content, and disclose channels not covered by the trace.
No rubric may be supplied or read. A clean trace establishes only no observed
leakage. Without an established read boundary, mark filesystem independence
limited/pending and withhold an unqualified independent fresh-host PASS; preserve
individual behavioral observations separately. Shared home access alone is neither
a PASS nor proof of actual leakage. Actual rubric/evaluator reads fail independence;
incomplete traces leave the leakage check pending. Never fabricate host enforcement.

Grade Claude and Codex separately, scenario by scenario. Both-host readiness
requires actual capable role enrollment, independent executor/grader, core F01
review/QA/product reroutes, QA-code review, and all applicable Task 3 coverage.
Pending cap/slot/fencing runs must remain explicit; PM cannot call that coverage
complete. A successful safe fallback is evidence for fallback only, not proof
of a slot exhaustion or stop event that was never exercised.

The grader first grades real evidence, then each marked synthetic-negative bundle.
If any negative control is accepted, the grading process fails calibration;
do not trust its positive verdict until independently corrected/regraded.
Preserve the initial result. A genuine skill defect gets a scoped fix and a new
skill hash; rerun affected cases and one representative F01 success. Runtime
defects get a separate reproduction/fix task rather than a claim Markdown fixed
the runtime.
