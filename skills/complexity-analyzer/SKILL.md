---
name: complexity-analyzer
description: Analyze cognitive complexity (Rust via Clippy, JS/TS via ESLint + SonarJS), rank complex functions, and suggest refactorings; also computes deterministic metrics and four overall scores (complexity, coupling, over-engineering, maintainability) for Rust, Python and JS/TS repos. Use when asked to analyze complexity, coupling, over-engineering or overall code health, or to find functions to refactor.
---

# Complexity Analyzer

Analyze code complexity, rank ALL complex functions, and provide detailed
refactoring suggestions. Supports **Rust** (Clippy) and **JavaScript /
TypeScript** (ESLint + SonarJS).

Resolve `SKILL_DIR` to the directory containing this SKILL.md in the installed plugin
(`skills/complexity-analyzer`), for either Claude Code or Codex. All commands below
use this skill-relative location; run them from the target repository.

## When to Use

Invoke this skill when the user asks to:
- Analyze code complexity
- Find complex functions
- Check cognitive complexity
- Review code complexity
- Get complexity metrics
- Find functions to refactor

## What This Skill Does

1. Detects the language and runs the matching cognitive-complexity analyzer
2. Extracts and ranks ALL functions exceeding the threshold
3. Shows a complete ranking table of ALL flagged functions
4. Provides MULTIPLE refactoring strategies for each function analyzed

## Instructions

### Step 0: Detect the Language

| Signal | Language | Analyzer | Threshold |
|---|---|---|---|
| `Cargo.toml` present | Rust | `cargo clippy` | 25 |
| `.js` / `.mjs` / `.cjs` / `.ts` files | JavaScript | `js/analyze.sh` | 15 cognitive |

If a repo has both, ask which to analyze, or run both and report separately.
Never report Rust and JavaScript scores in the same ranking table — the two
tools' scales are not comparable (see "Comparing Scales" below).

### Step 1a: Run Analysis — Rust

```bash
cargo clippy --all-targets --all-features -- -W clippy::cognitive_complexity 2>&1 | \
  grep -E "warning: the function|-->" | \
  paste - - | \
  sort -t'(' -k2 -rn
```

### Step 1b: Run Analysis — JavaScript / TypeScript

```bash
"$SKILL_DIR"/js/analyze.sh <file-or-dir> [more...]
```

Output is TSV, highest cognitive complexity first:

```
cognitive<TAB>cyclomatic<TAB>file<TAB>line<TAB>function
```

What the driver does, and why:

- **Two metrics.** `sonarjs/cognitive-complexity` (threshold 15) is the direct
  analogue of `clippy::cognitive_complexity` — it weights *nesting*, so deeply
  indented code scores far higher than a flat chain of branches. The built-in
  ESLint `complexity` rule (threshold 10) reports **cyclomatic** complexity as
  a second signal: a raw count of independent paths, ignoring nesting.
  A function high in cyclomatic but low in cognitive is usually a flat
  dispatch table and is often fine. A function high in **both** is the real
  refactoring target.
- **A cognitive score of `0`** in the output means the function tripped only
  the cyclomatic rule; it is listed below all genuine cognitive findings.
- **Preprocessor directives are stripped** (`js/strip-directives.py`) before
  parsing. PixInsight PJSR scripts carry `#include` / `#define` / `#feature-id`
  lines that are not JavaScript and would otherwise fail the parse. Directives
  are blanked rather than deleted, so **reported line numbers still match the
  original files**.
- **Function names are recovered from the source.** SonarJS's message text
  contains no function name, so the driver reads the reported line and
  extracts the identifier.
- **The toolchain installs once into the persistent per-user Swarm cache**,
  `${SWARM_DATA_DIR:-~/.local/share/swarm}/tools/complexity-analyzer/node`
  (Windows: `%LOCALAPPDATA%\swarm\tools\complexity-analyzer\node`). Do NOT switch this to `npx --package`:
  packages in npx's ephemeral prefix are not importable from
  `eslint.config.mjs` and the run dies with `ERR_MODULE_NOT_FOUND`.
- **ESLint resolves its config from the current directory**, not from the lint
  target, which is why the driver `cd`s into its scratch tree before running.

