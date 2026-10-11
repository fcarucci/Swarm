# Swarm autonomy (drive mode): final QA runbook

Owner of this file: QA prep (Cecil Terwilliger). Code: branch `feat/drive-qa`, worktree `~/src/Swarm-autonomy-qa`,
`e2e/drive_live.sh` and `e2e/drive_live/`. A copy of this file is committed as `e2e/drive_live/RUNBOOK.md`.
Covers plan Tasks 15 (E2E), 16 (chaos), 19 (live acceptance) and what the judge (Task 20) needs.

## What this is

`e2e/drive_live.sh` activates a tiny throwaway goal job ("add a `--version` flag with a test" to a scratch repo, or an
impossible goal) in drive mode on an ISOLATED board, then runs only `swarm supervise` passes (what the systemd timer
runs) until the job is terminal. Nothing else touches the job: zero coordinator turns, no Claude Code session.
It asserts the end state, the budget, that every seat was launched and stopped by the supervisor, and that the target
branch was fast-forwarded to the judged candidate on every destination. It writes the timeline and the costs.

The chaos suite injects the faults below into the same kind of run and asserts MET, UNREACHABLE or CANCELLED: never
stuck, never auto-closed.

## Isolation (what is guaranteed)

Everything lives under ONE run directory `~/work/swarm-autonomy/qa/runs/<stamp>-<name>/`:

| piece | where | how it is kept away from the real box |
|---|---|---|
| HOME of every process | `<run>/home` | config, markers, spool, supervisor private dir, ledger and receipts are all under it; `~/.local/share/swarm` is unreachable |
| board | throwaway Postgres 16 in `<run>/pg` (127.0.0.1, own random port, trust auth, TCP only) or `--backend sqlite\|file` | `SWARM_CONFIG` is the run's own file; the harness refuses to run if it equals the shared config; only the harness's own `pg_ctl -D <run>/pg/data` starts and stops the cluster (also via `atexit`); no other connection is ever touched |
| git | `<run>/forge/{origin,backup}.git`, file:// URLs only | fast-forward-only `main`, no merge commits (an `update` hook), branch deletion allowed |
| work trees | `<run>/work/...` | `[supervise] allowed_workdirs` is the run dir only |
| shims | `<run>/bin/{swarm,claude,codex}` | fake mode: `claude`/`codex` are `fake_harness.py`; real mode: the real binaries on the box PATH |

`/tmp` is refused everywhere (`qa_lib.check_run_dir`). The shared swarm board and real repositories are never named.
Postgres is stopped when a run ends (also on error). `pgrep -f swarm-autonomy/qa/runs` must be empty afterwards.

## One-time setup

```sh
~/src/Swarm-autonomy-qa/e2e/drive_live/setup_qa.sh        # venv (hash-checked requirements) + self-contained Postgres
```

Already done on this box: `~/work/swarm-autonomy/qa/venv`, `~/work/swarm-autonomy/qa/pgdist`. Note: the `~/.pg0` Postgres no
longer starts (needs `libicuuc.so.70`); `pgdist` is a copy of the `pgserver` wheel with its own libraries.

## Running it against the integrated head

```sh
git -C ~/src/Swarm-autonomy-eng-mirror fetch -q github
git -C ~/src/Swarm-autonomy-qa checkout --detach github/feat/swarm-autonomy      # or the branch under test; never ~/src/Swarm-autonomy
cd ~/src/Swarm-autonomy-qa && e2e/drive_live.sh pending                          # which pieces are present
e2e/drive_live.sh preflight                                                       # works on any checkout (~5 s)
e2e/drive_live.sh met                                                             # fake harness, MET run
e2e/drive_live.sh unreachable
e2e/drive_live.sh chaos                                                           # all scenarios; or: chaos kill_seat_mid_task corrupt_ledger
e2e/drive_live.sh all                                                             # preflight + met + unreachable + chaos
SWARM_QA_REPO=/path/to/other/checkout e2e/drive_live.sh pending                  # probe another checkout read-only
```

Exit codes: `0` everything passed, `1` any failure, `3` only skips (a drive piece is pending: the message reads
`pending 6a/6b/8a/8b/8c/11b`, naming exactly the missing pieces), `2` setup missing.

