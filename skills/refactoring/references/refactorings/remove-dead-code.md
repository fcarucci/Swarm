# Remove Dead Code

Source: https://refactoring.com/catalog/removeDeadCode.html
Aliases: None listed in this skill.

## Applicability

- Use when code is no longer reachable, referenced, configured, or externally required.
- Inspect references, dynamic loading, reflection, plugin hooks, configs, migrations, CLI routes, generated registries, and documentation examples before editing.
- Best fit when the desired result is behavior-preserving and can be verified by existing or added tests.
- Avoid or stop when the change risks removing extension points, generated hooks, migrations, or code invoked outside static search without explicit user approval.
- Load this file only after the skill selects this specific refactoring.

## Description

This is a deletion refactoring. It removes code only after proving the code is unsupported dead weight rather than a dynamic extension point. For this refactoring, the practical target is: code is no longer reachable, referenced, configured, or externally required. Keep the transformation narrow enough that a failed test can be traced to this refactoring alone.

## Steps

1. Prove the code is unused with search, compile or type checks, and knowledge of dynamic entry points.
2. Check generated registries, plugin hooks, reflection, config, migrations, and documentation examples.
3. Delete the smallest dead slice and any tests that only assert dead behavior.
4. Keep compatibility shims if external consumers may still call the path.
5. Inspect the diff for accidental behavior removal.
6. Build, then run unit tests and integration tests (or the manual-test procedure if no automated coverage exists), before deleting another slice.

## Potential Tests

- Compile, typecheck, or link tests that prove references are gone.
- Unit tests for adjacent behavior that should remain.
- Smoke tests for dynamic entry points, plugin loading, CLI routes, or config-driven behavior.
- Integration tests that cover the subsystem after deletion.
- Regression tests for any behavior that was easy to break while applying this refactoring.

## Completion Check

- Diff shows this refactoring only; no opportunistic rewrite is mixed in.
- Build succeeded for the affected target(s) after this refactoring.
- Unit tests passed after this refactoring.
- Integration tests passed after this refactoring.
- If no automated coverage exists for the changed behavior, the manual-test procedure was executed and recorded.
- Any compatibility shim, migration need, or skipped verification is documented in the final response.