If the command prints `eslint produced no report`, it will dump ESLint's own
stderr — read it rather than guessing.

### Step 2: Generate Response

Provide response in this format:

```
## Complexity Analysis Results

**Language**: [Rust | JavaScript]
**Found**: [N] functions exceeding threshold ([25 Rust | 15 JS])
**Highest**: [score]
**Average**: [avg]

### All Complex Functions (Ranked)

| Rank | Function | File | Line | Cognitive | Cyclomatic | Severity |
|------|----------|------|------|-----------|------------|----------|
| 1 | `function_name()` | path/file.js | 123 | 40 | 22 | 🔴 Critical |
| 2 | `another_func()` | path/file2.js | 456 | 35 | 19 | 🟠 High |
| ... | ... | ... | ... | ... | ... | ... |
```

Omit the Cyclomatic column for Rust (Clippy reports only the one metric).

**Severity, Rust** (threshold 25): 🔴 Critical (40+), 🟠 High (31-40), 🟡 Warning (26-30)

**Severity, JavaScript** (threshold 15): 🔴 Critical (40+), 🟠 High (25-39), 🟡 Warning (16-24)

```
---

### Detailed Analysis

[For each function in the top 5-7 (or all if < 10 total)]

#### 1. `function_name()` - Cognitive: 40/15 (167% over)
**Location**: [file.js:123](file.js#L123)

**Structure Analysis**:
[Read the actual code and describe structure]
- Nesting depth: X levels
- Conditional branches: Y
- Main complexity drivers: [list]

**Refactoring Strategies**:

##### Strategy 1: Extract Helper Functions
[Specific description with code example]

##### Strategy 2: Use Early Returns / Guard Clauses
[Specific description with code example]

##### Strategy 3: [Additional strategy if applicable]
[Specific description with code example]

**Expected Impact**: 40 → ~15 cognitive
**Priority**: High (function changes frequently / causes bugs)
```

### Step 3: Key Principles

- **Always read the actual function code** from the file before suggesting refactoring
- **Be specific**: Identify exact code patterns causing complexity
- **Multiple strategies**: Provide 2-3 different refactoring approaches per function
- **Show code**: Include concrete examples for each strategy
- **Estimate impact**: Give realistic complexity reduction estimate
- **Show ALL functions**: Include complete ranking table, not just top 3
- **Re-measure after refactoring.** Extracting a helper that carries the same
  branching merely *relocates* complexity. The only proof a refactor worked is
  a second run of the analyzer showing the total came down, not just the one
  function.
- **Verify the tool discriminates** before trusting a clean result. If a run
  reports zero findings, check it against a known-complex revision
  (e.g. `git show <old-rev>:path`) — zero findings should mean clean code, not
  a misconfigured analyzer.

## Deterministic metrics and overall scores

Use this when the user wants an overall picture ("how complex is this code", coupling, over-engineering,
before/after comparisons) rather than a ranked refactoring list. Same input gives byte-identical output.

Setup once (Linux, Windows, macOS; needs Python >= 3.11 and `uv`; Node >= 20.19 for JS/TS analysis and duplication):

```bash
python3 "$SKILL_DIR"/bin/install_tools.py
# rust-code-analysis (sha256 checked) + node tools into the persistent cache tools/complexity-analyzer/node (npm ci from a committed lockfile).
# Python tools (radon, vulture, tree-sitter) are pinned in tool-manifest.json and run through `uv run --with`
# (or `install_tools.py --venv` for a persistent cache venv). Nothing is installed globally; nothing uses /tmp.
```

Downloaded binaries, npm packages and the optional venv live outside the plugin cache,
so plugin upgrades preserve them. The explicit installer above verifies pinned SHA256
hashes for downloaded assets and binaries, and npm uses the committed lockfile.
`install_tools.py --check` checks readiness without downloading anything.

Per-language tools (versions pinned in `tool-manifest.json`; `report.md` lists the tool used for every metric):

