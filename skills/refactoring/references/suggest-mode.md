# Suggest Mode

Use this file when the user asks for refactoring **suggestions** rather than refactoring **edits**. In Suggest Mode you analyze code and return a ranked list of candidate refactorings drawn from the Fowler catalog. You do **not** edit, build, or run tests in this mode.

## When This Mode Applies

Trigger on any of: "suggest", "recommend", "propose", "what could we refactor", "what should I clean up", "analyze for refactorings", "find smells", "review for refactoring", "dry run", "don't edit yet", "just tell me what to fix", `--suggest`, "planning only". Also use when the user asks for refactoring on code they don't own, code they're still reviewing, or a changelist/PR/commit they have not approved yet.

If the user later says "go", "apply that", "do it", "yes", or picks an item from your list, switch to Apply Mode and follow the Required Workflow in [SKILL.md](../SKILL.md).

## Why Suggest Mode Exists

Editing without consent is the single most common way this skill creates rework. Suggest Mode lets the user see the menu and approve a slice before any code changes. It also matches how Fowler describes catalog use: smells are spotted first, then a named refactoring is chosen, then the transformation runs. Suggest Mode covers the first two steps explicitly.

## Procedure

1. **Confirm scope.** Same scope discovery as Apply Mode (commit, single file, multi-file, branch), but stop short of editing. Read [scope-selection.md](scope-selection.md) for commits and branches.
2. **Read, do not change.** Use Read, Grep, Glob, and (for commits/branches) the appropriate diff tool — `git show`, `git diff <merge-base>...HEAD`, or for Perforce shelves/changelists use `p4_get_changelist` / `p4_get_shelf` / `p4_diff_rev` / `p4_diff_paths`. Never invoke Edit or Write tools while in Suggest Mode.
3. **Identify smells.** Scan for the smells below and locate each one with `file:line` references.
4. **Map smells to catalog refactorings.** Use [catalog-index.md](catalog-index.md) — each entry has a one-line trigger. Pick the smallest refactoring that addresses the smell. When two refactorings could apply, list both and explain the tradeoff in one sentence.
5. **Rank.** Order by `value / risk`. High value with low risk goes first. See *Ranking* below.
6. **Produce the suggestion list.** Use the output format below — no edits, no test runs, no build.
7. **Hand off.** End by asking the user which suggestion(s) to apply. Do not bundle "and I'll go ahead and apply #1" — wait for explicit selection.

## Smells to Look For

Drawn from the Fowler smell vocabulary. Each smell is a trigger; the catalog entry is the response.