Options: `--backend postgres|sqlite|file` (default postgres), `--harness fake|real` (default fake), `--budget-usd N`,
`--timeout-min N`, `--interval-s N` (seconds between supervisor passes), `--kill-supervisor-once` (kills one supervise pass
mid-run, plan Task 19 addition), `chaos --today-only` (only the no-driver layers).

### Fake vs real harness

* `--harness fake` (default): `claude`/`codex` are scripted by `scripts/*.json`; real hooks (`bin/swarm-hook`), real `swarm`
  CLI, real git against the fake forge; cost is the script's `cost_usd` per session. $0, minutes.
* `--harness real` (the Task 19 gate): the real `claude` (and a real Codex engineer, plan [F18], on a drive-ready host).
  The run's HOME is isolated, so copy auth in first: `mkdir -p <run>/home/.claude && cp ~/.claude/.credentials.json <run>/home/.claude/`
  (or export `ANTHROPIC_API_KEY`, which the harness passes through), and `claude_plugin_dir` points at the checkout under
  test so the seats load THIS plugin. Budget cap defaults to `max_cost_usd=10`.

### What it needs from the integrated head (adapt points)

The harness was written against the plan and the in-flight branches, not a landed driver. On the first integrated run
expect to touch, at most:

1. `fake_harness.py` role functions (`role_lead`, `role_engineer`, `role_reviewer`, `role_judge`, `role_integrator`): the
   seat tag name in the drive prompts (it reads `[swarm seat: X]`, else `[swarm title: X]`), the exact `swarm seat request`,
   `swarm review`, `swarm verdict ... --open N`, `swarm land`, `swarm goal-ready`, `swarm ask` flags (taken from 6b's
   `drive_cli.py` and the ask-answer skill).
