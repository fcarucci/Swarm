# Refactoring Catalog Index

Based on the official refactoring.com catalog for Martin Fowler's *Refactoring*, 2nd Edition.

Each entry below is a one-line trigger: when does the refactoring apply? After selecting one, **load only that refactoring's context file** for the full description, mechanics, examples, and applicability notes.

Sources:
- https://martinfowler.com/books/refactoring.html
- https://refactoring.com/catalog/

How to use this index:

1. Skim the trigger lines below until you find a match for the smell you observed.
2. Open the linked context file under `references/refactorings/`.
3. Apply only that one refactoring in the smallest useful step.
4. Build, then run unit and integration tests, before moving on.

## Basic and Naming

- [Change Function Declaration](refactorings/change-function-declaration.md) — Function name, parameters, or return shape no longer match what the function actually does or how callers want to use it.
- [Extract Function](refactorings/extract-function.md) — A fragment inside a larger function has its own purpose and deserves a name; pull it into a new function.
- [Extract Variable](refactorings/extract-variable.md) — A subexpression inside a larger expression is hard to read or appears more than once; name it with a local variable.
- [Inline Function](refactorings/inline-function.md) — The function body is as clear as its name and the indirection no longer pays off; replace calls with the body.
- [Inline Variable](refactorings/inline-variable.md) — A variable name adds no information beyond the expression it holds; replace the variable with the expression.
- [Rename Field](refactorings/rename-field.md) — A record/object field's name no longer reflects its meaning; rename it (and its accessors) across all callers.
- [Rename Variable](refactorings/rename-variable.md) — A variable's name is misleading, too generic, or out of date; rename to reflect its actual role.
- [Slide Statements](refactorings/slide-statements.md) — Related statements are scattered through a function; move them adjacent to each other to clarify intent and enable later extraction.
- [Split Variable](refactorings/split-variable.md) — One variable is reassigned for two distinct purposes; introduce separate variables, each used for a single purpose.

## Encapsulation and Data

- [Change Reference to Value](refactorings/change-reference-to-value.md) — A shared mutable object is being threaded through callers when an immutable value would be simpler and safer.
- [Change Value to Reference](refactorings/change-value-to-reference.md) — Multiple copies of the "same" value need to stay in sync; replace duplicate values with a single shared reference.
- [Encapsulate Collection](refactorings/encapsulate-collection.md) — A class exposes a raw collection (list, set, map) and callers mutate it directly; replace direct access with add/remove methods and return read-only views.
- [Encapsulate Record](refactorings/encapsulate-record.md) — Bare records/structs leak field access throughout the codebase; wrap them in a class so behavior can grow around the data.
- [Encapsulate Variable](refactorings/encapsulate-variable.md) — A widely accessed top-level variable needs validation, logging, or future migration; route access through getter/setter functions.
- [Replace Derived Variable with Query](refactorings/replace-derived-variable-with-query.md) — A field is just a cached calculation that can be recomputed cheaply; remove the field and compute it on demand.
- [Replace Magic Literal](refactorings/replace-magic-literal.md) — A literal value (number, string) appears in code with no explanation of what it means; replace with a named constant.
- [Replace Primitive with Object](refactorings/replace-primitive-with-object.md) — A primitive (string, number) is starting to carry behavior, validation, or formatting; promote it to a small dedicated class.

## Moving and Grouping

- [Combine Functions into Class](refactorings/combine-functions-into-class.md) — Several functions operate on the same data and pass it around explicitly; group them into a class with that data as state.
- [Combine Functions into Transform](refactorings/combine-functions-into-transform.md) — Several functions derive new fields from the same input record; combine them into a single transform that returns the enriched record.
- [Extract Class](refactorings/extract-class.md) — A class is doing two unrelated jobs and its fields/methods cluster into two groups; split off the secondary cluster into its own class.
- [Hide Delegate](refactorings/hide-delegate.md) — Callers reach through one object to use another (`a.getB().doX()`); add a method on the first that hides the chain.
- [Inline Class](refactorings/inline-class.md) — A class no longer carries its weight and exists only to pass calls along; fold its members back into its only caller.
- [Move Field](refactorings/move-field.md) — A field is consistently used together with another class's data; move it to where it belongs.
- [Move Function](refactorings/move-function.md) — A function references another module's data more than its own; move it to the module whose data it actually uses.
- [Move Statements into Function](refactorings/move-statements-into-function.md) — The same preparatory statements appear before every call to a function; move them inside the function.
- [Move Statements to Callers](refactorings/move-statements-to-callers.md) — A function does one thing for some callers and a slightly different thing for others; move the divergent statements out to the callers.
- [Remove Middle Man](refactorings/remove-middle-man.md) — A class does nothing but forward methods to a delegate; let callers talk to the delegate directly.

## Conditionals, Loops, and Algorithms