- **Long function / mysterious name** → Extract Function, Rename Variable, Extract Variable, Slide Statements, Decompose Conditional.
- **Duplicated code** (same logic in two+ places) → Extract Function, Pull Up Method, Replace Inline Code with Function Call, Substitute Algorithm.
- **Long parameter list** → Introduce Parameter Object, Preserve Whole Object, Replace Parameter with Query, Remove Flag Argument.
- **Global / mutable shared data** → Encapsulate Variable, Encapsulate Record, Encapsulate Collection, Change Reference to Value.
- **Divergent change** (one class changes for many reasons) → Extract Class, Split Phase, Move Function, Move Field.
- **Shotgun surgery** (one change touches many classes) → Combine Functions into Class, Combine Functions into Transform, Move Function, Move Field, Inline Class.
- **Feature envy** (function uses another module's data more than its own) → Move Function, Move Field, Extract Function.
- **Data clumps** (same group of values traveling together) → Introduce Parameter Object, Extract Class, Preserve Whole Object.
- **Primitive obsession** → Replace Primitive with Object, Replace Type Code with Subclasses, Replace Magic Literal.
- **Repeated switches / type codes** → Replace Conditional with Polymorphism, Replace Type Code with Subclasses.
- **Loops doing too much** → Split Loop, Replace Loop with Pipeline, Replace Control Flag with Break.
- **Lazy element / speculative generality / dead code** → Inline Class, Inline Function, Collapse Hierarchy, Remove Dead Code, Remove Subclass.
- **Temporary field** → Extract Class, Introduce Special Case, Replace Derived Variable with Query.
- **Message chains** (`a.getB().getC().do()`) → Hide Delegate, Move Function.
- **Middle man** (class only forwards) → Remove Middle Man, Inline Class.
- **Insider trading** (modules mutually rummage in each other's internals) → Move Function, Move Field, Hide Delegate, Extract Class, Replace Subclass with Delegate.
- **Large class** → Extract Class, Extract Superclass, Replace Type Code with Subclasses.
- **Alternative classes with different interfaces** → Change Function Declaration, Move Function, Extract Superclass.
- **Data class** (only getters/setters, no behavior) → Move Function, Encapsulate Record, Combine Functions into Class.
- **Refused bequest** (subclass uses little of parent) → Push Down Method, Push Down Field, Replace Subclass with Delegate, Replace Superclass with Delegate.
- **Comments explaining tricky code** → Extract Function (let the name replace the comment), Introduce Assertion, Rename Variable.
- **Errors signaled by special return values** → Replace Error Code with Exception, Replace Exception with Precheck.
- **Mutating-and-returning functions** → Separate Query from Modifier, Return Modified Value.

## Ranking

For each candidate, score three dimensions on a 1–3 scale, then sort by `value / risk`:

- **Value** — how much readability, safety, or future-change leverage this refactoring buys. `3` = unblocks a follow-on refactoring or removes recurring bug surface; `1` = cosmetic.
- **Risk** — blast radius and behavior-preservation difficulty. `3` = touches public API, persistence, protocol, generated code, or large unfamiliar areas; `1` = local to one function with good test coverage.
- **Cost** — rough edit size. `3` = many files / cross-cutting; `1` = one function. Use cost as a tiebreaker, not a primary sort key.

Put high-value/low-risk items first. Call out any item with risk `3` explicitly so the user knows it needs more care before approving.

## Output Format

Always use this structure. It lets the user pick items by number.

```
# Refactoring Suggestions — <scope, e.g. "CL 38020231" or "Mcp/Handlers/ProjectRepositoryHandlers.cpp">

## Summary
- N suggestions across <M> files. <one-sentence overall theme, e.g. "mostly long handler functions and duplicated dispatch boilerplate".>

## Suggestions

### 1. <Catalog Refactoring Name> — <one-line description>
- Location: `path/to/file.ext:LINE` (or `LINE_START-LINE_END`)
- Smell: <name from the smell list above>
- Why now: <one sentence on the leverage — e.g. "blocks #3 below" or "the same pattern repeats in 4 handlers">
- Suggested step: <one or two sentences on the smallest useful edit>
- Value/Risk/Cost: V<1-3> / R<1-3> / C<1-3>
- Verification after applying: <which existing tests cover it, or "characterization test needed first">

### 2. ...
```

Follow with:

```
## Recommended Sequence
1. Suggestion #X (lowest risk, unblocks the rest)
2. Suggestion #Y
...

## Items With Elevated Risk
- Suggestion #Z — <why>

## Next Step
Which of these would you like to apply? I'll switch to Apply Mode and run the build + test gate after each one.
```

## What Suggest Mode Does Not Do

- No `Edit` or `Write` calls. If you find yourself about to call one, you are in the wrong mode.
- No baseline build or baseline test runs. Suggestion is read-only.
- No speculative scope expansion. Stay inside the scope the user asked about. Items outside that scope go in a short "Out of Scope, FYI" section at the end if they matter, with no recommended action.
- No bundling. Each suggestion is one named catalog refactoring. If two refactorings are needed together, list them as #1 and #2 with a note that #2 depends on #1.

## Stop Conditions for Suggest Mode

Stop and report instead of producing a list when:

- The requested scope cannot be located (missing file, unknown commit/changelist, branch not present).
- The code is generated, vendored, or otherwise off-limits for refactoring — say so plainly.
- The user's request is actually a redesign (behavior-changing) rather than a refactoring — say so and ask whether they want a design discussion instead.
