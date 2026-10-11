# Swarm Autonomy (drive mode) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
> In this job the tasks are filled as Swarm seats by the EL (`swarm seat request` once Task 6 lands; until then by the PM's spawns). Each task names its seat, model and reviewer.

**Goal:** Every goal job (`--goal` implies drive mode; `--no-drive --reason` opts out) reaches MET, UNREACHABLE or CANCELLED with zero coordinator turns: the supervisor pass drives staffing, resumes, review/fix/judge rounds, landing, rulings and recovery from board state plus a host-private ledger.

**Architecture:** A new `swarm.supervisor.driver` runs first in every supervisor pass. It enforces deadlines from a host-private ledger, reconciles launches, computes the next actions with a pure `next_actions(snapshot)` and executes them through one atomic `claim_drive_launch` plus the existing runner/marker machinery. Seats (schema 25) replace coordinator spawns; hook-written receipts authenticate every agent-written control record; `swarm land` is the only path to the target branch.

**Tech Stack:** Python 3.11+ stdlib (as the rest of `lib/swarm`), unittest, SQLite/Postgres/file/memory board backends, systemd user units, Claude Code `claude -p`, Codex `codex exec`.

**Spec:** `docs/superpowers/specs/2026-10-10-swarm-autonomy-design.md` (revision 6: re-judge fixes **[Gn]**, delta re-review fixes **[RRn]**; design-judge fixes marked **[Fn]**, PM ruling **[A]**). Read §2.2 (guarantee and limits), §3.2–§3.3 (driver), §4.0 (threat model) and §4.5 (claim protocol) before any task.

## Global Constraints

- Terminal states of a drive goal job: MET, UNREACHABLE (judge verdict with reasons, or the labelled mechanical one), CANCELLED (a person). Never idle/stalled/waiting for the coordinator.
- No new always-on process: only the timer-driven pass, its per-session runners, and the existing opt-in events listener.
- Every agent joins the board (enrol deadline `enrol_minutes` kills sessions that do not).
- Models: product defaults for drive seats as below. **Build staffing for this job** (Francesco, 2026-10-10: "i want the engineers to be haiku"): every engineer seat in every wave runs on Haiku; reviewers and QA on Sonnet; the security reviewer, the judge and the lead on Opus. Product defaults ([P1], `[drive.models]`, configurable, per job `--model ROLE=MODEL`, per seat `--model`): `engineer = worker = integrator = "haiku"`, `reviewer = qa = "sonnet"`, `judge = lead = security = "opus"`.
- Landing: rebase only (or `[land] strategy = squash-ff` where the project sets it), fast-forward pushes only, never a force push, CI green on the exact rebased SHA, through `swarm land`.
- Tests: on the box only targeted tests through `/var/tmp/test-slots/test-slot -- <cmd>` run from `tests/` (`cd tests && /var/tmp/test-slots/test-slot -- python3 -B -m unittest test_x`); full suites only in GitHub Actions; wait for CI with `swarm ci wait --repo fcarucci/Swarm --sha <exact sha> --timeout 540` in the foreground; never `gh run watch`.
- Never put work trees, clones or builds under /tmp (live acceptance uses `~/work/drive-accept/`; seat workdirs under /tmp are refused by the driver). Unit tests keep using the suite sandbox `support.SANDBOX`/`TMPDIR`, short-lived and small, as the existing suite does.
- Judges only judge; reviewers only review; fixes happen on writer seats.
- Never a bypass flag (`launch.check_safe`), `--setting-sources user`, Codex `workspace-write` (read-only for judges/reviewers).
- **One record format** (couples Tasks 5 and 6): every CLI command that creates or changes a drive control record prints, as its last stdout line, exactly `swarm-record: kind=<kind> id=<integer> rev=<integer>` (`kind` ∈ `done, review, verdict, seat, ask, answer, post, goal_ready, ack, withdraw, halt, unhalt, deactivate, activate`; `rev` is 1 for records without revisions). Task 6 prints it; Task 5 parses it with `re.fullmatch(r"swarm-record: kind=([a-z_]+) id=(\d+) rev=(\d+)", last_line)`.
- In drive jobs the CLI never spools a control command (spec [F18]); it fails with a clear error instead.
- Schema version becomes 25 (`SCHEMA_VERSION = 25`); verdicts `("met", "not_met", "unreachable")`.
- Budget **configurable defaults** (`[drive]` in config.toml; per job overridable at activation; spec §4.1 [D1]; Francesco: "spending cap is configurable"): `max_sessions=60`, `max_seats=12`, `max_rounds=8`, `no_progress_rounds=3`, `max_attempts=3`, `max_cost_usd=40` (shipped default), `ruling_reserve_usd=5` (shipped default), `session_usd_cap=5`, `min_session_usd=0.5`, `max_session_minutes=60`, `max_wall_hours=24`, `ruling_reserve = {sessions=2, minutes=60, usd=5}`, `ruling_attempts=2`, `ruling_deadline_minutes=120`, `ruling_grace_minutes=15`, `question_expiry="4h"`, `stall_minutes=90`, `driver_error_passes=6`, `max_concurrent_sessions=4`, `codex_usd_per_mtok=10`.
- Commits end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`; CHANGELOG.md gets an entry per user-visible task (the changelog check in CI enforces it).

## Review Focus

1. A Claude seat that exits normally after posting prose ("DONE: all good") but no `swarm done`: the user expects a retry with a nudge, then lead wake, never a silent idle job → test in Task 8 (`test_prose_done_is_not_a_handoff`).
2. Two drive jobs on the same repo landing on `main` at once: the user expects one integrator at a time, the other held then proceeding → test in Task 11 (`test_second_job_waits_for_land_lease`).
3. A person re-activates a job with a corrected goal while seats run: the user expects old sessions stopped before new ones start and old verdicts ignored → test in Task 8 (`test_rollover_stops_old_generation_first`).
4. A question asked with `--expires 30m` while the box sleeps for 2 h: on the next pass the default is applied once, the seat woken once → test in Task 12 (`test_expiry_after_downtime_applies_once`).
5. Claude reports no `total_cost_usd` (killed at the wall clock): the job is charged the reserved ceiling, so later launches are held correctly → test in Task 9 (`test_missing_usage_charges_reservation`).

---

## Staffing and order

| Wave | Tasks | Seats (model) | Reviewer |
|------|-------|---------------|----------|
| — | reviewer (sonnet): line-by-line + test-quality + mutation gate on every seat; security reviewer (opus): Tasks 3, 4, 5, 8a–8c, 11, 20; QA (sonnet); judge (opus); lead/integrator (opus) | | |
| 0 | 1 doctor fix | eng-doctor (haiku) | reviewer (sonnet) |
| 1 | 2 schema+verdicts, 4 ledger+receipts, 9a clock+barriers | eng-board (haiku), eng-ledger (haiku), eng-runner (haiku) | reviewer (sonnet); security reviewer (opus) on 4 |
| 2 | 3 seats+claim, 9 runner, 12 questions | eng-board, eng-runner, eng-questions (haiku) | reviewer (sonnet); security reviewer (opus) on 3 |
| 3 | 5 hooks+receipts+guardrails, 6 CLI, 7a model freeze | eng-hooks (haiku), eng-cli (haiku), eng-driver (haiku) | security reviewer (opus) on 5; EL on 7a |
| 4 | 7 next_actions, 8a execute+reconcile, 8b deadlines/terminalize/rollover, 8c tamper/pass/prompts | eng-driver, eng-driver2, eng-driver3, eng-driver4 (all haiku) | EL + reviewer (sonnet); security reviewer (opus) on 8a, 8b, 8c |
| 5 | 10 pipeline, 11 land+mirrors, 13 triggers | eng-pipeline (haiku), eng-land (haiku), eng-trigger (haiku) | reviewer (sonnet); security reviewer (opus) on 11 |
| 6 | 14 skill docs, 18 compat | eng-docs (haiku), eng-board | reviewer |
| 7 | 15 E2E, 16 chaos, 17 systemd test | qa (sonnet), qa-chaos (sonnet) | EL |
| 8 | 19 live acceptance, 20 security review, judge | qa + EL; security reviewer (opus); judge (opus) | judge |

**Review gates for Haiku engineers** (every seat's diff, before integration; owned by the Sonnet reviewer, enforced by the lead):
1. **Line-by-line review** of the whole diff against the task text and spec sections: every changed line accounted for; any behaviour change outside the task's files or Interfaces is a blocking finding (hidden behaviour changes); deleted or weakened existing assertions are blocking.
2. **Test-quality check:** each new test must fail on the base commit (the reviewer runs it there through test-slot and records the failure) and must assert observable behaviour, not mocks of the code under test or tautologies (vacuous tests are blocking).
3. **Mutation gate:** the reviewer applies at least 5 targeted mutations to the task's new or changed logic (negate a condition, drop a guard or `return`, off-by-one a bound, swap a CAS/revision check, skip an fsync/lock call), one at a time in a scratch copy of the worktree under `~/work/mutants/<seat>/` (never /tmp), and runs the task's targeted tests through test-slot. Every mutant must be killed; a survivor is a blocking finding with the mutant diff attached. The mutants and results go in the review report.
4. **Opus security review** additionally gates Tasks 3, 4, 5, 8a/8b/8c, 11 and 20.

Every task: its own branch `feat/drive-<task>` off `feat/swarm-autonomy`, `swarm done --artifact feat/drive-<task>@<sha>`, review, integration by rebase onto `feat/swarm-autonomy`. The final branch lands on `main` through the pipeline after CI green on the exact SHA.

---

### Task 1: Doctor reads the timer through the user manager's environment

Seat: eng-doctor (haiku). Spec §5.

**Files:**
- Modify: `lib/swarm/supervisor/systemd.py` (`timer_state`, `linger`, `install`; add `manager_env`)
- Modify: `lib/swarm/supervisor/runner.py:458-466` (`_manager_env` becomes an import of `systemd.manager_env`)
- Modify: `lib/swarm/bootstrap.py:1195-1205` (doctor line)
- Test: `tests/test_supervise_setup.py`, `tests/test_doctor.py`

**Interfaces:**
- Produces: `systemd.manager_env(env: dict | None = None) -> dict` (allowlisted env with `XDG_RUNTIME_DIR=/run/user/<uid>` when missing and the dir exists); `systemd.timer_state(run=subprocess.run) -> dict` now passes `env=manager_env()`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_supervise_setup.py
class TimerStateEnv(unittest.TestCase):
    def test_timer_state_sets_xdg_runtime_dir(self):
        from swarm.supervisor import systemd
        seen = {}
        def run(cmd, **kw):
            seen.update(kw.get("env") or {})
            return mock.Mock(returncode=0, stdout="ActiveState=active\nUnitFileState=enabled\n")
        with mock.patch.dict(os.environ, {}, clear=False), \
             mock.patch("pathlib.Path.is_dir", return_value=True):
            os.environ.pop("XDG_RUNTIME_DIR", None)
            state = systemd.timer_state(run)
        self.assertEqual(state["ActiveState"], "active")
        self.assertTrue(seen["XDG_RUNTIME_DIR"].startswith("/run/user/"))

# tests/test_doctor.py
    def test_timer_unknown_from_shell_without_bus_is_not_fail(self):
        # timer_state() == {} and supervise last run 1 minute ago
        lines = self.doctor_lines(timer_state={}, last_run_minutes=1)
        line = next(l for l in lines if "supervise timer" in l)
        self.assertTrue(line.startswith("OK") or line.startswith("WARN"), line)
        self.assertIn("unknown (no user bus from this shell)", line)
```

(`doctor_lines` is the helper in `test_doctor.py` that patches `systemd.timer_state` and the state file; add the two keyword arguments to it.)

- [ ] **Step 2: Run them to verify they fail**

Run: `cd tests && /var/tmp/test-slots/test-slot -- python3 -B -m unittest test_supervise_setup.TimerStateEnv test_doctor`
Expected: FAIL (`KeyError: 'XDG_RUNTIME_DIR'`; doctor line starts with `FAIL`).

- [ ] **Step 3: Implement**

```python
# lib/swarm/supervisor/systemd.py
def manager_env(env=None) -> dict:
    """`env` (default: the runner's allowlisted environment) with XDG_RUNTIME_DIR set when missing:
    the user manager's bus lives there. Agent shells lack it, and systemctl --user then fails."""
    from swarm import compat
    from swarm.supervisor.runner import _allowed_env
    env = _allowed_env(os.environ) if env is None else dict(env)
    if not env.get("XDG_RUNTIME_DIR"):
        d = Path(f"/run/user/{compat.uid()}")
        if d.is_dir():
            env["XDG_RUNTIME_DIR"] = str(d)
    return env

def timer_state(run=subprocess.run) -> dict:
    try:
        result = run(["systemctl", "--user", "show", TIMER, "-p", "ActiveState", "-p", "UnitFileState"],
                     capture_output=True, text=True, timeout=10, env=manager_env())
    ...
```

Same `env=manager_env()` in `linger` and in each `run(command, ...)` of `install`. In `runner.py` replace the body of `_manager_env` with `return systemd.manager_env(env)`. In `bootstrap.py`, when `state == {}` and the last run is younger than `2 * timer_minutes`, emit `Check("supervise timer", True, "unknown (no user bus from this shell); last run recent")`.

- [ ] **Step 4: Run the tests and `tests/test_supervise_runner.py`**

Run: `cd tests && /var/tmp/test-slots/test-slot -- python3 -B -m unittest test_supervise_setup test_doctor test_supervise_runner`
Expected: PASS. Then `swarm doctor | grep 'supervise timer'` on the box prints `OK ... active/enabled`.

- [ ] **Step 5: Commit** — `git commit -m "doctor: read the supervise timer through the user manager's environment"` (+ CHANGELOG line).

---

### Task 2: Schema 25 part 1 — typed verdicts, `unreachable`, generation

Seat: eng-board (haiku). Spec §3.1, §9.

**Files:**
- Modify: `lib/swarm/board/base.py` (`VERDICTS`, `SCHEMA_VERSION = 25`, `record_verdict`, `goal_is_unmet`, `CloseGuard`)
- Modify: `lib/swarm/board/postgres.py` (CHECK constraint, `verdict_scope`, `verdict_generation`, `verdict_candidate`, `verdict_req` columns)
- Modify: `lib/swarm/board/sqlite.py` (`MIGRATIONS` + CHECK rebuild), `lib/swarm/board/memory.py`, `lib/swarm/board/file.py`
- Modify: `lib/swarm/review.py` (`verdict_data` stores scope/generation/candidate/req)
- Modify: `lib/swarm/cli.py:3013` (`verdict` choices; `--scope`, `--open`, `--progress`, `--candidate`, `--req`), `lib/swarm/spool.py:311`
- Test: `tests/test_board_contract.py` (new class `DriveVerdicts`), `tests/test_verdict_details.py`, `tests/test_migrate.py`

**Interfaces:**
- Produces: `VERDICTS = ("met", "not_met", "unreachable")`; `VERDICT_SCOPES = ("artifact", "goal", "ruling")`;
  `Board.record_verdict(job, judge_name, verdict, reason, next_steps=None, artifact=None, details=None, *, scope="artifact", generation=None, open_count=None, progress=None, candidate=None, req=None) -> bool`;
  `JobStatus.verdict_scope: str | None`, `.verdict_generation: int | None`, `.verdict_candidate: str | None`, `.verdict_req: str | None`;
  `goal_is_unmet(goal, verdict)` returns False for `met` and `unreachable`.
  Rules: `unreachable` requires `details`; `not_met` with `scope` in (`goal`,`artifact`) on a drive job requires `open_count`.

- [ ] **Step 1: Failing contract tests (run on every backend through the harness matrix)**

```python
class DriveVerdicts(ContractBase):
    def test_unreachable_requires_details(self):
        b = self.board(); self.goal_job(b, "J")
        judge = self.seat_judge(b, "J")
        with self.assertRaises(ValueError):
            b.record_verdict("J", judge, "unreachable", "cannot", scope="goal", generation=1)
        self.assertTrue(b.record_verdict("J", judge, "unreachable", "cannot", details="# why\n...",
                                         scope="goal", generation=1))
        js = b.job_status("J")
        self.assertEqual((js.verdict, js.verdict_scope, js.verdict_generation), ("unreachable", "goal", 1))

    def test_artifact_met_is_not_goal_met(self):
        b = self.board(); self.goal_job(b, "J"); judge = self.seat_judge(b, "J")
        b.record_verdict("J", judge, "met", "ok", artifact="feat/x@" + "a" * 40, scope="artifact", generation=1)
        self.assertEqual(b.job_status("J").verdict_scope, "artifact")

    def test_unreachable_settles_goal(self):
        from swarm.board.base import goal_is_unmet
        self.assertFalse(goal_is_unmet("g", "unreachable"))
        self.assertTrue(goal_is_unmet("g", "not_met"))
```

Migration test: open a `schema18_*` fixture store, migrate, assert `verdict='unreachable'` is accepted and old rows keep `verdict_scope IS NULL`.

- [ ] **Step 2: Run** `cd tests && /var/tmp/test-slots/test-slot -- python3 -B -m unittest test_board_contract.DriveVerdicts test_migrate` with `SWARM_TEST_BACKEND=sqlite` then `memory` → FAIL.
- [ ] **Step 3: Implement.** Postgres migration block:

```sql
ALTER TABLE jobs DROP CONSTRAINT IF EXISTS jobs_verdict_check;
ALTER TABLE jobs ADD CONSTRAINT jobs_verdict_check CHECK (verdict IN ('met', 'not_met', 'unreachable'));
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS verdict_scope text;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS verdict_generation integer;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS verdict_candidate text;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS verdict_req text;
```

SQLite: append the four columns to `MIGRATIONS`; the CHECK change uses the existing table-rebuild helper used for earlier CHECK changes (search `CHECK(verdict` in `sqlite.py`). `review.verdict_data` adds `scope`, `generation`, `open`, `progress`, `candidate`, `req` to each per-artifact record. CLI: `vd.add_argument("verdict", choices=["met", "not_met", "unreachable", "show"])` and the new flags; `--details -` mandatory for `unreachable`.
- [ ] **Step 4: Run** the step-2 command on sqlite, memory, file (and postgres via `run_postgres_suite.py` where a test database is available on the box; otherwise CI) → PASS. Also `test_verdict_details test_goals test_judge_handover`.
- [ ] **Step 5: Commit** — "board: schema 25 typed verdicts and unreachable".

---

### Task 3: Schema 25 part 2 — seats, drive locks, atomic launch claim

Seat: eng-board (haiku). Security reviewer reviews. Spec §3.2, §3.4, §3.8, §4.5.

**Files:**
- Create: `lib/swarm/board/seats.py` (SQL + memory storage, like `board/blockers.py`)
- Modify: `lib/swarm/board/base.py` (dataclasses `Seat`, `DriveLock`; methods below; `Restart` gains `action_id`, `launch_id`, `reserved_usd`, `cost_usd`, `tokens_in`, `tokens_out`)
- Modify: `lib/swarm/board/postgres.py`, `sqlite.py`, `memory.py`, `file.py` (tables, columns, `UNIQUE(job, action_id, attempt)`)
- Modify: `lib/swarm/board/blockers.py` (columns `seat`, `generation`; `open_blocker(..., seat=None, generation=None)`)
- Test: `tests/test_board_contract.py` (class `Seats`, class `DriveClaims`), `tests/test_blockers.py`

**Interfaces:**
- Produces:
  - `Seat(id, job, generation, seat, role, title, harness, model, workdir, brief_path, task_ref, complexity, requested_by, requested_at, state, agent_key, session_id, revision, launches, last_outcome, wake_reason, wake_watermark, updated_at)`; `SEAT_STATES = ("requested", "running", "idle", "released", "failed", "cancelled", "refused")`.
  - `Board.request_seat(job, generation, seat, *, role, harness, model, workdir, brief_path, title=None, task_ref=None, complexity="standard", requested_by) -> Seat` (idempotent: same fields → same row and revision; changed fields while `requested` → revision+1; otherwise `ValueError("seat is <state>: release it first")`).
  - `Board.seats(job, generation=None, include_closed=True) -> list[Seat]`; `Board.seat(job, generation, seat) -> Seat | None`.
  - `Board.set_seat_state(seat_id, expected_revision, state, **fields) -> Seat | None` (CAS; revision+1).
  - `Board.claim_drive_launch(*, job, generation, action_id, attempt, launch_id, seat_id, seat_revision, harness, reserve_usd, reserve_minutes, locks: list[str], lock_seconds, max_host_running, ruling=False) -> Restart | None` — one transaction: seat CAS (state → `running`), all `locks` inserted or refused all-or-nothing (sorted), host running cap (`ruling=True` allows cap+1), insert restart row; returns None on any conflict.
  - `Board.renew_drive_locks(launch_id, expires_at) -> int`; `Board.release_drive_locks(launch_id) -> int`; `Board.drive_locks(prefix="") -> list[DriveLock]`.
  - `Board.finish_restart(id, outcome, *, cost_usd=None, tokens_in=None, tokens_out=None)` (extended).

- [ ] **Step 1: Failing contract tests**

```python
class DriveClaims(ContractBase):
    def test_claim_is_atomic_and_unique(self):
        b = self.board(); self.goal_job(b, "J")
        s = b.request_seat("J", 1, "eng-a", role="engineer", harness="claude", model="sonnet",
                           workdir="/w/a", brief_path="/w/a/BRIEF.md", requested_by="lead")
        r = b.claim_drive_launch(job="J", generation=1, action_id="1|seat|eng-a", attempt=1,
                                 launch_id="L1", seat_id=s.id, seat_revision=s.revision, harness="claude",
                                 reserve_usd=5, reserve_minutes=60, locks=["wt:aaa"], lock_seconds=4200,
                                 max_host_running=4)
        self.assertIsNotNone(r)
        self.assertEqual(b.seat("J", 1, "eng-a").state, "running")
        again = b.claim_drive_launch(job="J", generation=1, action_id="1|seat|eng-a", attempt=1,
                                     launch_id="L2", seat_id=s.id, seat_revision=s.revision, harness="claude",
                                     reserve_usd=5, reserve_minutes=60, locks=[], lock_seconds=1,
                                     max_host_running=4)
        self.assertIsNone(again)                     # stale revision and duplicate attempt

    def test_locks_all_or_nothing(self):
        b = self.board(); self.goal_job(b, "J")
        a = self.seat(b, "J", "a"); c = self.seat(b, "J", "c")
        self.assertIsNotNone(self.claim(b, a, locks=["land:x:main", "land:y:main"]))
        self.assertIsNone(self.claim(b, c, locks=["land:y:main", "land:z:main"]))
        self.assertEqual({l.key for l in b.drive_locks("land:")}, {"land:x:main", "land:y:main"})

    def test_request_seat_idempotent(self):
        b = self.board(); self.goal_job(b, "J")
        one = self.seat(b, "J", "a"); two = self.seat(b, "J", "a")
        self.assertEqual((one.id, one.revision), (two.id, two.revision))
```

Plus `test_blockers.py`: `open_blocker(..., seat="eng-a", generation=1)` round-trips on every backend.
- [ ] **Step 2: Run** `cd tests && /var/tmp/test-slots/test-slot -- python3 -B -m unittest test_board_contract.DriveClaims test_board_contract.Seats test_blockers` → FAIL.
- [ ] **Step 3: Implement.** Postgres DDL:

```sql
CREATE TABLE IF NOT EXISTS seats (
  id bigserial PRIMARY KEY, job text NOT NULL REFERENCES jobs(job) ON DELETE CASCADE, generation integer NOT NULL,
  seat text NOT NULL, role text NOT NULL, title text, harness text NOT NULL CHECK (harness IN ('claude','codex')),
  model text, workdir text NOT NULL, brief_path text NOT NULL, task_ref text,
  complexity text NOT NULL DEFAULT 'standard' CHECK (complexity IN ('simple','standard','complex')),
  requested_by text NOT NULL, requested_at timestamptz NOT NULL, state text NOT NULL, agent_key text,
  session_id text, revision integer NOT NULL DEFAULT 1, launches integer NOT NULL DEFAULT 0, last_outcome text,
  wake_reason text, wake_watermark bigint, updated_at timestamptz NOT NULL, UNIQUE (job, generation, seat));
CREATE TABLE IF NOT EXISTS drive_locks (key text PRIMARY KEY, holder text NOT NULL, job text NOT NULL,
  generation integer NOT NULL, launch_id text NOT NULL, host text NOT NULL, expires_at timestamptz NOT NULL);
ALTER TABLE restarts ADD COLUMN IF NOT EXISTS action_id text;
ALTER TABLE restarts ADD COLUMN IF NOT EXISTS launch_id text;
ALTER TABLE restarts ADD COLUMN IF NOT EXISTS reserved_usd double precision;
ALTER TABLE restarts ADD COLUMN IF NOT EXISTS cost_usd double precision;
ALTER TABLE restarts ADD COLUMN IF NOT EXISTS tokens_in bigint;
ALTER TABLE restarts ADD COLUMN IF NOT EXISTS tokens_out bigint;
CREATE UNIQUE INDEX IF NOT EXISTS restarts_action_attempt ON restarts (job, action_id, attempt) WHERE action_id IS NOT NULL;
ALTER TABLE blockers ADD COLUMN IF NOT EXISTS seat text;
ALTER TABLE blockers ADD COLUMN IF NOT EXISTS generation integer;
```

`claim_drive_launch` on Postgres: `BEGIN; SELECT ... FROM seats WHERE id=%s FOR UPDATE;` check revision; `INSERT INTO drive_locks ... ON CONFLICT DO NOTHING` per sorted key, rollback if any affected 0 rows (an expired lock is not reclaimed here: Task 8 releases it after fencing); count open restarts of this host; insert restart row; `UPDATE seats SET state='running', revision=revision+1, launches=launches+1`; `COMMIT`. SQLite uses `BEGIN IMMEDIATE`; memory/file under the store lock. The `attempt` column is set from the argument, not computed.
- [ ] **Step 4: Run** step 2 on memory, sqlite, file → PASS; Postgres in CI.
- [ ] **Step 5: Commit** — "board: seats, drive locks and atomic drive launch claims (schema 25)".

---

**Additions from the design judge:**
- [ ] **[F20]** `drive_locks` gains a `path` column. Create `lib/swarm/supervisor/worktree.py` with `worktree_root(path: str) -> str | None` (walk up from `os.path.realpath(path)` to the first directory containing a `.git` file or directory, without running git). `claim_drive_launch` refuses a `wt:` lock whose `path` equals, is an ancestor of, or is a descendant of any live `wt:` row's `path` (`a == b or a.startswith(b + "/") or b.startswith(a + "/")`), inside the transaction. Test:

```python
def test_nested_worktrees_conflict(self):
    b = self.board(); self.goal_job(b, "J")
    a, c = self.seat(b, "J", "a"), self.seat(b, "J", "c")
    self.assertIsNotNone(self.claim(b, a, locks=["wt:" + h("/w/repo")], paths={"wt:" + h("/w/repo"): "/w/repo"}))
    self.assertIsNone(self.claim(b, c, locks=["wt:" + h("/w/repo/sub")], paths={"wt:" + h("/w/repo/sub"): "/w/repo/sub"}))
```

  `claim_drive_launch` therefore takes `lock_paths: dict[str, str]` in addition to `locks`.

### Task 4: Host-private ledger and receipts

Seat: eng-ledger (haiku). Security reviewer reviews. Spec §4.0, §4.5.

**Files:**
- Create: `lib/swarm/supervisor/ledger.py`
- Create: `lib/swarm/receipts.py`
- Test: `tests/test_drive_ledger.py`, `tests/test_receipts.py`

**Interfaces:**
- Produces:
  - `ledger.Intent(launch_id: str, action_id: str, attempt: int, seat: str | None, reserved_usd: float, reserved_minutes: float, locks: list[str], state: str)` with states `claiming|claimed|concluded`, plus `outcome: str | None`, `success: bool | None`, `charged_usd: float | None`.
  - `ledger.Ledger` fields: `board_key, job, generation, generation_requested, activated_at, budgets: dict, intents: list[Intent], owners: dict[str, str]` (group → seat), `integrated: dict[str, dict]`, `ruling_due_at: str | None`, `ruling_attempts: int`, `rounds_without_progress: int`, `errors: int`, `closing: str | None`, `halted: dict | None`, `terminal: dict | None`, `mirror_fetch_pending: list[str]`, `last_pass: str | None`, `watermarks: dict[str, int]`.
  - `ledger.load(board_key, job) -> Ledger | None`; `ledger.create(board_key, job, *, budgets, activated_at) -> Ledger`; `Ledger.save()` (privfs atomic write + fsync); `ledger.unfinished(board_key) -> list[Ledger]` (terminal is None); `Ledger.charged_usd() -> float` (concluded charged + open reserved).
  - `receipts.Receipt(job, generation, action_id, attempt, seat, kind, record_id, args_sha)`; kinds: `done, review, verdict, goal_ready, integrated, seat_request, post, ask, answer, blocker_resolution, person`.
  - `receipts.write(board_key, r: Receipt) -> Path`; `receipts.find(board_key, job, kind, record_id) -> Receipt | None`; `receipts.args_sha(argv: list[str]) -> str` (sha256 of `"\0".join(argv)`).
  - Directory: ledgers in `~/.local/share/swarm/supervisor/drive/` (via `supervisor.privfs`), receipts in `~/.local/share/swarm/host/enrolled/` next to enrolment records (via `safefs`, same validation style as `enrolment._validate`).

- [ ] **Step 1: Failing tests**

```python
class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp(dir=support.SANDBOX)); self.env = mock.patch.dict(os.environ, support.home_env(self.home)); self.env.start()
    def test_roundtrip_and_charges(self):
        from swarm.supervisor import ledger
        l = ledger.create("bk", "J", budgets={"max_cost_usd": 40}, activated_at="2026-10-10T00:00:00+00:00")
        l.intents.append(ledger.Intent("L1", "1|seat|a", 1, "a", 5.0, 60.0, [], "claimed"))
        l.save()
        again = ledger.load("bk", "J")
        self.assertEqual(again.charged_usd(), 5.0)
        self.assertEqual([x.job for x in ledger.unfinished("bk")], ["J"])
    def test_planted_symlink_refused(self):
        from swarm.supervisor import ledger, settings
        d = settings.private_dir() / "drive"; d.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        os.symlink("/etc", d)
        with self.assertRaises(settings.PrivateDirError):
            ledger.create("bk", "J", budgets={}, activated_at="x")

class ReceiptTests(unittest.TestCase):
    def test_receipt_binds_args(self):
        from swarm import receipts
        r = receipts.Receipt("J", 1, "1|seat|a", 1, "a", "done", "42", receipts.args_sha(["done", "--artifact", "b@" + "a"*40]))
        receipts.write("bk", r)
        self.assertEqual(receipts.find("bk", "J", "done", "42"), r)
        self.assertIsNone(receipts.find("bk", "J", "done", "43"))
```

(`support.SANDBOX` is the suite sandbox `support.py` already creates; tests never hard-code /tmp paths.)
- [ ] **Step 2: Run** `cd tests && /var/tmp/test-slots/test-slot -- python3 -B -m unittest test_drive_ledger test_receipts` → FAIL.
- [ ] **Step 3: Implement** with `privfs.dir_fd()`/`privfs.write_atomic` for the ledger (subdir `drive`, file `<sha256(board_key|job)>.json`, JSON with `"v": 1`), dataclasses with `asdict`, strict field validation on load (unknown/malformed → `None`, logged). Receipts mirror `enrolment._store/_load` (file `receipt-<sha256(board_key|job|kind|record_id)>.json`).
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** — "supervisor: host-private drive ledger and control-record receipts".

---

**Additions from the design judge:**
- [ ] **[F2]** `ledger.unfinished(board_key)` enumerates ledger **files by name**: a file that fails to parse yields `LedgerBad(job_hint, path, why)` instead of being skipped; `ledger.load` raises `LedgerBadError` rather than returning None for an existing-but-bad file. Test: write garbage to a ledger file → `unfinished()` returns a `LedgerBad`.
- [ ] **[F6]** fields `terminal_intent: dict | None`, `halted_total_seconds: float`, `requester_session: str | None`, `pinned_requirements: dict[str, str]` (path → blob id).
- [ ] **[F7]** writes are `write → fsync(file) → rename → fsync(dir)`; test with `mock.patch("os.fsync")` asserting two calls per save (file and directory).
- [ ] Receipt kinds add `ack, withdraw, land` (land receipts carry `destinations: dict[url, {"state": "intent"|"pushed"|"verified", "sha": str}]`).

### Task 5: Hooks — receipts, seat authorizations, person path, guardrails, kicks

Seat: eng-hooks (haiku). Security reviewer reviews. Spec §3.4, §3.11, §4.0, §4.4.

**Files:**
- Modify: `lib/swarm/hooks.py` (PostToolUse and PreToolUse handlers for drive jobs)
- Create: `lib/swarm/person.py`
- Modify: `lib/swarm/shellguard.py` (target-push and agent-launch patterns)
- Test: `tests/test_drive_hooks.py`

**Interfaces:**
- Consumes: `receipts.write/find/args_sha` (Task 4); `Board.seat`, `Board.request_seat` (Task 3); `enrolment.find` (existing).
- Produces:
  - `hooks.drive_post_tool(board, cfg, payload) -> None`: for a Bash tool call whose parsed argv is `swarm done|review|verdict|seat request|post --to|ask|answer` (parsed with `cli.build_parser()`), exit status 0, session enrolled as a seat of a drive job → write a `Receipt` (kind by subcommand, `record_id` = the id from the CLI's last line in the one record format of Global Constraints), then `kick()`.
  - `hooks.kick(now=time.time) -> bool`: `systemctl --user start --no-block swarm-supervise.service` with `systemd.manager_env()`, at most once per 20 s (stamp `kick.stamp` in the private dir).
  - `person.require(action: str, job: str | None) -> str` → returns `"person"` or raises `PermissionError`: refused if the calling session id has an enrolment record; otherwise prompts on `/dev/tty` `Type the job name to confirm <action>:` and compares.
  - PreToolUse refusals (drive jobs only): judges/reviewers: `git commit`, `git push`, file-writing tools; everyone: `git push` whose refspec targets the recipe's target branch, any `--force`/`-f`/`+refspec`, `claude -p`, `claude --print`, `codex exec`. Message points to `swarm land` / `swarm seat request`.

- [ ] **Step 1: Failing tests** (built on `test_hooks_cli.Env`)

```python
class DriveHooks(Env):
    def test_done_from_seat_session_writes_receipt(self):
        self.drive_job("J"); key = self.launch_seat("J", "eng-a")
        out = self.run_cli_as(key, "done", "--job", "J", "--artifact", "feat/a@" + "a"*40, "--summary", "x")
        rid = re.fullmatch(r"swarm-record: kind=([a-z_]+) id=(\d+) rev=(\d+)", out.strip().splitlines()[-1]).group(2)
        self.hook("post", agent_id=key, tool_name="Bash",
                  tool_input={"command": f"swarm done --job J --artifact feat/a@{'a'*40} --summary x"},
                  tool_response={"exit_code": 0, "stdout": out})
        self.assertIsNotNone(receipts.find(self.board_key, "J", "done", rid))

    def test_seat_request_from_non_lead_gets_no_authorization(self):
        self.drive_job("J"); key = self.launch_seat("J", "eng-a", role="engineer")
        self.hook("post", agent_id=key, tool_name="Bash",
                  tool_input={"command": "swarm seat request --job J --seat eng-b --role engineer --brief B.md"},
                  tool_response={"exit_code": 0, "stdout": "swarm-record: kind=seat id=7 rev=1"})
        self.assertIsNone(receipts.find(self.board_key, "J", "seat_request", "7"))

    def test_row_swapped_before_hook_is_not_authorized(self):
        self.drive_job("J"); lead = self.launch_seat("J", "lead", role="engineering_lead")
        argv = "swarm seat request --job J --seat eng-b --role engineer --brief B.md --workdir /w/b"
        self.hook("post", agent_id=lead, tool_name="Bash", tool_input={"command": argv},
                  tool_response={"exit_code": 0, "stdout": "swarm-record: kind=seat id=7 rev=1"})
        with self.board() as b:
            b.request_seat("J", 1, "eng-b", role="engineer", harness="claude", model="opus",
                           workdir="/w/other", brief_path="B.md", requested_by="lead")   # forged change
        self.assertFalse(drive_authz.seat_request_authorized(self.board_key, self.board(), "J", 1, "eng-b"))

    def test_push_to_target_refused(self):
        self.drive_job("J"); key = self.launch_seat("J", "eng-a")
        r = self.hook("pre", agent_id=key, tool_name="Bash", tool_input={"command": "git push origin HEAD:main"})
        self.assertIn("swarm land", r["reason"])

    def test_enrolled_session_cannot_take_person_path(self):
        with self.assertRaises(PermissionError):
            person.require("unhalt", "J")   # session env of an enrolled seat set by the fixture

    def test_kick_debounced(self):
        calls = []
        with mock.patch("subprocess.run", side_effect=lambda *a, **k: calls.append(a) or mock.Mock(returncode=0)):
            self.assertTrue(hooks.kick(now=lambda: 100.0)); self.assertFalse(hooks.kick(now=lambda: 110.0))
        self.assertEqual(len(calls), 1)
```

`drive_authz.seat_request_authorized(board_key, board, job, generation, seat) -> bool` lives in `lib/swarm/supervisor/drive_authz.py` (create it here): true iff a `seat_request` receipt exists for the seat's row id whose `args_sha` equals `receipts.args_sha(canonical_seat_args(seat_row))` and whose recorded revision equals the row's revision.
- [ ] **Step 2: Run** `cd tests && /var/tmp/test-slots/test-slot -- python3 -B -m unittest test_drive_hooks` → FAIL.
- [ ] **Step 3: Implement.** CLI subcommands touched print the one record format of Global Constraints as their last stdout line (Task 6 prints it, this task parses it; no other format). `canonical_seat_args(seat) -> list[str]` returns the argv the CLI would have needed to produce those fields (fixed order). Guardrail patterns live in `shellguard.py` next to the existing `gh run watch` refusals.
- [ ] **Step 4: Run** step 2 + `test_hooks_cli test_shellguard test_spawn` → PASS.
- [ ] **Step 5: Commit** — "hooks: drive receipts, seat authorization, person path, guardrails and supervisor kicks".

---

**Additions from the design judge:**
- [ ] Parse the one record format from Global Constraints (no other format).
- [ ] **[F17]** The requester session (owner record `requester_session`) gets receipts for `answer`, `deactivate`, `halt`, `unhalt`, `activate`, `seat request`; any other session's `deactivate` of a drive job gets no receipt (the driver treats the board change as tamper). Test `test_requester_answer_is_receipted` and `test_foreign_deactivate_not_receipted`.
- [ ] **[A]/[F17]** SubagentStart refuses Agent-tool spawns by the requester session and by the lead seat in a drive job, pointing to `swarm seat request`. Test `test_requester_cannot_spawn_subagents_in_drive_job`.
- [ ] **[F18]** Codex hooks write receipts exactly like Claude's (`test_codex_done_receipted` via `test_codex_hooks.Env`).
- [ ] **[F21]** A failed receipt write is logged to the private dir (`receipt-failures.log`); doctor FAILs on an entry younger than 24 h (`test_doctor_fails_on_receipt_write_failure`). Barrier `hook_after_cli_before_receipt` sits between parsing and `receipts.write`.

### Task 6: CLI — `activate --drive`, `swarm seat`, `swarm halt|unhalt`, `swarm review`

Seat: eng-cli (haiku). Spec §3.1, §3.4, §3.6, §4.3.

**Files:**
- Modify: `lib/swarm/cli.py` (parsers + handlers), `lib/swarm/enrolment.py` (`write_job_owner(..., roots: list[str], remotes: dict[str, str])`)
- Create: `lib/swarm/drive_cli.py` (handlers, keeps `cli.py` from growing)
- Test: `tests/test_drive_cli.py`

**Interfaces:**
- Consumes: Tasks 2–5.
- Produces commands:
  - `swarm activate --job J --goal G [--drive (accepted no-op)] [--lead-brief PATH] [--workdir DIR] [--recipe coding|artifact] [--allow-workdir DIR]... [--remote NAME=URL]... [--lead-harness claude|codex] [--max-cost-usd N] [--ruling-reserve-usd N] [--budget KEY=VALUE]...` → job + owner record (roots, pinned remotes: default = `git -C DIR remote -v` read once, sanitized env) + `ledger.create` + `drive.enabled=1`, `drive.generation_requested` + lead seat request with driver-authored authorization + `hooks.kick()`. Re-activation of an existing drive job writes `generation_requested = generation + 1` only (Task 8 rolls over). Person path (`person.require`) when re-activating.
  - `swarm seat request|list|show|release|cancel` (flags as spec §3.4); last stdout line `swarm-record: kind=seat id=<n> rev=<r>` (Global Constraints).
  - `swarm review --job J --artifact REF verified|changes --report PATH` (structured review result; posts `REVIEW <artifact> VERIFIED|CHANGES <path>`).
  - `swarm halt [--job J] [--reason T]` / `swarm unhalt [--job J]` (person path; job-level: ledger `halted`, `runner` stop of the job's scopes; global: private `supervise.off`).
  - `swarm ask` in a drive job: `--default` required; `--expires` default `[drive] question_expiry`; `--blocks` default = asking seat (core `seat` column).

- [ ] **Step 1: Failing tests**

```python
class DriveCli(CliEnv):
    def test_activate_drive_creates_ledger_and_lead_seat(self):
        rc, out, _ = self.cli("activate", "--job", "J", "--goal", "g", "--drive",
                              "--lead-brief", str(self.work / "LEAD.md"), "--workdir", str(self.work))
        self.assertEqual(rc, 0)
        self.assertIsNotNone(ledger.load(self.board_key, "J"))
        with self.board() as b:
            lead = b.seat("J", 1, "lead")
        self.assertEqual((lead.role, lead.model, lead.state), ("engineering_lead", "opus", "requested"))

    def test_seat_request_outside_job_roots_refused(self):
        self.activate_drive("J", workdir=self.work)
        rc, _, err = self.cli_as_lead("seat", "request", "--job", "J", "--seat", "e", "--role", "engineer",
                                      "--brief", "B.md", "--workdir", str(self.other_repo))
        self.assertNotEqual(rc, 0); self.assertIn("outside the job's work roots", err)

    def test_seat_request_under_tmp_refused(self):
        self.activate_drive("J", workdir=self.work, allow=["/tmp/x"])
        rc, _, err = self.cli_as_lead("seat", "request", "--job", "J", "--seat", "e", "--role", "engineer",
                                      "--brief", "B.md", "--workdir", "/tmp/x")
        self.assertIn("never /tmp", err)

    def test_ask_in_drive_job_requires_default(self):
        self.activate_drive("J", workdir=self.work)
        rc, _, err = self.cli_as_seat("eng-a", "ask", "--job", "J", "--to", "human", "Which DB?")
        self.assertNotEqual(rc, 0); self.assertIn("--default", err)

    def test_halt_refused_for_enrolled_agent(self):
        self.activate_drive("J", workdir=self.work)
        rc, _, err = self.cli_as_seat("eng-a", "halt", "--job", "J")
        self.assertNotEqual(rc, 0)
```

- [ ] **Step 2–4:** run `cd tests && /var/tmp/test-slots/test-slot -- python3 -B -m unittest test_drive_cli test_ask_answer test_goals` → FAIL, implement, PASS. Model resolution: `models.model_for(cfg, harness, role)` then clamp (`engineering_lead`, `judge` → `[drive] lead_model`/`judge_model`, default `"opus"`), `--complexity simple` → `"haiku"` for engineer/qa seats, `--model` honoured only if in `[drive] allowed_models` (default `["haiku","sonnet","opus"]`).
- [ ] **Step 5: Commit** — "cli: activate --drive, swarm seat, swarm review, halt/unhalt".

---

**Additions from the design judge and the PM ruling:**
- [ ] **[A]** `--goal` implies drive; `--no-drive --reason TEXT` (refused without `--reason`); `--lead-brief` optional (generated at `<workdir>/.swarm/lead-brief-<job>.md`); `--workdir` defaults to cwd when under `allowed_workdirs` and not under /tmp, else error. Tests: `test_goal_implies_drive`, `test_no_drive_requires_reason`, `test_no_drive_shown_not_autonomous`, `test_generated_lead_brief`, `test_workdir_default_refused_under_tmp`.
- [ ] **[A]** `swarm ask` in any goal job requires `--default`.
- [ ] **[F10]** `--requirements PATH` (repeatable) pins blob ids in the owner record and ledger; `GOAL-READY` (`swarm goal-ready --job J --target REF@SHA [--add-requirements PATH]...`) cannot drop pinned files. Test `test_goal_ready_cannot_replace_pinned_requirements`.
- [ ] **[F9]** `swarm seat release --withdraw-artifact GROUP --reason TEXT`. **[F4]** `swarm seat ack --upto ID`.
- [ ] **[F2]** `swarm drive repair --job J` (person path) rebuilds a bad ledger from the board plus receipts; `swarm job move` refuses drive jobs unless `--with-ledger`.
- [ ] **[F2]** Activation order: ledger created and fsynced before the board row (test with a crash between them: the next pass sees a ledger without a board row and creates the row; never the reverse).
- [ ] **[F22]** Status shows `driving: <next action or running seats>` (`test_status_driving_line`); per-seat bounded agent `swarm wait` (`test_agent_wait_is_per_seat_and_bounded`).

### Task 7a: Freeze the driver model (Snapshot, Action, fixtures)

Seat: eng-driver (haiku). Reviewed by EL. Lands before wave 4 so Tasks 7, 8a, 8b, 8c build on one interface.

**Files:**
- Create: `lib/swarm/supervisor/driver_model.py` (dataclasses only: `Snapshot`, `Action`, `SeatView`, `Question`, `HandoffView`, `VerdictView`; no logic)
- Create: `tests/drive_fixtures.py` (factories `seat`, `q`, `ho`, `v`, `L`, `iso`, `minutes`, `snap` used by Tasks 7–8c)
- Test: `tests/test_drive_model.py`

**Interfaces:**
- Produces the exact field lists given in Task 7's Interfaces (moved here), frozen dataclasses with `__slots__`, plus `Action.kind ∈ {"launch","record_unreachable","close","hold","nothing","rollover","stop_seat","complete_land"}` and `Action.not_before: datetime | None`, `Action.responsible: str | None`.

- [ ] **Step 1:** test that every factory builds a valid frozen object and that `dataclasses.replace` works (`test_snapshot_is_frozen`, `test_factories_cover_every_field`).
- [ ] **Step 2–4:** run `cd tests && /var/tmp/test-slots/test-slot -- python3 -B -m unittest test_drive_model` → FAIL, implement, PASS.
- [ ] **Step 5:** commit "driver: frozen snapshot and action model with test fixtures"; post the interface on the board for eng-driver2..4.

### Task 7: Driver core — snapshot and pure `next_actions`

Seat: eng-driver (haiku). Reviewed by EL. Spec §3.2, §3.3, §3.5–§3.7, §3.9, §4.1.

**Files:**
- Modify: `lib/swarm/supervisor/driver_model.py` (Task 7a's frozen dataclasses; this task adds the pure `next_actions`)
- Create: `lib/swarm/supervisor/driver_snapshot.py` (reads board + ledger + receipts into the model)
- Test: `tests/test_drive_next_actions.py` (table tests, no board), `tests/test_drive_snapshot.py`

**Interfaces:**
- Consumes: `Ledger` (Task 4), `Seat` (Task 3), receipts (Task 4), `review.latest_handoffs/artifact_verdicts` (existing).
- Produces:
  - `Snapshot(job, generation, goal, status, halted, ledger, seats: dict[str, Seat], live: set[str] (seat names with a live attempt), handoffs: list[Handoff] (receipted only), reviews: dict[artifact, str], verdicts: dict[artifact, dict] (receipted), goal_verdict: dict | None, goal_ready: dict | None, questions: dict[seat, list] (open, receipted), wakes: dict[seat, list[int]] (pending event ids above watermark), integrated: dict[group, dict], now)`.
  - `Action(kind, action_id, seat, prompt_kind, artifact=None, ruling=False, reason="")` with `kind ∈ {"launch","record_unreachable","close","hold","nothing","rollover","stop_seat"}`.
  - `next_actions(s: Snapshot, cfg: dict) -> list[Action]` implementing spec §3.3 order 1–10 and the single progress counter; never returns `[]` for an active unhalted non-terminal job (returns `hold` with `not_before` or `launch` or `nothing(responsible=...)`).
  - `progress(prev: VerdictView | None, new: VerdictView) -> bool` (spec §3.6: new artifact AND `open` decreased, or `new.progress is True`, or integration). PM ruling: the board stores `progress` as a bool (the CLI's `--progress yes|no` maps to True/False); `VerdictView.progress` is `bool | None`.

- [ ] **Step 1: Failing table tests** (one per branch; examples)

```python
def snap(**kw):
    base = dict(job="J", generation=1, goal="g", status="active", halted=False, ledger=L(), seats={},
                live=set(), handoffs=[], reviews={}, verdicts={}, goal_verdict=None, goal_ready=None,
                questions={}, wakes={}, integrated={}, now=T0)
    base.update(kw); return Snapshot(**base)

class NextActions(unittest.TestCase):
    def test_requested_lead_is_launched(self):
        a = next_actions(snap(seats={"lead": seat("lead", "requested", role="engineering_lead")}), CFG)
        self.assertEqual([(x.kind, x.seat) for x in a], [("launch", "lead")])

    def test_question_blocks_only_its_seat(self):
        s = snap(seats={"a": seat("a", "idle"), "b": seat("b", "requested")},
                 questions={"a": [q(1)]}, wakes={"a": [5]})
        self.assertEqual([x.seat for x in next_actions(s, CFG) if x.kind == "launch"], ["b"])

    def test_nothing_running_wakes_lead_and_counts_round(self):
        s = snap(seats={"lead": seat("lead", "idle", role="engineering_lead")})
        (a,) = next_actions(s, CFG)
        self.assertEqual((a.kind, a.seat, a.reason), ("launch", "lead", "invariant-fallback"))

    def test_three_rounds_without_progress_make_ruling_due(self):
        s = snap(ledger=L(rounds_without_progress=3), seats={"lead": seat("lead", "idle", role="engineering_lead")})
        a = next_actions(s, CFG)
        self.assertTrue(any(x.ruling for x in a))

    def test_ruling_quiesces_live_seats_first(self):
        s = snap(ledger=L(ruling_due_at=iso(T0)), live={"a"}, seats={"a": seat("a", "running")})
        self.assertEqual([x.kind for x in next_actions(s, CFG)], ["hold"])           # within grace
        s2 = snap(ledger=L(ruling_due_at=iso(T0 - minutes(16))), live={"a"}, seats={"a": seat("a", "running")})
        self.assertEqual([x.kind for x in next_actions(s2, CFG)], ["stop_seat"])

    def test_budget_exhausted_ruling_uses_reserve(self):
        s = snap(ledger=L(spent_usd=35.5), seats={"a": seat("a", "requested")})
        a = next_actions(s, CFG)            # 40 - 35.5 < 5 reserve + 0.5 min → no seat launch
        self.assertTrue(all(x.ruling or x.kind != "launch" for x in a))

    def test_not_met_resumes_owner_seat(self):
        s = snap(seats={"a": seat("a", "idle")}, ledger=L(owners={"feat/a": "a"}),
                 handoffs=[ho("feat/a@" + "a"*40, by="a")], reviews={"feat/a@" + "a"*40: "verified"},
                 verdicts={"feat/a@" + "a"*40: v("not_met", open=2)})
        (a,) = next_actions(s, CFG)
        self.assertEqual((a.kind, a.seat, a.prompt_kind), ("launch", "a", "fix"))

    def test_released_owner_gets_successor(self):
        s = snap(seats={"a": seat("a", "released")}, ledger=L(owners={"feat/a": "a"}),
                 handoffs=[ho("feat/a@" + "a"*40, by="a")], verdicts={"feat/a@" + "a"*40: v("not_met", open=1)},
                 reviews={"feat/a@" + "a"*40: "verified"})
        (a,) = next_actions(s, CFG)
        self.assertEqual(a.seat, "a~1")

    def test_goal_ready_and_all_integrated_launches_goal_judge(self): ...
    def test_unreceipted_verdict_is_ignored(self): ...
    def test_halted_job_does_nothing(self): ...
    def test_terminal_verdict_closes(self): ...
```

(Write every listed test fully; `seat`, `q`, `ho`, `v`, `L`, `iso`, `minutes` are small factories at the top of the test module.)
- [ ] **Step 2: Run** `cd tests && /var/tmp/test-slots/test-slot -- python3 -B -m unittest test_drive_next_actions` → FAIL.
- [ ] **Step 3: Implement** `next_actions` as a sequence of small pure functions, one per §3.3 step (`_terminal`, `_ruling`, `_pipeline`, `_seat_requests`, `_seat_wakes`, `_stalls`, `_lead_wake`, `_goal_judgment`, `_fallback`), each returning `list[Action] | None`; the first non-None wins except `_seat_requests`/`_seat_wakes`, which accumulate within the host cap. Action ids: `f"{gen}|{kind}|{seat}|{group or '-'}|{round}|{event or '-'}"`.
- [ ] **Step 4: Run** → PASS; then `test_drive_snapshot` (snapshot reads on the memory backend: receipted vs unreceipted records, generation filtering).
- [ ] **Step 5: Commit** — "driver: snapshot and pure next_actions".

---

**Additions from the design judge** (tests in `test_drive_next_actions.py`, each written fully):
- [ ] **[F1]** `test_gated_lead_holds_until_question_expiry`: only a lead gated by an open question → one `hold` with `not_before == until`; no lead wake; no round counted.
- [ ] **[F3]** `test_failed_lead_gets_one_successor_then_ruling`.
- [ ] **[F9]** `test_withdrawn_group_does_not_block_goal_judge`.
- [ ] **[F11]** `test_finalize_blocked_cycle_counts_no_progress`, `test_max_seats_refusal_wake_counts_no_progress`, `test_nothing_else_to_do_definition`.
- [ ] **[F12]** `test_outage_attempt_not_counted`.
- [ ] **[F15]** `test_question_to_role_wakes_that_seat`.
- [ ] **[F19]** `test_land_lease_hold_over_limit_makes_ruling_due`.
- [ ] **[F22]** `test_driver_error_passes_make_ruling_due`, `test_stall_minutes_wakes_lead`, `test_max_seats_refused`.

### Task 8a: Driver execution — claims and reconciliation

Seat: eng-driver2 (haiku). Reviewed by EL and the security reviewer (opus). Spec §3.2, §3.9, §4.5 incl. [F7].

Scope note: this task owns `driver.execute`, `driver.reconcile` and the drive-run reaping; `enforce_deadlines`, `terminalize`, `rollover` move to Task 8b and tamper/`_pass`/prompts to Task 8c. The Interfaces block below is shared; the tests listed are split by owner as marked: 8a owns `test_claim_without_run_file_is_concluded_after_grace`, `test_forged_restart_row_ignored`, `test_prose_done_is_not_a_handoff`; 8b owns `test_deadline_records_mechanical_unreachable_without_snapshot`, `test_rollover_stops_old_generation_first`, `test_terminalize_settles_questions_and_closes`; 8c owns `test_tamper_closed_job_is_reopened`, `test_broken_job_does_not_starve_next`.

**Files:**
- Create: `lib/swarm/supervisor/driver.py`
- Create: `lib/swarm/supervisor/prompts.py` (seat, lead, fix, review, judge, ruling, goal-judge, integrator prompt templates; untrusted fencing reused from `brief.py`)
- Modify: `lib/swarm/supervisor/command.py:_pass` (driver first; `pipeline.run`, `orphans.run`, `candidates` skip drive jobs; housekeeping deadlines)
- Modify: `lib/swarm/supervisor/orphans.py`, `stuck.py` (drive jobs: per-seat question gate)
- Test: `tests/test_drive_driver.py`

**Interfaces:**
- Consumes: Tasks 3, 4, 6, 7; `launch.spec_for` (Task 9 signature), `runner.start`, `markers.write_resume_marker`.
- Produces:
  - `driver.enforce_deadlines(board, cfg, led, now) -> bool` (ledger + job row only; records mechanical UNREACHABLE via `board.record_verdict(job, "swarm supervisor", "unreachable", reason, details=..., scope="ruling", generation=g)` then sets `led.closing`).
  - `driver.reconcile(board, cfg, led, now, deadline) -> None` (spec §4.5 incl. claim without run file → concluded failed).
  - `driver.execute(board, cfg, sup, led, action, *, start_runner, now) -> dict | None` (steps 1a–4 of §4.5; returns the run dict).
  - `driver.terminalize(board, cfg, led, now) -> bool` (six steps; resumable).
  - `driver.rollover(board, cfg, led, now) -> bool` (four steps; resumable).
  - `driver.drive(board, cfg, sup, state, *, now, say, start_runner, which, scope_ok, config_path, deadline) -> set[str]` (returns drive job names so other passes skip them).
  - `driver.unit_gone(unit) -> bool` = `runner.unit_presence(unit) == "absent"` (Task 9).

- [ ] **Step 1: Failing tests** (memory backend, fake `start_runner`, `support.ManualClock`)

```python
class DriverTests(DriveEnv):
    def test_claim_without_run_file_is_concluded_after_grace(self):
        led = self.ledger_with_intent(state="claiming")
        self.board.claim_drive_launch(**self.claim_args(launch_id=led.intents[0].launch_id))
        self.clock.advance(91)
        driver.reconcile(self.board, self.cfg, led, self.now(), deadline=self.mono() + 20)
        self.assertEqual(led.intents[0].state, "concluded"); self.assertFalse(led.intents[0].success)
        self.assertEqual(self.board.restarts(job="J")[-1].outcome, "failed")

    def test_forged_restart_row_ignored(self):
        self.board.claim_drive_launch(**self.claim_args(launch_id="FORGED"))
        led = self.ledger()
        driver.reconcile(self.board, self.cfg, led, self.now(), deadline=self.mono() + 20)
        self.assertEqual(led.charged_usd(), 0.0)

    def test_deadline_records_mechanical_unreachable_without_snapshot(self):
        led = self.ledger(ruling_due_at=iso(self.now() - minutes(121)))
        with mock.patch("swarm.supervisor.driver_snapshot.read", side_effect=RuntimeError):
            self.assertTrue(driver.enforce_deadlines(self.board, self.cfg, led, self.now()))
        js = self.board.job_status("J")
        self.assertEqual((js.verdict, js.verdict_scope), ("unreachable", "ruling"))

    def test_rollover_stops_old_generation_first(self):
        self.launch("eng-a"); self.request_generation(2)
        self.tick()
        self.assertEqual(self.stopped_units, [self.unit_of("eng-a")]); self.assertEqual(self.ledger().generation, 1)
        self.units_absent(); self.tick()
        self.assertEqual(self.ledger().generation, 2)
        self.assertEqual(self.board.seat("J", 2, "lead").state, "running")

    def test_prose_done_is_not_a_handoff(self):
        self.launch("eng-a"); self.board.post("J", self.name_of("eng-a"), "DONE: all good")
        self.finish("eng-a", "completed"); self.tick()
        self.assertEqual(self.last_launch()["seat"], "eng-a")             # retried with the nudge
        self.assertIn("ended without a hand-off", self.last_launch()["stdin"])

    def test_terminalize_settles_questions_and_closes(self):
        self.question("eng-a"); self.goal_met_receipted(); self.tick(); self.units_absent(); self.tick()
        self.assertEqual(self.board.job_status("J").status, "completed")
        self.assertEqual(self.board.blockers("J", include_closed=True)[0].state, "expired")

    def test_tamper_closed_job_is_reopened(self):
        self.board.close_job("J", "cancelled", "forged", forced=True)
        self.tick()
        self.assertEqual(self.board.job_status("J").status, "active")
        self.assertTrue(any("DRIVER-TAMPER" in m.message for m in self.board.recent_messages(20, job="J")))

    def test_broken_job_does_not_starve_next(self):
        self.second_drive_job("K")
        with mock.patch("swarm.supervisor.driver_snapshot.read", side_effect=lambda b, l, j, **k: (_ for _ in ()).throw(RuntimeError) if j == "J" else REAL(b, l, j, **k)):
            self.tick()
        self.assertTrue(any(r["job"] == "K" for r in self.launched))
```

- [ ] **Step 2: Run** `cd tests && /var/tmp/test-slots/test-slot -- python3 -B -m unittest test_drive_driver` → FAIL.
- [ ] **Step 3: Implement** following spec §4.5 literally (write-ahead intent with fsync before `claim_drive_launch`; run file then marker then kill-switch recheck then `runner.start`). Per-job `try/except` with `deadline = min(pass_deadline, mono() + 30)`; jobs ordered by `led.last_pass`. `_pass` order: `driver.drive(...)` first, then existing steps with `runner.reap(cfg, board, deadline=mono()+20)`.
- [ ] **Step 4: Run** step 2 + `test_supervise_pipeline test_supervise_orphans test_supervise_command test_supervise_stuck` → PASS (non-drive behaviour unchanged).
- [ ] **Step 5: Commit** — "driver: claim protocol, reconciliation, deadlines, rollover, terminalization".

---

**Additions from the design judge ([F7]):**
- [ ] `test_lost_race_intent_concluded_lost_uncounted`.
- [ ] `test_deleted_board_row_with_live_lock_is_not_restarted` (run file + held flock + present unit → live, whatever the board says).
- [ ] `test_driver_reaps_drive_runs` (the existing `runner.reap` is called for drive intents by the driver).

### Task 8b: Driver deadlines, terminalization, rollover

Seat: eng-driver3 (haiku). Reviewed by EL and the security reviewer (opus). Spec §3.1 [F2][F6], §3.2, §4.3 [F5].

**Files:** Modify `lib/swarm/supervisor/driver.py` (functions below); Test `tests/test_drive_lifecycle.py`.

**Interfaces:** Consumes Task 8a's `execute`/`reconcile`; produces `enforce_deadlines`, `terminalize`, `rollover` with the signatures in Task 8a's Interfaces block, plus `driver.halt(board, cfg, led, actor, now)` / `driver.unhalt(...)` implementing [F5].

- [ ] **Step 1: Failing tests** (written fully, using `tests/drive_fixtures.py` and `support.ManualClock`): the three 8b tests named in Task 8a's scope note, plus
  `test_terminal_intent_fsynced_before_board_verdict` (crash barrier between them: next pass completes the close from the ledger),
  `test_mechanical_verdict_recognised_from_ledger_not_receipts`,
  `test_enforce_deadlines_ignores_board_verdict_column` (a forged board `met` does not stop the deadline),
  `test_bad_ledger_holds_and_posts_driver_ledger_bad`, `test_board_drive_job_without_ledger_posts_driver_orphan`,
  `test_halt_idles_seats_and_shifts_deadlines`, `test_halt_cancelled_attempts_not_counted`,
  `test_rollover_writes_ledger_before_board` (barrier `mid_rollover_after_ledger`).
- [ ] **Step 2–4:** `cd tests && /var/tmp/test-slots/test-slot -- python3 -B -m unittest test_drive_lifecycle` → FAIL, implement, PASS.
- [ ] **Step 5:** commit "driver: deadlines, ledger-first terminalization, rollover and halt".

### Task 8c: Driver tamper handling, pass integration, prompts

Seat: eng-driver4 (haiku). Reviewed by EL. Spec §3.2 (tamper), §3.5, §3.13, [F17].

**Files:** Modify `lib/swarm/supervisor/driver.py` (`drive`, tamper), Create `lib/swarm/supervisor/prompts.py`, Modify `lib/swarm/supervisor/command.py:_pass`, `orphans.py`, `stuck.py`, `pipeline.py` (skip ledger drive jobs); Test `tests/test_drive_pass.py`.

- [ ] **Step 1: Failing tests:** the two 8c tests named in Task 8a's scope note, plus `test_requester_deactivate_is_cancel_not_tamper`, `test_foreign_deactivate_is_tamper_and_halts_with_deadline_shift`, `test_other_passes_skip_jobs_with_a_ledger`, `test_pass_order_driver_first_with_bounded_reap`, `test_prompts_fence_untrusted_text` (a post containing `END untrusted` cannot close the fence), `test_lead_prompt_lists_pending_wake_ids_and_watermark`.
- [ ] **Step 2–4:** `cd tests && /var/tmp/test-slots/test-slot -- python3 -B -m unittest test_drive_pass test_supervise_command test_supervise_orphans test_supervise_pipeline` → FAIL, implement, PASS.
- [ ] **Step 5:** commit "driver: tamper handling, pass integration and seat prompts".

### Task 9: Runner and launch — one launch contract, budgets, usage, containment, clock, barriers

Seat: eng-runner (haiku). Spec §3.9, §4.1, §6.2. Task 9a (clock + barriers) lands in wave 1, the rest in wave 2.

**Files:**
- Create: `lib/swarm/clock.py`, `lib/swarm/supervisor/barrier.py`
- Modify: `lib/swarm/supervisor/launch.py` (`spec_for(..., budget_usd=None, read_only=False, resume=None)`)
- Modify: `lib/swarm/supervisor/runner.py` (`unit_presence`, `parse_usage`, lease renewal, finish with usage)
- Modify: `lib/swarm/hosts/resume.py` (return a resume handle only for drive seats)
- Test: `tests/test_drive_runner.py`, `tests/test_supervise_launch.py`

**Interfaces:**
- Produces:
  - `clock.now() -> datetime` (aware UTC), `clock.monotonic() -> float`, `clock.use(fake) -> contextmanager`; every new module uses these, and runner/marker grace/lease/blocker expiry code paths touched here switch to them.
  - `barrier.hit(name: str) -> None`: if `os.environ.get("SWARM_CRASH_AT") == name`, `os.kill(os.getpid(), signal.SIGKILL)`.
  - `launch.spec_for(cfg, harness, *, prompt, workdir, model, minutes, session_id=None, budget_usd=None, read_only=False, resume=None) -> LaunchSpec`: Claude adds `--max-budget-usd <budget>` when given, `--resume <sid>` when `resume`, and for `read_only` `--disallowedTools Edit,Write,NotebookEdit`; Codex adds `-c features.rollout_budget.enabled=true -c features.rollout_budget.limit_tokens=<floor(budget/usd_per_mtok*1e6)>`, `--sandbox read-only` for `read_only`, and `exec resume <thread>` form when `resume`. `check_safe` still runs on the result.
  - `runner.unit_presence(unit) -> "present" | "absent" | "unknown"` (systemctl answer; on error → cgroupfs check under `/sys/fs/cgroup/user.slice/user-<uid>.slice/user@<uid>.service/app.slice|.../<unit>`; unreadable → `"unknown"`).
  - `runner.parse_usage(harness, stdout_tail: str) -> Usage | None` with `Usage(cost_usd: float | None, tokens_in: int, tokens_out: int)`.
  - Runner `finish` records usage on the restart row; with none, the driver charges the reservation (Task 8).

- [ ] **Step 1: Failing tests**

```python
class LaunchContract(unittest.TestCase):
    def test_codex_budget_tokens(self):
        cfg = {"drive": {"codex_usd_per_mtok": 10}}
        spec = launch.spec_for(cfg, "codex", prompt="p", workdir="/w", model="m", minutes=5, budget_usd=2.5)
        self.assertIn("features.rollout_budget.limit_tokens=250000", " ".join(spec.argv))
    def test_resume_uses_same_contract(self):
        a = launch.spec_for({}, "claude", prompt="p", workdir="/w", model="opus", minutes=5, budget_usd=1)
        b = launch.spec_for({}, "claude", prompt="p", workdir="/w", model="opus", minutes=5, budget_usd=1, resume="sid")
        self.assertEqual([x for x in b.argv if x not in ("--resume", "sid")][1:4], a.argv[1:4])
        self.assertIn("--max-budget-usd", b.argv)
    def test_judge_read_only(self):
        spec = launch.spec_for({}, "codex", prompt="p", workdir="/w", model="m", minutes=5, read_only=True)
        self.assertIn("read-only", spec.argv)

class Usage(unittest.TestCase):
    def test_claude_json(self):
        u = runner.parse_usage("claude", '{"type":"result","total_cost_usd":0.42,"usage":{"input_tokens":10,"output_tokens":5}}')
        self.assertEqual((u.cost_usd, u.tokens_in, u.tokens_out), (0.42, 10, 5))
    def test_missing_usage_charges_reservation(self):
        self.assertIsNone(runner.parse_usage("claude", "killed"))
        # driver side: concluded intent with no usage → charged_usd == reserved_usd
        led = make_ledger(intent(reserved_usd=5.0, state="concluded", charged_usd=None))
        self.assertEqual(led.charged_usd(), 5.0)

class Presence(unittest.TestCase):
    def test_systemctl_error_falls_back_to_cgroupfs(self):
        with mock.patch.object(runner, "_systemctl", return_value=None), \
             mock.patch("os.path.isdir", return_value=False), mock.patch("os.access", return_value=True):
            self.assertEqual(runner.unit_presence("swarm-r1-ab.scope"), "absent")
    def test_unreadable_is_unknown(self):
        with mock.patch.object(runner, "_systemctl", return_value=None), \
             mock.patch("os.access", return_value=False):
            self.assertEqual(runner.unit_presence("swarm-r1-ab.scope"), "unknown")
```

- [ ] **Step 2–4:** run `cd tests && /var/tmp/test-slots/test-slot -- python3 -B -m unittest test_drive_runner test_supervise_launch test_supervise_runner test_hosts_resume` → FAIL, implement, PASS.
- [ ] **Step 5: Commit** — "runner: one launch contract with budgets, usage capture, tri-state containment, clock and crash barriers".

---

**Additions from the design judge:**
- [ ] **[F12]** `runner.classify_failure(harness, tail) -> "outage" | "error" | None` (Claude `is_error` with API/overload/auth subtypes; Codex error events of the same classes; binary launch failure). Test `test_overload_is_outage`.
- [ ] **[F16]** `unit_presence` fallback only on cgroup v2 with readable `user@<uid>.service` and the slice recorded in the run file at launch (`systemctl show -p Slice`); otherwise `unknown`. Tests `test_cgroup_v1_is_unknown`, `test_wrong_slice_is_unknown`.
- [ ] **[F22]** `test_slow_systemctl_capped_by_remaining_time`; doctor line for the Claude Bash sandbox ("boundary" or "guardrail only") and the Codex rollout-budget check (`test_doctor_codex_rollout_budget`).

### Task 10: Pipeline — review stage, owner seats, progress, ruling and goal judges

Seat: eng-pipeline (haiku). Spec §3.6, §3.7.

**Files:**
- Modify: `lib/swarm/supervisor/pipeline.py` (review stage via recipe `review_role`; drive jobs delegate launch decisions to the driver, keep `transitions` shared)
- Modify: `skills/engineering-team/swarm_plugin.py:286-337` (`coding_recipe`: `review_role="reviewer"`, titles add `reviewer: "Reviewer"`)
- Modify: `lib/swarm/supervisor/prompts.py` (from Task 8: ruling and goal-judge text with `--candidate`/`--req`)
- Modify: `lib/swarm/spool.py`/`cli.py` verdict path: refuse `not_met` while `drive.ruling` is set; ruling/goal `met` requires `--candidate` and `--req`
- Test: `tests/test_drive_pipeline.py`, `tests/test_supervise_pipeline.py`

**Interfaces:**
- Consumes: `next_actions` (Task 7) calls `pipeline.transitions(..., drive=True)`.
- Produces: `pipeline.transitions(board, js, cfg, config_path=None, *, drive=False, reviews=None) -> list[Transition]` where `Transition.role ∈ {"reviewer","judge","worker","finalizer"}`; in drive mode `reviews` (receipted `swarm review` results) gate the judge.

- [ ] **Step 1: Failing tests**

```python
class DrivePipeline(DriveEnv):
    def test_handoff_goes_to_reviewer_before_judge(self):
        self.handoff("eng-a", "feat/a@" + "a"*40); self.tick()
        self.assertEqual(self.last_launch()["role"], "reviewer")
    def test_changes_resume_owner_then_review_again(self):
        self.handoff("eng-a", "feat/a@" + "a"*40); self.review("feat/a@" + "a"*40, "changes"); self.tick()
        self.assertEqual((self.last_launch()["seat"], self.last_launch()["prompt_kind"]), ("eng-a", "fix"))
    def test_ruling_refuses_not_met(self):
        self.make_ruling_due(); rc, _, err = self.judge_cli("not_met", "--reason", "x", "--next", "y", "--open", "1")
        self.assertNotEqual(rc, 0); self.assertIn("ruling", err)
    def test_goal_met_needs_candidate(self):
        rc, _, err = self.judge_cli("met", "--scope", "goal", "--reason", "ok")
        self.assertIn("--candidate", err)
    def test_stale_met_after_target_advance_reruns_goal_judge(self):
        self.goal_met(candidate="main@" + "b"*40); self.mirror_head("main", "c"*40); self.tick()
        self.assertEqual(self.last_launch()["prompt_kind"], "goal_judge")
        self.assertNotEqual(self.board.job_status("J").status, "completed")
```

- [ ] **Step 2–4:** run `cd tests && /var/tmp/test-slots/test-slot -- python3 -B -m unittest test_drive_pipeline test_supervise_pipeline test_pipeline_plugins test_team_plugin` → FAIL, implement, PASS.
- [ ] **Step 5: Commit** — "pipeline: review stage, owner seats, progress, ruling and goal judges".

---

**Additions from the design judge:**
- [ ] **[F10]** goal and ruling verdicts must name the pinned requirements digest in `--req`; `test_goal_met_with_unpinned_req_rejected`.
- [ ] **[F13]** `test_clean_rebase_same_patch_ids_reuses_met` (only exact-SHA CI required) and `test_rebase_changing_patch_id_needs_new_verdict`.
- [ ] **[F14]** `report:<path>@<blob>` artifacts: `test_report_artifact_is_a_handoff`.

### Task 11: `swarm land`, private mirrors, land leases

Seat: eng-land (haiku). Security reviewer reviews. Spec §3.8, §4.0.

**Files:**
- Create: `lib/swarm/supervisor/mirror.py`, `lib/swarm/land.py`
- Modify: `lib/swarm/cli.py` (`swarm land`), `skills/engineering-team/swarm_plugin.py` (integrator prompt uses `swarm land`; `coding_integrated` defers to receipts in drive jobs)
- Test: `tests/test_drive_land.py` (local bare repositories created under the test temp root)

**Interfaces:**
- Produces:
  - `mirror.Mirror(url: str)`: `.path` (private bare repo), `.fetch(timeout: float) -> dict[str, str]` (ref → sha), `.head(branch) -> str | None`, `.is_ancestor(a, b) -> bool`; git runs with `env={"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "HOME": <private>, "PATH": service_path()}` and `-c core.hooksPath=/dev/null -c protocol.allow=never -c protocol.https.allow=always -c protocol.ssh.allow=always -c protocol.file.allow=always`.
  - `land.land(board, cfg, job, artifact, *, caller_session) -> LandResult(ok: bool, landed_sha: str | None, why: str)`: checks caller is the generation's integrator seat with leases, receipted `met` on the exact SHA, CI green (`ci` plugin `status`), then for each pinned destination `git push <url> <sha>:refs/heads/<target>` (no `--force`), verifies via mirror, deletes the branch, writes `integrated` receipt + ledger entry, posts `INTEGRATED <artifact>`.
- [ ] **Step 1: Failing tests**

```python
class Land(DriveEnv):
    def test_land_fast_forwards_every_destination(self):
        a, b = self.bare("a"), self.bare("b"); self.pin(a, b); sha = self.approved_rebased_commit()
        res = land.land(self.board, self.cfg, "J", f"feat/x@{sha}", caller_session=self.integrator_sid)
        self.assertTrue(res.ok); self.assertEqual(self.head(a, "main"), sha); self.assertEqual(self.head(b, "main"), sha)
    def test_non_fast_forward_refused(self):
        self.advance_target_elsewhere(); res = land.land(...); self.assertFalse(res.ok); self.assertIn("rebase", res.why)
    def test_wrong_caller_refused(self):
        res = land.land(self.board, self.cfg, "J", self.artifact, caller_session=self.engineer_sid)
        self.assertFalse(res.ok)
    def test_mirror_ignores_repo_config(self):
        self.agent_repo_config("core.sshCommand", "touch PWNED")
        Mirror(self.url).fetch(timeout=30); self.assertFalse((self.work / "PWNED").exists())
    def test_second_job_waits_for_land_lease(self):
        self.claim_land("J"); a = next_actions(self.snapshot("K"), CFG)
        self.assertTrue(any(x.kind == "hold" and "land lease" in x.reason for x in a))
```

- [ ] **Step 2–4:** `cd tests && /var/tmp/test-slots/test-slot -- python3 -B -m unittest test_drive_land test_team_land` → FAIL, implement, PASS.
- [ ] **Step 5: Commit** — "land: trusted swarm land with private mirrors and per-destination leases".

---

**Additions from the design judge:**
- [ ] **[F8]** `land.land` journals each destination in a host-private land receipt (`intent` → `pushed` → `verified`); `driver` action `complete_land` finishes an interrupted landing. Tests `test_interrupted_land_completed_by_driver` (barrier `land_after_first_destination`), `test_quiesce_exempts_inflight_land`.
- [ ] **[F19]** `test_orphan_lock_row_reported`; foreign-lease test `test_foreign_lease_never_reclaimed_then_ruling`.
- [ ] **[F24]** `mirror.audit_target(since_sha, candidate, judged: set[str]) -> list[str]` (commits not reachable from a judged-met SHA and with no patch-id match); `test_unjudged_commit_on_target_blocks_close`.

### Task 12: Questions per seat

Seat: eng-questions (haiku). Spec §3.10.

**Files:**
- Modify: `skills/ask-answer/swarm_plugin.py` (`open_question` passes `seat`, `generation`; drive defaults), `lib/swarm/board/blockers.py` (`expire_blockers` unchanged semantics; driver applies expiry for drive jobs from receipted `until`)
- Modify: `lib/swarm/supervisor/orphans.py:human_question`, `stuck.py:stuck_reason`, `command.py:candidates/_invalid` (skip the job-level gate for drive jobs)
- Test: `tests/test_drive_questions.py`

- [ ] **Step 1: Failing tests**

```python
class DriveQuestions(DriveEnv):
    def test_other_seats_progress_while_question_open(self):
        self.question("eng-a", default="postgres", expires="2h"); self.request("eng-b"); self.tick()
        self.assertEqual(self.last_launch()["seat"], "eng-b")
    def test_expiry_after_downtime_applies_once(self):
        self.question("eng-a", default="postgres", expires="30m"); self.finish("eng-a", "completed")
        self.clock.advance(7200); self.tick(); self.tick()
        launches = [r for r in self.launched if r["seat"] == "eng-a"]
        self.assertEqual(len(launches), 1); self.assertIn("default applied: postgres", launches[0]["stdin"])
    def test_human_question_heuristic_ignored_for_drive(self):
        self.board.post("J", self.name_of("eng-a"), "ok?", to="human"); self.request("eng-b"); self.tick()
        self.assertEqual(self.last_launch()["seat"], "eng-b")
```

- [ ] **Step 2–4:** `cd tests && /var/tmp/test-slots/test-slot -- python3 -B -m unittest test_drive_questions test_ask_answer test_blockers test_supervise_orphans` → FAIL, implement, PASS.
- [ ] **Step 5: Commit** — "questions: per-seat gating and driver-applied defaults in drive jobs".

---

**Additions:** [A] `--default` mandatory in every goal job (`test_goal_job_question_without_default_refused`); [F1] chaos case lives in Task 16.

### Task 13: Triggers — NOTIFY kick, triage off, alert changes

Seat: eng-trigger (haiku; re-sized by the design judge: Postgres LISTEN, debounce, ledger-based DRIVER-STALLED). Spec §3.11, §3.13.

**Files:**
- Modify: `lib/swarm/events_listener.py` (LISTEN `swarm_board` → `hooks.kick()` for drive jobs, same debounce)
- Modify: `lib/swarm/events_safety.py` (`check_job`: drive jobs skip `waiter-stale`/`unacked`; `triage()` never for drive jobs; `DRIVER-STALLED` when the ledger shows no responsible actor for > 2 timer periods)
- Test: `tests/test_events.py` (new cases)

- [ ] **Step 1: Failing tests**

```python
def test_drive_job_has_no_waiter_stale(self):
    js = self.drive_job_status(); problems = events_safety.check_job(self.board, S, js, NOW)
    self.assertNotIn("waiter-stale", [p.code for p in problems])
def test_triage_skipped_for_drive_jobs(self):
    with mock.patch("subprocess.run") as run:
        events_safety.run_check(self.board, {"events": {"enabled": True, "triage": True}}, say=lambda m: None)
    self.assertFalse(any("claude" in str(c) for c in run.call_args_list))
```

- [ ] **Step 2–4:** `cd tests && /var/tmp/test-slots/test-slot -- python3 -B -m unittest test_events test_events_listener` → FAIL, implement, PASS.
- [ ] **Step 5: Commit** — "events: kick the supervisor on NOTIFY for drive jobs; no waiter alerts or triage for them".

---

### Task 14: Engineering-team skill and docs

Seat: eng-docs (haiku). Spec §3.5, §3.13.

**Files:**
- Modify: `skills/engineering-team/SKILL.md`, `references/hosts.md`, `references/pm-events.md`, `references/recovery.md`, `references/team-roles.md`
- Modify: `skills/swarm/SKILL.md` (seat, review, land, halt, drive activation), `docs/REFERENCE.md` (new "Drive mode" section), `config.example.toml` (`[drive]` with every default from Global Constraints), `team.example.toml`, `CHANGELOG.md`
- Test: `tests/test_layout.py`, `tests/test_plugin_manifest.py`, `tests/test_changelog.py` (existing doc checks), plus a new `tests/test_drive_docs.py` asserting every `[drive]` key in `driver` defaults appears in `config.example.toml` and `docs/REFERENCE.md`.

- [ ] **Step 1:** write `test_drive_docs.py`:

```python
def test_drive_defaults_documented(self):
    from swarm.supervisor import driver
    example = (ROOT / "config.example.toml").read_text(); ref = (ROOT / "docs/REFERENCE.md").read_text()
    for key in driver.DEFAULTS:
        self.assertIn(key, example); self.assertIn(f"`{key}`", ref)
```

- [ ] **Step 2:** run → FAIL. **Step 3:** rewrite: every goal job is driven (`swarm activate --goal`; `--no-drive --reason` to opt out); the invoking session is "requester/observer"; the EL brief template (seat requests, worktrees per writer, GOAL-READY with requirements blob id); PM event procedure scoped to non-drive jobs; the ruling/UNREACHABLE semantics; never /tmp; test-slot and `swarm ci wait --timeout 540`. **Step 4:** run the doc tests → PASS. **Step 5:** commit "docs: drive mode in the engineering-team skill and reference".

---

### Task 15: Fake harnesses and the E2E acceptance suite (no coordinator)

Seat: qa (sonnet). Spec §6.2 scenarios 1, 2, 4, 5, 6, 7.

**Files:**
- Create: `tests/fixtures/drive/fake_harness.py` (executable; installed on PATH as `claude` and `codex` in the test env), `tests/fixtures/drive/scripts/*.json` (per-scenario role scripts)
- Create: `tests/test_drive_e2e.py`
- Modify: `.github/workflows/test.yml` (no change needed if discovered by `unittest discover`; confirm the file backend matrix entry runs it)

**Interfaces:**
- Consumes everything above. The fake reads its prompt from stdin, extracts `[swarm role: X]`, `[swarm job: J]` and the seat name, drives the real hooks through `bin/swarm-hook` with a synthetic session id (as `test_pause_e2e` does), runs real `swarm` CLI commands (`seat request`, `done`, `review`, `verdict`, `land`, `ask`), commits to local bare repos for engineers, and prints a Claude-style final JSON with `total_cost_usd`. Its behaviour per role comes from the scenario script, e.g. `{"judge": [{"verdict": "not_met", "open": 2}, {"verdict": "met"}]}`.
- `start_runner` is the real `runner.start` with `SWARM_NO_SYSTEMD=1` replaced by an injected scope-less executor `tests/fixtures/drive/local_executor.py` (process group, same run-file/lock protocol), because GitHub runners have no user manager; Task 17 covers real scopes.

- [ ] **Step 1: Write the MET scenario test**

```python
class DriveE2E(DriveE2EEnv):          # one instance per backend: memory, sqlite, file, postgres (CI)
    def test_met_without_coordinator(self):
        self.activate_drive(goal="Add --version with tests", script="met.json")   # once, from the test shell
        status = self.run_passes(max_passes=60)                                   # real run_pass, ManualClock
        self.assertEqual(status.status, "completed"); self.assertEqual(status.verdict, "met")
        self.assertEqual(self.coordinator_turns(), 0)
        self.assertTrue(all(a.agent_key for a in self.agents()))                  # every agent joined
        self.assertEqual(self.models_by_role()["engineer:simple"], "haiku")
        self.assertEqual(self.models_by_role()["judge"], "opus")
        self.assertTrue(self.target_head_equals_candidate())
        self.assertTrue(self.invariant_held_after_every_pass())

    def test_unreachable_by_ruling(self):
        self.activate_drive(goal="impossible", script="unreachable.json")
        st = self.run_passes(max_passes=80)
        self.assertEqual((st.status, st.verdict, st.verdict_scope), ("failed", "unreachable", "ruling"))
        self.assertIsNotNone(self.board.verdict_details("J"))

    def test_codex_parity(self):
        self.activate_drive(goal="Add --version with tests", script="met.json", lead_harness="codex",
                            engineer_harness={"eng-a": "codex"})
        self.assertEqual(self.run_passes(max_passes=60).verdict, "met")

    def test_forgeries_change_nothing(self): ...       # spec §6.2 scenario 5, each forgery asserted ignored/refused
    def test_generation_rollover(self): ...            # scenario 6
    def test_stale_met_after_target_advance(self): ... # scenario 7
```

`run_passes` loops `command.run_pass(cfg, start_runner=local_executor.start, now=clock.now())`, advances the clock by `timer_minutes` each loop, waits for executor children to exit between passes, asserts the invariant (spec §3.3) after each pass, and fails with the snapshot when `max_passes` is reached.
- [ ] **Step 2:** run `cd tests && SWARM_TEST_BACKEND=sqlite /var/tmp/test-slots/test-slot -- python3 -B -m unittest test_drive_e2e` → FAIL until all tasks land; then PASS on memory, sqlite, file locally and all four in CI.
- [ ] **Step 3: Commit** — "tests: drive-mode E2E acceptance with scripted harnesses and no coordinator".

---

**Additions from the design judge and the PM ruling:**
- [ ] Add `test_drive_e2e` (and Task 16's `test_drive_chaos`) to `tests/run_postgres_suite.py` so they run in the Postgres CI job.
- [ ] Replace `coordinator_turns() == 0` with `assert_no_coordinator()`: no restart row whose reason starts with `orphan-coordinator:`, no orphan-coordinator launch in the supervise log, and no agent row with a role in `[events] orchestrator_roles` in the job.
- [ ] **[F18]** The fake Codex harness models the sandbox: with `SWARM_FAKE_SANDBOX=1` it cannot open the board directly unless the test marks the host drive-ready; `test_codex_seat_refused_when_not_drive_ready` and `test_codex_parity` (drive-ready) both run.
- [ ] **[A]** `test_goal_without_drive_flag_is_driven` and `test_no_drive_with_reason_runs_as_before`.

### Task 16: Chaos suite

Seat: qa-chaos (sonnet). Spec §6.2 scenario 3.

**Files:**
- Create: `tests/test_drive_chaos.py`
- Test helpers reuse Task 15's env; passes run in a child process (`python -m swarm supervise` with `SWARM_CRASH_AT`) so barriers SIGKILL a real process.

- [ ] **Step 1: Write the tests** — each ends MET or UNREACHABLE within `max_passes`:

```python
BARRIERS = ["before_claim", "after_claim", "after_run_file", "after_marker", "after_runner_start",
            "runner_mid_session", "before_finish", "mid_terminalization", "mid_rollover",
            "hook_after_cli_before_receipt", "land_after_first_destination", "after_intent_before_claim",
            "mid_rollover_after_ledger"]

class DriveChaos(DriveE2EEnv):
    def test_every_barrier_recovers(self):
        for barrier in BARRIERS:
            with self.subTest(barrier=barrier):
                self.fresh_job(script="met.json")
                self.run_passes(max_passes=3, crash_at=barrier)
                st = self.run_passes(max_passes=60)
                self.assertIn(st.verdict, ("met", "unreachable"))
                self.assertEqual(self.duplicate_attempts(), [])
    def test_kill_engineer_mid_task(self): ...           # SIGKILL a fake session; stuck → relaunch with brief
    def test_kill_lead_mid_turn(self): ...
    def test_unanswered_question_default_applies(self): ...
    def test_ci_red_twice_then_ruling(self): ...          # fake CI red on the rebased SHA twice
    def test_budget_exhaustion_ruling_from_reserve(self): ...   # --budget max_sessions=8
    def test_ruling_judge_fails_twice_mechanical_unreachable(self): ...
    def test_halt_then_unhalt_reaches_met(self): ...      # person path via a pty fixture outside any enrolment
    def test_user_bus_down_holds_then_recovers(self): ... # systemctl stub errors for N passes, cgroupfs unreadable
```

- [ ] **Step 2:** run locally per backend `cd tests && SWARM_TEST_BACKEND=<memory|sqlite|file> /var/tmp/test-slots/test-slot -- python3 -B -m unittest test_drive_chaos` → PASS once Tasks 1–15 are in; Postgres runs in the CI job (Task 15 addition).
- [ ] **Step 3: Commit** — "tests: drive-mode chaos suite with SIGKILL crash barriers".

---

**Additions from the design judge:** `test_lead_asks_human_all_else_idle` ([F1]: holds until expiry, default applied, lead woken, ends MET); `test_provider_outage_does_not_burn_attempts` ([F12]); `test_bad_ledger_holds_not_silent` ([F2]); all on four backends.

### Task 17: Real-systemd test (box only)

Seat: qa-chaos (sonnet). Spec §6.2a.

**Files:** Create `tests/test_drive_systemd.py` (skip unless `SWARM_TEST_SYSTEMD=1` and `runner.scope_available()`).

- [ ] **Step 1:** tests: runner SIGKILL → next `reap` stops the scope and its forked grandchild (assert via `systemctl --user show` + `/proc`); `swarm halt` racing a launch leaves no live scope and no open row; writer fencing waits for `unit_presence == "absent"` before relaunch.
- [ ] **Step 2:** run on the coder box: `cd tests && SWARM_TEST_SYSTEMD=1 /var/tmp/test-slots/test-slot -- python3 -B -m unittest test_drive_systemd` → PASS; record the output in the job's evidence file.
- [ ] **Step 3: Commit** — "tests: box-only real systemd containment checks for drive mode".

---

### Task 18: Compatibility with older clients

Seat: eng-board (haiku). Spec §9.

**Files:** Modify `lib/swarm/board/postgres.py` (set `swarm.client_schema` on connect; triggers on `jobs, agents, messages, restarts, blockers, seats, drive_locks, events` refusing writes to drive jobs without it), `lib/swarm/board/sqlite.py`/`file.py` (guard older clients honour, decided by the test); Create `tests/test_drive_compat.py`.

- [ ] **Step 1:** test that a connection without the setting cannot `INSERT INTO messages` for a drive job (Postgres, CI) and can for a non-drive job; test the 0.2.3 release (`git worktree add ~/work/swarm-023 v0.2.3` in CI's workspace, never /tmp) opening a schema-25 SQLite and file store: assert it refuses, or add the guard until it does.
- [ ] **Step 2–3:** implement, run in CI (`swarm ci wait --repo fcarucci/Swarm --sha <sha> --timeout 540`), commit "board: older clients cannot write drive jobs".

---

### Task 19: Live acceptance on the coder box (manual gate before landing)

Seats: qa (sonnet) runs it, EL checks. Spec §6.3.

**Files:** Create `e2e/drive_live.sh`, `e2e/drive_live/toy-repo-template/` (tiny Python CLI + one test), `e2e/drive_live/LEAD-met.md`, `e2e/drive_live/LEAD-unreachable.md`.

- [ ] **Step 1:** the script: create `~/work/drive-accept/<stamp>/{toy.git,toy}` (bare remote + clone), `swarm activate --job drive-accept-<stamp> --goal "$(cat goal)" --lead-brief .../LEAD-met.md --workdir ~/work/drive-accept/<stamp>/toy --budget max_cost_usd=10`, then only `swarm status --job ...` every 60 s until terminal or 3 h; no Claude Code session is involved; exit 0 iff MET with the landed head equal to the judged candidate and CI (the toy repo's `make test` run by `swarm ci` local adapter) green on it.
- [ ] **Step 2:** run it once for MET and once with `LEAD-unreachable.md` (expects UNREACHABLE). Attach `swarm transcript export --job ...`, `swarm snapshot --job ...` and the supervise log excerpt to `docs/superpowers/evidence/2026-10-drive-live.md` (redacted).
- [ ] **Step 3:** commit the script and evidence: "e2e: live drive-mode acceptance on the coder box".

---

**Additions from the design judge:** the MET run includes one **real Codex engineer** seat on a drive-ready host ([F18]), and the script kills the supervisor mid-run once (`systemctl --user kill swarm-supervise.service` during an active pass, then `kill -9` of one runner); the run must still reach MET with no duplicate attempts in the ledger.

### Task 20: Security review and final judging

Seats: security reviewer (opus; read-only), judge (opus).

- [ ] **Step 1:** security reviewer reviews the whole branch diff against spec §4.0–§4.6 and the round-1–3 findings tables, focusing on: receipt forgery paths, `swarm land` checks, mirror git sanitisation, person path, budget reservation races, path validation (workdir roots, /tmp, symlinks), prompt fencing. Report with `swarm review --job J --artifact feat/swarm-autonomy@<sha> verified|changes --report <path>`.
- [ ] **Step 2:** fixes go back to the owning seats (drive pipeline once available, otherwise EL dispatch).
- [ ] **Step 3:** judge (opus) records the goal verdict on `main@<landed sha>` with `--candidate` and `--req <spec blob id>`, citing: CI green on the exact SHA, Task 15/16 results on all backends, Task 17 and Task 19 evidence.

---

## Self-review notes (EL)

- Spec coverage: §3.1 → T2, T6, T8; §3.2–3.3 → T7, T8; §3.4 → T3, T5, T6; §3.5 → T7, T8 (prompts), T14; §3.6–3.7 → T10; §3.8 → T11; §3.9 → T8, T9; §3.10 → T12; §3.11 → T5, T13; §3.12 → T6, T9; §3.13 → T8, T13, T14; §4.0 → T4, T5; §4.1 → T7, T9; §4.2 → T6; §4.3 → T6; §4.4 → T5, T9; §4.5 → T8; §4.6 → T5, T6; §5 → T1; §6 → T15–T17, T19; §9 → T18.
- Names used across tasks: `claim_drive_launch`, `request_seat`, `set_seat_state`, `seat`, `seats`, `ledger.load/create/unfinished`, `Intent`, `receipts.write/find/args_sha`, `next_actions`, `Snapshot`, `Action`, `driver.enforce_deadlines/reconcile/execute/terminalize/rollover/drive`, `launch.spec_for(..., budget_usd, read_only, resume)`, `runner.unit_presence`, `runner.parse_usage`, `mirror.Mirror`, `land.land`, `hooks.kick`, `person.require`, `drive_authz.seat_request_authorized` — each defined in exactly one task's Interfaces block.

## Delta re-review additions (spec [RR1]–[RR8])

- Task 6: [RR1] five-step durable activation; test `test_activating_ledger_completed_by_next_pass` (crash after each step).
- Task 8b: [RR2] `test_rejected_met_clears_terminal_intent_and_rejudges`; [RR3] `test_halted_job_question_default_not_applied`; [RR4] `test_halt_with_unknown_usage_charges_reservation`; [RR5] `test_terminalization_completes_land_journal_first` and `test_partial_land_recorded_on_timeout`.
- Task 3: [RR6] host-wide lock in `claim_drive_launch`; `test_concurrent_nested_root_claims_one_wins` (two processes, sqlite and postgres).
- Task 5/9: [RR7] doctor per-role Codex readiness; `test_codex_judge_overridden_to_claude_on_sqlite`.
- Task 11: [RR8] `swarm land` records a receipted land request; the driver executes `land.land` in the pass (`Action.kind == "land"`); tests `test_land_request_executed_by_driver`, `test_codex_integrator_lands_via_request`.
- Task 12: [RR3] generic `expire_blockers` skips drive jobs; `test_generic_sweep_skips_drive_questions`.

## D1 answered: configurable spending cap (spec rev 5.2)

- Task 6: `--max-cost-usd N` and `--ruling-reserve-usd N` (plus `--budget KEY=VALUE`), defaults from `[drive]` in config.toml, stored in `led.budgets`; reserve > cap refused. Tests `test_max_cost_flag_overrides_config_default`, `test_config_default_used_without_flag`, `test_reserve_larger_than_cap_refused`.
- Task 6/8c: `swarm status --job J` shows the effective budget line; Task 1's doctor gains one line per active drive job (`drive budget J: cost x/y USD (reserve r), sessions a/b, wall h/H h`); test `test_doctor_shows_effective_budget_per_job`.
- Task 14: config.example.toml and REFERENCE.md present 40/5 USD as configurable defaults, not limits.

## Re-judge fixes G1–G12 (spec revision 6)

- **Task 5/9 [G1]:** in drive mode judge and reviewer seats always launch on Claude; a Codex request is overridden with the reason posted. Tests `test_codex_judge_overridden_to_claude_on_every_backend` (memory, sqlite, file, postgres) and `test_doctor_codex_readonly_line`. Replaces the [RR7] test `test_codex_judge_overridden_to_claude_on_sqlite`.
- **Task 6/11 [G2]:** activation pins one credential source per destination in the owner record (`--credential DEST=helper:<allowlisted cmd>|ssh-key:<path>|ssh-agent`; default: `helper:gh auth git-credential` for https, `ssh-agent` for ssh). `mirror.Mirror(url, credential)` passes it explicitly (`-c credential.helper=...`, `GIT_SSH_COMMAND=ssh -i <key> -o IdentitiesOnly=yes -o BatchMode=yes`, or `SSH_AUTH_SOCK` for that call only). Doctor: dry-run `git ls-remote` per pinned destination. Tests: `test_land_against_authenticated_remote` (a local `git http-backend` behind a tiny stdlib HTTP server requiring basic auth, credential helper fixture script on the allowlist; without the pinned helper the push fails, with it the landing succeeds), `test_credential_helper_not_on_allowlist_refused`, `test_doctor_ls_remote_per_destination`. The land request is the integrator's hand-off: `test_integrator_exits_after_land_request_driver_posts_integrated`, `test_land_refused_wakes_owner_and_lead`.
- **Task 10/15 [G3]:** per-recipe closing gate. `--recipe artifact` (default with no pinned git target): MET on `report:<path>@<blob>` with receipted goal `met`, blob unchanged at close, no target/CI/audit. Tests `test_artifact_recipe_met_without_target` (Task 10) and E2E `test_non_coding_goal_reaches_met` (Task 15, all backends): a research goal, one QA/research seat hands off `report:findings.md@<blob>`, goal judge `met` → completed, with `assert_no_coordinator()`.
- **Task 8b [G4]:** `close_deadline_minutes` (60, configurable). Test `test_unevaluable_pending_met_rejected_after_close_deadline` (mirror fetch stub always times out → after 60 min the intent is rejected "gate not evaluable", a ruling becomes due, and `enforce_deadlines` later records the mechanical UNREACHABLE).
- **Task 14 [G5]:** release notes list three breaking changes (goal implies drive; `--default` required; requester Agent-tool spawns refused in drive jobs); rewrite `skills/engineering-team/SKILL.md`, `references/hosts.md`, `pm-events.md`, `recovery.md` and `skills/swarm/SKILL.md` so staffing goes through `swarm seat request` and the invoking session is requester/observer. Doc test `test_skills_never_tell_requester_to_spawn_in_drive_jobs` (greps the skill files for Agent-spawn instructions outside the non-drive section).
- **Task 6 [G6]:** `--drive` accepted as a no-op (`test_drive_flag_is_accepted_noop`); `--lead-brief` and `--workdir` optional (tests already listed under [A]); requester-session person authority tests (`test_requester_can_unhalt_without_tty`, `test_enrolled_seat_refused_person_path`).
- **Task 6 [G7]:** `swarm drive budget --job J KEY=VALUE...` without rollover (`test_drive_budget_changes_without_rollover`); `test_cap_below_reserve_plus_min_session_refused`.
- **Task 9 [G9]:** `test_missing_binary_is_error_not_outage`.
- **Task 6 [G10]:** `test_generated_brief_is_git_excluded` (`.git/info/exclude` contains `.swarm/`).
- **Task 20 [G11]:** the security review explicitly confirms the requester-session trust choice is documented (§4.0) and limited to that job.

## Final judge edits (spec revision 6.1)

- **Task 11 [G2a]:** the owner record pins `GH_CONFIG_DIR` (or the helper's `HOME`) and the ssh-agent socket path per destination; `mirror.Mirror` passes them only in that git invocation's environment. `test_land_against_authenticated_remote` uses a real gh-style config dir: a fixture `hosts.yml` (`github.com: oauth_token: <test token>`) under a temporary `GH_CONFIG_DIR`, read by the allowlisted helper (the real `gh auth git-credential` when `gh` is installed in CI, otherwise a fixture helper that reads the token from `$GH_CONFIG_DIR/hosts.yml` and fails without it); assert the push fails when `GH_CONFIG_DIR` is not pinned and succeeds when it is. Also `test_ssh_agent_socket_pinned_and_passed_only_to_git` and `test_doctor_reports_missing_agent_socket`.
- **Task 7/10 [G3]:** step 9 for the artifact recipe requires every non-withdrawn group judged `met` (`test_artifact_recipe_goal_judge_after_all_groups_met`); `swarm goal-ready --candidate report:<path>@<blob>` (`test_goal_ready_accepts_report_candidate`).

## Main moved: supervisor opt-in (spec [M1])

- Task 6: `test_drive_activation_refused_when_supervisor_disabled` (message names `swarm supervise enable` and `--no-drive --reason`); `test_supervise_disable_warns_and_halts_drive_jobs`.
- Task 1: `test_doctor_fails_drive_job_on_host_with_supervisor_disabled`.
- [M1] additions (judge, board #27577):
  - Task 1: doctor's drive check requires a **running** timer (unit active+enabled via `systemd.manager_env()`, last pass < 2 × `timer_minutes`), not just `[supervise] enabled`: `test_doctor_fails_drive_job_when_timer_not_running`; on macOS/Windows doctor reports "drive mode unavailable: no supervisor timer".
  - Task 6: `swarm supervise disable` writes the [F5] halt into each local drive ledger and stops live seats; `enable` unhalts those and shifts deadlines (`test_supervise_disable_halts_drive_ledgers_and_stops_seats`, `test_supervise_enable_unhalts_and_shifts_deadlines`); `disable` refused for enrolled seat sessions (`test_supervise_disable_refused_for_seat_session`); drive activation refused when the timer is not running and on macOS/Windows (`test_drive_activation_refused_without_running_timer`, `test_drive_activation_refused_on_macos_and_windows`).
  - Task 14: release notes and skill text list the fourth breaking change (`activate --goal` fails until `swarm supervise enable`).

## Staffing change (Francesco, 2026-10-10): engineers on Haiku

All engineer seats (every wave) run on Haiku; reviewers and QA on Sonnet; security reviewer, judge and lead on Opus. Because Haiku engineers have a record of vacuous tests and hidden behaviour changes, every seat's diff passes the Review gates (Staffing section: line-by-line review, tests failing on base, mutation gate) before integration, and the Opus security reviewer keeps Tasks 3, 4, 5, 8a–8c, 11 and 20.

## PM ruling [P1]: per-role model table in drive mode

- Task 6: `[drive.models]` table with the shipped defaults above; `swarm activate --model ROLE=MODEL` (per job, in the ledger); `swarm drive models --job J ROLE=MODEL` (requester/person path); seat `--model` within `allowed_models`. Tests `test_drive_models_defaults`, `test_job_model_override`, `test_seat_model_override_within_allowlist`, `test_status_and_doctor_show_effective_models`.
- Task 10: recipe `review_gates = "haiku-strict"` by default when the effective engineer model is Haiku: the reviewer prompt requires line-by-line review, fails-on-base evidence per new test and a mutation gate (>= 5 mutants under the seat's work root); `test_haiku_engineer_selects_strict_review_gates`, `test_review_changes_on_surviving_mutant_report`.
- Task 14: config.example.toml `[drive.models]`, REFERENCE.md, skill text.

## Integration notes (wave 1 → wave 2)

- Task 4 as landed: `Ledger` has 26 fields (adds `incarnation`, a 32-hex id per create, and `ledger_rev`, the save revision); `ledger.unfinished()` **raises** when a `<sha>.lock` symlink is planted (fails closed). Tasks 8a/8c: the driver must catch that (and `LedgerBadError`), hold the affected jobs, post `DRIVER-LEDGER-BAD` and continue the pass for other jobs; test `test_planted_lock_symlink_holds_job_not_pass`.
- Task 11 is split: **Task 11a** (`lib/swarm/supervisor/mirror.py`: `Mirror(url, credential)` with pinned credential sources [G2]/[G2a], `fetch`, `head`, `is_ancestor`, `patch_ids(base, head)` for [F13], `audit_target(since_sha, candidate, judged)` for [F24], sanitized git environment; tests `test_mirror_ignores_repo_config`, `test_land_against_authenticated_remote`'s mirror half, `test_credential_helper_not_on_allowlist_refused`, `test_ssh_agent_socket_pinned_and_passed_only_to_git`, patch-id and audit tests) starts in wave 2; **Task 11b** (`land.py`, `swarm land` request, driver `land` action, leases, receipts) follows Tasks 3–8.
- Task 7 (pure `next_actions`) depends only on Task 7a and starts in wave 2.
- PM rulings after the Task 2 / 7a reviews: (a) `progress` is a bool on the board and `bool | None` in `VerdictView`; `progress()` tests `is True`. (b) Task 2 also makes the offline spool keep `scope`, `open`, `progress`, `candidate`, `req`, and makes the pause, stuck, command and agentview readers treat `unreachable` as settled. (c) Task 2's fix commits are additive; eng-seats (Task 3) rebases onto Task 2's new head when the lead says so.
- [B1] Task 5: `test_seat_enrols_from_marker_with_several_active_jobs` (two active jobs on the host, a supervisor-launched seat of the second one enrols in the second via its marker; a session with neither marker nor job tag joins nothing).

## Snapshot and action-id conventions (lead ruling after Task 7, binding for 7b/8a–8c/11b)

- `integrated[group]` is the per-group pipeline status dict: `landed` (sha, only from a verified land receipt), `blocked` (FINALIZE_BLOCKED text), `withdrawn` (reason), `lease_held_since` (ISO). Confirmed.
- `goal_ready`: `candidate`, `at`, **and** `req` (pinned requirements digest) and `added_requirements` (list of path@blob). The goal judge launch passes `req` through.
- `cfg["recipe"]` comes from the owner record/ledger (fixed at activation, [G3]), never from config.toml; the snapshot reader fills it. `cfg["review_role"]` and `cfg["review_gates"]` come from the recipe. Confirmed with that source rule.
- **Action ids (correction):** `gen<N>|<prompt kind>|<seat or ->|<group or ->|<round>|<trigger>`. The kind slot carries the prompt kind (`seat`, `wake`, `lead`, `relaunch`, `review`, `judge`, `fix`, `integrate`, `goal`, `ruling`, `close`, ...); the last slot is the **id of the record that triggered the action**, never a constant, so a new trigger is a new logical action and repeated successful actions never exhaust `max_attempts`:
  - review/judge/integrate: the hand-off receipt id; fix: the verdict, review or FINALIZE_BLOCKED record id;
  - seat request: the seat row revision; relaunch after a lost attempt: the lost intent's `launch_id`;
  - lead wake: the smallest pending event id; stall wake: the ISO time of the last progress it reacts to; invariant fallback: `rounds_without_progress` at decision time;
  - goal: the GOAL-READY record id; ruling: `ruling_due_at`.
- `ruling` and `goal` group slots: confirmed.
- seat=None for judge, reviewer, integrator, goal and ruling actions: confirmed for `next_actions`; Task 8a's executor materializes driver-owned seat rows with deterministic names (`judge`, `reviewer:<group>`, `integrator:<group>`, `goal-judge`, `ruling-judge`) and driver-authored authorizations before claiming.
- Deferred items, now owned:
  - **Task 7b (new, after Task 3):** the snapshot reader `driver_snapshot.read` + a per-group judged-round count (`rounds: dict[str, int]` added to `Snapshot`, a model change approved by the lead) + the `max_rounds` rule in `next_actions`.
  - **Task 8a:** executor-side state: `ruling_due_at` set when a ruling becomes due, `rounds_without_progress` advanced/reset by the progress rule, successor seat rows (`<owner>~n`, `lead~1`) created with driver authorizations.
  - **Task 8b:** `enforce_deadlines`, the 8b terminal records, rollover.
  - **Task 11b:** `complete_land`.
- Commit trailers (PM ruling, all seats): `Co-Authored-By` names the model that actually wrote the commit (e.g. `Claude Haiku 5.5 <noreply@anthropic.com>`), not the brief's example.
- Record kind ↔ receipt kind (Tasks 5/6): the CLI record format's kinds map to `receipts.KINDS` as `seat`→`seat_request`; `halt`, `unhalt`, `deactivate`, `activate` (person-path or requester-session commands)→`person` with the command in the decision fields; all other kinds are identical. Task 5 owns the mapping table and a test that every record-format kind maps to a member of `receipts.KINDS`.
- Task 3 security review (L5) → Tasks 8a/8b: the driver sweeps `drive_locks` **by host** (every lock whose `host` is this host, whatever its job), never through a cascade delete from the job: a cascade would skip writer fencing. An expired lock stays blocking until that host sweep has fenced its holder and released it.
- Dev boards: a board already stamped schema 25 by an earlier dev build of this branch does not detect tables added later in schema 25 (seats, drive_locks); rerun `swarm init` (or the migration) on such a board after integrating Task 3. Not an issue for released boards (24 → 25 runs the whole migration).
- Until [B1] lands, every seat spawn prompt must contain the line `[swarm job: swarm-autonomy]` itself (not only the brief file), or the seat may not enrol when the parent session has several active jobs.
- Left over from Task 9, now owned: **Task 8a** — runner `finish()` records usage (`cost_usd`, `tokens_in`, `tokens_out`) on the restart row and concludes the ledger intent with `charged_usd` (the reservation when usage is missing), plus the ledger half of `test_missing_usage_charges_reservation`. **Task 1 follow-up (eng-doctor, after Task 5)** — doctor lines: Claude Bash sandbox "boundary"/"guardrail only" ([F22]), Codex rollout-budget availability, Codex drive-readiness per role ([RR7]/[G1]), running-timer drive check ([M1]) and the per-job budget/model lines ([D1]/[P1]).
- Left over from Task 11a, now owned by **Task 11b**: doctor's dry-run `git ls-remote` per pinned destination and the push half of `test_land_against_authenticated_remote`.
- Task 7b brief additions (from the Task 7 review): a test asserting the stall-wake trigger (the ISO time of the last progress); the snapshot reader supplies `integrated[group]["blocked_id"]` (the FINALIZE_BLOCKED record id used as the fix trigger) and `goal_ready` with `id`, `req`, `candidate`, `at`.
- Review process: reviewers share `~/work/swarm-autonomy/reviews/`; every review prompt names per-task scripts and files (`mutate-t<N>.py`, `t<N>-*.md`). Reviewers and seats never run git commands in the integration worktree `~/src/Swarm-autonomy` (on 2026-10-10 something checked out a seat's commit there, detaching it); they use their own worktrees under `~/work/mutants/<task>/`.
- From the Task 9 review: **(a) Task 19 (live acceptance, pre-step owned by QA with EL)** captures a real `codex exec --json` run as the codex user on CT118 (`ssh root@10.10.0.1 'pct exec 118 -- runuser -u codex -- bash -lc "codex exec --json --skip-git-repo-check ..."'`, a two-turn prompt, ≤300 s, scratch under /home/codex/work/, removed afterwards) to settle whether `turn.completed` usage is cumulative per thread and whether input tokens include cached tokens; the result becomes a fixture in `tests/fixtures/codex/` and `runner.parse_usage` is adjusted (until then it sums turns: safe, may overcharge). **(b) Task 5 (hooks)** owns the PreToolUse refusals that make judge/reviewer seats read-only in practice: Bash and PowerShell commands that write (redirections, `tee`, `cp`/`mv`/`rm`, `git commit|push|add|apply|stash`, `sed -i`, interpreters writing files via `-c`/heredoc are matched by the existing shellguard writer patterns), the Monitor tool, and MCP tools whose names indicate writes; tests `test_readonly_judge_bash_write_blocked`, `test_readonly_judge_powershell_write_blocked`, `test_readonly_judge_monitor_and_mcp_write_blocked`, plus a positive `test_readonly_judge_can_read_and_run_targeted_tests`. **Task 1 follow-up (doctor)** reports whether the Claude Bash sandbox is active for read-only seats ("boundary") or only the hook applies ("guardrail only").
- From the Task 2 re-review: **Task 8b** fixes the auto-close outcome text (`board/base.py` ~1712) that says "no verdict" for a stale job whose verdict is `unreachable` (it must name the unreachable verdict). **Task 5** (if cheap) adds a test killing the surviving spool `candidate`/`req` mutant (offline-spooled goal verdict keeps both fields).

## Wave 3 notes (after Task 3 approval)

- **Interface changes from Task 3 (binding for Tasks 5–8):** `Board.set_seat_state(seat_id, expected_revision, state, *, job, **fields)` (job is required); `renew_drive_locks(launch_id, expires_at, *, job, holder)` and `release_drive_locks(launch_id, *, job, holder)` are scoped by job and holder; `lock_seconds` must be in (0, 86400]; `Seat` has `launch_id` (the claim that set it running). Residuals R1 (refuse or collapse a leading `//` worktree root) and R2 (a naive renewal datetime raises ValueError) are a small commit on top of Task 3.
- **Task 8a/8b sweeps:** end any seat left `running` with no `launch_id` (dev boards from 0bf7433) as `idle` with `last_outcome = "no launch"`; sweep `drive_locks` by host (see the L5 note).
- **Task 11b rulings (from the Task 11a reviews):** (a) the mirror resolves credential helpers only from system directories and `gh` lives in `~/.local/bin` here, so activation pins an explicit **absolute** helper path in the owner record and doctor reports a missing or unresolvable helper per destination (Task 1 follow-up and 11b); (b) for ssh destinations, activation or doctor seeds `known_hosts` in the private mirrors dir (BatchMode fails closed); (c) 11b adds a timed-out-fetch continuation, uses `patch_ids` as a sorted multiset, and handles `MissingCommitError`, `TargetRewoundError`, `UnpatchableCommitError`; (d) the twin match must be over the whole range, not per commit (Task 11a security re-check M2: landing judged A and C without B must fail the audit).
- **File ownership in wave 3 (no overlaps):** Task 5 owns `hooks.py`, `shellguard.py`, new `supervisor/drive_authz.py`, the record-kind map; Task 6 owns `cli.py`/new `drive_cli.py`, `enrolment.py`, **and `person.py`** (moved from Task 5: the CLI is its only caller; Task 5's tests import it only after Task 6 lands, until then they stub it); Task 7b owns new `supervisor/driver_snapshot.py` and the `rounds` field in `driver_model.py`; Task 12 owns `skills/ask-answer/swarm_plugin.py` (including `--default` required in goal jobs, moved from Task 6), `board/blockers.py` gating helpers, and the per-seat gate in `supervisor/orphans.py`, `stuck.py` and `command.py` candidates (driver-applied expiry stays in Task 8b).
- **File size limit (main 82a3bb0, Codex directory edition):** no file may exceed 256 KiB (`tests/test_codex_directory.py`). `lib/swarm/cli.py` is at ~253 KiB after moving `swarm verdict` into `lib/swarm/verdict_cli.py`; new commands live in their own modules (`drive_activate.py`, `drive_cli.py`, ...) with at most a registration line in `cli.py`.
- Task 3 as integrated: Windows UNC worktree roots are refused (fail safe; drive mode is refused on Windows anyway, [M1]). Task 8 and Task 14 docs mention it.

## Carrier contracts (PM ruling after Task 7b review; binding for 6a, 6b, 7b, 8a, 11b)

- **GOAL-READY** is a board message `GOAL-READY <candidate> req=<digest>` posted by `swarm goal-ready` (Task 6b); its record id is the message id; there is no `drive.goal_ready` job key. The snapshot takes `req` from `led.pinned_requirements`, never from the message (an unbound record); a mismatching `req=` makes the GOAL-READY invalid.
- **Goal and ruling verdicts** are at job data `drive.verdict.<gen>.<scope>` (spec §3.1).
- **Artifact verdicts** are identified by their board verdict message id: `swarm verdict` prints `swarm-record: kind=verdict id=<message id> rev=1` and `review.verdict_data` stores that message id (Task 6b); hook receipts (Task 5) bind `record_id` to it.
- **Recipe fields** live in the owner record: `Record.owner["recipe"]`, `["review_role"]`, `["review_gates"]` (Task 6a); the driver's `drive_cfg` fails closed (holds the job, `DRIVER-CONFIG`) when any is missing.
- **Land fields:** `integrated[group]["land_id"]` and `["land_rev"]` come from the land receipt (Task 11b).
- **Stall progress time (lead ruling):** newest activity is not acceptable (chatter would postpone stall wakes forever). Task 8a adds ledger field `last_progress_at` (ISO), set by the driver whenever the progress rule fires (new hand-off, integration, verdict with progress, seat state change to a hand-off); until 8a lands, the snapshot uses the newest *receipted* control record's time. The stall-wake trigger id is `last_progress_at`.