- [Consolidate Conditional Expression](refactorings/consolidate-conditional-expression.md) — Several separate conditional checks all produce the same result; combine them into one expression and (usually) extract a named function.
- [Decompose Conditional](refactorings/decompose-conditional.md) — A long if/else has complex condition and branch bodies; extract the condition and each branch into named functions.
- [Introduce Assertion](refactorings/introduce-assertion.md) — A section of code only works under an implicit assumption; make the assumption explicit with an assertion.
- [Introduce Special Case](refactorings/introduce-special-case.md) — Multiple call sites repeat the same check for a special value (null, "unknown"); replace that value with a special-case object that handles its own behavior.
- [Replace Conditional with Polymorphism](refactorings/replace-conditional-with-polymorphism.md) — Behavior switches on a type code or kind field; replace the conditional with subclass dispatch.
- [Replace Control Flag with Break](refactorings/replace-control-flag-with-break.md) — A boolean flag is used to exit a loop early; replace it with a direct `break`, `return`, or `continue`.
- [Replace Loop with Pipeline](refactorings/replace-loop-with-pipeline.md) — A loop accumulates a result through filter/map/reduce-shaped steps; replace it with a collection pipeline.
- [Replace Nested Conditional with Guard Clauses](refactorings/replace-nested-conditional-with-guard-clauses.md) — Several layers of nested `if`/`else` obscure the main flow; turn exceptional cases into early-exit guard clauses.
- [Separate Query from Modifier](refactorings/separate-query-from-modifier.md) — A function both returns a value and changes state; split it into one query and one command.
- [Split Loop](refactorings/split-loop.md) — A single loop computes two unrelated things; split it into two loops so each has one purpose (optimize back only if measured).
- [Substitute Algorithm](refactorings/substitute-algorithm.md) — The existing algorithm is correct but unnecessarily complex; replace it with a clearer one and rely on tests to confirm equivalence.

## Parameters, APIs, and Commands

- [Introduce Parameter Object](refactorings/introduce-parameter-object.md) — The same group of parameters travels together through many functions; wrap them in a small parameter object.
- [Parameterize Function](refactorings/parameterize-function.md) — Two functions do the same thing except for a literal value; merge them and pass the value as a parameter.
- [Preserve Whole Object](refactorings/preserve-whole-object.md) — Several values are pulled from an object only to be passed to a function; pass the whole object instead.
- [Remove Flag Argument](refactorings/remove-flag-argument.md) — A boolean (or enum) parameter selects between two distinct behaviors; split into two explicit functions.
- [Remove Setting Method](refactorings/remove-setting-method.md) — A field is meant to be set only at construction but exposes a setter; remove the setter to make it immutable after construction.
- [Replace Command with Function](refactorings/replace-command-with-function.md) — A command object exists only to run a single method and holds no useful state; replace it with a plain function.
- [Replace Constructor with Factory Function](refactorings/replace-constructor-with-factory-function.md) — Constructor calls need polymorphic dispatch, naming variants, or extra setup that constructors can't express cleanly; introduce a factory function.
- [Replace Error Code with Exception](refactorings/replace-error-code-with-exception.md) — Special return values signal errors and callers keep forgetting to check them; raise an exception instead.
- [Replace Exception with Precheck](refactorings/replace-exception-with-precheck.md) — An exception is being used for a condition the caller could and should check up front; replace the throw with a precondition test.
- [Replace Function with Command](refactorings/replace-function-with-command.md) — A function is long, has many local variables, or needs to be invoked in stages; promote it to a command object whose fields hold what were locals.
- [Replace Inline Code with Function Call](refactorings/replace-inline-code-with-function-call.md) — Inline code duplicates what an existing function already does; replace it with a call to that function.
- [Replace Parameter with Query](refactorings/replace-parameter-with-query.md) — A function takes a value the function itself can derive from data it already has; drop the parameter and compute it inside.
- [Replace Query with Parameter](refactorings/replace-query-with-parameter.md) — A function reaches out to a global or distant resource for a value; pass the value in to reduce coupling and aid testing.
- [Return Modified Value](refactorings/return-modified-value.md) — A function silently mutates a parameter or shared state; rewrite it to return the new value and let the caller assign it.
- [Split Phase](refactorings/split-phase.md) — Code does two different kinds of work tangled together (parsing+calculation, fetching+processing); split into sequential phases connected by an intermediate data structure.

## Inheritance and Delegation

- [Collapse Hierarchy](refactorings/collapse-hierarchy.md) — A subclass and its parent are no longer different enough to justify two classes; merge them into one.
- [Extract Superclass](refactorings/extract-superclass.md) — Two classes share methods and fields; lift the common pieces into a new superclass.
- [Pull Up Constructor Body](refactorings/pull-up-constructor-body.md) — Subclass constructors share initialization code; move the common part into a superclass constructor.
- [Pull Up Field](refactorings/pull-up-field.md) — Two or more subclasses hold the same field; move it to the superclass.
- [Pull Up Method](refactorings/pull-up-method.md) — Two or more subclasses implement the same method; move it to the superclass.
- [Push Down Field](refactorings/push-down-field.md) — A field on a superclass is only used by some subclasses; push it down to those subclasses.
- [Push Down Method](refactorings/push-down-method.md) — A method on a superclass is only relevant to some subclasses; push it down to those subclasses.
- [Remove Subclass](refactorings/remove-subclass.md) — A subclass no longer differs meaningfully from its parent; delete the subclass and use the parent directly.
- [Replace Subclass with Delegate](refactorings/replace-subclass-with-delegate.md) — Subclasses are used to vary one axis of behavior and inheritance is too rigid; replace the subclass with a delegate object.
- [Replace Superclass with Delegate](refactorings/replace-superclass-with-delegate.md) — A class inherits from a superclass only to reuse some functionality, not as a true is-a; replace inheritance with delegation to that class.
- [Replace Type Code with Subclasses](refactorings/replace-type-code-with-subclasses.md) — A type-code field drives different behavior in conditionals scattered through a class; replace the type code with subclasses (often a precursor to Replace Conditional with Polymorphism).

## Cleanup

- [Remove Dead Code](refactorings/remove-dead-code.md) — A function, branch, parameter, or import has no remaining callers or effect; delete it (rely on VCS, not comments, to remember it).
- [Replace Temp with Query](refactorings/replace-temp-with-query.md) — A temporary variable holds the result of an expression that could just as easily be a method; replace the temp with a method call (often a setup for Extract Function).
