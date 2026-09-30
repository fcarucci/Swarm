# Integrated engineering-team candidate freeze

**Candidate:** `85cd76518f3b448f67a36c061d92cadd8a18c4b5f0c8ce62d1549a9a1139087d`
**Requirement revision:** `product-r1`
**Source HEAD:** `53a4583e4ca829c8d908d860a54243129c1063d2` on `feature/engineering-team-current`
**Frozen source files:** 227 regular files; 22897 canonical manifest bytes
**Separate four-file skill package digest:** `341592f67b533be925e844178777ff9a6a9b640021e893cdc298721903541ad8`
**Fixture set:** `fixtures-r2`, seven unchanged preregistered hashes; hash-list SHA-256 `9509d03b4be022b77e01c50f017f3d7e6886b2b493460c20be77c58b7892bd87`

The project root is the deliverable root. The active manifest includes every regular operative source, test, skill, configuration and requirement file, including the design, plan, product and engineering baselines, the four skill files, restored custom-role runtime, and the seven immutable evaluation fixture inputs. The full path/hash list is in `candidate.json`; the raw canonical bytes are in `candidate.manifest`. The manifest is sorted by unsigned UTF-8 path bytes; each entry is path bytes, NUL, a lowercase 64-character raw-file SHA-256 digest, then LF. The candidate ID is SHA-256 of those bytes.

The exact exclusions are copied from the historical candidate: `.git` metadata, `.superpowers/` runtime scratch, `.venv/`, `.pytest_cache/`, Python bytecode caches, the predeclared evidence and fixture-runs directory prefixes, historical review evidence, and named mutable preflight, PM status, review, evaluation-result, preregistration-checksum and archive files. All observed excluded regular paths and reasons are recorded individually in `candidate.json`. Operative files are prohibited in evidence prefixes. No symlink or non-UTF-8 path was found. The history copy under `evidence/history/candidate-r1/` remains excluded and byte-identical to its pre-freeze hashes.

This tree combines upstream `e3cd14b` with custom-role recovery `3d9e560`, reviewed skill/fixtures, the PM identity correction `3217642`, and runtime followup `53a4583`. The E2 key correction has same-reviewer static READY evidence. Snake reported runtime followup READY on the board; this freeze is an identity record only and does not make a technical acceptance or installed-host claim. The new candidate has not been staged or graded on Claude or Codex. Old-package fixture observations remain historical, and prompt-assisted focused observations cannot be promoted to independent PASS.

Before final acceptance, PM independently recomputes the full-root manifest and package digest, confirms runtime reviewer evidence, stages exactly this source package, observes actual installed custom-role enrollment on each host, obtains fresh affected/representative graded runs and negative controls, then collects QA, product and EL decisions plus a matching judge verdict against this candidate and requirement revision. Postgres-specific runtime behavior remains unverified where the recovery review did not execute it. No merge, install, push, release or deployment is implied by this freeze.

## Fixture inputs

| Path | SHA-256 |
|---|---|
| `docs/superpowers/evaluations/engineering-team-fixtures/evaluation-baseline.md` | `d82701364c00f9961479b99f8397efd6c16cc6ae97139e564488baa575a074bc` |
| `docs/superpowers/evaluations/engineering-team-fixtures/executor.md` | `4c217b663f69982adde7a446c1c508583425e8d5efe8a5c14877482927c73222` |
| `docs/superpowers/evaluations/engineering-team-fixtures/grading.md` | `89ba22e513e8d0ec373785ae852bcb5b7abaeba2e8b52a59d04249784f171dc5` |
| `docs/superpowers/evaluations/engineering-team-fixtures/operator.md` | `8bd24bea4ad2e0359fb315effc5e98ebbb80f93361505a719c83fce8038d50d2` |
| `docs/superpowers/evaluations/engineering-team-fixtures/project/receipt.py` | `60842ad5e735f906e176e75c626f5027298902198970631fbc2c9027effc612c` |
| `docs/superpowers/evaluations/engineering-team-fixtures/project/test_total.py` | `2a7524b9ab86528aec2a62a6bc03d8261c81d559921a846e4b05e2993cdfe891` |
| `docs/superpowers/evaluations/engineering-team-fixtures/project/total.py` | `471c66883123e2773c4ba608f760c61cc56b873fb83121b72d3fa1bc6a6fba38` |