2. `rig.py` `check_*` (restart rows vs launches, ledger fields) if the landed ledger or restart schema differs.
3. Known 6a gap found while building this: `drive_activate.default_credential` treats `file://` as ssh and demands
   `SSH_AUTH_SOCK`. Until fixed, run with `SWARM_QA_REMOTE_STYLE=path` (pins plain paths; the forge URLs stay file://).
4. `activate` refuses unless a supervisor pass ran in the last 2 minutes; the rig runs one first.

## Scenarios and what they assert

Live (`met`, `unreachable`), each: status/verdict as expected (completed+met; failed+unreachable with details), never
`closed_by=auto`, never forced; ledger charged <= budget; no duplicate `(action_id, attempt)`; zero coordinator turns
(no `orphan-coordinator:` restart row, no orchestrator-role agent); every agent of the job has a supervisor restart row and is
ended; the harness log shows one session per launch; for MET: forge `main` linear, both destinations equal, something landed.

Chaos (`e2e/drive_live.sh chaos --list`). Each has a `<name>/today` row (needs no driver; runs now) and a driven row:

| scenario | injected fault | passes when |
|---|---|---|
| `kill_seat_mid_task` | SIGKILL the engineer session after it wrote its pid file | seat relaunched (2+ starts), job MET |
| `kill_runner_mid_session` | SIGKILL the runner of a live session | next pass reaps, relaunches, job MET |
| `kill_supervisor_after_claim` / `_after_run_file` / `_after_runner_start` | `SWARM_CRASH_AT=<barrier>` SIGKILLs the pass mid-launch | no duplicate attempt, job MET |
| `corrupt_ledger` | garbage over the job's ledger, restored 3 passes later | job held (not closed) and `DRIVER-LEDGER-BAD` posted; MET after restore. Today: every corruption (truncated, garbage, wrong shape, other job, oversize) raises `LedgerBadError` / is reported by `unfinished()`, never "no ledger" |
| `stale_lock` | left-over `.lock` file, expired `drive_locks` row of a dead holder (postgres) | nothing blocks, MET. Today: stale lock file blocks no save; a planted lock symlink is refused and never followed |
| `reactivated_long_idle` | a drive job halted, silent 5 h (backdated), swept, unhalted | not auto-closed, MET. Today (postgres): reproduces TODAY's bug: a goal job silent 10 h and re-activated is closed by the next sweep. Reports `SKIP known bug, pending 8b` until the quiet-clock fix (924b58c) is present, `FAIL` if it is present and the job is still closed (verified: passes on `feat/drive-eng-life`) |
| `question_default_expires` | lead asks a human with `--default left --expires 2m`, nobody answers | default applied (a post says so), other seats progressed, MET |
| `forged_closed_by_driver` | agent calls `close_job(completed, closed_by="driver")` directly | the forged close does not stay: no `completed` without a ledger terminal; job ends MET via the real flow |
| `forged_receipt` | `DONE feat/forged@fff...` and `INTEGRATED ...` rows posted with no hook receipt | no session is ever launched for the forged artifact; MET. Today: forged row, other incarnation/generation, expired, missing receipt all refused by `receipts.find/matches` |

Not covered here (own tasks): real systemd containment (Task 17, `SWARM_TEST_SYSTEMD=1`, box only), older-client compat (18),
the full barrier matrix on four backends (Task 16 in `tests/test_drive_chaos.py`, CI).

## Expected duration and cost

Estimates until the first integrated run replaces them with measured numbers.

| run | wall time | cost |
|---|---|---|
| `preflight` | ~5 s | $0 |
| `chaos --today-only` | ~10 s (sqlite), ~20 s with the postgres reactivation repro | $0 |
| `met`, fake | 3-8 min (passes every 20 s, ~15-25 passes) | $0 (script cost $0.05/session in the report) |
| `unreachable`, fake | 4-10 min | $0 |
| `chaos`, fake, all 11 scenarios | 40-70 min | $0 |
| `met`, real Claude (+ one real Codex engineer) | 30-90 min, hard limit 3 h | about $3-8, hard cap `max_cost_usd=10` |
| `unreachable`, real | 20-60 min | about $2-6 |

Two real runs (met + unreachable) are the Task 19 gate; run chaos with the fake harness, not the real one.

## What to capture for the judge

Per run, `<run>/logs/evidence/` (written by every live and chaos run):

* `status.txt`, `snapshot.json` (`swarm snapshot --json --job J`), `transcript.txt` (`swarm transcript export`)
* `supervise-tail.log` (last 400 lines of the passes), `timeline.jsonl` (every command, pass, state change, injected fault
  with elapsed seconds), `harness.jsonl` (every session: role, seat, model, harness, budget, cost), `costs.json`
  (sessions, launches, passes, ledger cost vs harness cost, by role, wall seconds)
* `<run>/logs/supervise.log` (all passes in full), `<run>/pg/pg.log`, the forge audit (`forge.py audit --forge <run>/forge --since <initial>`)
* `~/work/swarm-autonomy/qa/runs/last-report.txt` (the PASS/FAIL/SKIP table of the last invocation)

For the judge's evidence file `docs/superpowers/evidence/2026-10-drive-live.md` (plan Task 19): the head SHA the run used
(first timeline line), the `last-report.txt` table for `met`, `unreachable` and `chaos`, `costs.json` of each, the
`pending`-probe output (all present), redacted transcripts, plus the chaos `<name>/today` rows. Task 20's judge verdict
cites: CI green on the exact SHA, Task 15/16 results on all backends, Task 17 and Task 19 evidence (this).

## Cleanup

Runs stay on disk for inspection (small). `rm -rf ~/work/swarm-autonomy/qa/runs/<run>` removes one; the Postgres is already
stopped. Verify nothing is left: `pgrep -af 'swarm-autonomy/qa/runs'` prints nothing.

## Smoke tests that run today

`cd tests && /var/tmp/test-slots/test-slot -- python3 -B -m unittest test_drive_qa_harness` (20 tests, ~5 s; with the QA venv
python the no-driver chaos layers also run): probes and `Pending` text, run-dir guards, fake forge (exact-SHA CI, fast-forward
only, no merge commits, audit), the scripted engineer's change passes `make test` (and the impossible one cannot), and the
corrupt-ledger / stale-lock / forged-receipt layers. Mutants checked: forge fast-forward refusal, exact-SHA CI, audit linearity.
