# Split Loop

Source: https://refactoring.com/catalog/splitLoop.html
Aliases: None listed in this skill.

## Applicability

- Use when one loop performs multiple independent jobs over the same collection.
- Inspect branch precedence, predicate side effects, loop ordering, cleanup paths, finalization, transactions, and error behavior before editing.
- Best fit when the desired result is behavior-preserving and can be verified by existing or added tests.
- Avoid or stop when the change risks changing evaluation order, branch precedence, loop count, cleanup behavior, or edge-case semantics without explicit user approval.
- Load this file only after the skill selects this specific refactoring.

## Description

This is a control-flow refactoring. It clarifies decisions, branches, loops, or algorithms without changing results, errors, or ordering semantics. For this refactoring, the practical target is: one loop performs multiple independent jobs over the same collection. Keep the transformation narrow enough that a failed test can be traced to this refactoring alone.

## Steps

1. Write or identify tests that cover every branch and boundary value before changing structure.
2. Make predicates side-effect free or prove evaluation order is safe.
3. Change one branch, loop, or algorithm structure at a time and preserve precedence.
4. Prefer intention-revealing names for predicates, variants, guards, or algorithms.
5. Check cleanup, finalization, async, lock, and transaction boundaries after restructuring.
6. Build, then run unit tests and integration tests (or the manual-test procedure if no automated coverage exists), before the next conditional refactoring.

## Potential Tests

- Unit tests for every branch, guard, loop case, and boundary value.
- Truth-table or parameterized tests for complex predicates.
- Golden or property-style tests for algorithm replacement when useful.
- Integration tests over realistic data and end-to-end paths that depend on ordering or side effects.
- Regression tests for any behavior that was easy to break while applying this refactoring.

## Completion Check

- Diff shows this refactoring only; no opportunistic rewrite is mixed in.
- Build succeeded for the affected target(s) after this refactoring.
- Unit tests passed after this refactoring.
- Integration tests passed after this refactoring.
- If no automated coverage exists for the changed behavior, the manual-test procedure was executed and recorded.
- Any compatibility shim, migration need, or skipped verification is documented in the final response.