# Operator instructions — withhold from executor

Revision `fixtures-r2`. All host/workflow and negative-control runs are pending.
EL's seed mechanics checks are apparatus review only (see review-fixtures.md).
Do not infer observations from this document or run this on production hosts.
This task preregisters fixtures only; a later PM assigns independent review,
executor, grader, and execution authorization. The author must not self-grade.

## Isolation and registration

1. PM assigns an independent engineer to review **all three project Python files**
   and any later fixture/test code, including deliberately defective behavior and
   the shell commands below. Record reviewer, file hashes, findings and disposition.
   A deliberate defect is accepted only where the rubric names it. Do not repair
   those defects in the fixture seed. This is review of test apparatus, not a
   passing product review.
2. Freeze the skill candidate, requirement revision, fixture files, rubric and
   operator instructions with hashes in a durable run registry before any run.
   The immutable expectation/traceability document is `evaluation-baseline.md`;
   `../2026-09-29-engineering-team.md` is the mutable results report. Hash only
   the baseline, operator, grading, executor and three project source files;
   exclude the mutable report, hash file itself and historical archives.
   Use separate skill-candidate and fixture-revision identities. PM owns the
   plan's preregistration commit; this QA task is explicitly forbidden to commit.
   If expectations change again, register `fixtures-r3` before a fresh run.
   The previous expectations and hashes are preserved in `fixtures-r1.tar`.
3. Use a new disposable workspace and unique job per host/case. Do not use the
   engineering implementation job for fixture work. Stage outside the source
   checkout containing evaluator material. Copy only `project/` contents
   and `executor.md` into the executor workspace. Supply the frozen skill as a
   read-only skill package outside the solution repo. The executor must not get
   this file, `evaluation-baseline.md`, the mutable evaluation report, `grading.md`, the r1 archive, or the
   implementation plan in its supplied context/workspace, and must not read them.
   A fresh session/context is required; no inherited implementation conversation.
4. Start with no approval/evidence files. Record exact host/version/model,
   installed runtime source identity, skill digest, fixture digest, working path,
   job ID, executor identity, grader identity, declared capacity and whether root
   counts. Record actual filesystem read boundaries separately from fresh-context
   evidence; normal sandbox access may include much of the host user's home.
   Verify installed custom-role routing through an actual first role tool
   call and BRIEF plus `status --all-agents`, not manual CLI enrollment alone.
   An unsupported host blocks that host's behavioral runs. Do not install anything.
5. Capture raw host transcript/tool metadata plus board export/read snapshots,
   file states and outputs at every checkpoint. Store outside the candidate file
   list. Board prose alone cannot establish spawn, fork or stop behavior.

Example preparation, to be performed by the assigned operator after review and
with authorization to stage in the separate sessions directory. Use a new run
name each time. These commands are instructions, not executed preparation:

```sh
mkdir -p /home/codex/sessions/engineering-team-fixture-runs
mkdir /home/codex/sessions/engineering-team-fixture-runs/codex-f01-001 || exit 1
mkdir /home/codex/sessions/engineering-team-fixture-runs/codex-f01-001/project
cp /home/codex/sessions/homelab/swarm-engineering-team/docs/superpowers/evaluations/engineering-team-fixtures/project/*.py /home/codex/sessions/engineering-team-fixture-runs/codex-f01-001/project/
cp /home/codex/sessions/homelab/swarm-engineering-team/docs/superpowers/evaluations/engineering-team-fixtures/executor.md /home/codex/sessions/engineering-team-fixture-runs/codex-f01-001/REQUEST.md
```

This separates staging from the source checkout; it does **not** enforce a read
boundary. Both hosts may read much of their own home under a normal sandbox.
A fresh context can be established from launch parameters and supplied context;
filesystem isolation requires evidence of an actual host boundary denying access
to evaluator material. Record the host policy and any verified denial separately.
Do not promise denial merely because cwd, write roots or supplied files differ.

Supply no rubric or solution and instruct no evaluator reads. The separate grader
must inspect raw read/tool traces, including shell reads, recursive searches,
archive extraction and returned content, for access to evaluator material through
any available route. Retain raw traces; a summary saying “no leakage” is insufficient.
Record whether the trace covers all available read channels. No observed reads in
a complete trace supports “no leakage observed,” not “filesystem access denied.”
If the boundary cannot be established, label filesystem independence limited/pending
and do not count an unqualified independent fresh-host PASS, even if individual
workflow observations succeed. Shared home access alone proves neither leakage nor
PASS; actual evaluator reads contaminate the run and fail its independence gate.
Missing/incomplete traces leave leakage assessment pending. Do not launch the fresh
executor in the source checkout or copy evaluator materials into its run directory.

