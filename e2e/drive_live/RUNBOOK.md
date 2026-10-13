# Swarm autonomy (drive mode): final QA runbook

Owner of this file: QA prep (Cecil Terwilliger). Code: branch `feat/drive-qa`, worktree `~/src/Swarm-autonomy-qa`,
`e2e/drive_live.sh` and `e2e/drive_live/`. A copy of this file is committed as `e2e/drive_live/RUNBOOK.md`.
Covers plan Tasks 15 (E2E), 16 (chaos), 19 (live acceptance) and what the judge (Task 20) needs.

## What this is

`e2e/drive_live.sh` activates a tiny throwaway goal job ("add a `--version` flag with a test" to a scratch repo, or an
impossible goal) in drive mode on an ISOLATED board, then drives it only with `swarm drive run` (the session driver's loop; `--driver passes`
runs one-shot `swarm supervise` passes instead) until the job is terminal. Nothing else touches the job: zero coordinator turns, no Claude Code session.
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
| git | `<run>/hub/{origin,backup}.git`, file:// URLs only | fast-forward-only `main`, no merge commits (an `update` hook), branch deletion allowed |
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

SIGTERM/SIGINT stop the throwaway Postgres too (trust auth on 127.0.0.1 is open to local users while a run lasts; a SIGKILLed harness leaves its cluster: stop it with `pg_ctl -D <run>/pg/data stop`).

Exit codes: `0` everything passed, `1` any failure, `3` only skips (a drive piece is pending: the message reads
`pending 6a/6b/8a/8b/8c/11b`, naming exactly the missing pieces), `2` setup missing.

Options: `--backend postgres|sqlite|file` (default postgres), `--harness fake|real` (default fake), `--budget-usd N`,
`--timeout-min N`, `--interval-s N` (seconds between supervisor passes), `--kill-supervisor-once` (SIGKILLs a supervise pass in flight and one runner; FAILs if it never managed to), `--systemd` (real scopes; needs a user manager), `chaos --today-only` (only the no-driver layers).

### Fake vs real harness

* `--harness fake` (default): `claude`/`codex` are scripted by `scripts/*.json`; real hooks (`bin/swarm-hook`), real `swarm`
  CLI, real git against the fake hub; cost is the script's `cost_usd` per session. $0, minutes.
