---
name: refactoring
description: Use when asked to refactor code, improve internal design, simplify conditionals, reduce duplication, clean APIs, reorganize data or inheritance, apply Fowler-style refactorings, or simply to suggest/recommend/propose refactorings without editing — works on a commit, one file, selected files, or a feature branch.
---

# Fowler-Style Refactoring

Use this skill for behavior-preserving refactoring based on Martin Fowler's *Refactoring* series and the official refactoring.com catalog. Treat the catalog as a menu of small transformations, not as permission for broad rewrites.

Do not copy book text into answers or code comments. Use the refactoring names, concepts, and the local context files in this skill.

## Operating Modes

This skill runs in one of two modes. Pick the mode **before** doing anything else; the wrong mode is the most common way this skill wastes work.

- **Suggest Mode** — Analyze code and return a ranked list of candidate refactorings. No edits, no build, no tests. Use when the user says any of: "suggest", "recommend", "propose", "what could we refactor", "what should I clean up", "analyze for refactorings", "find smells", "review for refactoring", "dry run", "don't edit yet", "just tell me what to fix", `--suggest`, "planning only". Also use when the user asks for refactoring on code they don't own or that they are still reviewing. Read [suggest-mode.md](references/suggest-mode.md) and follow its procedure. Do **not** run the Required Workflow below.

- **Apply Mode** — Pick one refactoring at a time, edit, build, test. Default mode when the user says "refactor", "clean up", "apply", "fix", "do it", or has already accepted a suggestion. Follow the Required Workflow below.

If you are uncertain which mode the user wants, ask in one sentence before doing any work. Defaulting to Apply Mode on ambiguous requests has caused unwanted edits in the past.

## Language Context

Before selecting or applying any refactoring, detect the primary language of the code being refactored. If language-specific guidance exists under `languages/<lang>/`, load every file in that folder and apply its constraints throughout the session.

Supported languages and their guidance files:

- **C++** — `languages/cpp/raii.md`: RAII idiom — what it is, how it works, and how to use it to simplify and correct resource-management code.
- **Rust** — `languages/rust/macro-restraint.md`: Restraint on macro-based refactorings — avoid introducing macros unless all other options have been evaluated and ruled out.

If the language is not listed above, proceed without language-specific constraints.

Load the relevant file(s) immediately after detecting the language and before consulting the catalog or making any edits.

## Required Workflow (Apply Mode)

Use this workflow only in Apply Mode. If the user asked for suggestions, switch to [suggest-mode.md](references/suggest-mode.md) instead.

1. **Establish scope before editing.**
   - Commit: inspect the commit diff and target the code introduced or touched by it.
   - Single file: inspect that file plus its callers, tests, and public API.
   - Multiple files: inspect all requested files and shared call paths.
   - Feature branch: compare against the merge base and work in small slices.
   - Read [scope-selection.md](references/scope-selection.md) when scope is a commit or branch.

2. **Discover verification commands before editing.**
   - Read repo instructions first: `AGENTS.md`, `CLAUDE.md`, `README`, CI config, package scripts, test docs.
   - Identify one **build** command, one **unit-test** command, and one **integration-test** command.
   - If automated tests do not exist for the area being changed, identify a **manual-test procedure** (reproducible script, sample run, fixture replay, UI smoke path) and confirm it with the user before proceeding.
   - Run a **baseline build + test** unless the user explicitly asked only for planning.
   - Read [verification-gates.md](references/verification-gates.md) before changing code.

3. **Choose exactly one refactoring at a time.**
   - Use [catalog-index.md](references/catalog-index.md) — each entry has a one-line trigger — to pick the catalog entry.
   - Load only the selected refactoring's context file under `references/refactorings/`.
   - Apply that one named refactoring in the smallest useful step.

4. **After every refactoring step, run the verification gate before doing anything else.**
   This gate is mandatory and runs in this order:
   1. **Build** the affected target(s). A green build proves the change still compiles, links, and produces deployable artifacts.
   2. **Run automated unit tests** scoped to the affected module, then the broader unit-test command.
   3. **Run automated integration tests** for the affected subsystem.
   4. **If no automated coverage exists for the changed behavior, run the manual-test procedure** identified in step 2 and record the exact steps and observed result.
   5. If any command is unavailable or impossible to run, **stop and report the concrete blocker** — do not proceed to another refactoring.
   6. If a gate fails, diagnose whether the failure was introduced by the last refactoring. Fix or back out only your own last step; never silently broaden the change.

5. **Continue only when build is green and every applicable test gate (automated or manual) passed for the current refactoring.** Never batch multiple catalog refactorings into one verification gate unless the user explicitly approved batching.

6. **Final response must list:**
   - Scope handled.
   - Refactorings applied, by name and in order.
   - For each refactoring: the exact build command, unit-test command, integration-test command, and (if used) manual-test steps that ran, along with their outcome.
   - Any skipped or blocked verification, with the exact reason.

## Refactoring Discipline

- Preserve observable behavior. If behavior must change, stop and get explicit confirmation.
- Prefer automated IDE/compiler refactors when available, then inspect the diff.
- Keep transformations reversible and reviewable.
- Do not mix formatting churn with semantic refactoring unless formatting is the refactoring target.
- Do not refactor across persistence, serialization, protocol, database, or public API boundaries without checking migrations and compatibility.
- Add characterization tests before refactoring poorly covered behavior. If a real test harness cannot be added in the time available, write down a manual-test procedure and treat its passing run as the gate.
- Do not batch several catalog refactorings into one verification gate unless the user explicitly approves batching.

## Selection Heuristics

- Hard-to-read local logic: start with Extract Function, Extract Variable, Split Variable, Slide Statements, or Decompose Conditional.
- Duplicate conditionals or repeated branches: consider Consolidate Conditional Expression, Replace Nested Conditional with Guard Clauses, or Replace Conditional with Polymorphism.
- Awkward parameters: consider Introduce Parameter Object, Preserve Whole Object, Replace Parameter with Query, or Change Function Declaration.
- Scattered data behavior: consider Encapsulate Record, Replace Primitive with Object, Move Function, or Extract Class.
- Inheritance pressure: consider Pull Up Method, Push Down Method, Replace Subclass with Delegate, or Collapse Hierarchy.
- Public API cleanup: favor wrappers, deprecation shims, and compatibility tests before Change Function Declaration, Rename Field, or Remove Setting Method.

## Stop Conditions

Stop and report instead of pushing through when:

- Baseline build or tests fail in unrelated areas and the user did not ask to fix them.
- Build, unit, integration, or manual-test procedure cannot be discovered or run.
- The needed transformation is behavior-changing, not behavior-preserving.
- The chosen refactoring touches generated code, vendored code, migrations, or binary artifacts.
- The diff grows beyond the agreed scope.
