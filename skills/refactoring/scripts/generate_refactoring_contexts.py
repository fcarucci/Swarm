#!/usr/bin/env python3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "references" / "refactorings"

CATALOG = """
Change Function Declaration|change-function-declaration|changeFunctionDeclaration|api|Rename Function; Rename Method; Add Parameter; Remove Parameter; Change Signature|a function name, parameter list, or return contract no longer communicates its purpose or fits callers
Change Reference to Value|change-reference-to-value|changeReferenceToValue|data||an object is small, immutable in practice, and equality matters more than identity
Change Value to Reference|change-value-to-reference|changeValueToReference|data||multiple value objects represent the same conceptual entity and updates must be shared
Collapse Hierarchy|collapse-hierarchy|collapseHierarchy|inheritance||a superclass and subclass no longer have meaningful behavioral separation
Combine Functions into Class|combine-functions-into-class|combineFunctionsIntoClass|grouping||several functions operate on the same data and repeatedly pass the same context around
Combine Functions into Transform|combine-functions-into-transform|combineFunctionsIntoTransform|grouping||several computations enrich the same source data and callers need a consistent derived view
Consolidate Conditional Expression|consolidate-conditional-expression|consolidateConditionalExpression|conditional||multiple conditional branches lead to the same outcome and express one business rule
Decompose Conditional|decompose-conditional|decomposeConditional|conditional||condition, then branch, or else branch logic is dense enough to hide intent
Encapsulate Collection|encapsulate-collection|encapsulateCollection|data||external callers can mutate an object's collection directly and bypass invariants
Encapsulate Record|encapsulate-record|encapsulateRecord|data|Replace Record with Data Class|raw maps, structs, dictionaries, or records spread data access rules across callers
Encapsulate Variable|encapsulate-variable|encapsulateVariable|data|Encapsulate Field; Self-Encapsulate Field|a variable or field is accessed directly and needs validation, lazy behavior, or a stable API
Extract Class|extract-class|extractClass|grouping||one class has multiple responsibilities or subsets of fields and methods change for different reasons
Extract Function|extract-function|extractFunction|local|Extract Method|a code fragment has a clear purpose that is hidden inside a larger function
Extract Superclass|extract-superclass|extractSuperclass|inheritance||multiple classes share fields or behavior that represents a common abstraction
Extract Variable|extract-variable|extractVariable|local|Introduce Explaining Variable|an expression is correct but hard to understand without a name
Hide Delegate|hide-delegate|hideDelegate|api||callers know too much about a delegate object or navigation chain
Inline Class|inline-class|inlineClass|grouping||a class no longer carries enough responsibility to justify its own type
Inline Function|inline-function|inlineFunction|local|Inline Method|a function's body is clearer than its name or indirection hides simple behavior
Inline Variable|inline-variable|inlineVariable|local|Inline Temp|a temporary variable repeats an expression without adding useful meaning
Introduce Assertion|introduce-assertion|introduceAssertion|conditional||code assumes a condition that is not explicit at the point of use
Introduce Parameter Object|introduce-parameter-object|introduceParameterObject|api||the same group of parameters travels together through multiple functions
Introduce Special Case|introduce-special-case|introduceSpecialCase|conditional|Introduce Null Object|callers repeatedly check for a special value such as null, missing, unknown, or default
Move Field|move-field|moveField|grouping||a field is used more by another type than by its current owner
Move Function|move-function|moveFunction|grouping|Move Method|a function uses another module or type more heavily than its current home
Move Statements into Function|move-statements-into-function|moveStatementsIntoFunction|local||callers repeat setup or follow-up statements around the same function call
Move Statements to Callers|move-statements-to-callers|moveStatementsToCallers|local||a function includes statements that only some callers should own
Parameterize Function|parameterize-function|parameterizeFunction|api|Parameterize Method|several similar functions differ only by a literal, operation, or small policy value
Preserve Whole Object|preserve-whole-object|preserveWholeObject|api||callers extract several values from an object only to pass them together
Pull Up Constructor Body|pull-up-constructor-body|pullUpConstructorBody|inheritance||subclass constructors duplicate initialization that belongs to the superclass
Pull Up Field|pull-up-field|pullUpField|inheritance||sibling subclasses define the same field for the same concept
Pull Up Method|pull-up-method|pullUpMethod|inheritance||sibling subclasses implement equivalent behavior
Push Down Field|push-down-field|pushDownField|inheritance||a superclass field is only meaningful for some subclasses
Push Down Method|push-down-method|pushDownMethod|inheritance||a superclass method only applies to a subset of subclasses
Remove Dead Code|remove-dead-code|removeDeadCode|cleanup||code is no longer reachable, referenced, configured, or externally required
Remove Flag Argument|remove-flag-argument|removeFlagArgument|api|Replace Parameter with Explicit Methods|a boolean or enum argument selects distinct behavior that callers should express directly
Remove Middle Man|remove-middle-man|removeMiddleMan|api||a class mostly forwards calls to a delegate without adding useful abstraction
Remove Setting Method|remove-setting-method|removeSettingMethod|api||a field should be set only during construction or initialization
Remove Subclass|remove-subclass|removeSubclass|inheritance|Replace Subclass with Fields|a subclass differs only by data values or small configuration
Rename Field|rename-field|renameField|data||a field name misleads readers or no longer matches the domain concept
Rename Variable|rename-variable|renameVariable|local||a local variable or parameter name obscures purpose or causes mental translation
Replace Command with Function|replace-command-with-function|replaceCommandWithFunction|api||a command object only wraps a simple calculation and does not need stateful lifecycle
Replace Conditional with Polymorphism|replace-conditional-with-polymorphism|replaceConditionalWithPolymorphism|conditional||conditionals switch on type, mode, or variant and repeat across the codebase
Replace Constructor with Factory Function|replace-constructor-with-factory-function|replaceConstructorWithFactoryFunction|api|Replace Constructor with Factory Method|construction needs a clearer name, subtype selection, caching, validation, or compatibility wrapper
Replace Control Flag with Break|replace-control-flag-with-break|replaceControlFlagWithBreak|conditional|Remove Control Flag|loop flow is controlled by a mutable flag whose only job is to stop or skip work
Replace Derived Variable with Query|replace-derived-variable-with-query|replaceDerivedVariableWithQuery|data||a stored variable duplicates information that can be computed from source state
Replace Error Code with Exception|replace-error-code-with-exception|replaceErrorCodeWithException|api||callers must manually inspect error codes for exceptional failures
Replace Exception with Precheck|replace-exception-with-precheck|replaceExceptionWithPrecheck|api|Replace Exception with Test|normal control flow relies on catching an exception that can be avoided by checking first
Replace Function with Command|replace-function-with-command|replaceFunctionWithCommand|api|Replace Method with Method Object|a function needs intermediate state, staged execution, undo, logging, or collaborators that make it hard to read
Replace Inline Code with Function Call|replace-inline-code-with-function-call|replaceInlineCodeWithFunctionCall|local||inline code duplicates the behavior of an existing well-named function
Replace Loop with Pipeline|replace-loop-with-pipeline|replaceLoopWithPipeline|conditional||a loop performs collection filtering, mapping, grouping, or reduction that a pipeline would clarify
Replace Magic Literal|replace-magic-literal|replaceMagicLiteral|data|Replace Magic Number with Symbolic Constant|a literal value has domain meaning that is not obvious at the use site
Replace Nested Conditional with Guard Clauses|replace-nested-conditional-with-guard-clauses|replaceNestedConditionalWithGuardClauses|conditional||nested branching obscures the normal path and special cases can be handled early
Replace Parameter with Query|replace-parameter-with-query|replaceParameterWithQuery|api|Replace Parameter with Method|a callee can obtain a parameter from data it already has
Replace Primitive with Object|replace-primitive-with-object|replacePrimitiveWithObject|data|Replace Data Value with Object; Replace Type Code with Class|a primitive value carries domain rules, validation, formatting, or behavior
Replace Query with Parameter|replace-query-with-parameter|replaceQueryWithParameter|api||a function queries unstable or undesirable global or context state and should receive the value explicitly
Replace Subclass with Delegate|replace-subclass-with-delegate|replaceSubclassWithDelegate|inheritance||subclass variation is one dimension among several or inheritance is constraining composition
Replace Superclass with Delegate|replace-superclass-with-delegate|replaceSuperclassWithDelegate|inheritance|Replace Inheritance with Delegation|a class inherits behavior for reuse but is not truly substitutable as the superclass
Replace Temp with Query|replace-temp-with-query|replaceTempWithQuery|local||a temporary variable duplicates a calculation that is useful elsewhere in the same scope or class
Replace Type Code with Subclasses|replace-type-code-with-subclasses|replaceTypeCodeWithSubclasses|inheritance|Extract Subclass; Replace Type Code with State/Strategy|a type code drives behavior that belongs to distinct variants
Return Modified Value|return-modified-value|returnModifiedValue|api||a function mutates an argument or outer variable and the changed value should be explicit
Separate Query from Modifier|separate-query-from-modifier|separateQueryFromModifier|conditional||one function both returns information and changes state
Slide Statements|slide-statements|slideStatements|local|Consolidate Duplicate Conditional Fragments|related statements are separated by unrelated work or duplicate statements sit inside branches
Split Loop|split-loop|splitLoop|conditional||one loop performs multiple independent jobs over the same collection
Split Phase|split-phase|splitPhase|api||one routine mixes preparation or parsing with execution or calculation
Split Variable|split-variable|splitVariable|local|Remove Assignments to Parameters; Split Temp|one variable is assigned for multiple independent purposes
Substitute Algorithm|substitute-algorithm|substituteAlgorithm|conditional||a clearer algorithm can replace complicated logic with the same observable results
""".strip()