| | Rust | Python | JS / TS |
|---|---|---|---|
| per-function complexity | rust-code-analysis | radon (cyclomatic, MI, Halstead, raw) + rust-code-analysis (cognitive, cross-check) | ESLint + eslint-plugin-sonarjs (cognitive), ESLint `complexity` (cyclomatic), @typescript-eslint/parser; rust-code-analysis cross-check |
| module graph | tree-sitter + Cargo.toml | `ast` imports | dependency-cruiser (tsconfig paths, re-exports, type-only imports) |
| unused code (OEI) | name references | vulture | knip (unused exports, types, files) |
| duplication | jscpd | jscpd | jscpd |

Without node the JS/TS part falls back to rust-code-analysis functions and a regex import graph; the report then says
**DEGRADED OUTPUT** in its header (`--no-node` forces this). knip needs the repository's `package.json` (and
installed dependencies when its config imports them, and it executes the repository's own config files, so use `--no-node` on untrusted repositories); when it cannot run, the regex export heuristic is used and the
header says so.

Run (Windows: `bin\metrics.cmd`, or `uv run --no-project --with tree-sitter==0.26.0 --with tree-sitter-rust==0.24.2 --with radon==6.0.1 --with vulture==2.16 python bin/metrics.py ...`):

```bash
"$SKILL_DIR"/bin/metrics <repo> [--lang rust,python,js] [--include-tests] [--out DIR]
# writes DIR/metrics.json (raw per-function/module/crate data) and DIR/report.md
# --no-node: skip node tools (degraded JS/TS); bin/compare_runs.py OLD NEW compares two metrics.json per language
"$SKILL_DIR"/bin/selftest                 # determinism + fixture + formula tests
```

The four scores (0-100, higher is better, letter A-E; production code only, tests with `--include-tests`
are scored separately):

| Score | Measures | Built from |
|---|---|---|
| CXI complexity | how hard functions are to read | code-line-weighted cognitive-complexity risk profile over functions and closures (each its own unit, per-language benchmark limits) + count-based tail |
| COI coupling | dependency tangle | cycles (Tarjan SCC), fan-out, Henry-Kafura hubs, Martin distance D |
| OEI over-engineering (heuristic) | abstraction not paid for by use | single-impl traits, pass-through fns, generics, unreferenced pub, markers, single-use abstractions |
| MAI maintainability | SIG-style ratings, interpolated 0.5-5.5 | unit size, complexity, interfacing, duplication, module coupling |

How to read them: report all four together with the grade, the one-line reading and the top hotspots from
`report.md`; a good CXI with a poor COI/OEI usually means complexity was moved, not removed. Scores are proxies:
OEI is unvalidated, Rust module edges are name-based, macros/async/unsafe are invisible, and duplication needs
node (the report says when it was skipped and MAI is then partial). Thresholds live in `thresholds.json`
(versioned); never retune them to make a repo look better. For a ranked refactoring list, still use the Clippy /
ESLint workflow above and the refactoring suggestions. Formulas and thresholds are implemented in
`lib/cxmetrics/scores.py` and `thresholds.json`.

## Comparing Scales

Clippy's cognitive complexity and SonarJS's cognitive complexity are
*different implementations of the same idea*, not the same number. Clippy's
default threshold is 25; Sonar's is 15. Do not carry a score from one language
to the other, and do not rank Rust and JavaScript functions in one table.
Clippy's lint (restriction group) adds no nesting increment, skips closure and
async-block bodies and subtracts `return`s, so on nested code it reads 2-3x lower
than rust-code-analysis (Sonar rules, used by `bin/metrics`): `validate` in Astro
Loom is 18 in Clippy and 45 in rca.

## Common Refactoring Patterns

Patterns marked *(Rust)* or *(JS)* are language-specific; the rest apply to both.

### Extract Helper Functions
When: Deep nesting, repeated logic blocks, long functions
Pattern: Pull nested logic into separate functions with early returns
Example: Extract 3-5 line blocks into descriptively named functions

### Guard Clauses / Early Returns
When: Multiple nested precondition checks, arrow-shaped code
Pattern: Invert conditions and return early to reduce nesting
Example: Replace `if valid { ... }` with `if !valid { return; }`

