# Move Statements to Callers

Source: https://refactoring.com/catalog/moveStatementsToCallers.html
Aliases: None listed in this skill.

## Applicability

- Use when a function includes statements that only some callers should own.
- Inspect local variables, data flow, side effects, async or exception boundaries, locks, and comments before editing.
- Best fit when the desired result is behavior-preserving and can be verified by existing or added tests.
- Avoid or stop when the change risks moving code across side effects, changing evaluation order, or making names less precise without explicit user approval.
- Load this file only after the skill selects this specific refactoring.

## Description

This is a local code-shaping refactoring. It improves the internal expression of an existing function or small region without changing its externally visible result. For this refactoring, the practical target is: a function includes statements that only some callers should own. Keep the transformation narrow enough that a failed test can be traced to this refactoring alone.

## Steps

1. Confirm the enclosing behavior is covered by tests or add characterization coverage first.
2. Select the exact expression, statement group, variable, or helper boundary to change.
3. Check variable lifetimes, side effects, async or await points, exceptions, and lock boundaries.
4. Apply the smallest source edit or IDE refactor that performs only this transformation.
5. Inspect the diff for accidental formatting churn or behavior changes.
6. Build, then run unit tests and integration tests (or the manual-test procedure if no automated coverage exists), before choosing another refactoring.

## Potential Tests

- Unit tests for the enclosing function's normal path and edge cases.
- Unit tests for branch, error, and side-effect-sensitive behavior touched by the edit.
- Characterization tests for unclear legacy behavior before changing structure.
- Integration tests through the original caller path, not only the extracted helper.
- Regression tests for any behavior that was easy to break while applying this refactoring.

## Completion Check

- Diff shows this refactoring only; no opportunistic rewrite is mixed in.
- Build succeeded for the affected target(s) after this refactoring.
- Unit tests passed after this refactoring.
- Integration tests passed after this refactoring.
- If no automated coverage exists for the changed behavior, the manual-test procedure was executed and recorded.
- Any compatibility shim, migration need, or skipped verification is documented in the final response.