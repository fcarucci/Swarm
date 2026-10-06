# Extract Class

Source: https://refactoring.com/catalog/extractClass.html
Aliases: None listed in this skill.

## Applicability

- Use when one class has multiple responsibilities or subsets of fields and methods change for different reasons.
- Inspect imports, call direction, field ownership, construction paths, visibility, dependency cycles, and module boundaries before editing.
- Best fit when the desired result is behavior-preserving and can be verified by existing or added tests.
- Avoid or stop when the change risks creating dependency cycles, exposing internals, over-splitting responsibilities, or moving behavior across ownership boundaries without explicit user approval.
- Load this file only after the skill selects this specific refactoring.

## Description

This is a responsibility-movement refactoring. It moves fields, functions, or behavior toward the owner that best matches the domain responsibility. For this refactoring, the practical target is: one class has multiple responsibilities or subsets of fields and methods change for different reasons. Keep the transformation narrow enough that a failed test can be traced to this refactoring alone.

## Steps

1. Identify the responsibility boundary and prove the moved member belongs with the target owner.
2. Find imports, call direction, construction paths, and dependency cycles before moving code.
3. Move one field, function, or responsibility at a time.
4. Leave forwarding compatibility only when callers or public API need a transition path.
5. Inspect the diff for new cycles, visibility leaks, and bloated abstractions.
6. Build, then run unit tests and integration tests (or the manual-test procedure if no automated coverage exists), before the next move.

## Potential Tests

- Unit tests for the moved behavior at its new home and through the old public path if retained.
- Unit tests for construction, ownership, and collaborator behavior.
- Compile or type tests that catch dependency and visibility problems.
- Integration tests through real consumers that exercise the moved responsibility.
- Regression tests for any behavior that was easy to break while applying this refactoring.

## Completion Check

- Diff shows this refactoring only; no opportunistic rewrite is mixed in.
- Build succeeded for the affected target(s) after this refactoring.
- Unit tests passed after this refactoring.
- Integration tests passed after this refactoring.
- If no automated coverage exists for the changed behavior, the manual-test procedure was executed and recorded.
- Any compatibility shim, migration need, or skipped verification is documented in the final response.