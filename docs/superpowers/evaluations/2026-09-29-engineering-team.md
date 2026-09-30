# Engineering-team evaluation results

PM/report owner: **Rabbi Hyman Krustofsky**. Fixture author: **Hubert Wong**.
Job: `engineering-team-impl-20260929`. Fixture revision: **fixtures-r2**.
Status: **implementation merge prepared; full host readiness not established**.

This mutable report is excluded from candidate identity. Immutable expectations,
applicability and evidence requirements remain in the
[evaluation baseline](engineering-team-fixtures/evaluation-baseline.md),
[operator instructions](engineering-team-fixtures/operator.md), and
[grading rubric](engineering-team-fixtures/grading.md). Their preregistered
[hashes](engineering-team-fixtures/preregistration.sha256) have not changed.
Observations below are not grader verdicts or a both-host readiness claim.

## Merge-focused implementation status (2026-09-30)

The implementation is ready to merge as the reviewed Swarm source and skill
change. This does not certify both-host workflow readiness or release readiness.

- The current integration branch restores custom agent roles on upstream
  e3cd14b, adds the engineering-team skill, and includes the reviewed design
  and preregistered evaluation. The skill/runtime source reviews are READY,
  including same-reviewer follow-up for the role parser and PM identity fixes.
- Forty focused regression tests passed on the integrated runtime.
- A fresh Claude two-job check confirmed job/session-specific PM identities
  and own-job board reads after both jobs resumed.
- The representative Claude receipt run reached implementation, independent
  review, QA tests, and review of QA tests. A nested reviewer cleanup command
  was denied by Claude Code's built-in safety check because its variable path
  could not be bounded. It was not run or retried. Final freeze, final QA,
  product, EL, and judge acceptances were withheld for that run.
- The installed Codex preflight still loaded the older cache and enrolled a
  requested reviewer as default; Codex automatic role routing remains
  unsupported until a fresh capable runtime check is made.
- An independent grader correctly rejected all three eligible synthetic
  failure controls. Its five real scenario decisions remain PENDING where
  required snapshots, reviewer assignment, QA exercise, or unbiased prompt
  evidence were absent. Two additional controls remain PENDING.

These runtime limits remain in the linked evidence. They do not indicate an
open source-review finding. Do not describe this merge as final host acceptance,
Codex runtime support, or a release.

## Registration and source identities

- Independent [apparatus review](../implementation/engineering-team/review-fixtures.md)
  accepted fixtures-r2 before preregistration commit `916282b` and before host execution.
  The original r1 history remains archived; it is never supplied to executors.
- Original evaluated candidate: `6b414e69aaa1e87ff098a69ae2f03197c1b1eacd2e2369d03ffe3434c46b62c1`,
  requirements `product-r1`; four-file skill package
  `6db9ce7c33fb9cb13a35fba845a850ac65b206cf6a1cd649af52d5d4eb12930e`.
  Registry: `../implementation/engineering-team/evidence/evaluation-run-registry.json`.
- The original package has a confirmed defect E2-R2: its literal PM key is global,
  so a second job can move the first job's PM record. Original runs remain historical
  evidence. Operator identity corrections are interventions, not observed behavior
  of a corrected skill.
- The [host guide fix](../implementation/engineering-team/evidence/pm-identity-fix.md)
  is commit `74699cc`, independently rechecked by the same E2 reviewer. Its
  `hosts.md` hash is `b8302b26642eb8236f120a4ba90372f0e047132d830837197987742fde257223`.
  A revised integrated candidate/package and fresh affected checks remain pending.
- Current upstream `e3cd14b` omitted the previously merged custom-role support.
  Recovery commit `3d9e560` restores it while preserving upstream changes;
  its [independent review](../implementation/engineering-team/evidence/role-recovery-review.md)
  requests scoped fixes before final integration. No runtime installation is claimed.

## Observed host prerequisites

[Claude preflight](../implementation/engineering-team/claude-preflight.md) observed
an actual first role invocation, BRIEF and automatic `reviewer` enrollment using a
source snapshot with `--plugin-dir`. The separately installed old cache was also
enabled; source schema9 versus board schema10 and launcher bootstrap side effects
are recorded. Session loading is not evidence of a global plugin installation.

[Codex preflight](../implementation/engineering-team/codex-preflight.md) observed
that the session-local marketplace override still loaded old cached hooks. Its
fresh `reviewer__structure` child enrolled as `default`. Static document checks
passed, but capable automatic custom-role routing did not. All dependent Codex
behavior remains pending a capable installed/trusted runtime; manual role joins
are not substituted for hook evidence.

