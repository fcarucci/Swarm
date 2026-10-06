# Scope Selection

Use this file when the user asks to refactor a commit, one file, selected files, or a whole feature branch.

## Commit Scope

1. Identify the commit and parent:
   - `git show --stat --find-renames <commit>`
   - `git show --find-renames --find-copies <commit> --`
2. Determine whether the user wants to refactor the current working tree code that came from that commit or rewrite the commit history. Default to current working tree unless explicitly told to rewrite history.
3. Target touched files, their call sites, and their tests. Do not refactor nearby code only because it is visible.
4. Keep each refactoring independent of commit-history operations. Do not amend, rebase, or force-push unless explicitly requested.

## Single File Scope

1. Read the file completely enough to understand module responsibilities.
2. Find direct tests and callers with `rg`.
3. Prefer local refactorings first: Extract Function, Extract Variable, Split Variable, Inline Variable, Rename Variable.
4. If the change affects public names, load the relevant API context file and search all call sites.

## Multiple File Scope

1. Treat the requested file list as the boundary.
2. Map shared abstractions and call direction.
3. Refactor one abstraction at a time and run the full unit and integration gate after each named refactoring.
4. Avoid moving code between ownership boundaries unless the request or repo architecture supports it.

## Feature Branch Scope

1. Find the merge base:
   - `git merge-base HEAD origin/main`
   - if unavailable, inspect branch config and default branch names.
2. Inspect branch diff:
   - `git diff --stat <merge-base>...HEAD`
   - `git diff --name-status <merge-base>...HEAD`
3. Group changes by subsystem and risk. Start with the smallest high-value slice.
4. Use one refactoring per gate. For large branches, report progress after each green gate.
5. Do not refactor unrelated base-branch code unless it blocks the branch cleanup.

## Dirty Worktrees

- Run `git status --short` before editing.
- Preserve user changes. Never revert unrelated work.
- If a file has user changes that overlap the intended refactoring, inspect carefully and work with the current contents.
- Only back out your own last refactoring if a verification gate fails because of it.

