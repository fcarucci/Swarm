# Engineering-team evaluation baseline

Owner: **Hubert Wong**, independent QA, key `engineering-impl-qa`.
Job: `engineering-team-impl-20260929`; PM: **Rabbi Hyman Krustofsky**.
Revision: `fixtures-r2`, 2026-09-29.

This is the immutable preregistration baseline for Task 3. The r2 expectations
are unchanged by separating serialization from mutable results. Freeze this
baseline with operator, grading, executor and project source files after the
same EL review. Record review outcomes, run status, observations and handoffs in
[the mutable evaluation report](../2026-09-29-engineering-team.md), outside the
fixture hash list. A results update must not edit this baseline.

Sources: [approved plan](../../plans/2026-09-29-engineering-team.md),
[design](../../specs/2026-09-29-engineering-team-design.md),
[product-r1](../../implementation/engineering-team/product-baseline.md), and
[engineering brief](../../implementation/engineering-team/engineering-brief.md).
External market research for this implementation is N/A; F03b separately tests
future behavior when research matters and access is unavailable.

## Delivery and execution boundary

- [Executor request](executor.md): user task only,
  no solution or implementation plan.
- [Python seed project](project/receipt.py),
  [calculation](project/total.py), and
  [incomplete smoke tests](project/test_total.py):
  standard library only, synthetic data, intentionally defective preregistered seed.
- [Operator protocol and pressure seeds](operator.md):
  launch, injection timing, isolation and concrete state setup; withheld from executor.
- [Separate grading rubric](grading.md): exact outcomes,
  owner/board/gate checks, command probes and failing controls; grader only.

Fresh host executor receives frozen skill + executor request + selected seed
and actual host facts, staged outside the source checkout. Its supplied context
and run directory do not contain this record, operator instructions,
rubric, approved implementation plan, or author conversation. Separate grader
receives actual resulting evidence and the preregistered rubric. All QA-written
fixture/test code must receive independent engineering review before use. PM
assigns that reviewer; this QA author cannot approve it.

Fresh-context evidence is separate from filesystem isolation. Both hosts may
read much of their home under normal sandbox rules. No evaluator material may
be supplied or read; the grader inspects raw read/tool traces for leakage and
discloses missing channels. A clean trace does not prove denied access. Without
an actual verified host read boundary, filesystem independence remains
limited/pending and the run cannot count as an unqualified independent-host PASS.
Actual evaluator reads fail independence; shared access alone does not prove
leakage or establish a PASS.

The approved plan requests committed expectations before execution; this bounded
task forbids commits. PM must preserve a reviewed, hashed/committed preregistration
snapshot before scheduling execution. Files existing is not evidence of a commit.
No expected outcome in these files is an observation. Static fixture inspection
cannot establish host behavior. Seeds/replays supplied by an operator are marked
as such, never attributed to natural engineer behavior.

## Request trace and Task 3 coverage

Skill clauses below are target sections from the approved contract, **not verified
citations to a frozen implementation**. At preregistration authoring the skill
files did not yet exist. Executor/grader registry must pin actual skill hashes
and map these targets to final sections before a run; no line numbers invented.

| User request | Target skill clause | Product/engineering trace | Expected observed coverage |
|---|---|---|---|
| U1 PM scheduling/reporting | SKILL operating sequence; hosts PM board and bounded returns | PR-1; ER-1/4 | F01 final requirement report; F05 direct staffing return plus actual PM board read/action; F07 queue |
| U2 specifications, U2b requirements | team-roles product authority; artifacts product baseline | PR-2; ER-2/3 | F01 baseline comparison and criterion results; F10 split baseline; F14 correction of unauthorized scope |
| U2a competitor research | artifacts research applicability/access fields | PR-2; ER-3 | F03a acknowledged N/A; F03b observed access failure and blocked relevant research |
| U2c product acceptance | team-roles acceptance; artifacts frozen candidate | PR-2/8; ER-1/3 | F01 direct text/error exercise, rejection, repair and new product result; F09 freshness |
| U3 architecture/software requirements/plan | team-roles EL; artifacts ER/task map | PR-3; ER-1/2/3 | F05 actual EL architecture, dependencies and assignment; F01 technical acceptance |
| U3a staffing by complexity | team-roles tiers; hosts waves | PR-3/6; ER-2/4 | F01 EL-justified tier and its required independence; F05 EL assesses two-adapter work; F06/07 capacity decisions |
| U3b EL adherence review | team-roles technical acceptance | PR-3/8; ER-1/3 | F01 ER-linked technical result; F09 refreshed EL acceptance |
| U4 implementation, U4a peer review, U4b fixes | team-roles author/reviewer and finding policy | PR-4/9; ER-2 | F01 seeded zero defect and reviewer recheck; F02 self-review rejection; F14 high finding gate |
| U5 QA writes/runs tests | team-roles QA; artifacts applicability/performance | PR-5; ER-1/3 | F01 acceptance/CLI integration/end-to-end probes and independent QA-test review; F04 threshold owners, no invented goal |
| U6 Claude and Codex | hosts capability/routing/independence | PR-7/10; ER-4/6 | Both-host registry for every case; F12 actual hook role enrollment; F11 observed Codex fresh forks |
| U7 team size not fixed at four | team-roles tiers; hosts capacity/child limits | PR-6; ER-2/4 | F05 justified staffing, F06 nonrefundable child caps, F07 known/unknown capacity |

Additional Task 3 obligations: F08 actual stopped-writer transfer/reconstruction;
F09 changed PR/source after met, evidence-only stability and add/delete manifest;
F10 mixed baseline/result separation; F11 wrong-role follow-up rejection;
F13 actual judge not_met reroute. Every case has a separately defined negative
control. F01 contains a small realistic project whose tier EL must justify; focused scenarios avoid adding
infrastructure just to increase the apparent test count.

## Predeclared applicability and ownership

Acceptance, CLI integration, and command-line end-to-end checks apply to F01.
No dependencies or production infrastructure are needed for the synthetic code.
The seed smoke suite deliberately misses defects and is not a release gate.
Performance thresholds are **unset**: product owns user-facing goals; EL owns
technical limits before measurement. F04 tests the missing-threshold response,
not performance success. Any omission needs EL concurrence and PM disclosure.
No user performance target is invented here.

Host cap, child admission, lifecycle/fencing and source-backed role tests need
actual host support. F06 additionally needs an authorized disposable board and
throwaway SWARM_CONFIG whose two caps equal 1; provisioning is outside this task.
F07b/F08 are pending unless safe supported mechanisms are verified.

## Required run evidence

For each future row attach: run ID; executor/grader identity and separation;
host/runtime source/model; fixture revision/hash; skill candidate hash;
receipt candidate manifest/requirement revision; exact inputs and mutation
hashes; command outputs/exit codes; board post IDs; transcript/host event paths;
observed result; verdict; limitations. Do not replace this table with a generic
“passed tests” summary. Keep fixture-mechanics and scenario-reasoning results
separate from actual host/workflow behavior as defined by the grading rubric.

## Preregistration serialization

`preregistration.sha256` contains sorted relative repository paths and SHA-256
hashes for exactly this baseline, `operator.md`, `grading.md`, `executor.md`,
and the three Python source files in `project/`. It excludes the mutable report,
the hash list itself and historical archives. Preserve `fixtures-r1.tar` as
initial snapshot history; it is evaluator-only historical material, not active
r2 expectations. The hash list records bytes, not approval or host readiness.