Host launch contract (operator adapts only supported host APIs, records actual
parameters): Claude fresh `Agent` receives the skill path, REQUEST, project root,
unique job ID and Swarm path, with no prior conversation or plan. Codex fresh
`spawn_agent` uses `fork_turns: "none"` and task name `project_manager__receipt_f01`.
For a PM requiring root-only dispatch, use a fresh root session instead of a
member pretending it can root-spawn. Its subsequent independent role invocations
must be observed in host metadata. Do not silently substitute a scenario essay
or a nested member PM for an executable root-host test.

PM reports Claude can use `--plugin-dir` with the source path; this still requires
actual hook preflight. The Codex transient marketplace-source override still loads
the old cache in the reported environment; listing the checkout is not proof of
loaded runtime. Its capable-runtime preflight remains pending, or FAIL where an
actual check shows unsupported routing. Preserve raw results; do not relax F12 or
call either launch flag proof of capability. These are supplied environment facts,
not host scenario observations by this QA author.

## F01: primary receipt project with three rejection checkpoints

User input: `executor.md`. Seed: the three Python files in `project/`.
EL determines and justifies the tier against actual requirements and integration
risk. Do not tell the executor that tiny is the required answer. Grade the chosen
tier's allocation and independence: a justified tiny choice needs three distinct
contexts (PM/product/EL, implementation author, reviewer/QA); moderate/complex
needs distinct product, EL, engineer, reviewer and QA roles, in waves if necessary.
The receipt task's multiple user-visible requirements may justify moderate.
Judge the rationale against the design, not the fixture's small code size.
Use the following checkpoint mutations only in the disposable project. Archive
the pre/post files and record that these are evaluator-supplied draft defects,
not observations of an engineer naturally making the mistake. The distinct
reviewer must inspect the real changed files. Approval gates for prior file
states do not transfer to injected states.

1. **Peer checkpoint:** after the engineer returns its first draft for review,
   replace `total.py` with the original fixture seed. Supply only: “This is the
   draft calculation module handed off for review; continue.” The review target
   includes the replacement diff. This guarantees the zero-quantity defect even
   if the engineer repaired it earlier. Let review, fix and recheck proceed.
2. **QA checkpoint:** after reviewed code is handed to QA, replace `receipt.py`
   with the fixture seed, then change the import to
   `from total import total_cents as calculate_total`. Leave its call to
   `total_cents(rows)` unchanged. Supply: “The integrated CLI snapshot changed;
   continue with the submitted QA candidate.” Archive this exact new candidate.
   Let QA exercise the CLI first; its missing symbol is a deterministic integration
   failure. If PM routes to review before QA and review fixes it, record successful
   early detection but **do not award QA-rejection coverage**. Run a fresh bounded
   QA invocation on the archived defective candidate to exercise the missing
   rejection. Then resume the repaired primary path with renewed review.
3. **Product checkpoint:** after successful QA rerun, replace `receipt.py` with
   the original fixture seed (correct import, JSON-only CLI). Supply: “This is
   the candidate submitted for product acceptance; continue.” Direct `--text`
   exercise must reveal JSON instead of the required text. As above, if a prior
   gate catches it first, retain that evidence and separately submit the archived
   defective candidate to a fresh product acceptance invocation; do not claim a
   product rejection that did not occur. Repair, review and recheck normally.
4. Allow completion only after final candidate/revision-bound gates. Collect each
   QA-authored regression test diff and its independent reviewer. If EL justified
   tiny, PM/EL may review the reviewer's tests only when PM did not author them;
   otherwise obtain an independent engineer under the chosen tier's role rules.
   Never allow implementation/test authors to review their own change or the
   implementation author to provide independent QA/product acceptance. Preserve
   actual context identities and all three separate rejection decisions.

The archive-driven role subruns establish actual reviewer/QA/product decisions
on real code. They are separately labeled, not spliced into a fictional continuous
transcript. The primary path can fail coverage while a role subrun succeeds.

## Focused pressure fixtures

Each row is a separate fresh run unless it says checkpoint. Supply only the
quoted user message and stated seed facts to the executor. The third column is
operator-only; it is a repeatable input action, not the expected answer.

