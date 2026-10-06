# Replace Derived Variable with Query

Source: https://refactoring.com/catalog/replaceDerivedVariableWithQuery.html
Aliases: None listed in this skill.

## Applicability

- Use when a stored variable duplicates information that can be computed from source state.
- Inspect reads, writes, construction paths, equality, hashing, validation, serialization, persistence mappings, and schema migrations before editing.
- Best fit when the desired result is behavior-preserving and can be verified by existing or added tests.
- Avoid or stop when the change risks breaking persisted data, exposing mutable state, changing identity semantics, or merging distinct domain meanings without explicit user approval.
- Load this file only after the skill selects this specific refactoring.

## Description

This is a data-representation refactoring. It changes how data is named, accessed, or represented while preserving domain behavior and stored compatibility. For this refactoring, the practical target is: a stored variable duplicates information that can be computed from source state. Keep the transformation narrow enough that a failed test can be traced to this refactoring alone.

## Steps

1. Find all reads, writes, construction paths, serialization paths, and persistence mappings.
2. Identify the invariant or ownership rule the new representation should make clearer.
3. Introduce the new representation behind a narrow seam before migrating all callers.
4. Migrate callers in the smallest safe slice and preserve compatibility where external data is involved.
5. Remove direct access only after tests cover the new access path.
6. Build, then run unit tests and integration tests (or the manual-test procedure if no automated coverage exists), before continuing.

## Potential Tests

- Unit tests for validation, equality, mutation, and invalid data.
- Unit tests for read/write parity and default handling.
- Serialization, deserialization, migration, or database round-trip tests where relevant.
- Integration tests through consumers that load, save, display, or transmit the data.
- Regression tests for any behavior that was easy to break while applying this refactoring.

## Completion Check

- Diff shows this refactoring only; no opportunistic rewrite is mixed in.
- Build succeeded for the affected target(s) after this refactoring.
- Unit tests passed after this refactoring.
- Integration tests passed after this refactoring.
- If no automated coverage exists for the changed behavior, the manual-test procedure was executed and recorded.
- Any compatibility shim, migration need, or skipped verification is documented in the final response.