# Introduce Parameter Object

Source: https://refactoring.com/catalog/introduceParameterObject.html
Aliases: None listed in this skill.

## Applicability

- Use when the same group of parameters travels together through multiple functions.
- Inspect declarations, exports, overloads, call sites, test doubles, docs, generated bindings, CLI flags, serialization names, and network contracts before editing.
- Best fit when the desired result is behavior-preserving and can be verified by existing or added tests.
- Avoid or stop when the change risks breaking callers, changing compatibility contracts, or hiding a behavior change inside a signature cleanup without explicit user approval.
- Load this file only after the skill selects this specific refactoring.

## Description

This is an API-boundary refactoring. It changes how callers express an operation while preserving the same behavior and compatibility expectations. For this refactoring, the practical target is: the same group of parameters travels together through multiple functions. Keep the transformation narrow enough that a failed test can be traced to this refactoring alone.

## Steps

1. Map every declaration, export, overload, caller, test double, and external entry point.
2. Decide whether a compatibility wrapper or deprecation shim is needed before changing callers.
3. Update one API boundary at a time and keep behavior equivalent for existing use cases.
4. Update call sites mechanically, then inspect each semantic difference by hand.
5. Check docs, generated bindings, serialization names, CLI flags, and network contracts when relevant.
6. Build, then run unit tests and integration tests (or the manual-test procedure if no automated coverage exists), before changing another API surface.

## Potential Tests

- Unit tests for each old and new call shape that remains supported.
- Unit tests for invalid arguments, defaults, and boundary cases.
- Contract tests for public APIs, CLI output, serialization, or network payloads.
- Integration tests through real callers that cross package, process, UI, or persistence boundaries.
- Regression tests for any behavior that was easy to break while applying this refactoring.

## Completion Check

- Diff shows this refactoring only; no opportunistic rewrite is mixed in.
- Build succeeded for the affected target(s) after this refactoring.
- Unit tests passed after this refactoring.
- Integration tests passed after this refactoring.
- If no automated coverage exists for the changed behavior, the manual-test procedure was executed and recorded.
- Any compatibility shim, migration need, or skipped verification is documented in the final response.