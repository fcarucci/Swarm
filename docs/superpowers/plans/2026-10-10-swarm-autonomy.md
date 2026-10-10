# Swarm Autonomy (drive mode) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
> In this job the tasks are filled as Swarm seats by the EL (`swarm seat request` once Task 6 lands; until then by the PM's spawns). Each task names its seat, model and reviewer.

**Goal:** A goal job activated with `--drive` reaches MET, UNREACHABLE or CANCELLED with zero coordinator turns: the supervisor pass drives staffing, resumes, review/fix/judge rounds, landing, rulings and recovery from board state plus a host-private ledger.

**Architecture:** A new `swarm.supervisor.driver` runs first in every supervisor pass. It enforces deadlines from a host-private ledger, reconciles launches, computes the next actions with a pure `next_actions(snapshot)` and executes them through one atomic `claim_drive_launch` plus the existing runner/marker machinery. Seats (schema 25) replace coordinator spawns; hook-written receipts authenticate every agent-written control record; `swarm land` is the only path to the target branch.

**Tech Stack:** Python 3.11+ stdlib (as the rest of `lib/swarm`), unittest, SQLite/Postgres/file/memory board backends, systemd user units, Claude Code `claude -p`, Codex `codex exec`.

**Spec:** `docs/superpowers/specs/2026-10-10-swarm-autonomy-design.md` (revision 4, commit 8d0a2cf). Read §2.2 (guarantee and limits), §3.2–§3.3 (driver), §4.0 (threat model) and §4.5 (claim protocol) before any task.

## Global Constraints

- Terminal states of a drive goal job: MET, UNREACHABLE (judge verdict with reasons, or the labelled mechanical one), CANCELLED (a person). Never idle/stalled/waiting for the coordinator.
- No new always-on process: only the timer-driven pass, its per-session runners, and the existing opt-in events listener.
- Every agent joins the board (enrol deadline `enrol_minutes` kills sessions that do not).
- Models: `engineering_lead = judge = "opus"`; `engineer = qa = reviewer = integrator = worker = "sonnet"`; `--complexity simple` → `"haiku"`. Lead and judge clamped to Opus in drive mode.
- Landing: rebase only (or `[land] strategy = squash-ff` where the project sets it), fast-forward pushes only, never a force push, CI green on the exact rebased SHA, through `swarm land`.
- Tests: on the box only targeted tests through `/var/tmp/test-slots/test-slot -- <cmd>` run from `tests/` (`cd tests && /var/tmp/test-slots/test-slot -- python3 -B -m unittest test_x`); full suites only in GitHub Actions; wait for CI with `swarm ci wait --repo fcarucci/Swarm --sha <exact sha> --timeout 540` in the foreground; never `gh run watch`.
- Never put work trees, clones or builds under /tmp (live acceptance uses `~/work/drive-accept/`; seat workdirs under /tmp are refused by the driver). Unit tests keep using the suite sandbox `support.SANDBOX`/`TMPDIR`, short-lived and small, as the existing suite does.
- Judges only judge; reviewers only review; fixes happen on writer seats.
- Never a bypass flag (`launch.check_safe`), `--setting-sources user`, Codex `workspace-write` (read-only for judges/reviewers).
- Schema version becomes 25 (`SCHEMA_VERSION = 25`); verdicts `("met", "not_met", "unreachable")`.
- Budget defaults (`[drive]`): `max_sessions=60`, `max_seats=12`, `max_rounds=8`, `no_progress_rounds=3`, `max_attempts=3`, `max_cost_usd=40`, `session_usd_cap=5`, `min_session_usd=0.5`, `max_session_minutes=60`, `max_wall_hours=24`, `ruling_reserve = {sessions=2, minutes=60, usd=5}`, `ruling_attempts=2`, `ruling_deadline_minutes=120`, `ruling_grace_minutes=15`, `question_expiry="4h"`, `stall_minutes=90`, `driver_error_passes=6`, `max_concurrent_sessions=4`, `codex_usd_per_mtok=10`.
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
| 0 | 1 doctor fix | eng-doctor (haiku, simple) | reviewer (sonnet) |
| 1 | 2 schema+verdicts, 4 ledger+receipts, 9a clock+barriers | eng-board (sonnet), eng-ledger (sonnet), eng-runner (sonnet) | reviewer; security reviewer on 4 |
| 2 | 3 seats+claim, 9 runner, 12 questions | eng-board, eng-runner, eng-questions (sonnet) | reviewer; security on 3 |
| 3 | 5 hooks+receipts+guardrails, 6 CLI | eng-hooks (sonnet), eng-cli (sonnet) | security reviewer |
| 4 | 7 next_actions, 8 driver execute | eng-driver (sonnet), eng-driver2 (sonnet) | EL + reviewer |
| 5 | 10 pipeline, 11 land+mirrors, 13 triggers | eng-pipeline (sonnet), eng-land (sonnet), eng-trigger (haiku, simple) | reviewer; security on 11 |
| 6 | 14 skill docs, 18 compat | eng-docs (sonnet), eng-board | reviewer |
| 7 | 15 E2E, 16 chaos, 17 systemd test | qa (sonnet), qa-chaos (sonnet) | EL |
| 8 | 19 live acceptance, 20 security review, judge | qa + EL; security reviewer; judge (opus) | judge |

Every task: its own branch `feat/drive-<task>` off `feat/swarm-autonomy`, `swarm done --artifact feat/drive-<task>@<sha>`, review, integration by rebase onto `feat/swarm-autonomy`. The final branch lands on `main` through the pipeline after CI green on the exact SHA.

---

### Task 1: Doctor reads the timer through the user manager's environment

Seat: eng-doctor (haiku, simple). Spec §5.

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

Seat: eng-board (sonnet). Spec §3.1, §9.

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

Seat: eng-board (sonnet). Security reviewer reviews. Spec §3.2, §3.4, §3.8, §4.5.

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

### Task 4: Host-private ledger and receipts

Seat: eng-ledger (sonnet). Security reviewer reviews. Spec §4.0, §4.5.

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

### Task 5: Hooks — receipts, seat authorizations, person path, guardrails, kicks

Seat: eng-hooks (sonnet). Security reviewer reviews. Spec §3.4, §3.11, §4.0, §4.4.

**Files:**
- Modify: `lib/swarm/hooks.py` (PostToolUse and PreToolUse handlers for drive jobs)
- Create: `lib/swarm/person.py`
- Modify: `lib/swarm/shellguard.py` (target-push and agent-launch patterns)
- Test: `tests/test_drive_hooks.py`

**Interfaces:**
- Consumes: `receipts.write/find/args_sha` (Task 4); `Board.seat`, `Board.request_seat` (Task 3); `enrolment.find` (existing).
- Produces:
  - `hooks.drive_post_tool(board, cfg, payload) -> None`: for a Bash tool call whose parsed argv is `swarm done|review|verdict|seat request|post --to|ask|answer` (parsed with `cli.build_parser()`), exit status 0, session enrolled as a seat of a drive job → write a `Receipt` (kind by subcommand, `record_id` = the id the CLI printed on its last line `id=<n> rev=<r>`), then `kick()`.
  - `hooks.kick(now=time.time) -> bool`: `systemctl --user start --no-block swarm-supervise.service` with `systemd.manager_env()`, at most once per 20 s (stamp `kick.stamp` in the private dir).
  - `person.require(action: str, job: str | None) -> str` → returns `"person"` or raises `PermissionError`: refused if the calling session id has an enrolment record; otherwise prompts on `/dev/tty` `Type the job name to confirm <action>:` and compares.
  - PreToolUse refusals (drive jobs only): judges/reviewers: `git commit`, `git push`, file-writing tools; everyone: `git push` whose refspec targets the recipe's target branch, any `--force`/`-f`/`+refspec`, `claude -p`, `claude --print`, `codex exec`. Message points to `swarm land` / `swarm seat request`.

- [ ] **Step 1: Failing tests** (built on `test_hooks_cli.Env`)

```python
class DriveHooks(Env):
    def test_done_from_seat_session_writes_receipt(self):
        self.drive_job("J"); key = self.launch_seat("J", "eng-a")
        out = self.run_cli_as(key, "done", "--job", "J", "--artifact", "feat/a@" + "a"*40, "--summary", "x")
        rid = out.split("id=")[1].split()[0]
        self.hook("post", agent_id=key, tool_name="Bash",
                  tool_input={"command": f"swarm done --job J --artifact feat/a@{'a'*40} --summary x"},
                  tool_response={"exit_code": 0, "stdout": out})
        self.assertIsNotNone(receipts.find(self.board_key, "J", "done", rid))

    def test_seat_request_from_non_lead_gets_no_authorization(self):
        self.drive_job("J"); key = self.launch_seat("J", "eng-a", role="engineer")
        self.hook("post", agent_id=key, tool_name="Bash",
                  tool_input={"command": "swarm seat request --job J --seat eng-b --role engineer --brief B.md"},
                  tool_response={"exit_code": 0, "stdout": "id=7 rev=1"})
        self.assertIsNone(receipts.find(self.board_key, "J", "seat_request", "7"))

    def test_row_swapped_before_hook_is_not_authorized(self):
        self.drive_job("J"); lead = self.launch_seat("J", "lead", role="engineering_lead")
        argv = "swarm seat request --job J --seat eng-b --role engineer --brief B.md --workdir /w/b"
        self.hook("post", agent_id=lead, tool_name="Bash", tool_input={"command": argv},
                  tool_response={"exit_code": 0, "stdout": "id=7 rev=1"})
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
- [ ] **Step 3: Implement.** CLI subcommands touched print `id=<n> rev=<r>` as their last stdout line (Task 6 owns the CLI; coordinate through the board: eng-hooks posts the exact format, eng-cli implements it). `canonical_seat_args(seat) -> list[str]` returns the argv the CLI would have needed to produce those fields (fixed order). Guardrail patterns live in `shellguard.py` next to the existing `gh run watch` refusals.
- [ ] **Step 4: Run** step 2 + `test_hooks_cli test_shellguard test_spawn` → PASS.
- [ ] **Step 5: Commit** — "hooks: drive receipts, seat authorization, person path, guardrails and supervisor kicks".

---

### Task 6: CLI — `activate --drive`, `swarm seat`, `swarm halt|unhalt`, `swarm review`

Seat: eng-cli (sonnet). Spec §3.1, §3.4, §3.6, §4.3.

**Files:**
- Modify: `lib/swarm/cli.py` (parsers + handlers), `lib/swarm/enrolment.py` (`write_job_owner(..., roots: list[str], remotes: dict[str, str])`)
- Create: `lib/swarm/drive_cli.py` (handlers, keeps `cli.py` from growing)
- Test: `tests/test_drive_cli.py`

**Interfaces:**
- Consumes: Tasks 2–5.
- Produces commands:
  - `swarm activate --job J --goal G --drive --lead-brief PATH --workdir DIR [--allow-workdir DIR]... [--remote NAME=URL]... [--lead-harness claude|codex] [--budget KEY=VALUE]...` → job + owner record (roots, pinned remotes: default = `git -C DIR remote -v` read once, sanitized env) + `ledger.create` + `drive.enabled=1`, `drive.generation_requested` + lead seat request with driver-authored authorization + `hooks.kick()`. Re-activation of an existing drive job writes `generation_requested = generation + 1` only (Task 8 rolls over). Person path (`person.require`) when re-activating.
  - `swarm seat request|list|show|release|cancel` (flags as spec §3.4); last stdout line `id=<n> rev=<r>`.
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

### Task 7: Driver core — snapshot and pure `next_actions`

Seat: eng-driver (sonnet). Reviewed by EL. Spec §3.2, §3.3, §3.5–§3.7, §3.9, §4.1.

**Files:**
- Create: `lib/swarm/supervisor/driver_model.py` (pure: dataclasses + `next_actions`)
- Create: `lib/swarm/supervisor/driver_snapshot.py` (reads board + ledger + receipts into the model)
- Test: `tests/test_drive_next_actions.py` (table tests, no board), `tests/test_drive_snapshot.py`

**Interfaces:**
- Consumes: `Ledger` (Task 4), `Seat` (Task 3), receipts (Task 4), `review.latest_handoffs/artifact_verdicts` (existing).
- Produces:
  - `Snapshot(job, generation, goal, status, halted, ledger, seats: dict[str, Seat], live: set[str] (seat names with a live attempt), handoffs: list[Handoff] (receipted only), reviews: dict[artifact, str], verdicts: dict[artifact, dict] (receipted), goal_verdict: dict | None, goal_ready: dict | None, questions: dict[seat, list] (open, receipted), wakes: dict[seat, list[int]] (pending event ids above watermark), integrated: dict[group, dict], now)`.
  - `Action(kind, action_id, seat, prompt_kind, artifact=None, ruling=False, reason="")` with `kind ∈ {"launch","record_unreachable","close","hold","nothing","rollover","stop_seat"}`.
  - `next_actions(s: Snapshot, cfg: dict) -> list[Action]` implementing spec §3.3 order 1–10 and the single progress counter; never returns `[]` for an active unhalted non-terminal job (returns `hold` with `not_before` or `launch` or `nothing(responsible=...)`).
  - `progress(prev: dict | None, new: dict) -> bool` (spec §3.6: new artifact AND `open` decreased, or `progress == "yes"`, or integration).

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

### Task 8: Driver execution — claims, reconciliation, deadlines, rollover, terminalization, tamper

Seat: eng-driver2 (sonnet). Reviewed by EL and the security reviewer. Spec §3.1, §3.2, §3.9, §4.5.

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

### Task 9: Runner and launch — one launch contract, budgets, usage, containment, clock, barriers

Seat: eng-runner (sonnet). Spec §3.9, §4.1, §6.2. Task 9a (clock + barriers) lands in wave 1, the rest in wave 2.

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

### Task 10: Pipeline — review stage, owner seats, progress, ruling and goal judges

Seat: eng-pipeline (sonnet). Spec §3.6, §3.7.

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

### Task 11: `swarm land`, private mirrors, land leases

Seat: eng-land (sonnet). Security reviewer reviews. Spec §3.8, §4.0.

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

### Task 12: Questions per seat

Seat: eng-questions (sonnet). Spec §3.10.

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

### Task 13: Triggers — NOTIFY kick, triage off, alert changes

Seat: eng-trigger (haiku, simple). Spec §3.11, §3.13.

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

Seat: eng-docs (sonnet). Spec §3.5, §3.13.

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

- [ ] **Step 2:** run → FAIL. **Step 3:** rewrite: moderate/complex jobs use `swarm activate --drive`; the invoking session is "requester/observer"; the EL brief template (seat requests, worktrees per writer, GOAL-READY with requirements blob id); PM event procedure scoped to non-drive jobs; the ruling/UNREACHABLE semantics; never /tmp; test-slot and `swarm ci wait --timeout 540`. **Step 4:** run the doc tests → PASS. **Step 5:** commit "docs: drive mode in the engineering-team skill and reference".

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

### Task 16: Chaos suite

Seat: qa-chaos (sonnet). Spec §6.2 scenario 3.

**Files:**
- Create: `tests/test_drive_chaos.py`
- Test helpers reuse Task 15's env; passes run in a child process (`python -m swarm supervise` with `SWARM_CRASH_AT`) so barriers SIGKILL a real process.

- [ ] **Step 1: Write the tests** — each ends MET or UNREACHABLE within `max_passes`:

```python
BARRIERS = ["before_claim", "after_claim", "after_run_file", "after_marker", "after_runner_start",
            "runner_mid_session", "before_finish", "mid_terminalization", "mid_rollover"]

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

- [ ] **Step 2:** run `cd tests && SWARM_TEST_BACKEND=sqlite /var/tmp/test-slots/test-slot -- python3 -B -m unittest test_drive_chaos` → PASS once Tasks 1–15 are in.
- [ ] **Step 3: Commit** — "tests: drive-mode chaos suite with SIGKILL crash barriers".

---

### Task 17: Real-systemd test (box only)

Seat: qa-chaos (sonnet). Spec §6.2a.

**Files:** Create `tests/test_drive_systemd.py` (skip unless `SWARM_TEST_SYSTEMD=1` and `runner.scope_available()`).

- [ ] **Step 1:** tests: runner SIGKILL → next `reap` stops the scope and its forked grandchild (assert via `systemctl --user show` + `/proc`); `swarm halt` racing a launch leaves no live scope and no open row; writer fencing waits for `unit_presence == "absent"` before relaunch.
- [ ] **Step 2:** run on the coder box: `cd tests && SWARM_TEST_SYSTEMD=1 /var/tmp/test-slots/test-slot -- python3 -B -m unittest test_drive_systemd` → PASS; record the output in the job's evidence file.
- [ ] **Step 3: Commit** — "tests: box-only real systemd containment checks for drive mode".

---

### Task 18: Compatibility with older clients

Seat: eng-board (sonnet). Spec §9.

**Files:** Modify `lib/swarm/board/postgres.py` (set `swarm.client_schema` on connect; triggers on `jobs, agents, messages, restarts, blockers, seats, drive_locks, events` refusing writes to drive jobs without it), `lib/swarm/board/sqlite.py`/`file.py` (guard older clients honour, decided by the test); Create `tests/test_drive_compat.py`.

- [ ] **Step 1:** test that a connection without the setting cannot `INSERT INTO messages` for a drive job (Postgres, CI) and can for a non-drive job; test the 0.2.3 release (`git worktree add ~/work/swarm-023 v0.2.3` in CI's workspace, never /tmp) opening a schema-25 SQLite and file store: assert it refuses, or add the guard until it does.
- [ ] **Step 2–3:** implement, run in CI (`swarm ci wait --repo fcarucci/Swarm --sha <sha> --timeout 540`), commit "board: older clients cannot write drive jobs".

---

### Task 19: Live acceptance on the coder box (manual gate before landing)

Seats: qa (sonnet) runs it, EL checks. Spec §6.3.

**Files:** Create `e2e/drive_live.sh`, `e2e/drive_live/toy-repo-template/` (tiny Python CLI + one test), `e2e/drive_live/LEAD-met.md`, `e2e/drive_live/LEAD-unreachable.md`.

- [ ] **Step 1:** the script: create `~/work/drive-accept/<stamp>/{toy.git,toy}` (bare remote + clone), `swarm activate --job drive-accept-<stamp> --goal "$(cat goal)" --drive --lead-brief .../LEAD-met.md --workdir ~/work/drive-accept/<stamp>/toy --budget max_cost_usd=10`, then only `swarm status --job ...` every 60 s until terminal or 3 h; no Claude Code session is involved; exit 0 iff MET with the landed head equal to the judged candidate and CI (the toy repo's `make test` run by `swarm ci` local adapter) green on it.
- [ ] **Step 2:** run it once for MET and once with `LEAD-unreachable.md` (expects UNREACHABLE). Attach `swarm transcript export --job ...`, `swarm snapshot --job ...` and the supervise log excerpt to `docs/superpowers/evidence/2026-10-drive-live.md` (redacted).
- [ ] **Step 3:** commit the script and evidence: "e2e: live drive-mode acceptance on the coder box".

---

### Task 20: Security review and final judging

Seats: security reviewer (sonnet; read-only), judge (opus).

- [ ] **Step 1:** security reviewer reviews the whole branch diff against spec §4.0–§4.6 and the round-1–3 findings tables, focusing on: receipt forgery paths, `swarm land` checks, mirror git sanitisation, person path, budget reservation races, path validation (workdir roots, /tmp, symlinks), prompt fencing. Report with `swarm review --job J --artifact feat/swarm-autonomy@<sha> verified|changes --report <path>`.
- [ ] **Step 2:** fixes go back to the owning seats (drive pipeline once available, otherwise EL dispatch).
- [ ] **Step 3:** judge (opus) records the goal verdict on `main@<landed sha>` with `--candidate` and `--req <spec blob id>`, citing: CI green on the exact SHA, Task 15/16 results on all backends, Task 17 and Task 19 evidence.

---

## Self-review notes (EL)

- Spec coverage: §3.1 → T2, T6, T8; §3.2–3.3 → T7, T8; §3.4 → T3, T5, T6; §3.5 → T7, T8 (prompts), T14; §3.6–3.7 → T10; §3.8 → T11; §3.9 → T8, T9; §3.10 → T12; §3.11 → T5, T13; §3.12 → T6, T9; §3.13 → T8, T13, T14; §4.0 → T4, T5; §4.1 → T7, T9; §4.2 → T6; §4.3 → T6; §4.4 → T5, T9; §4.5 → T8; §4.6 → T5, T6; §5 → T1; §6 → T15–T17, T19; §9 → T18.
- Names used across tasks: `claim_drive_launch`, `request_seat`, `set_seat_state`, `seat`, `seats`, `ledger.load/create/unfinished`, `Intent`, `receipts.write/find/args_sha`, `next_actions`, `Snapshot`, `Action`, `driver.enforce_deadlines/reconcile/execute/terminalize/rollover/drive`, `launch.spec_for(..., budget_usd, read_only, resume)`, `runner.unit_presence`, `runner.parse_usage`, `mirror.Mirror`, `land.land`, `hooks.kick`, `person.require`, `drive_authz.seat_request_authorized` — each defined in exactly one task's Interfaces block.
