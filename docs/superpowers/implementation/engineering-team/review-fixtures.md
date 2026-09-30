# Independent review of QA fixture apparatus

**Reviewer:** Hans Moleman, engineering lead; did not author the fixture files.
**Reviewed revisions:** initial `fixtures-r1` and corrected `fixtures-r2` preregistration snapshots on 2026-09-29.
**Scope:** three Python seed files, `operator.md`, `executor.md`, `grading.md`, the immutable `evaluation-baseline.md`, and the mutable evaluation report's separation. This reviews fixture correctness and evaluator protocol, not behavior of the skill on either host.
**Decision:** **READY for preregistration freeze** at the seven `fixtures-r2` hashes below. QA-R1 and QA-R2 are resolved in the protocol. This is not a host-behavior PASS; all fixture host/workflow runs remain pending. Preserve the deliberate seed defects.

## Findings

| ID | Severity | Initial evidence and impact | `fixtures-r2` disposition |
|---|---|---|---|
| QA-R1 | Medium | In r1, `operator.md:61,92-95` called F01 a tiny path, and `grading.md:77` required the compact three-context allocation. The supplied task has two modules and multiple user-visible requirements (`executor.md:8-15`). The [approved design](../../specs/2026-09-29-engineering-team-design.md#staffing-tiers-and-independence) makes user-visible behavior, multiple requirements, or nontrivial integration a moderate trigger. A correct EL could have chosen moderate and been marked failed. | **Resolved.** Current operator F01 section requires an EL-justified tier, gives distinct context rules for tiny versus moderate/complex, and explicitly permits moderate. Current grading F01 section requires the same rationale and separation; its negative bundle tests collapsing roles required by the chosen tier. The evaluation baseline's U3a trace also says EL-justified tier. |
| QA-R2 | High | In r1, `operator.md:40-43` staged the executor run inside the source checkout containing evaluator material, contrary to its own isolation rule. Prompt or working-directory separation could not establish denied read access, risking leakage into supposedly independent execution. | **Resolved as an apparatus protocol.** Current operator example stages in `/home/codex/sessions/engineering-team-fixture-runs`, outside the source checkout, and supplies selected files only. Operator, grading host-readiness rules, and evaluation baseline separate fresh context, trace-observed leakage, and a verified filesystem read boundary. They prohibit an unqualified independent-host PASS without boundary evidence and mark missing evidence pending. No such boundary or host PASS is claimed here. |

## Reproducible mechanics check

I ran the focused seed checks with `python3 -B` from the seed project's directory; no fixture file changed. The two seed smoke tests passed. `total_cents([{unit_cents:250, quantity:0}])` returned **250** instead of zero. The JSON CLI on a positive row returned `{"total_cents": 500, "currency": "USD"}` with exit 0. The same input with `--text` returned JSON, not `USD 5.00\n`. A negative quantity exited 1 with a `ValueError` traceback instead of exit 2 and the exact one-line stderr. Executing the preregistered alias mutation in memory (`from total import total_cents as calculate_total`, retaining `total_cents(rows)`) raised `NameError: name 'total_cents' is not defined`. These observations support the intended peer, QA integration, and product rejection triggers. They are fixture-mechanics evidence only; no independent executor, grader, or host workflow ran.

The seed tests are intentionally incomplete: `test_total.py` checks only positive quantity and empty rows, so its passing result is a valid weak-test control. `total.py` contains the deliberate `quantity or 1` defect; `receipt.py` has the intended missing text/error behavior. The operator's checkpoint replacements reintroduce these defects into a disposable candidate and explicitly revoke prior approval states. The rubric distinguishes actual workflow/host events from fixture mechanics and requires failing negative bundles. F02–F14 preregister owners, injections, expected gates and negative controls; F06/F07b/F08/F12 correctly require real host mechanisms or a PENDING result. The evaluation record marks every host scenario PENDING and does not claim observed behavior.

## Initial `fixtures-r1` file hashes (historical)

Paths below are relative to `docs/superpowers/evaluations/engineering-team-fixtures/`, except the final evaluation record.

| File | SHA-256 |
|---|---|
| `project/total.py` | `471c66883123e2773c4ba608f760c61cc56b873fb83121b72d3fa1bc6a6fba38` |
| `project/receipt.py` | `60842ad5e735f906e176e75c626f5027298902198970631fbc2c9027effc612c` |
| `project/test_total.py` | `2a7524b9ab86528aec2a62a6bc03d8261c81d559921a846e4b05e2993cdfe891` |
| `operator.md` | `095fcf48a8d910633b9b46a9c44a699b6ca3c00492a9d29475e9d61a35b64fa3` |
| `executor.md` | `740bf720c60e81befadb3ae4c0575cb95d81cf2b6f1d6a4be4263e74b7165e72` |
| `grading.md` | `1d63ff8272a67aff403eb0b95d07fa4b6943b35834e9953c515b729da622357e` |
| `../2026-09-29-engineering-team.md` | `2bbca31bbef06fe83943047fbed6d9666e8c8736d0b72f2c600af77d0aa0e39d` |

These were the initial QA `preregistration.sha256` inputs and are preserved in `fixtures-r1.tar`. They are historical review evidence, not the active run inputs.

## Final `fixtures-r2` preregistration check

`sha256sum -c preregistration.sha256` passed for all seven current files. The list has seven unique repository-relative paths in sorted order: the immutable evaluation baseline, operator, executor, grading rubric, and all three Python seed files. It excludes the mutable evaluation results report, the hash list itself, and the r1 archive. The current Python seed hashes match r1, so the deliberate peer/QA/product defects remain. The revised Markdown files have no trailing whitespace. The r1 archive lists the initial seven files and initial hash list; it is retained outside active r2 expectations.

| Active file | SHA-256 |
|---|---|
| `engineering-team-fixtures/evaluation-baseline.md` | `d82701364c00f9961479b99f8397efd6c16cc6ae97139e564488baa575a074bc` |
| `engineering-team-fixtures/executor.md` | `4c217b663f69982adde7a446c1c508583425e8d5efe8a5c14877482927c73222` |
| `engineering-team-fixtures/grading.md` | `89ba22e513e8d0ec373785ae852bcb5b7abaeba2e8b52a59d04249784f171dc5` |
| `engineering-team-fixtures/operator.md` | `8bd24bea4ad2e0359fb315effc5e98ebbb80f93361505a719c83fce8038d50d2` |
| `engineering-team-fixtures/project/receipt.py` | `60842ad5e735f906e176e75c626f5027298902198970631fbc2c9027effc612c` |
| `engineering-team-fixtures/project/test_total.py` | `2a7524b9ab86528aec2a62a6bc03d8261c81d559921a846e4b05e2993cdfe891` |
| `engineering-team-fixtures/project/total.py` | `471c66883123e2773c4ba608f760c61cc56b873fb83121b72d3fa1bc6a6fba38` |

The PM can freeze this preregistration snapshot. Any later expectation edit needs a new fixture revision and renewed review before the affected host run. Source skill review, candidate freeze and each host's behavioral evidence have separate gates.
