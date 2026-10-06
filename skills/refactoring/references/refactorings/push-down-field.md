# Push Down Field

Source: https://refactoring.com/catalog/pushDownField.html
Aliases: None listed in this skill.

## Applicability

- Use when a superclass field is only meaningful for some subclasses.
- Inspect constructors, factories, subclasses, overrides, type checks, serialization, reflection, and public type references before editing.
- Best fit when the desired result is behavior-preserving and can be verified by existing or added tests.
- Avoid or stop when the change risks breaking substitutability, initialization order, serialized type identity, or callers typed against the hierarchy without explicit user approval.
- Load this file only after the skill selects this specific refactoring.

## Description

This is a hierarchy refactoring. It changes how variants share or delegate behavior while preserving substitutability and public type expectations. For this refactoring, the practical target is: a superclass field is only meaningful for some subclasses. Keep the transformation narrow enough that a failed test can be traced to this refactoring alone.

## Steps

1. Map the type hierarchy, constructors, factories, serialization, and public type references.
2. Confirm the target relationship is true substitutability or deliberate delegation, not just code reuse.
3. Move one field, method, constructor body, subclass, or delegate relationship at a time.
4. Preserve public type names or provide adapters when external consumers depend on them.
5. Check overridden hooks, initialization order, and polymorphic call sites.
6. Build, then run unit tests and integration tests (or the manual-test procedure if no automated coverage exists), before the next hierarchy change.

## Potential Tests

- Unit tests for every affected subclass or delegate variant.
- Unit tests for construction, initialization order, overrides, and unsupported operations.
- Serialization, reflection, or factory compatibility tests where type identity matters.
- Integration tests through polymorphic consumers and real factory paths.
- Regression tests for any behavior that was easy to break while applying this refactoring.

## Completion Check

- Diff shows this refactoring only; no opportunistic rewrite is mixed in.
- Build succeeded for the affected target(s) after this refactoring.
- Unit tests passed after this refactoring.
- Integration tests passed after this refactoring.
- If no automated coverage exists for the changed behavior, the manual-test procedure was executed and recorded.
- Any compatibility shim, migration need, or skipped verification is documented in the final response.