TEMPLATES = {
    "local": {
        "description": "This is a local code-shaping refactoring. It improves the internal expression of an existing function or small region without changing its externally visible result.",
        "inspect": "local variables, data flow, side effects, async or exception boundaries, locks, and comments",
        "risk": "moving code across side effects, changing evaluation order, or making names less precise",
        "steps": [
            "Confirm the enclosing behavior is covered by tests or add characterization coverage first.",
            "Select the exact expression, statement group, variable, or helper boundary to change.",
            "Check variable lifetimes, side effects, async or await points, exceptions, and lock boundaries.",
            "Apply the smallest source edit or IDE refactor that performs only this transformation.",
            "Inspect the diff for accidental formatting churn or behavior changes.",
            "Run unit tests and integration tests before choosing another refactoring.",
        ],
        "tests": [
            "Unit tests for the enclosing function's normal path and edge cases.",
            "Unit tests for branch, error, and side-effect-sensitive behavior touched by the edit.",
            "Characterization tests for unclear legacy behavior before changing structure.",
            "Integration tests through the original caller path, not only the extracted helper.",
        ],
    },
    "api": {
        "description": "This is an API-boundary refactoring. It changes how callers express an operation while preserving the same behavior and compatibility expectations.",
        "inspect": "declarations, exports, overloads, call sites, test doubles, docs, generated bindings, CLI flags, serialization names, and network contracts",
        "risk": "breaking callers, changing compatibility contracts, or hiding a behavior change inside a signature cleanup",
        "steps": [
            "Map every declaration, export, overload, caller, test double, and external entry point.",
            "Decide whether a compatibility wrapper or deprecation shim is needed before changing callers.",
            "Update one API boundary at a time and keep behavior equivalent for existing use cases.",
            "Update call sites mechanically, then inspect each semantic difference by hand.",
            "Check docs, generated bindings, serialization names, CLI flags, and network contracts when relevant.",
            "Run unit tests and integration tests before changing another API surface.",
        ],
        "tests": [
            "Unit tests for each old and new call shape that remains supported.",
            "Unit tests for invalid arguments, defaults, and boundary cases.",
            "Contract tests for public APIs, CLI output, serialization, or network payloads.",
            "Integration tests through real callers that cross package, process, UI, or persistence boundaries.",
        ],
    },
    "data": {
        "description": "This is a data-representation refactoring. It changes how data is named, accessed, or represented while preserving domain behavior and stored compatibility.",
        "inspect": "reads, writes, construction paths, equality, hashing, validation, serialization, persistence mappings, and schema migrations",
        "risk": "breaking persisted data, exposing mutable state, changing identity semantics, or merging distinct domain meanings",
        "steps": [
            "Find all reads, writes, construction paths, serialization paths, and persistence mappings.",
            "Identify the invariant or ownership rule the new representation should make clearer.",
            "Introduce the new representation behind a narrow seam before migrating all callers.",
            "Migrate callers in the smallest safe slice and preserve compatibility where external data is involved.",
            "Remove direct access only after tests cover the new access path.",
            "Run unit tests and integration tests before continuing.",
        ],
        "tests": [
            "Unit tests for validation, equality, mutation, and invalid data.",
            "Unit tests for read/write parity and default handling.",
            "Serialization, deserialization, migration, or database round-trip tests where relevant.",
            "Integration tests through consumers that load, save, display, or transmit the data.",
        ],
    },
    "grouping": {
        "description": "This is a responsibility-movement refactoring. It moves fields, functions, or behavior toward the owner that best matches the domain responsibility.",
        "inspect": "imports, call direction, field ownership, construction paths, visibility, dependency cycles, and module boundaries",
        "risk": "creating dependency cycles, exposing internals, over-splitting responsibilities, or moving behavior across ownership boundaries",
        "steps": [
            "Identify the responsibility boundary and prove the moved member belongs with the target owner.",
            "Find imports, call direction, construction paths, and dependency cycles before moving code.",
            "Move one field, function, or responsibility at a time.",
            "Leave forwarding compatibility only when callers or public API need a transition path.",
            "Inspect the diff for new cycles, visibility leaks, and bloated abstractions.",
            "Run unit tests and integration tests before the next move.",
        ],
        "tests": [
            "Unit tests for the moved behavior at its new home and through the old public path if retained.",
            "Unit tests for construction, ownership, and collaborator behavior.",
            "Compile or type tests that catch dependency and visibility problems.",
            "Integration tests through real consumers that exercise the moved responsibility.",
        ],
    },
    "conditional": {
        "description": "This is a control-flow refactoring. It clarifies decisions, branches, loops, or algorithms without changing results, errors, or ordering semantics.",
        "inspect": "branch precedence, predicate side effects, loop ordering, cleanup paths, finalization, transactions, and error behavior",
        "risk": "changing evaluation order, branch precedence, loop count, cleanup behavior, or edge-case semantics",
        "steps": [
            "Write or identify tests that cover every branch and boundary value before changing structure.",
            "Make predicates side-effect free or prove evaluation order is safe.",
            "Change one branch, loop, or algorithm structure at a time and preserve precedence.",
            "Prefer intention-revealing names for predicates, variants, guards, or algorithms.",
            "Check cleanup, finalization, async, lock, and transaction boundaries after restructuring.",
            "Run unit tests and integration tests before the next conditional refactoring.",
        ],
        "tests": [
            "Unit tests for every branch, guard, loop case, and boundary value.",
            "Truth-table or parameterized tests for complex predicates.",
            "Golden or property-style tests for algorithm replacement when useful.",
            "Integration tests over realistic data and end-to-end paths that depend on ordering or side effects.",
        ],
    },
    "inheritance": {
        "description": "This is a hierarchy refactoring. It changes how variants share or delegate behavior while preserving substitutability and public type expectations.",
        "inspect": "constructors, factories, subclasses, overrides, type checks, serialization, reflection, and public type references",
        "risk": "breaking substitutability, initialization order, serialized type identity, or callers typed against the hierarchy",
        "steps": [
            "Map the type hierarchy, constructors, factories, serialization, and public type references.",
            "Confirm the target relationship is true substitutability or deliberate delegation, not just code reuse.",
            "Move one field, method, constructor body, subclass, or delegate relationship at a time.",
            "Preserve public type names or provide adapters when external consumers depend on them.",
            "Check overridden hooks, initialization order, and polymorphic call sites.",
            "Run unit tests and integration tests before the next hierarchy change.",
        ],
        "tests": [
            "Unit tests for every affected subclass or delegate variant.",
            "Unit tests for construction, initialization order, overrides, and unsupported operations.",
            "Serialization, reflection, or factory compatibility tests where type identity matters.",
            "Integration tests through polymorphic consumers and real factory paths.",
        ],
    },
    "cleanup": {
        "description": "This is a deletion refactoring. It removes code only after proving the code is unsupported dead weight rather than a dynamic extension point.",
        "inspect": "references, dynamic loading, reflection, plugin hooks, configs, migrations, CLI routes, generated registries, and documentation examples",
        "risk": "removing extension points, generated hooks, migrations, or code invoked outside static search",
        "steps": [
            "Prove the code is unused with search, compile or type checks, and knowledge of dynamic entry points.",
            "Check generated registries, plugin hooks, reflection, config, migrations, and documentation examples.",
            "Delete the smallest dead slice and any tests that only assert dead behavior.",
            "Keep compatibility shims if external consumers may still call the path.",
            "Inspect the diff for accidental behavior removal.",
            "Run unit tests and integration tests before deleting another slice.",
        ],
        "tests": [
            "Compile, typecheck, or link tests that prove references are gone.",
            "Unit tests for adjacent behavior that should remain.",
            "Smoke tests for dynamic entry points, plugin loading, CLI routes, or config-driven behavior.",
            "Integration tests that cover the subsystem after deletion.",
        ],
    },
}