Executors receive separate workspaces and fresh contexts. These do not establish
an OS-enforced read boundary against the home filesystem. A separate grader must
audit complete raw read/tool traces and record missing channels. No unqualified
filesystem independence or fresh-host PASS is claimed.

## Execution register

PM inspected the focused operator's launch prompts and found that F04/F05 explicitly
requested some expected ownership, direct-return and board-read steps. Their actual
observations are **prompt-assisted**, not independent evidence that the skill alone
elicited those steps. Preserve exact prompts; new prompts need apparatus review
before launch. The grader must evaluate this limitation rather than infer clean
independence from a fresh session ID.

Every case remains pending independent grading. A recorded partial observation
does not close missing conjunctive gates in the preregistered rubric.

| Case | Claude observations | Codex | Remaining gate / evidence |
|---|---|---|---|
| F01 peer | Actual seeded zero-quantity defect rejected; author repaired; same reviewer rechecked and accepted | PENDING | Raw phase01 result and review artifacts; independent grading pending |
| F01 QA/test-code | Independent review caught and repaired QA tests that passed vacuously under optimization. CLI alias defect was caught by peer review before QA | PENDING | Fresh bounded QA rejection on reconstructed archived defective candidate required; primary repair/QA continuation underway |
| F01 product | Primary run reached product checkpoint; operator executing registered mutation/continuation | PENDING | Actual product rejection, repair and final bound acceptance still pending |
| F02 self-review | PM declined author-as-reviewer suggestion but initially left review unassigned | PENDING | Operator applies permitted no-waiver clarification, then captures actual fresh review; preserve the initial suggested EL reuse and PM intervention |
| F03a/F03b research | F01 recorded internal-format research N/A; relevant blocked-research environment not exercised | PENDING | Grade N/A acknowledgment/report; F03b actual unavailable access required |
| F04 performance target | Product and EL proposed thresholds marked unapproved; no measured PASS claimed | PENDING | Verify actual QA request and owner routing in raw traces; threshold gate remains unresolved |
| F05 staffing/direct return | Fresh old-package run underway | PENDING | EL assessment, direct return, PM board read and assignment must be graded |
| F06 member/job caps | Not executed | PENDING | Disposable board/config prerequisite and actual admissions/refusals needed |
| F07a/F07b capacity | Host-capacity observations exist in F01; safe actual exhaustion not established | PENDING | Grade actual scheduling; no assumed slot release or fabricated limit |
| F08 interruption/recovery | Not executed | PENDING | Supported confirmed stop/fencing before transfer required |
| F09 stale/evidence identity | Awaiting actual matching final approvals and judge met in F01 | PENDING | Operator owns registered checkpoint mutations; no stale verdict acceptance |
| F10 baseline/results split | Fresh old-package run underway | PENDING | Actual split, manifest and evidence/requirement mutations required |
| F11 fresh contexts/reuse | Claude context identities recorded; Codex fork field inherently N/A on Claude | PENDING | Independent raw-metadata audit; unsupported Codex run cannot pass |
| F12 host preflight | Actual capable Claude routing observed; full independence grading pending | PENDING / unsupported runtime observed | Both capability branches must be distinguished from installation labels |
| F13 judge repair | Not yet executed | PENDING | Actual not_met on missing QA-test review, real repair and matching verdict |
| F14 scope/high finding | Held for revised frozen package | PENDING | Actual baseline comparison and unresolved-high decision required |
| Negative controls | QA preparing clearly labeled synthetic bundles and code-level seed controls from existing evidence | PENDING | Independent apparatus review and separate grader must reject all failing controls |

## Evidence and outstanding acceptance

Raw task logs and executor outputs are retained under the task's `.superpowers`
run directory and protected Swarm transcript storage; they are not committed or
presented as local memory. Operators preserve original traces and disclose mutation
provenance. The first peer/QA injections saved a full before snapshot and mutation
hash; any full defective after snapshot reconstructed from those inputs is labeled
reconstructed, not a contemporaneous archive.

The [technical assessment](../implementation/engineering-team/evidence/technical-acceptance.md)
withholds final acceptance. The [product assessment](../implementation/engineering-team/evidence/product-acceptance.md)
is static and historical, not final host acceptance. Outstanding work includes
scoped runtime re-review, revised freeze, fresh PM identity regression and a
representative successful revised flow, grading/negative controls, and matching
QA/product/EL/judge decisions. Pending or unsupported cases stay explicit.
