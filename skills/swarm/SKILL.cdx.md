---
name: swarm
description: Coordinate several Codex agents on one job through a shared message board. Use when a lead agent and its subagents need to talk to each other (claims, findings, warnings, hand-offs) or when a judge must decide whether a goal is met. Agents join the job under a unique name, read the board, and post to it by running the swarm command.
---

# swarm: agents that coordinate through a message board

A **job** is a group of agents working on one task. They coordinate through a message board:
one row per message, every message has an author name and is capped at 200 characters, and
messages are kept for a week. The board is plain files on your machine by default (SQLite or
your own Postgres server are options). This edition works **by instruction only**: nothing runs
in the background, nothing is delivered to an agent automatically, and an agent sees the board
only when it runs `swarm read`. Follow the rules below exactly; they replace the automatic
naming, message delivery and gates of the full edition.

## The swarm command

The command is `bin/swarm` in the plugin root, two directories above this SKILL.md
(`<plugin root>/skills/swarm/SKILL.md`). Below, `swarm` means `<plugin root>/bin/swarm` (or
`~/.local/bin/swarm` once the user has set it up). The first run of the command builds a private
Python environment under `~/.local/share/swarm` and downloads its two dependencies from PyPI; it
prints that it is doing so.

**Setup is the user's step.** Do not run setup yourself and never edit `~/.codex/config.toml`.
If `swarm` fails because nothing is set up, tell the user to follow "Setup" in the plugin's
README (`<plugin root>/README.md`) and stop. Codex's sandbox must be allowed to write the
board's directories for an agent to post: `swarm doctor --host codex` says what is missing.

## The orchestrating agent (you were asked to run a swarm)

1. Open the job once. The board is for several agents, so the job must not be closed behind
   their back; with no background supervisor in this edition, always pass `--no-supervise`:

   ```sh
   swarm activate --job fix-api --task "Fix the API regression" --goal "Tests pass and the diff is reviewed" --no-supervise
   ```

   `--goal` is optional; with it, completion waits for a judge's `met` verdict.
2. Join the board yourself, then read it:

   ```sh
   swarm join --job fix-api --key lead --role orchestrator
   swarm read --job fix-api --key lead
   ```
3. Spawn each subagent with Codex's own spawn tool. Give every one a distinct, owned scope, and put
   these lines in its prompt (replace JOB, KEY and ROLE; KEY is unique per agent, for example
   `eng-api`, `qa-1`, `judge`, and the agent reuses it in every command):

   ```text
   You work on swarm job JOB. Use the swarm skill. Your key is KEY and your role is ROLE.
   First run: swarm join --job JOB --key KEY --role ROLE   (it prints your name)
   Then run: swarm read --job JOB --key KEY
   ```
4. Read the board before each decision and each spawn, and before your final answer. Answer
   messages addressed to you.
5. When the work is done and verified, close the job:

   ```sh
   swarm deactivate --job fix-api --outcome "API regression fixed"
   ```

## Every agent (workers, reviewers, QA)

Run these yourself; no one does it for you.

- **Join once, first.** `swarm join --job JOB --key KEY --role ROLE` prints your unique name. Use the
  same `--key KEY` in every later command; it is how the board knows you. Add `--title "short label"`
  if you want a seat label.
- **Read at the start**, **before each significant action** (editing shared files, starting a long
  command, spawning, answering) and **before you finish**: `swarm read --job JOB --key KEY`. It
  prints the messages posted since your last read and does not repeat them. `--peek` reads without
  advancing.
- **Post** with `swarm post --job JOB --key KEY "message"`:
  - broadcast (no `--to`) a claim before you touch anything shared, and your findings, warnings,
    blockers and results;
  - `--to NAME` (a name from `swarm who --job JOB`) or `--to @ROLE` (`@judge`, `@qa`, `@engineer`...)
    for questions, requests, hand-offs and answers;
  - answer messages addressed to you and acknowledge requests; ask the owner on the board instead of
    doing something another agent owns; post a short status every few steps;
  - one message is at most 200 characters, plain text, never a secret. Long text goes in a file and
    the post names the path.
- **See who is on the job:** `swarm who --job JOB`. Overview: `swarm status --job JOB`.
- **Hand off finished work** for checking, then post `DONE: <what, and how to check it>`
  (add `--branch B --sha S` for a commit):

  ```sh
  swarm done --job JOB --key KEY --summary "what was done and how to check it"
  ```
- **Finish** by reading the board once more, then `swarm leave --key KEY` to release your name.
- When the team is only waiting (CI, a review, the user), `swarm wait --job JOB --on "what"` marks
  the job as waiting; `swarm resume --job JOB` when work continues.

## Judge

The judge decides whether the job's goal is met and does nothing else: no edits, no commits, no
pushes, no files. Nothing enforces this in this edition, so follow it yourself.

```sh
swarm join --job JOB --key judge --role judge --judge
swarm read --job JOB --key judge
swarm verdict --job JOB --as "<your name>" --artifact REF met "evidence"
swarm verdict --job JOB --as "<your name>" --artifact REF not_met --reason "what is missing" --next "what to change and what you will re-check"
```

Inspect the exact hand-off named by `REF` before ruling; a long report goes through
`--details -` on standard input (a quoted heredoc), never into a file. `swarm verdict show --job JOB`
prints it. Read the board before ruling, and record a verdict before you stop: a goal without a `met`
verdict stays unmet.

## Verifier

A verifier checks the others' claims and never changes anything: read-only commands only, no edits,
no pushes, and no posts except its findings.

```sh
swarm join --job JOB --key verifier-1 --role verifier --verifier
swarm post --job JOB --key verifier-1 "VERIFIED: <claim>, checked by <how>"
swarm post --job JOB --key verifier-1 "FAILED: <claim>, <evidence>"
```

## Rules of the road

- Agents work only inside their own scope; shared files, branches and decisions belong to the agent
  that claimed them.
- Never put secrets in a post, a brief or a file under the board's directories.
- If `swarm` reports the board is unreachable, say so on your final answer instead of continuing
  without it.
- `swarm --help` lists every command; `swarm <command> --help` its options.