* `--harness real` (the Task 19 gate): the real `claude` (and a real Codex engineer, plan [F18], on a drive-ready host).
  The run's HOME is isolated: `~/.claude/.credentials.json` is copied (mode 600) into it automatically (or export `ANTHROPIC_API_KEY`), and `claude_plugin_dir` points at the checkout under
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
   `SSH_AUTH_SOCK`. Until fixed, run with `SWARM_QA_REMOTE_STYLE=path` (pins plain paths; the hub URLs stay file://).
4. `activate` refuses unless a supervisor pass ran in the last 2 minutes; the rig runs one first.

## Scenarios and what they assert

Live (`met`, `unreachable`), each: status/verdict as expected (completed+met; failed+unreachable with details), never
`closed_by=auto`, never forced; ledger charged <= budget; no duplicate `(action_id, attempt)`; zero coordinator turns
(no `orphan-coordinator:` restart row, no orchestrator-role agent); every agent of the job has a supervisor restart row and is
ended; the harness log shows one session per launch; for MET: hub `main` linear, both destinations equal, something landed.

Chaos (`e2e/drive_live.sh chaos --list`, 37 rows). Each has a `<name>/today` row where something can be checked without the
driver, and a driven row. **No row can pass vacuously**: every driven scenario names the evidence that its fault fired
(a pass died by SIGKILL, a pid file was killed, a row was planted and read back, a CI red was recorded...) and FAILs with
"the injected fault never fired: nothing was tested" otherwise; a crash barrier that has no `hit("name")` in `lib/` of the
checkout is a SKIP naming it (`pending barrier: ...`), never a pass.

| scenario | injected fault | passes when |
|---|---|---|
| `kill_seat_mid_task`, `kill_lead_mid_turn` | SIGKILL the engineer / lead session at its hang point | seat relaunched (2+ starts), MET |
| `kill_runner_mid_session` | SIGKILL the runner of a live session | next pass reaps and relaunches, MET |
| `kill_supervisor_and_runner_once` (also `--kill-supervisor-once` on live runs) | SIGKILL a supervisor pass in flight while a runner is live, then SIGKILL a runner | both kills recorded, no duplicate attempt, MET. Swarm installs no timer or service unit, so there is no unit-level variant |
| `barrier_<name>` x16 | `SWARM_CRASH_AT=<name>` armed on every pass until a pass dies by SIGKILL (or a seat hook / runner session is killed: barriers `runner_mid_session`, `before_finish`, `hook_after_cli_before_receipt` live in seat processes and are passed through `[supervise] pass_env`) | fired (evidence recorded), no duplicate attempt, ends MET/UNREACHABLE. The 13 plan barriers plus `terminal_intent_saved`, `terminal_verdict_recorded`, `terminal_closed_before_ledger`. **In the tree today: `terminal_*`, `mid_rollover_after_ledger`, `land_after_first_destination`; the other 10 are SKIP until their owners add the `hit()` (posted on the board)**; `mid_terminalization` is covered by the three `terminal_*` names |
| `corrupt_ledger` | garbage over the ledger, restored 3 passes later | held, `DRIVER-LEDGER-BAD` posted, not closed while corrupt, MET after restore. Today: five corruption kinds raise `LedgerBadError` / are reported, never "no ledger" |
| `stale_lock` | stale `.lock` file (after the ledger exists) and an expired `drive_locks` row (postgres), both read back | planted proof, MET. Today: stale lock file blocks no save; planted lock symlink refused, target untouched |
| `reactivated_long_idle` | job halted, silent 10 h (postgres backdating), swept (`purge`, `status`, a pass), unhalted | still open after the sweeps, MET. Today (postgres): reproduces the bug; SKIP "known bug, pending 8b" until `quiet_start` is present (verified passing on `feat/drive-eng-life`) |
| `question_default_expires` | lead asks a human, `--default left --expires 2m` | `swarm questions --all` shows the **default applied** event for that question, the lead was woken (2+ starts), another seat made progress, MET |
| `forged_closed_by_driver` | `close_job(completed, closed_by="driver")` straight into the board, then one pass | not left `completed` without a ledger terminal; ends MET via the real flow |
| `forged_receipt` | `DONE`, `INTEGRATED`, `GOAL-READY` rows posted without receipts | no session launched for the forged SHA, forged SHA never on the target, MET. Today: forged row, other incarnation/generation, expiry, missing receipt all refused |
| `forged_person_commands` | `verdict`, `done`, `review` from a plain shell; `seat request` with a /tmp work dir | all refused (exit codes recorded), no seat launched |
| `forged_seat_row` (postgres) | seat row inserted into the database with no authorization | never launched (SKIP if the row cannot be inserted) |
| `forged_unhalt_by_seat` | an enrolled seat's `unhalt` | **not automated** (SKIP with reason; covered by `tests/test_drive_hooks`) |
| `ci_red_twice` | CI red on the rebased SHA twice (`hub.py ci-fail`) | two reds recorded, FINALIZE_BLOCKED/fix rounds/ruling, ends MET or UNREACHABLE |
| `budget_ruling_max_sessions_8` | `--budget max_sessions=8` with a reviewer that keeps asking for changes | 8 launches reached, ruling from the reserve, ends terminal within the cap + reserve |
| `ruling_judge_fails_twice` | the ruling judge exits without a verdict twice | mechanical UNREACHABLE (scope labelled mechanical) |
| `provider_outage` | the first 2 sessions fail like a provider outage (529) | outages recorded, attempts not burned, MET |
| `bus_down` | `systemctl` stub errors for 4 passes | held, then recovers, MET. Needs `SWARM_QA_SYSTEMD=1` and a user manager (else SKIP) |
| `stale_met` | the target moves after landing, before the close | goal judge reruns (2+ goal-judge sessions), terminal |
| `rollover` | re-activation with a changed goal mid-run | ledger generation >= 2, terminal |
| `codex_parity` | lead and engineer on the (fake) codex harness | codex sessions ran, MET |

Live runs add one row each (`live-met/...`): `models` (agent rows match the `[drive.models]` table: lead and judge opus, engineer
haiku, reviewer sonnet), `invariant` (no stall window over the run; an observable approximation of spec 3.3, replace with the
driver's own `invariant(snapshot)` once it exists), `landed-head` (landed head == the judged candidate on BOTH destinations,
linear, green CI recorded for that exact SHA and consulted by `ci-check`; FAIL when the candidate cannot be found),
`codex-engineer` (real mode with a `codex` binary; SKIP otherwise, this box has none, so [F18] stays unchecked),
`real-systemd` (`--systemd`: passes run with real transient scopes; SKIP by default), and for unreachable `unreachable-details`.

Cost guard: inside the drive loop, spending (ledger charge or restart-row cost, whichever is larger) above the budget halts the job and
fails the run ("COST CAP"), not only a check after the run.

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
* `<run>/logs/supervise.log` (all passes in full), `<run>/pg/pg.log`, the hub audit (`hub.py audit --hub <run>/hub --since <initial>`)
* `~/work/swarm-autonomy/qa/runs/last-report.txt` (the PASS/FAIL/SKIP table of the last invocation)

For the judge's evidence file `docs/superpowers/evidence/2026-10-drive-live.md` (plan Task 19): the head SHA the run used
(first timeline line), the `last-report.txt` table for `met`, `unreachable` and `chaos`, `costs.json` of each, the
`pending`-probe output (all present), redacted transcripts, plus the chaos `<name>/today` rows. Task 20's judge verdict
cites: CI green on the exact SHA, Task 15/16 results on all backends, Task 17 and Task 19 evidence (this).

## Cleanup

Runs stay on disk for inspection (small). `rm -rf ~/work/swarm-autonomy/qa/runs/<run>` removes one; the Postgres is already
stopped. Verify nothing is left: `pgrep -af 'swarm-autonomy/qa/runs'` prints nothing.

## Smoke tests that run today

`cd tests && /var/tmp/test-slots/test-slot -- python3 -B -m unittest test_drive_qa_harness` (27 tests, ~5 s; with the QA venv
python the no-driver chaos layers also run): probes and `Pending` text, run-dir guards, fake hub (exact-SHA CI, fast-forward
only, no merge commits, audit), the scripted engineer's change passes `make test` (and the impossible one cannot), and the
corrupt-ledger / stale-lock / forged-receipt layers. Mutants checked: hub fast-forward refusal, exact-SHA CI, audit linearity.