### Replace Match with Methods *(Rust)*
When: Large match statements, repeated match patterns
Pattern: Move match arms to enum methods (polymorphism)
Example: `enum.handle()` instead of `match enum { A => ..., B => ... }`

### Replace Switch with a Lookup Table *(JS)*
When: Long `switch` or `else if` chains mapping a key to behaviour
Pattern: Object literal (or `Map`) from key to handler function
Example: `const HANDLERS = { a: handleA, b: handleB }; HANDLERS[key](arg)`
This collapses cyclomatic complexity to 1 and cognitive nearly to 0.

### Extract Variable / Decompose Expression
When: Complex boolean conditions, long expressions
Pattern: Break complex expressions into well-named intermediate variables
Example: `let is_valid = x > 0 && y < 10;` instead of inline condition

### Replace Conditional with Polymorphism
When: Type-based conditionals, feature flags
Pattern: Rust — trait objects or enum dispatch. JS — a method on each object,
or a strategy object, instead of branching on a `type` field.

### Replace Nested Conditional with Guard Clauses
When: Multiple levels of if-else nesting
Pattern: Handle error/edge cases first, then main logic
Example: Fail fast at function start, reduce indentation levels

### Decompose Conditional
When: Complex multi-part conditions in if statements
Pattern: Extract condition logic into well-named boolean functions
Example: `if is_eligible_for_discount()` instead of `if age > 65 && member && ...`

### Split Phase
When: Function does multiple distinct operations
Pattern: Separate data gathering from processing/formatting
Example: Split parsing from validation, computation from rendering

### Extract a Builder / Config Object
When: Complex object construction with many options, or a constructor that
wires up a large amount of state (a very common cause of high scores in UI code)
Pattern: Rust — builder with chainable methods. JS — split the constructor into
`buildX()` methods that each return a finished part, and assign them; make sure
each extracted method takes what it needs as **parameters**, since a method
cannot see the constructor's local variables.

### State Machine Pattern
When: Complex control flow with state transitions
Pattern: Enum/const-keyed state machine with clear transition methods
Example: Explicit states and transitions instead of scattered flags

### Replace Loop with Iterator Methods
When: Manual loops with complex logic
Pattern: Rust — `iter().filter().map().collect()`. JS — `filter`/`map`/`reduce`.
Caution in JS: hot loops and environments without modern array methods
(e.g. older embedded engines) may need the explicit loop.

### Replace Temp with Query
When: Temporary variables calculated from other data
Pattern: Extract into a function that computes on demand

### Introduce Parameter Object
When: Functions with many related parameters
Pattern: Group related parameters into a struct / plain object

### Replace Magic Numbers with Named Constants
When: Numeric literals with unclear meaning
Pattern: Define named constants
Example: `const MAX_RETRIES = 3;` instead of hardcoded `3`

### Consolidate Duplicate Conditional Fragments
When: Same code in all branches of conditional
Pattern: Move common code outside the conditional

## Edge Cases

**No functions exceed threshold**:
```
✅ Excellent! No functions exceed the cognitive complexity threshold.
Your codebase follows good complexity practices.
```
Before reporting this, confirm the analyzer actually runs on this code — see
"Verify the tool discriminates" above.

**Clippy fails**: Check it's a Rust project, Clippy is installed, try building first.

**ESLint fails to parse a file**: Usually a non-standard dialect (PJSR
directives, JSX, TypeScript syntax in a `.js` file). Directives are handled;
for JSX/TS add the matching parser to `js/eslint.config.mjs`.

**Parser/Matcher/dispatch functions**: Note when high complexity is inherent to
the operation. A tokenizer or a 40-case dispatch is not automatically a defect.

## Files

- `bin/metrics`, `bin/metrics.py`, `bin/install_tools.py`, `bin/selftest` — deterministic metrics (see above)
- `lib/cxmetrics/`, `thresholds.json`, `tool-manifest.json`, `tests/` — implementation, versioned thresholds, pinned tools, fixture

- `js/analyze.sh` — JavaScript driver (install, strip, lint, rank)
- `js/eslint.config.mjs` — rules and thresholds
- `js/strip-directives.py` — blanks `#` preprocessor lines, preserving line numbers
