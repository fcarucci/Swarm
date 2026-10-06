# Verification Gates

Use this file before the first edit and **after every named refactoring**. A "gate" here means: a concrete command (or documented manual procedure) whose green result lets you proceed to the next refactoring. Without a green gate, you do not move on.

## Discover Commands

Look for repo-specific commands in this order:

1. Agent instructions: `AGENTS.md`, `CLAUDE.md`, `README`, `CONTRIBUTING`, docs.
2. CI definitions: GitHub Actions, GitLab CI, Azure Pipelines, Jenkins, Buildkite.
3. Language and toolchain manifests:
   - **C/C++**: `CMakeLists.txt`, `build.bat`/`build.sh`, `Makefile`, ninja/MSBuild target. Note `cmake --build` invocations and per-target test executables.
   - **JavaScript/TypeScript**: `package.json` scripts such as `build`, `test`, `test:unit`, `test:integration`, `check`.
   - **Rust**: `cargo build`, `cargo test`, crate-specific `cargo test -p <crate>`, integration tests under `tests/`.
   - **Python**: `pytest`, `tox`, `nox`, `uv run pytest`; if a build artifact is produced, `python -m build` or the project's packaging script.
   - **.NET**: `dotnet build`, `dotnet test`.
   - **Java/Kotlin**: `mvn compile test`, `gradle build test`, `gradle integrationTest`.
   - **Go**: `go build ./...`, `go test ./...`.
4. Existing developer scripts.

You should end up with three named commands (or documented procedures):

- **Build command** — produces the artifact(s) affected by the refactoring.
- **Unit-test command** — fast, scoped tests.
- **Integration-test command** — broader cross-module or end-to-end tests.

If automated coverage does not exist for the area you are about to refactor, define a **manual-test procedure** instead: a numbered, reproducible set of steps (input fixture, command line, sample data, UI walkthrough) and the expected observable result. Confirm the procedure with the user before treating it as a gate.

## Baseline

Before editing, run, in this order:

1. **Build command.** If the project does not build before you start, you cannot tell whether your refactoring broke it.
2. Unit-test command.
3. Integration-test command.
4. Manual-test procedure (only if it is the chosen substitute for missing automated coverage).

If any baseline step fails, decide whether the failure blocks the requested refactoring:

- Same area as the intended refactor: fix or add characterization tests / manual procedure first.
- Unrelated existing failure: report it and ask only if it prevents meaningful work.
- Missing command: search once more, then report the exact files and locations checked.

## After Each Refactoring

For every named refactoring, run the full gate in this order — **no shortcuts, no batching across refactorings unless the user explicitly approved batching**:

1. **Build** the affected target(s). A green build proves the edit still compiles, links, and produces deployable artifacts. For C/C++ projects this is especially important: tests can't run if the binary didn't link.
2. Run **targeted unit tests** first if available (faster failure signal).
3. Run the full **unit-test command**.
4. Run the **integration-test command**.
5. If you relied on a **manual-test procedure** (because the changed behavior has no automated coverage), execute it now and record each observed step.
6. Capture exact command names, arguments, and outcome for the final answer.

Do not start the next refactoring while any gate is red or unreported.

## Test Selection

Add or strengthen tests when current coverage cannot catch behavior drift:

- Characterization tests for legacy behavior.
- Boundary tests for public API signatures, serialization, database rows, config, CLI output, and network contracts.
- Integration tests for moved code, dependency direction, persistence, cross-process behavior, or UI-to-backend paths.
- Manual-test procedures only as a documented fallback when an automated test would take longer to write than the refactoring itself — and only with user confirmation. Treat the procedure as a real gate: write it down, run it, record the result.

## Failure Handling

1. Read the failure; do not guess.
2. Confirm whether it was introduced by the last refactoring.
3. If yes, fix the refactoring or reverse only your last change. Never broaden the diff to chase a failure into other modules.
4. If no, preserve the evidence (compiler output, failing test name, manual-procedure step that diverged) and report it.
5. Never silence a failing gate by disabling tests, weakening assertions, or marking tests as "expected to fail" without explicit user approval.
