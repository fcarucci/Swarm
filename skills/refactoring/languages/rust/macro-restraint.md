# Rust Refactoring: Macro Restraint

## Guideline

Avoid refactoring solutions that introduce or expand macro usage unless macros are the **only viable option** and all other alternatives have been explicitly evaluated and ruled out.

## Why

Macros in Rust are powerful but carry real costs:

- **Readability**: Macro invocations hide what code is actually generated. A reader cannot understand the behavior without mentally expanding the macro or reading its definition.
- **Debuggability**: Compiler errors inside macro expansions are often hard to trace. Stack traces and `rust-analyzer` diagnostics are degraded inside macro bodies.
- **Refactorability**: Macros resist further refactoring. Code inside a macro body is opaque to automated tools (rename, extract function, inline variable).
- **Testability**: Logic inside macros cannot be unit-tested directly. It has to be tested through every callsite.
- **Composability**: Macros do not compose naturally with traits, generics, or closures.

## Alternatives to Evaluate First

Before reaching for a macro, exhaust these options in order:

1. **Traits and generics** — If the repetition is across types, a generic function or a trait impl covers it without macros.
2. **Closures and higher-order functions** — If the repetition is behavior with varying logic, pass a closure.
3. **Builder pattern or fluent API** — If the repetition is configuration or construction, a builder removes boilerplate without macros.
4. **Default trait implementations** — If multiple structs share the same method body, move it to a default impl on a shared trait.
5. **Enum dispatch** — If you are matching over a family of types with identical interfaces, an enum with a shared method is clearer than a macro over those types.
6. **Newtype or wrapper struct** — If you need to add behavior to an existing type, wrap it rather than generating code for it.
7. **Procedural macro as last resort** — If a derive or attribute macro already exists in the ecosystem for the exact need (e.g., `#[derive(Debug)]`, `thiserror`, `serde`), using it is acceptable. Writing a new proc macro is a last resort.

## When Macros Are Acceptable

A macro refactoring is acceptable only when **all** of these are true:

- Every alternative above has been considered and a concrete reason for each rejection is stated.
- The macro removes duplication that cannot be removed any other way (e.g., repetition over a syntactic pattern that generics cannot express).
- The macro is small, well-named, and documented with a `/// # Example` showing its expansion.
- The macro is tested via its generated output, not just assumed correct.

## How to Apply This in Refactoring

When the skill would normally suggest "extract repeated code into a macro":

1. State the repeated pattern explicitly.
2. Work through the alternatives list above and explain why each does not apply.
3. Only if all alternatives are genuinely ruled out, proceed with the macro and document the reasoning in a comment above the macro definition.

If you reach for a macro out of habit or convenience rather than necessity, stop and revisit the alternatives list.