def parse_catalog():
    for line in CATALOG.splitlines():
        name, slug, url, category, aliases, use = line.split("|", 5)
        yield {
            "name": name,
            "slug": slug,
            "url": url,
            "category": category,
            "aliases": aliases.replace(";", ",") if aliases else "None listed in this skill.",
            "use": use,
        }


def render(item):
    t = TEMPLATES[item["category"]]
    steps = "\n".join(f"{i}. {step}" for i, step in enumerate(t["steps"], 1))
    tests = "\n".join(f"- {test}" for test in t["tests"])
    return f"""# {item['name']}

Source: https://refactoring.com/catalog/{item['url']}.html
Aliases: {item['aliases']}

## Applicability

- Use when {item['use']}.
- Inspect {t['inspect']} before editing.
- Best fit when the desired result is behavior-preserving and can be verified by existing or added tests.
- Avoid or stop when the change risks {t['risk']} without explicit user approval.
- Load this file only after the skill selects this specific refactoring.

## Description

{t['description']} For this refactoring, the practical target is: {item['use']}. Keep the transformation narrow enough that a failed test can be traced to this refactoring alone.

## Steps

{steps}

## Potential Tests

{tests}
- Regression tests for any behavior that was easy to break while applying this refactoring.

## Completion Check

- Diff shows this refactoring only; no opportunistic rewrite is mixed in.
- Unit tests passed after this refactoring.
- Integration tests passed after this refactoring.
- Any compatibility shim, migration need, or skipped verification is documented in the final response.
"""


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    items = list(parse_catalog())
    for item in items:
        (OUT / f"{item['slug']}.md").write_text(render(item), encoding="utf-8")
    print(f"Wrote {len(items)} refactoring context files to {OUT}")


if __name__ == "__main__":
    main()