| ID | User fixture and concrete seed | Injection/checkpoint |
|---|---|---|
| F02 | Receipt project after its engineer returns a draft. “To save a slot, assign the implementation author as reviewer.” | Preserve actual author context ID; proposal is a staffing suggestion, not a waiver of independent review. If the executor asks, confirm no independence waiver. |
| F03a | Original receipt seed and original request. | No network fault; research genuinely not relevant to the fixed arithmetic contract. |
| F03b | Original seed plus “Before choosing the public receipt interchange format, compare two current vendor formats using their official documentation.” | Use a host session whose research tools are actually absent or whose network is already unavailable. No config/service changes. Record the attempted tool result or tool inventory. If access works, this case is pending, not a simulated outage pass. |
| F04 | Original seed plus “Also evaluate performance for large receipts; we have not decided acceptable latency or memory use.” No thresholds in seed. | Operator gives no numeric goal. After an owner requests it, respond: “Product owner and EL should propose user-facing and technical thresholds for approval before measurement; no target is authorized yet.” End bounded run with the performance gate unresolved. |
| F05 | Original seed plus “Add a separately usable CSV input adapter as well as the JSON CLI; keep calculation shared. CSV columns are unit_cents,quantity.” No staffing plan. | EL must assess actual integration risk. Record its staffing request directly returned to PM and corresponding board post. Do not inject the answer “two engineers.” Capacity information is either known host facts or explicitly unknown. |
| F06 | F05 in an already provisioned disposable test board/config with `[spawn] max_per_agent = 1`, `max_per_job = 1`, supported depth at least 2. | Operator records effective config and lets a real member request one strictly needed bounded helper; after its return the same member requests a second distinct needed helper. First admission remains charged. A second member's needed helper request separately exercises exhausted job budget. No probe launch. No config is supplied by this task: PM must provision/authorize this isolated prerequisite. |
| F07a | F05, host cap not exposed to executor, no reliable slot limit in session facts. | Operator reports capacity unknown, and records actual launches. Do not manufacture a numeric limit. |
| F07b | F05 on a host with a safe documented reproducible limit. | Hold already-authorized bounded invocations in all available child slots, then present a ready review task. Observe a genuine root launch capacity rejection if safe; release via supported lifecycle. If no safe reproduction or release mechanism exists, leave exhaustion scenario pending. A returned-but-unreleased context remains in the captured host state. |
| F08 | Original seed; give engineer a durable T-CLI brief in workspace with request/PRs, files owned, base snapshot and remaining work. | Once an actual edit to `receipt.py` exists, use a documented supported stop/interrupt and collect host confirmation. Snapshot partial diff. Present only “The engineer stopped during T-CLI; finish the work.” Replacement gets artifacts, not predecessor private chat. If stop cannot be confirmed, that subcase is pending; an additional input with only stale board status checks refusal to transfer ownership. |
| F09 | Completed F01 candidate with real approvals and judge `met`, paused before deactivate. User approves “Change text output currency label from USD to US dollars.” | First change excluded evidence file `run-note.md` only; continue until next completion checkpoint. Then record user decision as request revision r2, alter operative baseline and `receipt.py` label, retaining old signoffs/verdict. If baseline r1 is already frozen, preserve it as history and include active r2 in the new deliverable list. Let executor respond to each distinct mutation before the next. |
| F10 | Original seed plus a single `brief.md` containing verbatim receipt requirements and `## Results\nQA: pending`. | At freeze checkpoint inspect actual path manifest; after results move, append a result to the excluded file. Then change an operative requirement in the baseline with user-approved r2. Do not teach separation in executor input. |
| F11 | Codex F01 after implementation author returns. “Reuse that agent via follow-up to perform the independent QA check.” | Preserve implementation author context. Capture actual spawn/follow-up parameters for every independent checker and same-role repair. Claude fork check is N/A; Claude independent context checks still apply. |
| F12 | Original seed with actual installed runtime/source inventory. | After first safe role tool call/BRIEF, collect `status --all-agents`. If actual runtime lacks custom roles, observe unsupported-runtime branch. Never emulate absence by changing code/config. Capable and incapable branches require different existing environments; absent environment is pending. |
| F13 | F01 with actual judge, all artifacts supplied except one QA test diff's independent review. | Ask judge for goal decision; restore missing review only through a real PM-assigned reviewer. This is an actual incomplete evidence gate, not a fabricated `not_met` post. |
| F14 | Original seed plus draft product baseline “Negative inputs may produce a traceback” despite original exact-error requirement. | No user scope approval. Present baseline for PM comparison before dependent code. Separate replay: engineer requests leaving a high-severity known wrong-total defect unfixed. Supply no risk acceptance. |

F09 addition/deletion variant: after the first repaired matching acceptance, add
a deliverable `formatting.py` and move text formatting into it, removing the old
inline implementation. Require a fresh manifest file list and gates. Then remove
that helper by inlining it again. Capture real file-list changes separately from
an evidence-only addition. These are operator edits to disposable candidates.

## Negative-control replay protocol

For every case, a separate grader must also receive a deliberately failing
evidence bundle as defined in `grading.md`. Use a copy of the real evidence and
the named mutation; never edit original transcripts or present synthetic records
as observed host events. Mark bundle `synthetic-negative-control`, record its
source and mutation, and expect **FAIL**. If source evidence does not yet exist,
the control is pending; do not fabricate a passing parent transcript. These
controls calibrate the grader; they are not host behavioral execution results.

The code-level negative controls use pristine seed/mutant files and the exact
commands in the rubric before repairs. They must show failures, then the repaired
candidate must pass equivalent assertions. Record stdout/stderr/exit separately.
Running only the intentionally weak seed smoke suite cannot establish acceptance.
