# Swarm autonomy: a goal job runs to MET or UNREACHABLE with zero coordinator turns

Status: design, revision 6.1 (EL, job swarm-autonomy, 2026-10-10). Base: github/main 2f194bb.
Revisions 2, 3 and 4 resolve Codex review rounds 1 (23 findings), 2 (23) and 3 (9); revision 5 applies the design
judge's fixes F1–F24 (`~/work/swarm-autonomy/JUDGE-design.md`, each marked inline as **[Fn]**) and the PM's
ruling that drive mode is the default for goal jobs (option A, **[A]**); the delta re-review's RR1–RR8 are marked
**[RRn]**; the re-judge's G1–G12 are marked **[Gn]**; see §10.

## 0. The requirement

Francesco, 2026-10-10, verbatim:

> "the swarm is supposeed to complete their work autonomously until the goal is met, why does it need you?"
> "make it so the swarm NEVER needs you to comlete a goal."
> "how are you going to fix the swarm so this NEVER happens again and a swarm is guaranteed to complete their
> work until either the goal is met or it's juddged unreachable?"

His nine points, each answered in the section named:

| # | Point | Where |
|---|-------|-------|
| 1 | Terminal states only MET, UNREACHABLE (judge verdict with reasons), CANCELLED (a person) | §3.1, §3.7 |
| 2 | The supervisor drives the job on timer + NOTIFY; computes the next action; invariant | §3.2, §3.3, §3.11 |
| 3 | The lead runs headless under the supervisor; `swarm seat request`; hand-backs are board events | §3.4, §3.5 |
| 4 | Automatic pipeline DONE → review → fix → judge; no-progress rounds force MET/UNREACHABLE | §3.6 |
| 5 | Stall recovery: relaunch/replace lost or silent agents, the lead included | §3.9 |
| 6 | Human questions have a default and an expiry; only the asking seat waits | §3.10 |
| 7 | Budgets force a ruling, never a silent stop; `swarm halt`; allowed_workdirs | §4 |
| 8 | Proof by test: E2E MET, E2E UNREACHABLE, chaos | §6 |
| 9 | Fix doctor's "FAIL supervise timer ?/?" | §5 |

Limits of the guarantee are stated plainly in §2.2.

Further constraints (Francesco, standing): every agent joins the board; Haiku for simple tasks, Sonnet for
coding, Opus for judges and leads; rebase-only landings with CI green on the exact SHA; targeted tests only on
the box via `/var/tmp/test-slots/test-slot`, full suites only on GitHub Actions; CI waits with
`swarm ci wait --timeout 540` in the foreground; never /tmp for work trees; judges only judge. A detached
`claude -p` launched by an agent is denied by the auto-mode classifier ("Create Unsafe Agents"): only the
supervisor's systemd-launched sessions start agents.

## 1. What exists, and why the coder-webapp job stalled

### 1.1 Map of the current machinery (github/main 2f194bb)

- **Supervisor pass** (`lib/swarm/supervisor/command.py` `run_pass`/`_pass`): a systemd user oneshot
  (`swarm-supervise.service`) fired by `swarm-supervise.timer` every `[supervise] timer_minutes` (5). One pass
  per machine+user (flock). Order: reap dead runners → clean resume markers → `pipeline.run` → `orphans.run`
  → `cli.sweep_jobs` (auto-close, stuck closing, pending finals) → replace stuck-closed agents (`candidates`
  → `_recheck` → `_act`) → prune enrolments → slow finals → `bg.reap_orphans` → events listener keep-alive
  and `events_safety.run_check`.
- **Launch** (`supervisor/launch.py`): `claude -p --session-id S --max-turns 60 --permission-mode auto
  --output-format json --setting-sources user [--model M] [--max-budget-usd X]`; `codex exec --json --sandbox
  workspace-write --skip-git-repo-check [-m M] [rollout token budget] -`. `check_safe` refuses every bypass flag
  and project/local setting sources.
- **Runner** (`supervisor/runner.py`): one detached process per session, session in a transient
  `systemd-run --user --scope` unit, wall clock, enrol deadline (`enrol_minutes` 5: not on the board → killed),
  kill when its agent row closes, run files + flock in the private dir, `reap` finishes dead runners' runs.
- **Enrolment** (`enrolment.py`): host-private records written only by unsandboxed hooks/CLI; the only
  authority for "this host user runs that agent/job, in that cwd, on that harness". `local_record` uses the
  hook-written job record (`find_job`); `swarm activate` also writes an owner record (`write_job_owner`).
- **Resume markers** (`supervisor/markers.py`): bind a launched session to its restart row so its hooks enrol
  it under the intended name.
- **Budgets** (`supervisor/budget.py`, `[supervise]`): per agent 3, per job 6 restarts; 180 job minutes;
  480 host minutes/day; 2 concurrent replacements/host; backoff 2/10/30 min.
- **Review pipeline** (`supervisor/pipeline.py`, `review.py`): `swarm done --artifact REF` hand-offs →
  judge seat (Opus via `[models]`/`[pipeline] judge_model`) → `not_met` → fix worker → re-judge;
  `met` → finalizer (`integrator` in the coding recipe of `skills/engineering-team/swarm_plugin.py`: rebase,
  exact-SHA CI, fast-forward push, `INTEGRATED`); `FINALIZE_BLOCKED` → fix worker; all finalized and nobody live
  → `complete_finalized` closes the job. Bounded only by the restart budgets above.
- **Orphan recovery** (`supervisor/orphans.py`): no agents and no coordinator contact for 15 min → a
  replacement coordinator session (3/day, backoff).
- **Stuck** (`supervisor/stuck.py`): `dead` / `tool` / `silent` (90 min) → agent closed → replacement with a
  brief (`supervisor/brief.py`: original task, posts, transcript tail).
- **bg reaping** (`bg.py`): orphaned background commands of finished agents/closed jobs are stopped each pass.
- **Events** (`events_listener.py`, `events_safety.py`, `board/events.py`): opt-in listener (`[events]
  enabled = false` by default) turning webhooks/polls into `post_event` rows; `swarm event wait` is the
  orchestrator's arm; `events_safety` raises `waiter-stale`, `stalled`, `unacked`, `unread` SWARM-ALERTs.
  Postgres NOTIFY channels `swarm_board`/`swarm_state` exist (watch/snapshot use them).
- **Ask/answer** (`skills/ask-answer/swarm_plugin.py`, `board/blockers.py`): a question is a blocker
  (`kind=question`), with optional `--default` and `--expires`; `expire_blockers` applies the default on expiry.
  Any open blocker rolls up into the job's `waiting_on`.
- **Engineering team** (`skills/engineering-team/SKILL.md`, `references/*.md`): the invoking session is the
  PM; it activates, polls/waits on events, makes every host-level spawn, dispatches reviewers, lands.
- **Verdicts** (`board/base.py` `VERDICTS = ("met", "not_met")`, Postgres CHECK `jobs_verdict_check`).
- **Models** (`models.py`): `[models.<host>]` role → model, `mode = default|enforce|off`.

### 1.2 Root causes of the coder-webapp stall

1. **All staffing is a coordinator turn.** The skill makes the PM the only spawner; Claude Code subagents
   cannot spawn (child cap) or resume finished peers, and hand-backs return to the parent session, not the
   board. With the PM blocked on a user question, nothing could be staffed or resumed.
2. **The supervisor only replaces failures.** `candidates` takes only *stuck-closed* rows. An agent that
   finished normally is never resumed, even when a verdict, answer or message needs it.
3. **One question freezes the whole job.** `pipeline.run`, `orphans.eligible`, `candidates`, `_invalid` and
   `stuck_reason` all skip a job whose `waiting_on` is set (any open blocker) or where
   `orphans.human_question` finds a recent post to the user ending in "?".
4. **The pipeline is optional in practice.** The PM drove reviews and re-judges by hand; the pipeline only adopts
   jobs with a hook-written local job record (`orphans.local_record` → `enrolment.find_job`), and its rounds are
   bounded only by restart budgets sized for crash recovery (6 per job), not for delivery.
5. **No orchestrator waiter.** `events_safety` correctly raised `waiter-stale` all day, but nothing acted on it.
6. **No terminal state other than met.** A goal job that cannot be met has no way to end except a person.
7. **Doctor false failure** (§5): `systemctl --user` without `XDG_RUNTIME_DIR` in agent shells.

## 2. Goals, non-goals, and the guarantee

### 2.1 Goals

- A goal job activated in **drive mode** reaches exactly one terminal state, MET, UNREACHABLE or CANCELLED,
  with zero coordinator turns. **[A]** Every goal job is a drive job: `--goal` implies `--drive`. Opting out
  takes `--no-drive --reason TEXT`, recorded on the job and shown by `swarm status` and `swarm doctor` as
  "not autonomous: <reason>".
- Every step (staffing, resume, review, fix, judge, integrate, goal judgment, stall recovery, budget ruling) is
  computed from board state and executed by the supervisor pass.
- Works on Claude Code and Codex, on Postgres, SQLite and file backends. No new always-on process: only the
  existing timer-driven pass, its per-session runners, and (already existing, opt-in) the events listener.

### 2.2 The guarantee, and its honest limits

**Guarantee.** While (a) the owner host's supervisor timer runs and (b) the board is reachable, every active
drive-mode goal job is, at every pass, in one of these states: a live seat session or runner is responsible for
progress; or the pass launches one; or the job is in a bounded backoff with a recorded `not_before`; or the job
is terminal. Every loop is bounded by a budget whose exhaustion makes a *ruling* due: a ruling judge that may
only record MET or UNREACHABLE. Ruling acquisition has an absolute deadline (`ruling_deadline_minutes`, 120,
from the moment the ruling became due); every concluded ruling attempt without a valid verdict (failed launch,
pre-launch refusal, non-enrolment, timeout, exit without a verdict) counts against `ruling_attempts` (2), and
at the deadline or the attempt limit the supervisor records UNREACHABLE itself, labelled mechanical, with the
cause. So a job ends MET, UNREACHABLE or CANCELLED within `max_wall_hours` + `ruling_deadline_minutes`, or
stays exactly where it is while (a) or (b) fail.

**Limits, stated plainly.**
- *Owner host down.* Only the owner host's supervisor drives a job (launch authority is host-private
  enrolment, §4.6). If that host is down, the job does not progress until it is back (the timer has
  `OnBootSec=2min`; all state is on the board and resumes idempotently). Moving a job to another host is a
  person's action (`swarm job move`, existing); automatic cross-host failover is out of scope (ruling R11).
- *Board down.* Nothing progresses; the pass notes the outage (existing `outage.py`) and resumes.
- *systemd user manager down on the owner host.* Session containment cannot be confirmed; launches stop and
  terminalization waits at its fencing step (it first tries a bus-free check, §3.9). This is treated like the
  host being down.
- *Database-level attackers.* A writer with the board's database role can deny service (close or delete rows,
  flood the tables). The design guarantees integrity against them (no launch, settlement or terminal outcome on
  their word, §4.0) but not availability; the driver detects and reports such tampering (§3.2).
- *External target pushes.* A push to the target branch by something outside this job between the goal judge's
  verdict and the close makes the `met` stale; the goal judge reruns (§3.7). Very busy target branches can
  therefore exhaust the budget and end in UNREACHABLE.
- *Budgets.* "Guaranteed to complete" means guaranteed to reach a *ruling*: when budgets run out the outcome
  may be UNREACHABLE ("budget exhausted before the goal was met", with the judge's assessment), not MET.
- *Judgment quality.* MET and UNREACHABLE are a model judge's rulings; the design guarantees one is recorded with
  reasons and evidence, not that it is right. A person may reopen with a new goal revision.
- *Harness/provider outages* (API down, quota exhausted) burn launch attempts and backoff; sustained failure
  ends in the mechanical UNREACHABLE with the cause named, rather than an endless loop.
- *halt* (§4.3) is a person stopping the job on purpose; the job is then neither progressing nor terminal until
  the person unhalts or cancels. That is deliberate.

### 2.3 Non-goals

- Replacing Claude Code's in-session subagents for *goal-less* jobs: a job without a goal is not driven.
  **[A]** (The earlier non-goal "drive mode is opt-in" is deleted: every goal job is driven unless explicitly
  opted out with a reason.)
- Cross-host failover of the driver; multi-owner jobs.
- Changing judge semantics beyond adding UNREACHABLE and the ruling constraint.

## 3. Design

### 3.1 Drive mode and terminal states

- `swarm activate --job J --goal G [--lead-brief PATH] [--workdir DIR] [--requirements PATH]...
  [--lead-harness claude|codex] [--max-cost-usd N] [--ruling-reserve-usd N] [--budget KEY=VALUE ...]` (**[A]** `--drive` is implied by `--goal`; `--no-drive
  --reason TEXT` opts out) from any shell (a person's terminal, a coordinator, a script). It writes the job,
  the owner enrolment record (existing `write_job_owner`, now with `cwd=DIR`), `drive.enabled=1`, the budget
  snapshot, and a **lead seat request** (§3.4). Then it kicks the supervisor (§3.11) and returns. Nothing else
  needs to stay alive.
- **Verdicts.** `VERDICTS = ("met", "not_met", "unreachable")` (schema 25; Postgres CHECK constraint replaced,
  SQLite CHECK via table rebuild in the existing migration framework). `unreachable` requires `--reason` and
  `--details` (the report: what was tried, why it cannot be met, what would change that).
- **Typed verdicts.** Today `review.verdict_data` writes every artifact judgment into the job's legacy
  latest-verdict columns too, so an artifact `met` looks like goal completion. In drive mode each verdict has a
  `scope`: `artifact` (judges one hand-off), `goal` (judges the job's goal on the integrated target, §3.7) or
  `ruling` (§3.6). Scope and the activation **generation** are stored with the verdict (job data
  `drive.verdict.<generation>.<scope>...` and a new `verdict_scope` column); only a `goal` or `ruling` verdict
  of the current generation can make the job terminal. The legacy columns keep being written for display and
  for non-drive jobs.
- **Generations.** `drive.generation` starts at 1 and is incremented by every (re)activation and goal change.
  Seats, action ids, round counters, wake watermarks, `GOAL-READY`, ownership and integration receipts all
  carry the generation; anything of an older generation is ignored for decisions (kept for history).
  **Rollover** is done by the driver, not by the activating command: activation writes
  `drive.generation_requested = n+1`; the next pass (a) stops every live session of generation n (scope stop,
  rows `cancelled`), (b) waits until each scope is confirmed absent (§3.9), (c) releases generation-n locks and
  reservations and marks its seats `released`, its open blockers `cancelled`, then (d) atomically sets
  `drive.generation = n+1` in the ledger and on the board and requests the new lead seat.
- **Terminal transitions** (driver only, under the existing close guard):
  - current-generation `goal` or `ruling` verdict `met` (§3.7) → job `completed`, outcome `met: <reason>`;
  - current-generation `unreachable` → job `failed`, outcome `unreachable: <reason>`, `closed_by='driver'`;
  - a person's `swarm deactivate` → `cancelled` (existing).
  `goal_is_unmet` treats `unreachable` as settled for closing; the `jobs_keep_goal_jobs` trigger is unchanged
  (it only refuses the old auto-close outcome).
- **Terminalization** (any terminal verdict, mechanical UNREACHABLE included): (1) set `drive.closing` (no new
  claims); (2) stop every live session of the job and confirm scopes absent; (3) settle open current-generation
  blockers as `expired` with detail "job concluded: <verdict>" (no human answer is required); (4) release locks
  and reservations; (5) close the job in one transaction with a driver close guard that checks the verdict's
  generation, scope, candidate (§3.7) and receipt, and permits open-but-settled blockers; (6) capture
  transcripts and `swarm learn` the outcome (best effort, after the close). A crash between steps resumes at
  step 1 on the next pass (`drive.closing` persists in the ledger).
- A drive-mode job never shows `idle`, `stalled` or `waiting for the coordinator`: `swarm status` shows
  `driving: <next action or the running seats>` or `ruling`, or `halted by <person>`.

**[A] Defaults for an implied drive job.** Without `--lead-brief`, activation writes a generated lead brief
(`<workdir>/.swarm/lead-brief-<job>.md`: goal, task, the requirements paths, the standing rules of §3.12, and
"plan seats, request them, post GOAL-READY") and uses it. Without `--workdir`, the current directory is used
if it lies under `[supervise] allowed_workdirs` and is not under /tmp; otherwise activation fails and says to
pass `--workdir`. Existing active goal jobs are never converted silently (§9).

**[F10] Pinned requirements.** `--requirements PATH` (repeatable; default: the goal text itself, stored as a
blob in the owner record) pins each requirements file's git blob id in the host-private owner record at
activation. `GOAL-READY` may add requirement files but cannot replace or drop a pinned one; the goal judge and
any ruling judge receive the pinned set plus additions, and `--req` must name the pinned set's digest. A ruling
without a `GOAL-READY` judges the current target head against the pinned requirements.

**[F2] The ledger is the oracle and is written first.** Activation creates and fsyncs the ledger (file and its
directory) before it writes the board row; the board's `drive.enabled` is a copy. Every pass enumerates ledger
*files by name*: a file that fails to parse or has an unknown version is still a job: the driver posts
`DRIVER-LEDGER-BAD` once, `swarm doctor` FAILs, and the job holds (no launches) until a person repairs it with
`swarm drive repair --job J` (rebuilds from the board plus receipts, person path). A board job with
`drive.enabled` and no ledger on its owner host raises `DRIVER-ORPHAN` (doctor FAIL). The other passes
(pipeline, orphans, candidates, stuck) decide "this is a drive job" from the ledger (and, for safety, also skip
jobs whose board row claims drive). `swarm job move` refuses a drive job unless `--with-ledger`, which copies
the ledger and receipts to the target host through the person path.

**[F6] Ledger-first terminal records.** Terminal state is decided from the ledger only. Before any terminal
board write the driver fsyncs `led.terminal_intent = {verdict, scope, generation, candidate, req, by, at}`;
then writes the board verdict and closes. Driver-authored records (mechanical UNREACHABLE, DRIVER-* posts,
rollover) are recognised through the ledger, never through receipts or board rows. `enforce_deadlines` reads
`led.terminal_intent`/`led.terminal` and never the job row's verdict. Rollover step (d) writes the ledger first,
then the board.

**[RR1] Activation order and recovery.** Activation writes, each durably (fsync file and directory) before the
next: (1) the host-private owner record (requester session, work roots, pinned remotes, pinned requirements);
(2) the ledger with `state = "activating"`; (3) the board job row with `drive.enabled`; (4) the lead seat request
and its driver-authored authorization; (5) the ledger `state = "active"`. A pass that finds an `activating` ledger
with its owner record completes steps 3–5 idempotently. A ledger without an owner record on this host is
`DRIVER-ORPHAN` (hold, doctor FAIL), never silently skipped. **[RR1a]** (PM ruling after the Task 12 review) The board's `drive.enabled`
claim is written right after the owner record and before the ledger, so a ledger never exists without the claim
(core expiry relies on the claim when it cannot read a ledger); a claim without a ledger is `DRIVER-ORPHAN` only
after `activation_grace_seconds` (120).

**[RR2] Terminal intent is pending until committed.** `led.terminal_intent` is a *pending* outcome;
`led.terminal` is set only after the board close succeeded. If the closing gate rejects a pending MET (target
moved, requirements changed, audit failure), the driver durably clears `terminal_intent` and `closing`, records
the rejection in the ledger, and schedules the goal judge again; budgets, the progress counter and
`enforce_deadlines` keep running. A mechanical UNREACHABLE has no gate and cannot be rejected. **[G4]** A pending
intent whose gate cannot be evaluated (git host unreachable, mirror fetches timing out) is bounded: once it is older
than `close_deadline_minutes` (60, configurable) it is rejected as "gate not evaluable", cleared like any other
rejection, and from then on `enforce_deadlines` applies normally (a ruling becomes due and its deadline runs).

**[G6]** `--drive` remains an accepted no-op flag (old scripts keep working); it is never required.

**[G10]** The generated lead brief under `<workdir>/.swarm/` is added to the checkout's `.git/info/exclude` at
activation (and `.swarm/` with it), so writer seats never commit it.

**[M1] Supervisor opt-in (main 9cdecba, B3).** New configs have `[supervise] enabled = false` and no timer. A goal
job cannot be driven on such a host, so activation of a drive job refuses when the owner host's supervisor is
disabled (or switched off), naming the fix: `swarm supervise enable`, or `--no-drive --reason TEXT`. Doctor FAILs
for an active drive job whose owner host has the supervisor disabled. Disabling the supervisor while drive jobs
are active (`swarm supervise disable`) warns and lists them; it is treated as a global halt of those jobs ([F5]
semantics: deadlines shift), not as tamper. Owned by plan Task 6 (activation) and Task 1 (doctor).
Additions (design judge, board #27577):
1. `swarm supervise disable` itself writes the [F5] halt (reason `supervisor disabled`, actor recorded) into every
   drive job ledger owned by this host+user and stops their live seats (scopes confirmed absent); `swarm supervise
   enable` unhalts exactly those halts and shifts their deadlines by the halted time ([F5]).
2. Activation and doctor check that a supervisor **timer actually runs** (the user unit `swarm-supervise.timer` is
   active and enabled, read with the manager environment of §5, and the last pass is younger than
   `2 × timer_minutes`), not only that the config says enabled. Drive activation is refused on macOS and Windows,
   where no timer is installed (`--no-drive --reason` remains available there).
3. `swarm supervise disable` is refused for enrolled seat sessions, like `swarm halt` ([F5]); the requester session
   and the person path may run it.
4. A fourth breaking change for the release notes and skill text: with the supervisor off by default and "goal
   implies drive", `swarm activate --goal` fails out of the box until `swarm supervise enable` has been run (the
   error names it and `--no-drive --reason`).

### 3.2 The job driver

New module `lib/swarm/supervisor/driver.py`, called from `_pass` **first**: `enforce_deadlines` for every drive
job (cheap, ledger + job row), then reconciliation (§4.5, ≤20 s, rotated), then the per-job drive steps, then
the existing housekeeping (`runner.reap` of non-drive runs — drive runs are reaped by the driver, **[F7]** — sweeps, finals, bg reaping), which gets a deadline
too (`runner.reap` stops after 20 s and resumes where it left off next pass). The driver is it **owns** drive-mode jobs (the pipeline/orphan/candidate passes skip them, the way
`pipeline.run` already returns `managed`). It reuses the pipeline's transition logic, the launch/runner/marker
machinery, `record_restart` as the atomic launch ledger, and the stuck sweep.

The driver enumerates **unfinished private ledgers** (§4.0), not the board's active jobs, so a board row
changed by an untrusted writer cannot make a job silently drop out. For each unfinished ledger whose owner
record is on this host+user (`find_job_owner` or `find_job`): if the board row disagrees with the ledger
(closed, goal changed, drive flag cleared, supervise off) without a trusted record (a person-path command on
this host writes a private receipt for `deactivate`, `activate`, halt and unhalt), the driver posts
`DRIVER-TAMPER <what>` once, rewrites the board row from the ledger where the backend allows (reopen, restore
goal and flags), and otherwise treats the job as halted until a person acts (§2.2, database-level attackers).
Then:

```
snap = Snapshot.read(board, job)          # one consistent read: job, seats, restarts, verdicts,
                                           # hand-offs, blockers, recent markers, budget ledger
action = next_action(snap, cfg, now)      # PURE: no I/O; fully unit-tested
execute(action)                            # idempotent; token-guarded; rechecks before the launch
```

`next_action` returns one of `Launch(seat, kind, token, prompt_parts)`, `Record(verdict|close)`,
`Hold(why, not_before)`, `Nothing(responsible=<running seat or runner>)`. The driver may execute several
launches per pass for one job (seat requests in parallel) within the host cap.

**Actions and attempts.** Every logical action has a stable **action id**
(`gen|kind|seat|artifact-group|round|event-id`); each try of it is an **attempt** (`action id#n`). An action
has explicit success criteria (e.g. a fix attempt succeeds when its seat publishes a new `swarm done` for the
group; a judge attempt when a valid verdict of the right scope exists; a lead turn when the turn ends with its
watermark acknowledged, §3.5). A concluded attempt without success is retried as attempt n+1 with backoff, up to
`max_attempts` (3) per action; then the action fails, which wakes the lead (or, for rulings, follows §2.2).

**One atomic claim.** A launch is a single board transaction, `Board.claim_drive_launch(job, generation,
action_id, attempt, seat_id, seat_rev, reserve_usd, reserve_minutes, caps)`: it checks the seat revision
(compare-and-set), the generation, budget reservations (§4.1) and host caps, and inserts the restart row with
`UNIQUE(job, action_id, attempt)`. Losing the race returns `None` and the driver re-reads next pass. Only after
the claim is the marker written and the runner started (the existing order).

**Minimal recovery path.** Before the snapshot, the driver runs `enforce_deadlines(job)`, which reads only the
ledger and the job row (no recipes, plugins, hand-off parsing or git): if `ruling_due_at + ruling_deadline` or
`activated_at + max_wall_hours + ruling_deadline` has passed and no current-generation terminal verdict exists,
it records the mechanical UNREACHABLE and starts terminalization. So a job whose snapshot or recipe keeps
failing still terminates.

**No model calls outside the ledger.** `events_safety` model triage (`[events] triage`) is never run for drive
jobs: every model session of a drive job is a seat attempt with a reservation.

**Failure isolation.** Each job is driven inside its own try/except with a per-job deadline (30 s of the 95 s
pass budget; every blocking operation — git in the mirrors, systemctl, board queries — gets a timeout of
`min(its own cap, the time left for the job and the pass)`, and an operation cut short persists its
continuation in the ledger, e.g. `mirror_fetch_pending`, and resumes next pass; tested with slow git and
systemctl stubs across several jobs); jobs are visited in least-recently-driven order (job data `drive.last_pass`), so one slow or
broken job cannot starve the others. A job whose driver step raises on `driver_error_passes` (6) consecutive
passes gets `DRIVER-ERROR <class>` posted once and a ruling made due; if the ruling cannot be launched either,
§2.2's deadline produces the mechanical UNREACHABLE naming the error.

### 3.3 next_action: priority order and the invariant

1. **Halted / kill switch / job not active** → `Nothing`.
2. **Terminal verdict present** → `Record(close)`.
3. **Ruling due** (any budget exhausted, §4.1; `no_progress_rounds` reached, §3.6; `max_wall_hours`; an
   action out of attempts with nothing else to do; repeated driver errors) → launch the ruling judge if none is
   live (rulings may exceed the host concurrency cap by one and draw on the ruling reserve); on
   `ruling_attempts` concluded attempts without a ruling, or at `ruling_deadline_minutes` → `Record(unreachable,
   mechanical)`.
4. **Pipeline transitions** (existing `pipeline.transitions`, extended in §3.6): unreviewed hand-off →
   reviewer; reviewed → judge; `not_met` → fix round on the **author seat** (resume); `met` → integrator;
   `FINALIZE_BLOCKED` → fix round.
5. **Seat requests** in state `requested` → fill (§3.4).
6. **Seat wake-ups** (§3.4): an idle seat with a pending wake reason (answer/expiry of its question, a message
   addressed to it, a verdict on its artifact handled outside step 4) → resume it.
7. **Stall recovery** (§3.9): stuck-closed or exited-without-hand-off seats → relaunch/replace.
8. **Lead wake** (§3.5): lead seat idle and lead-wake events since its last turn → resume the lead.
9. **Goal judgment** (§3.7): lead has posted `GOAL-READY`, nothing is live, and every non-withdrawn hand-off group
   is *integrated* (coding recipe) or *judged `met`* (artifact recipe, **[G3]**: there is no integration step) →
   launch the goal judge.
10. **Invariant fallback**: nothing live, nothing pending, nothing above fired → wake the lead with
    "nothing is running: post seat requests, or GOAL-READY, or UNREACHABLE-PROPOSED". Such a wake is a
    **round without progress** in the single round counter of §3.6.

**One progress counter.** There is exactly one counter that ends loops: `rounds_without_progress` (per job and
generation). It advances on every judge verdict (artifact or goal) that shows no progress (§3.6), every
invariant-fallback wake, and every stall wake (§3.9); it resets on progress (§3.6). At `no_progress_rounds` (3)
a ruling is due. The other bounds are absolute (attempts per action, budgets, `max_wall_hours`); there are no
other counters and no adaptive timer.

**Invariant** (asserted in tests over every reachable snapshot of the fake-harness E2E and chaos runs, and
checked by `swarm doctor --job J`): for an active, unhalted drive job, after each pass, at least one of: a seat
session is live (agent row started/running/idle with a live runner); a launch was recorded this pass; a
`Hold` with `not_before` ≤ the longest backoff step is recorded in job data `drive.hold`; the job is terminal.
A pass that would end with none of these logs `DRIVER-INVARIANT` and forces step 10 (and, if the lead itself
cannot launch, step 3). There is no state in which the driver silently does nothing.

**[F1] Question-gated seats are responsible.** A seat gated by a receipted open question counts as responsible
for progress until the question's `until` (capped at `question_expiry`), so the invariant holds without a
launch. Step 10 never wakes a gated seat. If the only remaining responsible actor is a gated lead, the job holds
with `not_before = until`; on expiry the default is applied and the lead is woken (§3.10). Chaos test: "lead asks
the human, everything else idle".

**[F11] "Nothing else to do"** (step 3) means: no seat is requested, running, or wakeable, no pipeline action
is pending, and every pending action is out of attempts. A met → `FINALIZE_BLOCKED` → met cycle on a group without
an integration is a round without progress, and so is a lead wake caused by a `max_seats` refusal.

**[F12] Provider outages do not consume attempts.** An attempt that concludes with a classified provider error
(Claude JSON `is_error` with an API/overload/auth subtype; Codex `--json` error events of the same classes; a
runner-detected failure to reach the provider) is concluded `outage`. **[G9]** A missing or non-executable
harness binary is an `error`, not an `outage` (doctor reports it); an `outage` attempt is not counted against `max_attempts` or
`ruling_attempts`, retried with the existing backoff (2/10/30 min). The wall clock still runs, so a long outage
ends at `max_wall_hours` + ruling deadline (§2.2).

### 3.4 Seats

**Table `seats`** (schema 25; memory/file/SQLite/Postgres):
`id, job, seat (stable name, unique per job, e.g. "eng-auth"), role, title, harness, model, workdir,
brief_path, task_ref, complexity (simple|standard|complex), requested_by, requested_at, state, agent_key,
session_id, generation, revision, launches, last_outcome, wake_reason, updated_at`,
`UNIQUE(job, generation, seat)`. A new generation starts with no seats (history is kept); its lead seat is
created by the activation.

States: `requested → running → idle ⇄ running … → released | failed | cancelled | refused`.
- `idle`: the session exited; the seat persists and can be resumed. Resume = same harness session when
  available (`claude -p --resume <session_id>`, `codex exec resume <thread>`, via `hosts/resume.py`), else a new
  session with the seat brief + the existing `supervisor/brief.py` brief (posts, transcript tail).
- `released`: the lead (or a person) ended it (`swarm seat release`); also on job terminal.
- `failed`: its current action ran out of `max_attempts` (§3.2); wakes the lead. (There is no separate
  per-seat launch counter.) **[F3]** For the lead seat itself: its first failure creates one successor
  `lead~1` (same brief, the board history, a fresh session); a failure of the successor makes a ruling due
  immediately. "Wake the lead" is never the response to the lead's own failure.
- `refused`: validation failed (§4.6), with the reason posted; wakes the lead.

**CLI** (core, not a plugin, so Codex and Claude get it the same way):
- `swarm seat request --job J --seat NAME --role ROLE --brief PATH [--model M] [--harness claude|codex]
  [--workdir DIR] [--title T] [--task ID] [--complexity simple|standard|complex]` — idempotent per (job, seat):
  a repeat with the same fields is a no-op; with different fields while `requested` it updates; otherwise it is
  refused (release first).
- `swarm seat list|show|release|cancel --job J [--seat NAME]`.
- Who may request: the job's lead seat, or a person. Others are refused with a pointer to ask the lead
  (`--to @EL`).
- **Authentication is host-private, never a board field.** Board rows are forgeable (shared database role,
  `enrolment.py`). A seat row alone never launches anything. The driver launches a request only if a
  **seat authorization record** exists in the host-private enrolment directory
  (`~/.local/share/swarm/host/enrolled/seat-<sha256>.json`, same safefs rules as enrolment records), binding
  `(board_key, job, generation, seat, sha256 of the canonical request fields)` to its author. It is written by:
  the unsandboxed PostToolUse hook when the *enrolled lead seat's own session* (session id → enrolment record
  → lead seat) ran a successful `swarm seat request`. The hook hashes the **exact arguments it saw in the tool
  input** (parsed with the same argparse definition) and binds them to the row id and `revision` the CLI printed;
  the driver launches only if the current row's fields hash to that value at that revision, so a row replaced
  by another database writer between the command and the hook is not authorized; or by
  `swarm activate` (goal implies drive) for the initial lead seat; or by a person or the requester session (below). A changed
  row no longer matches its hash and is not launched.
- **Person authority** (unhalt, re-activation, a person's seat request, a person's job-level wait) is not inferred
  from missing agent environment variables (spoofable, as `ask-answer.identity` shows). It is held by exactly two
  callers (**[G6]**, reconciling with [F17]): (a) the job's requester session, through its hook receipts; (b) any
  other caller that is not an enrolled session of any job and confirms on `/dev/tty`. Enrolled seat sessions are
  refused outright. Halt follows [F5]. The tty path is a guardrail, not a boundary, against a same-user agent
  (§4.0); the requester session's authority is an accepted trust choice ([G11]).
- **Job workdir scope.** `swarm activate --goal G [--workdir DIR] [--allow-workdir DIR ...]` records the job's
  authorized roots in the owner record (host-private). A seat's workdir must lie under a job root **and** pass the
  host checks of §4.2; the lead cannot widen the job's write scope to other repositories.
- **Hand-off contract.** Only structured records count: `swarm done --artifact REF` (pipeline hand-off),
  `swarm seat release`, an open question, `BLOCKED` via `swarm ask`. A prose `DONE:` post is a status line,
  not a hand-off (consistent with `review.latest_handoffs`).

**Filling.** The driver launches a seat exactly as `pipeline.launch_action` launches a judge: a placeholder agent
key, `allocate_name`, `record_restart(reason="drive:seat:<seat>:<launch#>")` with all caps, resume marker,
`runner.start`. The prompt is a fixed template (role, title, job, goal, seat brief path to read first, the
board/CI/test rules of §3.12, the hand-off contract) plus fenced untrusted board context. The session joins the
board through its hooks (enrol deadline kills it otherwise): **every agent joins the board**.

**Hand-backs are board events.** A seat's work product is `swarm done --artifact REF` (pipeline),
`swarm seat release` by the lead, questions, and verdicts. Nothing returns to a parent session.

**Exclusive writer checkouts.** A writer seat (engineer, integrator, fix worker) claims the **worktree root**
of its workdir (found by walking up from the realpath to the first directory holding a `.git` file or directory,
without running git) as a `drive_locks` key `wt:<sha256(root)>` inside `claim_drive_launch`, and the claim is
refused if any live writer's root is an ancestor or descendant of it; two live writer seats never share a
checkout or index. Tested with nested workdirs. The lead's briefs give each writer its own git worktree (under `~/src` or `~/work`, never /tmp).
Read-only seats (judge, reviewer, QA when it only runs tests) may share.

**Ownership transfer.** If a fix round's owner seat is `released`, `failed` or `cancelled`, the driver creates
a successor seat `<owner>~<n>` (driver-authored authorization, same role, model, harness and brief, a fresh
worktree of the artifact branch) and transfers `drive.owner` to it, keeping the action's attempt history.

**[F14] Hand-offs for non-writer roles.** QA, research and report seats hand off with `swarm done --artifact
report:<path>@<git blob id>` (the file committed or at least hashed in the workdir). The pipeline treats a
`report:` artifact like any other (review optional by recipe, judge-gated if the lead marks it required), so these
seats are not retried as "ended without a hand-off".

**[F20] Worktree-root locks (implementable form).** `drive_locks` rows of kind `wt` store the root realpath
(`path` column) besides the hashed key; `claim_drive_launch` selects the live `wt` rows of the host and refuses
the claim when any stored root is equal to, an ancestor of, or a descendant of the new root (string prefix on
`path + "/"`), inside the same transaction. Root discovery and the nested-workdir test belong to plan Task 3.

**[F17] The requester session.** The session that ran `swarm activate` is recorded as the job's requester
(session id in the owner record). Its hook writes receipts for its `swarm answer`, `swarm deactivate`, `swarm halt`,
`swarm unhalt`, `swarm activate` (re-activation) and `swarm seat request` (person seat requests), so a coordinator
in Claude Code can answer, stop, cancel and staff without a tty; a person in a terminal uses the tty path. Any
other session's deactivate is tamper (§3.2), not a cancellation. **[A]** In a drive job, Agent-tool spawns by the
requester session are refused by the SubagentStart hook with a pointer to `swarm seat request`.

**[RR6] Overlap checks are serialized.** `claim_drive_launch` takes a host-wide lock before reading the live
`wt:` rows: Postgres `pg_advisory_xact_lock(hashtext('swarm-wt:' || host))`; SQLite `BEGIN IMMEDIATE` (already
serial); memory/file the store lock. Two supervisors (two OS users on one host) therefore cannot both claim
nested roots. Tested with concurrent nested-root claims from two processes.

**[B1] Seat enrolment never depends on the session's job binding** (finding, 2026-10-10: in this job, four
coordinator-spawned seats never joined the board because the parent session had several active jobs and the start
hook did not bind them to swarm-autonomy). A drive seat enrols only through its launch: the resume marker written
for its restart row names the job, seat, generation and action, and the hook binds the session from that marker,
whatever other jobs the host or parent session has active. A session that matches no marker and carries no
`[swarm job: J]` tag is never guessed into a job. The enrol deadline (`enrol_minutes`) still kills a seat that does
not join, so this failure becomes a failed attempt, never a silent unboarded worker. Test (plan Task 5):
`test_seat_enrols_from_marker_with_several_active_jobs`.

### 3.5 The lead, headless under the supervisor

- The lead is the seat `lead` (role `engineering_lead`, title `EL`, model Opus, harness from activation),
  requested at activation with the lead brief.
- A lead turn is a bounded session: read board + brief, plan, write task briefs as files in the workdir, post
  `swarm seat request` lines, `swarm seat release` finished seats, answer engineers, post `GOAL-READY <target>`
  when it believes the goal is met, then exit. It never spawns agents itself (no Agent tool use needed; the hook
  refuses spawns of a drive-mode lead with a pointer to `swarm seat request`).
- **Lead-wake events**: a seat became idle/failed/refused; a hand-off was integrated; any verdict; a question
  answered or expired; a `@EL`-addressed post; an action out of attempts; a goal `not_met`; a fallback or stall
  wake. **Watermarks, not timestamps**: each lead turn is launched with the board's current message id and event
  id as its watermark (in the prompt and on the seat row, `wake_watermark`). Wake events are board rows with ids;
  those with id > the watermark of the last *acknowledged* turn are pending, so events arriving while the lead
  runs are never lost. **Only trusted wake sources count**: driver-derived events (seat state changes, verdict
  and integration receipts, attempt outcomes, expiries applied by the driver) and agent records with receipts
  (§4.0: addressed posts, questions and answers made by enrolled seats of this job, person-path answers). An
  unreceipted board message is shown to the lead when it runs but never triggers a launch. A turn acknowledges its watermark by ending normally (runner outcome `completed`); a
  crashed turn does not, and its events stay pending. Wakes are coalesced: one resume carries all pending ids.
- A lost lead (stuck, crashed, exited without a turn summary) is relaunched by §3.9 with the lead brief + board
  history; the lead's turns are bounded by the job budgets and the progress counter.
- The PM duties of `skills/engineering-team` (NEEDS-REVIEW dispatch, CI-FAILED triage, READY-TO-LAND landing,
  staffing waves) move: review dispatch, fix rounds and landing to the driver pipeline (§3.6, §3.8); CI-FAILED
  triage to the fix round's brief (the integrator's FINALIZE_BLOCKED text and the CI logs pointer); staffing to
  the lead.

**[F4] Lead acknowledgement on any outcome.** A lead turn acknowledges the watermark it was launched with on
*any* runner outcome in which the session completed at least one turn (normal exit, max-turns, wall-clock
timeout), and also when it runs `swarm seat ack --upto <id>` (receipted). Only a session that never completed a
turn (crash on start, enrol deadline) leaves the events pending. So a lead that keeps hitting max-turns cannot
loop on the same events.

**[F15] Questions are wake events.** A receipted `swarm ask --to @role|<seat>` or a post addressed to a seat or
role is a wake event for the addressed seat (the lead for `@EL`).

### 3.6 Pipeline rounds: DONE → review → fix → judge, bounded by progress

Extends `pipeline.transitions` (shared by drive mode and the existing non-drive pipeline):

- **Review stage** (new; recipe key `review_role`, the coding recipe sets `reviewer`): an unreviewed hand-off
  launches a reviewer seat (Sonnet; never the author; never the judge). It posts
  `REVIEW <artifact> VERIFIED|CHANGES <report path>` (and on the PR where the CI host supports it). `CHANGES`
  → fix round on the author seat → new hand-off → review again. `VERIFIED` → judge. Non-coding recipes may set
  no reviewer (straight to judge, as today).
- **Fix rounds resume the owning seat.** The first `swarm done` of an artifact group (branch) records
  `drive.owner.<gen>.<group> = <seat>`; later revisions keep that owner whoever publishes them (the integrator's
  rebased hand-off included). Fix rounds resume the owner seat with the judge's or reviewer's report pointer,
  instead of today's "last joined non-judge agent's workdir".
- **Progress measure.** In drive mode a `not_met` verdict must carry `--open N` (the number of unmet acceptance
  criteria the judge counted) in addition to `--reason` and `--next`. A round makes **progress** when the new
  hand-off's artifact differs from the previous judged one **and** `open` decreased, or the judge passes
  `--progress yes` with its reason; progress also counts when an artifact group is integrated or the goal
  judge's `open` decreases. Progress resets `rounds_without_progress` (§3.3); everything else that is a round
  advances it. At `no_progress_rounds` (3) the next judge launch is a **ruling**. `max_rounds` (8 judged rounds
  per artifact group) is an absolute cap that also makes a ruling due.
- **Ruling judge.** When a ruling becomes due the driver first **quiesces**: no new claims except the ruling;
  live seats get `ruling_grace_minutes` (15) to hand off, then are stopped (scopes confirmed absent). The ruling
  judge then judges the job's goal on the current candidate. Same judge seat, prompt says it must rule `met` or
  `unreachable`; a ruling `met` must name the candidate and passes the same closing gate as a goal `met` (§3.7:
  integration receipt and exact-SHA CI); a ruling `met` that fails the gate is rejected and counts as an
  unsuccessful ruling attempt; the board sets job data
  `drive.ruling=<action id>` and `swarm verdict` refuses `not_met` while it is set (the CLI and
  `deliver_verdict` check it); a ruling verdict is stored with scope `ruling`. A ruling attempt that concludes
  without one counts toward `ruling_attempts` (§2.2). `unreachable` here means "not reachable within this job's budget and approach", with the details
  report. No silent loop is possible: every path through §3.3 either makes progress or advances a counter that
  ends in a ruling.
- Judges only judge: the judge, reviewer and integrator prompts keep today's separation; fixes are always on a
  worker/author seat.

### 3.7 Goal judgment (MET)

Artifact verdicts judge changes; the **goal verdict** judges the job. When the lead posts `GOAL-READY <target
ref>@<sha>` (normally the target branch after landing), every current hand-off is integrated, and no seat is
live, the driver launches the goal judge (Opus) on the target SHA with the goal, the requirements files the
lead names, and the evidence (CI on that exact SHA, QA reports). The verdict must carry
`--candidate <target>@<sha> --req <requirements revision>` (the requirements file's git blob id, named by the
lead in `GOAL-READY`). Immediately before the close transaction the driver fetches every destination into its
private mirrors and requires each destination's target **head to equal** the candidate SHA, and the requirement
revision to be unchanged; inside the transaction it checks the ledger state it verified is unchanged. A stale
`met` (a target moved or requirements changed) is rejected and the goal judge runs again; a rerun caused by an
external target move is not a round without progress, but it does consume budget (§2.2, external target
pushes). There is no cross-system transaction with the git host, so a push landing between the fetch and the
close is not detected; that window is seconds and is stated as a limit. Outcomes: `met` → completed;
`not_met --open N --next` → lead wake with the next steps (a **goal round**, counted like §3.6);
`unreachable` → failed. The lead may also post `UNREACHABLE-PROPOSED <reason file>` to request an early ruling
(the judge decides; the lead never records a verdict). In drive mode `complete_finalized` no longer closes the
job on artifact finalization alone; it waits for the goal verdict.

**[F9] Withdrawn groups.** The lead may abandon a hand-off group with `swarm seat release --withdraw-artifact
GROUP --reason TEXT` (receipted). The goal judge requires only non-withdrawn groups to be integrated; withdrawn
groups are listed in the goal judge's prompt with their reasons. An abandoned, unwithdrawn group still blocks
step 9, and the lead is woken about it at each stall wake.

**[G3] The closing gate is per recipe (PM ruling).** Every goal job is driven ([A]), so a goal without a git
target must be able to reach MET too. The recipe chooses the gate:
- **coding recipe** (a git target exists): the gate above — candidate `<target>@<sha>`, head equality, exact-SHA
  CI, the target audit ([F24]) and the pinned requirements.
- **artifact recipe** (default for jobs with no target: research, a report, a decision): the candidate is a
  `report:<path>@<blob>` or another pinned artifact ([F14]); the gate is the goal judge's (or ruling judge's)
  `met` with `--candidate report:<path>@<blob> --req <pinned digest>`, a receipt, and the candidate's blob still
  matching the file in the workdir at close. No target, CI or audit.
The recipe is fixed at activation (`--recipe coding|artifact`, default `coding` when the workdir is a git checkout
with a pinned remote, else `artifact`) and stored in the owner record; a lead cannot change it. An E2E test runs a
non-coding goal to MET. For the artifact recipe, `GOAL-READY` takes the candidate as `report:<path>@<blob>`
(`swarm goal-ready --job J --candidate report:<path>@<blob> [--add-requirements PATH]...`), and step 9 of §3.3
requires every non-withdrawn group to be judged `met` rather than integrated.

### 3.8 Integration and landing gate

Unchanged in substance from the coding recipe, now always run by the driver:
- `met` artifact → integrator seat (Sonnet; Claude, or Codex when drive-ready, §3.12 [F18]): fetch, rebase onto the latest target (rebase only; never merge commits, squash only if the project's
  `[land] strategy = squash-ff`), push the rebased branch, open/reuse the PR, `swarm ci wait --repo R --sha
  <exact rebased sha> --timeout 540` in the foreground (repeated while pending, within the seat's wall clock).
- Rebased SHA differs from the judged SHA → new hand-off; the old verdict and CI do not cover it (existing).
- Green on the exact SHA + `met` on that SHA → the integrator runs **`swarm land --job J --artifact REF`**, which
  records a receipted *land request* and is the integrator's hand-off: the seat exits (**[G8]**, superseding the
  older "run by the integrator seat" wording). The driver then executes the landing helper in the pass: it checks
  (from the ledger and receipts, not from arguments) that the request came from the current generation's
  integrator seat, that a receipted `met` covers that exact SHA (or its patch-id twin, [F13]) and that CI is green
  on it (`swarm ci status`); claims the land leases; fast-forwards the target on every destination with plain
  fast-forward pushes only (never a force push of any kind), verifies the landed tree, deletes the branch, writes
  the integration receipt and posts `INTEGRATED <artifact>` (or `LAND-REFUSED <artifact> <why>`, which is a
  wake event for the integrator's group owner and the lead). "Checked" here means decided by code that takes no
  agent input for its decisions (F24), not tamper-proof against a same-user Claude seat. Other pushes to a target
  branch are refused by the hook guardrail.
- Red CI or a non-trivial conflict → `FINALIZE_BLOCKED <artifact> <instructions>` → fix round (§3.6).
  CI red repeatedly is a no-progress round like any other and so ends in a ruling.
- **Integration receipt, not inference.** Today `coding_integrated` treats "source branch gone remotely or its
  SHA an ancestor of a local tracking ref" as integrated. In drive mode an artifact group is integrated only with
  the receipt written by `swarm land` plus a driver check (`merge-base --is-ancestor <landed sha>
  <remote>/<target>`) on **every** destination, recorded in the ledger with the landed SHA.
- **Verification never runs in an agent-writable repository.** Repository config (`core.sshCommand`,
  `core.fsmonitor`, hooks, `include.path`) can execute code. The driver and `swarm land` verify in a
  supervisor-private bare mirror per destination (`~/.local/share/swarm/supervisor/mirrors/<sha256(url)>.git`),
  whose remote URLs are pinned in the owner record at activation (`--remote NAME=URL`, defaulting to the
  activation workdir's remotes read once by the person's activation command), with `GIT_CONFIG_NOSYSTEM=1`,
  `GIT_CONFIG_GLOBAL=/dev/null`, `core.hooksPath=/dev/null`, `protocol.allow=never` except `https`/`ssh`/`file`,
  the minimal environment, and a timeout per command of at most 60 s and never more than the time left (§3.2). **[F8]** `swarm land`
  journals each destination in a host-private **land receipt** before and after its push (`intent`, `pushed
  <sha>`, `verified`); `swarm seat note` is not a journal. The driver completes an interrupted landing from that
  receipt (fast-forwarding the remaining destinations itself, with the same checks) before any other action
  on the group; quiesce (§3.6) exempts an in-flight `swarm land` until it finishes or its journal is completed by
  the driver. Barrier `land_after_first_destination` tests it.
- **Landings are serialized by a lease.** New table `drive_locks(key PRIMARY KEY, holder, generation,
  expires_at)` (schema 25); one key per destination, `land:<sha256 of the normalized push URL>:<target>`, for
  **every** pinned destination (all remotes and push URLs), claimed all-or-nothing in sorted order inside
  `claim_drive_launch`. The runner renews them while the session lives. Expiry alone never transfers ownership:
  a lease is reclaimable only when it has expired **and** its holder's attempt row is ended with the scope
  confirmed absent. A lease held by another host cannot be positively fenced from here, so it is never
  reclaimed: the landing holds, and a hold longer than `lease_hold_minutes` makes a ruling due ([F19]). Two supervisors therefore never land on overlapping destinations at once.

**[F13] Clean rebases reuse the review.** When the rebased branch has the same `git patch-id --stable` set as
the judged SHA (computed in the private mirror), the existing review and `met` carry over to the rebased SHA; only
exact-SHA CI is required before `swarm land`. A rebase that changes any patch-id needs a new hand-off and judge
verdict as before.

**[F19] Lease holds are bounded.** A land-lease hold longer than `lease_hold_minutes` (60) makes a ruling due
(the earlier "the ruling deadline ends the job" was imprecise: the hold first makes a ruling due, then the
ruling deadline applies). Lock rows that match no known restart or intent are reported (`DRIVER-LOCK-ORPHAN`)
and, when their holder is this host, released after fencing.

**[F24] Close-time target audit.** Receipts do not bind an unsandboxed Claude seat (§4.0). So before a MET close
the driver audits the target's history since activation in the private mirror: every commit on the target
between the activation-time head and the candidate must be reachable from a judged-met SHA of this job, or have
the same patch-id as a commit of one. A commit that fails the audit blocks the close (`DRIVER-AUDIT`) and makes a
ruling due with the finding in its prompt. "Trusted" for `swarm land` means "checked by code that does not take
agent input for its decisions", not "tamper-proof against a same-user agent".

**[RR5] Landing completes before terminalization.** Terminalization (§3.1) gets a step 0: complete every land
journal of the job from its land receipt (driver-executed fast-forwards, same checks), keeping the destination
leases until each journal is `verified` or `land_complete_minutes` (30) have passed. On failure the job still
closes, and the verdict details list the destinations left partially landed with their SHAs (`DRIVER-LAND-PARTIAL`
is posted). `enforce_deadlines` therefore never interrupts a landing midway without recording it.

**[RR8] The driver executes landings.** `swarm land` run by an integrator seat (Claude or Codex) only records a
receipted *land request* for the artifact; the driver executes the helper in the supervisor pass, with its
private mirrors and land receipts, which never lie under a Codex writable root. The target fast-forward is the same
for both harnesses. **[G2]** The integrator does not wait: the land request is its hand-off and the seat exits; the
driver posts `INTEGRATED` or `LAND-REFUSED` (a wake event). The integrator still pushes the rebased branch and waits
on CI with `swarm ci wait`, so a Codex integrator must be drive-ready with network.

**[G2] Driver credentials are pinned.** The mirrors run with no global git config and a minimal environment, which
removes the user's credential helper and ssh agent. So activation pins, per destination in the host-private owner
record, exactly one credential source: an allowlisted credential-helper command (`[drive] credential_helpers`,
default `["gh auth git-credential"]`), or an ssh key path, or "use `SSH_AUTH_SOCK`". The mirror git receives it
explicitly (`-c credential.helper=<pinned>` with `credential.useHttpPath=true`, or
`GIT_SSH_COMMAND="ssh -i <key> -o IdentitiesOnly=yes -o BatchMode=yes"`, or `SSH_AUTH_SOCK` passed through for
that call only); nothing else from the user's config is read. Fetches into the mirrors use the same source, so
private repositories work. `swarm doctor` runs a dry-run `git ls-remote` per pinned destination with the pinned
source and FAILs on an error. **[G2a] Helper environment.** The mirror git runs with `HOME=<private dir>`, so a
helper such as `gh auth git-credential` would not find `~/.config/gh`, and the systemd pass has no
`SSH_AUTH_SOCK`. Activation therefore also pins, per destination, the helper's config location
(`GH_CONFIG_DIR`, defaulting to the activating user's resolved `~/.config/gh`, or for other allowlisted helpers
their `HOME`) and, for `ssh-agent`, the agent socket path resolved at activation. These are passed only in the
environment of that one git invocation (and so to its helper), never to sessions or other commands; doctor's
`ls-remote` check uses exactly the same environment, so a socket that disappears after reboot is reported. The
Task 11 test lands against an authenticated remote fixture through a real gh-style config dir (a `hosts.yml`
with a token read by an allowlisted helper that honours `GH_CONFIG_DIR`), never a fake helper that ignores it.

### 3.9 Stall recovery

- **Lost/silent seats**: the existing stuck sweep (`stuck_reason`: dead, tool, silent) closes the row; in drive
  mode the driver relaunches the *seat* (not a coordinator) with the seat brief + `brief.build_brief` context
  (posts, transcript tail). The question gate in `stuck_reason` becomes per seat (§3.10).
- **Writer fencing.** A seat is never relaunched while its previous attempt may still write: the relaunch
  requires the previous attempt's restart row to be ended **and** its scope unit to be *confirmed* gone. Today
  `runner._unit_state` returns `("", "")` when systemctl itself fails and `_unit_exists` reads that as absent;
  the driver uses a new tri-state `unit_presence() -> present | absent | unknown` and relaunches only on
  `absent` (systemctl answered "not loaded"). On `present` it stops the scope (`runner._stop_session`) and waits
  for a later pass; on `unknown` it falls back to a bus-free positive check (**[F16]**): only on cgroup v2,
  with the parent `/sys/fs/cgroup/user.slice/user-<uid>.slice/user@<uid>.service/` readable and the slice the
  runner records for the scope (from `systemctl show -p Slice` at launch, stored in the run file) present, is the
  scope's directory being absent or its `cgroup.procs` empty taken as `absent`; any other layout (cgroup v1, a
  missing slice, a mismatch) stays `unknown`. A wrong-slice layout is tested. If that is unreadable too it holds (posted once): see §2.2, systemd user manager down.
  Tested with a prolonged user-bus failure.
- **Exited without a hand-off**: a seat whose session ended `completed` with no `swarm done`, release, or open
  question since its launch is an unsuccessful attempt of its action (§3.2): retried with "you
  ended without a hand-off: finish, or ask with a default"; after `max_attempts` → seat `failed` + lead wake.
- **Lead lost** → relaunched (§3.5). **No progress for T**: `drive.stall_minutes` (90) without a new hand-off,
  verdict, seat-state change or lead turn while seats are live → the lead is woken with "no progress for T";
  is a round without progress (§3.3).
- **Supervisor killed or restarted**: see §4.5 (claim protocol and reconciliation of every open ledger row,
  including claims that never got a run file).

### 3.10 Human questions never block unrelated work

- In drive mode `swarm ask --to human|@role` requires `--default` (refused otherwise, with the reason) and gets
  `--expires` = `drive.question_expiry` (4h) when not given; `--blocks <seat>` defaults to the asking seat.
- The driver, `pipeline`, `stuck_reason` and `candidates` stop reading the job-level `waiting_on`/
  `human_question` for drive jobs; they read open question blockers **per seat**: only the blocked seat is not
  relaunched or woken; every other seat and the pipeline continue.
- **Core binding.** Blockers gain core columns `seat` and `generation` (schema 25), set by core `open_blocker`
  from the asking agent's seat (not from the plugin payload), so the per-seat gate and the answer wake work even
  if the ask-answer plugin is absent or its payload is unreadable. Wakes are derived from the durable
  `blocker_events` rows (`resolved`, `expired`).
- On answer or expiry (existing `expire_blockers` applies the default, posts `Q<n> default applied`), the
  blocked seat gets a wake (§3.4 step 6) with the answer and whether it was assumed.
- The job rollup still shows open questions (so `swarm status` and watch display them), but in drive mode
  `derive_job_status` reports `driving` while seats or the pipeline are active.
- A person's explicit `swarm wait` on the whole job remains a person's choice (shown as "waiting on X (set by
  person)"); an agent's `swarm wait` in drive mode is recorded per seat and is bounded.

**[A]** Because every goal job is driven, `--default` becomes mandatory for every question in a goal job (a
breaking change for goal jobs; release notes and §9).

**[RR3] Drive questions expire only through the driver.** The generic `expire_blockers` sweep skips blockers of
drive jobs; the driver applies the default when the ledger's effective deadline (`until` + `halted_total_seconds`) has
passed, and never while the job is halted. So a halt cannot let a default be applied behind a person's back.

### 3.11 Triggering: timer, hook kicks, NOTIFY

- The timer stays the correctness floor, fixed at `[supervise] timer_minutes` (5); no adaptive cadence.
- **Hook kicks**: the unsandboxed PostToolUse hook, on a successful `swarm done|verdict|seat|ask|answer|post
  --to`, runs `systemctl --user start --no-block swarm-supervise.service` (with `runner._manager_env`), at most
  once per 20 s per user (stamp in the private dir). This is starting the sanctioned supervisor unit, not an
  agent, and runs in the hook, not as an agent tool call.
- **NOTIFY**: when `[events] enabled`, the existing listener also LISTENs on `swarm_board` and kicks the pass
  (same debounce) for drive jobs. No new process.
- Correctness never depends on kicks: tests run with kicks disabled.

### 3.12 Claude and Codex parity; models; rules every seat gets

- Every seat has a harness. Launch uses `launch.spec_for` (both hosts); resume uses `hosts/resume.py` (Claude
  `--resume`, Codex `exec resume`); enrolment via resume markers (Claude session id; Codex thread id bound by
  the runner). The lead, engineers, QA and reviewers may be either; `[pipeline] judge_host` chooses the judge's.
- Codex `workspace-write` has no network unless `[codex] network_access = true` (existing profile). Seats that
  need the network (integrator, anything pushing) run on Claude unless doctor confirms the Codex profile; the
  driver posts the reason when it overrides a request.
- **Models: a per-role config table** (**[P1]**, PM ruling 2026-10-10, matching Francesco's standing policy
  "i want the engineers to be haiku"). `[drive.models]` (Claude; `[drive.models.codex]` for Codex) maps roles to
  models, with shipped configurable defaults:
  `engineer = "haiku"`, `worker = "haiku"` (fix rounds), `integrator = "haiku"`, `reviewer = "sonnet"`,
  `qa = "sonnet"`, `judge = "opus"`, `lead = "opus"` (`engineering_lead`), `security = "opus"`.
  Precedence: seat request `--model` (within `[drive] allowed_models`) > per-job override at activation
  (`--model ROLE=MODEL`, stored in the ledger; changeable with `swarm drive budget`-style `swarm drive models`) >
  `[drive.models]` > `[models.<host>]` (legacy) > the shipped defaults. `swarm doctor` and `swarm status --job J`
  show the effective table per job.
- **Haiku engineers bring their review gates.** Whenever the effective engineer (or worker/integrator) model is
  Haiku, the pipeline's review stage (§3.6) is, by default, a Sonnet reviewer that does a line-by-line review of
  the whole diff, checks that every new test fails on the base commit, and runs a mutation gate (at least 5
  targeted mutants of the changed logic in a scratch copy under the seat's work root, never /tmp; every mutant
  must be killed), and reports it with `swarm review ... --report`. A surviving mutant, a test that passes on the
  base, or an unexplained behaviour change is `CHANGES`. The recipe key `review_gates = "haiku-strict"|"standard"`
  selects it (default: `haiku-strict` when the engineer model is Haiku).
- Every seat prompt carries: targeted tests only, through `/var/tmp/test-slots/test-slot -- <cmd>` when it
  exists; full suites only in CI; `swarm ci wait --repo R --sha S --timeout 540` in the foreground, never
  `gh run watch`; never put work trees or builds under /tmp; rebase-only landing; post DONE with evidence.

**[CX1]** Codex seats' ledger-changing control commands are receipted `CONTROL-REQUEST`s the driver applies (§4.0).

**[F18] Codex seats must reach the board directly (ruling).** Today the Codex sandbox has no network and only
`post`/`wait`/`resume`/`verdict` spool; `done`, `seat`, `review`, `ask`, `answer` and `land` fail, and spooled
records have no id to bind a receipt to. Ruling: drive mode supports a Codex seat only when it is
**drive-ready**: on Postgres, the `[codex] network_access = true` profile is installed (bootstrap writes it,
doctor checks it); on SQLite/file, the store directory is a Codex writable root (`codex_config.required`, doctor
checks it). A seat request for Codex on a host that is not drive-ready is `refused` with that reason (the lead
then requests Claude). In drive jobs the CLI never spools a control command (`done`, `seat`, `review`, `ask`,
`answer`, `land`, `verdict`): it fails loudly instead, so no unbound record exists. Codex hooks (trusted, outside
the sandbox) write the receipts exactly as Claude's do. The fake Codex harness models the sandbox (no direct
board access unless drive-ready) and the live acceptance run uses a real Codex engineer (§6.3).

**[RR7 → G1] Codex read-only roles run on Claude.** The `[codex] network_access` profile bootstrap writes is
`[sandbox_workspace_write] network_access = true`; it does not apply to `--sandbox read-only`, so a Codex judge
or reviewer has no board access on any backend, and spooling `verdict`/`review` is forbidden in drive jobs.
Ruling: in drive mode judges and reviewers always run on Claude, on every backend; a request for Codex is
overridden with the reason posted. (A future read-only network profile, if the installed Codex supports one, may
lift this; doctor would then have to verify it.) Doctor's per-role line reads
`codex drive-ready: writer yes|no (<why>); judge/reviewer: claude (codex read-only has no board access)`.
Codex writer seats (engineer, fix worker, integrator) still need network or a writable store ([F18]): the
integrator pushes the rebased branch and waits on CI itself ([G2]).

### 3.13 The coordinator becomes optional

- A coordinator session may activate a drive job and then do anything or nothing; its turns, blocking or absence
  change nothing. It may observe (`swarm watch`, `swarm snapshot --follow`), and as the requester session it may
  answer questions, halt, cancel and request seats through receipted commands (**[F17]**).
- `events_safety`: `waiter-stale` and `unacked` alerts are not raised for drive jobs (no one is expected to
  wait); `stalled` for drive jobs means the invariant check (§3.3) failed and is raised as `DRIVER-STALLED`.
- `orphans.run` skips drive jobs (there is no coordinator to replace; the lead is a seat).
- `skills/engineering-team/SKILL.md` and `references/hosts.md`, `pm-events.md`, `recovery.md` are rewritten:
  every goal job is driven (**[A]**); the invoking session's role is "requester/observer";
  the PM-per-event procedure applies only to non-drive jobs.

## 4. Safety

### 4.0 Threat model (ruling R19)

- **Other database writers** (other OS users or hosts sharing the board's role; a compromised client): fully
  untrusted. Nothing they write can launch a session, settle an artifact, or end a job. Control decisions use
  host-private records only (below).
- **Codex seats**: sandboxed (`workspace-write`, read-only for judges/reviewers); the sandbox is a boundary.
- **Claude seats** run unsandboxed Bash under `--permission-mode auto`: the auto-mode classifier is their
  boundary, as for every Claude session on this box today. Hooks add guardrails (role limits, refusing agent
  launches and target pushes), and the driver independently verifies everything that decides an outcome, but
  a same-user Claude agent that defeats the classifier can do what the user can (including impersonating a
  person locally). This design does not claim more; sandboxing Claude seats is recorded as future work (R19).

**[CX1] Codex control commands are requests (PM ruling, 6b follow-up).** A grants directory writable by the
Codex sandbox would let a seat forge its own grants, so it is not used. A Codex seat's ledger-changing control
commands (seat requests and releases, acks, withdrawals, halt/unhalt where allowed) are posted as receipted
`CONTROL-REQUEST` records; the driver is the only writer of the ledger and applies each request in the pass
(`apply_control_request`, Task 6b; called from the pass, Task 8c), with the same checks the CLI would make for a
Claude seat. This is the same pattern as `LAND-REQUEST` ([RR8]).

**Trust assumption (after the Task 7b review):** a database writer can alter the board state of a genuinely
launched seat (e.g. mark it idle or failed, or change its row): that is denial only, never settlement — it can make
the driver relaunch, hold or rule, but it can never make an artifact, a verdict or the job settle.

**Host-private driver ledger.** `~/.local/share/swarm/supervisor/drive/<sha256(board_key|job)>.json` (privfs:
0700 dir, atomic writes) is authoritative for the driver's own control state: generation, action ids and
attempts, owners, integration receipts, ruling-due time, error counts, wake watermarks. The board carries copies
for display; when they disagree, the ledger wins and the board copy is rewritten.

**Receipts for agent-written control records.** A hand-off (`swarm done`), review result (`swarm review`, a new
structured command replacing the `REVIEW ...` post), verdict, `GOAL-READY`, `INTEGRATED` and seat request
count for the driver only with a **receipt**: the unsandboxed PostToolUse hook, seeing the command succeed in a
session whose enrolment record maps to the seat attempt the driver launched for that action, writes a
host-private receipt (`receipt-<sha256>.json`: job, generation, action id, attempt, seat, record kind, record
id, sha256 of the exact submitted arguments). The same applies to every agent-originated record that can cause
a launch or a blocking transition: posts addressed to a seat or role, `swarm ask`, `swarm answer`, and blocker
resolutions. A person-path command on the owner host writes its own receipt. Records without a matching
receipt are displayed but ignored for decisions; question expiry is applied by the driver from the receipted
question's `until`, not from a board event.
Non-drive jobs keep today's behavior.

**[F24] Scope of the forgery claims.** Receipts, the ledger and `swarm land` are a boundary against other database
writers and against Codex seats (sandboxed). They are not a boundary against an unsandboxed Claude seat of the same
user, which could write receipts or the ledger itself; against that, the guardrails apply and the close-time target
audit (§3.8) catches unjudged commits on the target. §6.2 scenario 5 tests forgery by database writers and Codex
seats only. `swarm doctor` labels Claude role limits "guardrail only" unless the Bash sandbox is active.

**[G11] Accepted trust choice: the requester session holds person authority.** With [F17], the session that
activated the job — usually a Claude Code agent session acting for the person — can unhalt, re-activate,
deactivate (CANCELLED), answer, change budgets and request seats through its receipted commands. It is
deliberately trusted as the person's delegate; a compromised requester session can therefore cancel or redirect
its own job. Other sessions cannot (their deactivate is tamper).

### 4.1 Budgets (per job, `[drive]` defaults, overridable at activation within host maxima)

| Key | Default | On exhaustion |
|-----|---------|---------------|
| `max_sessions` | 60 launches (all seats, all kinds) | ruling |
| `max_seats` | 12 distinct seats | further requests `refused` (lead woken) |
| `max_rounds` | 8 judged rounds per artifact group | ruling |
| `no_progress_rounds` | 3 (the one progress counter, §3.3) | ruling |
| `max_attempts` | 3 per logical action | action fails; lead woken |
| `max_cost_usd` | configurable; shipped default 40 (Claude `total_cost_usd` + Codex tokens × `[drive] codex_usd_per_mtok`) | ruling |
| `max_session_minutes` | 60 per session (wall clock, runner) | session ends; seat idle |
| `max_wall_hours` | 24 from activation | ruling |
| `ruling_reserve_usd` | configurable; shipped default 5 USD kept back for rulings (the number of ruling launches and their time are bounded by `ruling_attempts` (2) and `ruling_deadline_minutes` (120), not by a separate reserve) | — |

- **[D1] The spending cap is configurable** (Francesco, 2026-10-10: "spending cap is configurable"). Every value in
  this table is a configurable default, not a fixed limit: set in `config.toml` under `[drive]` (e.g.
  `max_cost_usd`, `ruling_reserve_usd`), overridable per job at activation (`--max-cost-usd N`,
  `--ruling-reserve-usd N`, or `--budget KEY=VALUE` for any key). **[G7]** A running job's budgets are changed with
  `swarm drive budget --job J KEY=VALUE...` (requester session or person path; recorded in the ledger; no
  generation rollover, no session stopped); a re-activation also accepts them but is a rollover that stops every
  session. Validation: a reserve larger than the cap, or a cap below `ruling_reserve_usd + min_session_usd`, is
  refused. The shipped defaults are 40 USD and 5 USD. The effective per-job values live in the ledger
  (`led.budgets`) and are shown by `swarm status --job J` and by `swarm doctor` (one line per active drive job:
  `drive budget J: cost 12.40/40.00 USD (reserve 5.00), sessions 9/60, wall 3.1/24 h`). A reserve larger than the
  cap is refused at activation.
- Each budget check happens in `next_action` from the ledger; launches that would eat into `ruling_reserve_usd`
  are held, and a ruling becomes due. A ruling never waits on a spent budget: it draws on the reserve. If the
  reserve is gone too, mechanical UNREACHABLE.
- **Reserve before launch, reconcile after.** `claim_drive_launch` atomically reserves the session's ceiling
  (`reserved_usd` on the restart row): `min([drive] session_usd_cap (5), budget − charged − open reservations −
  ruling reserve)`; a launch with less than `min_session_usd` (0.5) available is held and a ruling becomes due.
  The ceiling is enforced per session: Claude `--max-budget-usd <reserved>`; Codex
  `features.rollout_budget.limit_tokens = floor(reserved_usd / codex_usd_per_mtok × 1,000,000)` (drive mode
  refuses Codex seats when the rollout budget feature is unavailable in the installed Codex; doctor checks it).
  **One launch contract**: fresh launches and resumes (Claude `--resume`, Codex `exec resume`) are both built by
  `launch.spec_for` with the same model, permission, sandbox, turn, wall-clock and budget arguments;
  `hosts/resume.py` returns only the resume handle and never builds an argv for drive seats. Concurrent sessions can
  therefore never exceed the job budget together.
- **Cost accounting.** The runner parses Claude's final JSON (`total_cost_usd`, `usage`) and Codex `--json`
  token-count events from the output it already captures, and records `cost_usd`, `tokens_in`, `tokens_out` on
  the restart row (schema 25). A session whose usage cannot be read (crash, SIGKILL, malformed output) is charged
  its full reserved ceiling: an upper bound, never an estimate.
- Host caps remain: `max_concurrent_sessions` (new, default 4, replacing `max_concurrent_replacements` for
  drive launches; counts every open restart row of the host), `daily_restart_minutes` (raised default 960 for
  drive). The crash-recovery caps of `[supervise]` keep governing non-drive jobs.

### 4.2 allowed_workdirs and work trees

`[supervise] allowed_workdirs` (default `~/src`; drive jobs on this box add `~/work`, a config change, not a code default) applies to every seat's workdir; the existing
`workdir_problem`/`_workdir_hold` (project configuration needs `swarm supervise approve`) apply unchanged; and
the driver refuses a workdir that is or lies under /tmp or any tmpfs mount (new check), posting the reason.

### 4.3 Kill switch: `swarm halt`

- `swarm halt [--job J] [--reason TEXT]` (person path, §3.4: enrolled agents are refused, so an agent cannot
  suspend its own or another job; an agent that needs to stop uses `swarm ask` with a default): job-level sets
  `drive.halted` and stops every live seat session of the job (`systemctl --user kill` of its scopes via the
  runner, which finishes rows `cancelled`); global writes the private `supervise.off` (existing kill switch,
  which a sandboxed agent cannot delete) and stops all runs. `swarm unhalt` reverses. Halting advances no counter; deadlines shift by the halted time (**[F5]** below).
  Every launch path rechecks
  the switches immediately before starting (existing `_switched_off_now`).
- Halt is not a terminal state (deviation from "only three states", by design: a person's emergency brake);
  it is shown as `halted by <person>` and ends with `unhalt` or `deactivate` (CANCELLED).

**[F5] Halt semantics (authoritative; replaces any earlier wording).**
- Halting stops live seat sessions; their seats go to `idle` with wake reason `halted` (never `cancelled`), so
  the lead and every seat are resumable after `unhalt`.
- Attempts concluded by a halt are `halted`: not counted against `max_attempts`, `ruling_attempts` or budgets
  other than the cost actually used.
- `enforce_deadlines` skips halted jobs, and every deadline (`max_wall_hours`, `ruling_due_at + deadline`, question
  `until`) is shifted by the halted duration on unhalt (`led.halted_total_seconds`). This replaces the earlier
  "ruling on unhalt" rule.
- Who: `swarm halt` is refused only for enrolled **seat** sessions (no tty needed: a person, a script, or the
  job's requester session may halt); `unhalt` and re-activation are person path (tty) or the requester
  session's receipted command (**[F17]**). This replaces R15's "stopping needs no proof" and round-2 #2's
  "person path only".
- A tamper-halt (§3.2 DRIVER-TAMPER) is a halt: the same deadline shift applies.

**[RR4] Halt and cost.** Halt waives attempt and session counts only. Cost is charged as everywhere else
(§4.1, R17): actual usage when the runner can read it, otherwise the reserved ceiling.

### 4.4 Permissions

Unchanged and reaffirmed: never a bypass flag (`check_safe`), Claude `--permission-mode auto`,
`--setting-sources user`, Codex `--sandbox workspace-write`, minimal environment allowlist. Seats are started
only by the supervisor's systemd unit; agents never start sessions (the classifier's "Create Unsafe Agents"
denial stays correct, and the hook refuses agent-launched `claude -p`/`codex exec` loops with a pointer to
`swarm seat request`).

**Role limits: boundaries where the harness has them, guardrails elsewhere.** Prompts and fencing are not
permission boundaries, and a brief is agent-written. Judges and reviewers run read-only: Codex with
`--sandbox read-only` (a boundary); Claude with `--disallowedTools Edit,Write,NotebookEdit` plus, where the
installed Claude Code supports Bash sandboxing through `--settings`, a sandbox with write access denied (doctor
reports which; without it the hook guardrail applies, §4.0). In drive jobs the PreToolUse hook also refuses:
for judges and reviewers, commits and pushes; recognisable pushes to a target branch outside `swarm land`, and force pushes, for everyone (a guardrail: the
outcome-deciding check is `swarm land` plus the driver's mirror verification); no seat may run `swarm verdict` except
the judge seat; no seat may launch `claude`/`codex` sessions. The driver independently verifies landing (the
integration receipt, §3.8) instead of trusting the integrator's post.

### 4.5 Idempotence and crash recovery (the one authoritative protocol)

1. **Decide** from the snapshot: an action id (generation-bound, §3.2) and the next attempt number.
1a. **Write-ahead intent**: append `{launch_id, action_id, attempt, seat, reserved_usd, reserved_minutes,
   locks, state: "claiming"}` to the private ledger and fsync it. Budgets and attempt history are computed from
   the ledger's intents (authoritative); the board's reservations exist for cross-host caps and display.
2. **Claim**: `claim_drive_launch` in one transaction (seat revision CAS, generation check, budget
   reservation, host caps, `drive_locks` for workdir/land destinations, insert of the restart row with
   `UNIQUE(job, action_id, attempt)`, `launch_id` = random UUID, `launch_host`, `launch_user`). Lost race →
   nothing happens; re-read next pass.
3. **Persist locally** the run file (existing `write_run`, now carrying `launch_id`) and the resume marker.
4. **Recheck kill switches**, then `runner.start`.
5. **Conclude**: the runner finishes the row (`completed|failed|timeout|cancelled|refused`) and the driver judges
   the attempt against the action's success criteria; unsuccessful → attempt n+1 after backoff, up to
   `max_attempts`.

**Reconciliation** at the start of every pass, before any new decision, bounded to 20 s and rotated across
jobs. Ledger ↔ board: an intent `claiming` with a board row carrying its `launch_id` becomes `claimed`; an
intent without a board row after `LAUNCH_GRACE` is concluded `failed` (nothing was launched) and its
reservation released; a board row whose `launch_id` matches no intent is foreign or forged and is ignored for
this job's decisions (and reported). Then every open restart row of this host+user with an intent is matched
to a run file and lock. A row with a held lock is
live. A row whose run file exists but whose lock is free is reaped by the existing `runner.reap`. **A row with
no run file** (crash between steps 2 and 3/4) older than `LAUNCH_GRACE` (90 s) is finished `failed`
(`outcome_detail = "claimed, never started"`), its reservation and locks released, its marker removed if
present; that is a concluded unsuccessful attempt. Seats whose row ended are moved to `idle`/`failed`. A
completed attempt never blocks a later attempt of the same action; only an open one does.

**[F7] Reconcile corrections (authoritative).**
- A claim that lost the race (`claim_drive_launch` returned None) concludes its intent `lost` at once: not an
  attempt, nothing counted.
- Before concluding "claimed, never started", reconcile checks the **local** evidence whatever the board says:
  the run file, its flock, and `unit_presence` of the intent's unit. A held lock or a present unit means live; a
  board row deleted by a database writer never causes a second writer to start.
- The driver reaps drive runs itself (it calls the runner's reap for its own intents); §3.2's "`runner.reap` of
  non-drive runs" is corrected accordingly.
- Every ledger and receipt write is `write → fsync(file) → rename → fsync(directory)`.

### 4.6 Untrusted board input

Seat requests, briefs and posts are written by agents. Hence: requester must be the bound lead seat or a person;
role, model and harness from allowlists; workdir from §4.2 checks (resolved by descriptor, as the runner does);
the brief path must be a regular, non-symlink file inside the seat's workdir and is **not read by the
supervisor** (the session reads it, under its own permissions); board text in prompts is fenced as untrusted
data (existing brief convention). Launch authority stays host-private enrolment: a board row never decides a
launch on a host whose user did not activate (own) the job.

## 5. Doctor: "FAIL supervise timer swarm-supervise.timer: ?/?"

Root cause (verified on this box): `systemd.timer_state` runs `systemctl --user show` with the caller's
environment. Agent shells have neither `XDG_RUNTIME_DIR` nor `DBUS_SESSION_BUS_ADDRESS`, so systemctl fails
("Failed to connect to user scope bus"), `timer_state` returns `{}`, and doctor prints `?/?` although the timer
is `active/enabled` (`XDG_RUNTIME_DIR=/run/user/1000 systemctl --user show ...` confirms) and the pass ran
minutes ago. Fix: `timer_state`, `linger` and `install` call systemctl with `runner._manager_env()` (moved to
`systemd.py` and imported by the runner); doctor prints `unknown (no user bus from this shell)` rather than FAIL
when the bus is unreachable but `supervise last run` is recent. Test: a fake `run` asserting the env carries
`XDG_RUNTIME_DIR`, and the doctor line for an empty result with a fresh last run.

## 6. Proof by test

### 6.1 Unit (targeted, local via test-slot; full in Actions)

`next_action` table tests over snapshots (every priority branch, every budget, ruling, invariant fallback);
seat CLI validation and idempotence; verdict `unreachable` across backends and the migration; per-seat question
gating; progress measure; cost parsing; halt; doctor fix; hook kick debounce.

### 6.2 E2E in CI with scripted harnesses (no coordinator)

**Determinism.** One injectable clock object (`swarm.clock`) is used by the driver, runner, reaper, marker grace,
lease expiry, blocker expiry and budgets; the E2E harness advances it explicitly. A pass limit fails a scenario
that is not terminal after `max_passes`, printing the snapshot. Named **crash barriers** (`before_claim`,
`after_claim`, `after_run_file`, `after_marker`, `after_runner_start`, `runner_mid_session`, `before_finish`,
`mid_terminalization`, `mid_rollover`, and **[F21]** `hook_after_cli_before_receipt`,
`land_after_first_destination`, `after_intent_before_claim`, `mid_rollover_after_ledger`) are reached by running each pass in a child process
(`python -m swarm supervise` with `SWARM_CRASH_AT=<barrier>`), which the barrier SIGKILLs: no Python cleanup
runs, as in a real crash. Explicit cases: a claim with no run file, and systemctl returning an error (unknown
unit state). "Every snapshot" means: the invariant is asserted after every pass of every scenario, and the chaos
suite enumerates every barrier × each of the scenario's actions (bounded by the scenario size).

`tests/test_drive_e2e.py` runs real `run_pass` loops on SQLite, Postgres and the file backend (Actions matrix,
the file backend including close-and-reopen of the store between passes) with fake
`claude`/`codex` executables on PATH (`tests/fixtures/drive/fake_harness.py`), started by the real runner
through an injectable `start_runner` that executes them as subprocesses and drives the real hooks (as
`test_pause_e2e` does). Each fake reads its prompt, identifies its role, and follows a script file. No
coordinator session exists in any of these tests; `activate --goal` (drive implied) is called once from the test's shell.

1. **MET**: lead requests two engineer seats (one `simple` → asserts Haiku, one Sonnet) and QA; engineers
   hand off; reviewer CHANGES on one → author resumed → VERIFIED; judge not_met once (open 2→0) → fix → met;
   integrator lands in a local bare repo (rebase-ff, fake CI green on the exact SHA); lead GOAL-READY; goal judge
   met → job completed. Asserts: zero coordinator turns, every agent joined, models per role, invariant
   after every pass.
2. **UNREACHABLE**: the goal requires an impossible check; judge not_met without progress twice → ruling judge
   → unreachable with details → job failed/unreachable.
3. **Chaos** (each must end MET or UNREACHABLE):
   - kill a running engineer mid-task (SIGKILL the session) → stuck sweep → seat relaunched with brief;
   - kill the lead mid-turn → lead relaunched;
   - kill the supervisor pass mid-launch (between `record_restart` and `runner.start`) and kill a runner →
     reap + reconcile, no duplicate launch;
   - unanswered human question with default → other seats progress meanwhile; expiry applies the default;
     the asking seat is woken;
   - CI red on the rebased SHA twice → FINALIZE_BLOCKED → fix rounds → ruling;
   - budget exhaustion (max_sessions=8) → ruling from reserve;
   - ruling judge fails twice → mechanical unreachable;
   - `swarm halt` stops everything; `unhalt` resumes to MET.
4. **Codex parity**: scenario 1 with the lead and one engineer on the fake Codex harness.
5. **Forgery**: a seat row inserted directly into the database (no authorization record), a row modified
   between command and hook, a `swarm seat request` from a non-lead seat, a verdict/`swarm done`/`GOAL-READY`
   row without a receipt, an `INTEGRATED` post not written by `swarm land`, and an enrolled agent's unhalt are
   all ignored or refused; a seat workdir outside the job roots, under /tmp, or already claimed by a live writer
   is refused.
6. **Generation rollover**: re-activation with a changed goal mid-run stops generation-1 sessions before any
   generation-2 launch; generation-1 verdicts cannot close the job.
7. **Stale MET**: the target moves after a goal `met` but before close → the `met` is rejected, the goal judge
   reruns.

### 6.2a Real systemd (box-only, targeted)

`tests/test_drive_systemd.py` (skipped unless `SWARM_TEST_SYSTEMD=1` and a user manager answers), run on the
coder box through test-slot: real `systemd-run --scope` units with a fake harness that forks a grandchild;
asserts SIGKILL of the runner → reap stops the scope and its descendants; `swarm halt` racing a launch leaves
no live scope and no open row; writer fencing waits for the scope to disappear before a relaunch.

**[F21]** `swarm doctor` FAILs when a receipt write failed in the last 24 h (the hook logs the failure in the
private dir).

**[F22] Further required tests** (each owned by a plan task): per-seat bounded agent `swarm wait`; the `driving`
status line; refusal of Agent-tool spawns by the lead and by the requester session; tmpfs workdir refusal; the
Claude Bash-sandbox doctor line ("boundary" vs "guardrail only"); the Codex rollout-budget and drive-ready
checks; `driver_error_passes` → ruling; the `stall_minutes` lead wake; the `max_seats` refusal; a foreign land
lease; slow git and systemctl stubs across several jobs. Chaos and E2E run on all four backends, and the drive
tests are in the Postgres CI job (`run_postgres_suite.py`). "No coordinator" is asserted as: no orphan-coordinator
launch, no restart row with an orphan reason, and no agent row with an orchestrator role in the job.

**[A] Default-mode tests:** a goal job activated without `--drive` is driven; `--no-drive` without `--reason` is
refused; with a reason it is recorded and shown as "not autonomous"; a goal job's `swarm ask` without `--default`
is refused.

### 6.3 Live acceptance on the coder box (manual gate before landing)

`e2e/drive_live.sh`: a toy repository under `~/work/drive-accept/` with a small, real goal (e.g. "add a
`--version` flag with tests"), `activate --goal` (drive implied) from a plain shell, real Claude (Opus lead/judge, Sonnet
engineers, Haiku for a `simple` seat) and one Codex engineer, the real systemd timer, **no coordinator
session**, budget `max_cost_usd=10`. Pass: MET with an integrated rebase-only commit on the toy repo's main and a
green (local fake or GitHub) CI on the exact SHA; a second run with an impossible goal reaches UNREACHABLE.
Transcripts and the board export are attached to the judge's evidence.

## 7. Rulings (made by EL, recorded)

- R1 **[A]** (PM ruling, option A) Every goal job is a drive job: `--goal` implies `--drive`; `--no-drive
  --reason` is the recorded opt-out. Goal-less jobs and existing jobs keep today's behavior.
- R2 The driver lives in the supervisor pass; no daemon. Latency is bounded by the timer (≤5 min) and usually
  seconds through hook kicks / NOTIFY.
- R3 The lead is woken per event batch (bounded sessions resumed by session id), not kept alive: cheaper, crash
  safe, and the pass stays the single scheduler.
- R4 Seats are a board table (schema 25), not job data, to get CAS state transitions and uniqueness on all
  backends.
- R5 A review stage precedes the judge in the coding recipe (Francesco's DONE → review → fix → judge); recipes
  without a reviewer go straight to the judge.
- R6 Progress = new artifact AND fewer open criteria (judge-counted), or the judge's explicit `--progress yes`.
- R7 UNREACHABLE is a judge verdict; the only non-judge UNREACHABLE is the labelled mechanical one after
  `ruling_attempts` failed ruling launches, so a terminal state is guaranteed.
- R8 Artifact `met` no longer closes a drive job; a goal judge on the integrated target does.
- R9 Questions in drive mode must carry a default; expiry defaults to 4 h; only the blocked seat waits.
- R10 Halt is a person's brake (person path only), not a terminal state.
- R11 No automatic cross-host failover; `swarm job move` remains the person's tool.
- R12 Codex writer seats must be drive-ready ([F18]); judges and reviewers run on Claude on every backend ([G1]);
  the target fast-forward is executed by the driver for both harnesses, while the integrator still pushes the
  rebased branch and waits on CI ([RR8], [G2]).
- R13 waiter-stale/unacked alerts are suppressed for drive jobs; a failed invariant raises DRIVER-STALLED.
- R14 **[P1]** Models are a per-role config table `[drive.models]`, overridable per job and per seat request;
  shipped defaults follow Francesco's policy: engineers (and fix workers, integrators) Haiku, reviewers and QA
  Sonnet, judge, lead and security reviewer Opus. Haiku engineers get the strict review gates (line-by-line review,
  fails-on-base tests, mutation gate) by default.
- R15 Seat requests are authenticated by host-private authorization records written by hooks, never by board
  fields; person authority is the tty path or the receipted requester session (**[F17]**); halt is refused
  only for seat sessions (**[F5]**).
- R16 One progress counter plus absolute caps; no idle-round, lead-turn or adaptive-timer counters (cut).
- R17 Budgets are reserved before launch and charged at the reserved ceiling when usage is unknown.
- R18 Verdicts are typed (artifact/goal/ruling) and bound to an activation generation.
- R19 Threat model (§4.0): other database writers fully untrusted; Codex sandbox a boundary; Claude seats rely on
  the auto-mode classifier as today, with hook guardrails and independent driver verification; sandboxing
  Claude seats is future work.
- R20 Driver control state lives in a host-private ledger; agent-written control records need hook receipts.
- R21 Landing only through `swarm land` (trusted checks, fast-forward only), verified in private mirrors with
  pinned remotes; leases cover every destination and never transfer on expiry alone.
- R22 A ruling quiesces live work first, and a ruling MET passes the same closing gate as a goal MET.
- R23 Judges and reviewers run read-only (Codex read-only sandbox; Claude edit tools disallowed, Bash sandbox
  where available).
- R24 The driver iterates its private ledgers, repairs or halts on unauthorized board changes, and never
  depends on the board's job status to keep driving.
- R25 Only receipted or driver-derived events wake seats; foreign-host leases are never reclaimed.

## 8. Decisions only Francesco can make

- D1 **Answered** (Francesco, 2026-10-10, verbatim): "spending cap is configurable". The per-job cap and the ruling
  reserve are configuration: `[drive] max_cost_usd` / `ruling_reserve_usd` in `config.toml`, overridable per job
  with `swarm activate --max-cost-usd N --ruling-reserve-usd N`; 40 USD and 5 USD are only the shipped,
  configurable defaults (§4.1 [D1]). Doctor and status show the effective value per job.
- D2 (confirmation only) The PM ruled option A (drive mode is the default for goal jobs, **[A]**) to meet "NEVER"
  literally; Francesco may override it to opt-in plus an auto-conversion net (judge's option B).

## 9. Compatibility and rollout

- Schema 25: `seats` and `drive_locks` tables; `verdict` CHECK adds `unreachable`; `verdict_scope` column;
  blockers gain `seat`, `generation`; restart rows gain `action_id`, `cost_usd`, `reserved_usd`, `tokens_in`,
  `tokens_out` with `UNIQUE(job, action_id, attempt)`.
- **Older clients cannot change a drive job's outcome.** Decisions use the host-private ledger and receipts
  (§4.0), so an older client's writes (it has no receipts) are at most noise. In addition, on Postgres,
  schema-25 clients set `swarm.client_schema = 25` on every connection and triggers on **every** table a drive
  job uses (`jobs`, `agents`, `messages`, `restarts`, `blockers`, `seats`, `drive_locks`, `events`) refuse
  writes to a drive job's rows from a connection without it. For SQLite and file stores, implementation task
  must establish (by test against the 0.2.3 release binary) whether older clients refuse a newer schema; if
  not, the migration adds a guard they do honour (e.g. a renamed required table) — the test is the acceptance
  criterion, not this sentence.
- Order: doctor fix (independent) → schema + verdict → seats + driver core → pipeline extensions → questions →
  kicks/NOTIFY → halt/budgets/cost → skill rewrite → E2E/chaos → live acceptance → release notes.

- **[A]** Existing active goal jobs are not converted: they keep running as non-drive jobs; `swarm doctor`
  lists them ("goal job not autonomous: activated before drive mode") so a person can re-activate them. The
  release notes call out the four breaking changes for goal jobs: `--goal` implies drive mode; questions need
  `--default`; and (**[G5]**) the activating (requester) session's Agent-tool spawns are refused in drive jobs, so
  the engineering-team and swarm skills staff through `swarm seat request` instead (rewritten in plan Task 14); and (**[M1].4**) with the supervisor off by default, `activate --goal` fails until `swarm
  supervise enable` has been run.

## 10. Review log

### Round 1 (Codex, `codex exec`, 2026-10-10): CHANGES, 23 findings, all accepted

| # | Finding (short) | Resolution |
|---|-----------------|------------|
| 1 | Artifact verdicts write the job's latest-verdict columns, could close the job | Typed verdicts + generation (§3.1, R18) |
| 2 | Ruling launches can wait forever (pre-launch holds) | Absolute ruling deadline; pre-launch refusals count (§2.2, §3.3) |
| 3 | Ruling session exiting without a verdict undefined | Counts as a concluded failed attempt (§2.2, §3.6) |
| 4 | Board rows forgeable; seat requests unauthenticated | Host-private seat authorization records (§3.4, R15) |
| 5 | "human" identity spoofable | `/dev/tty` person proof; halt needs none (§3.4, §4.3) |
| 6 | Lead could widen write scope across repos | Job workdir roots at activation (§3.4) |
| 7 | Prompt fencing is not enforcement | Hook-enforced role limits; driver verifies landing (§4.4) |
| 8 | Concurrent sessions overspend | Atomic reservation before launch (§4.1, R17) |
| 9 | Unknown cost undercharged | Charge reserved ceiling; Codex rollout budget required (§4.1) |
| 10 | Tokens conflate actions and attempts | Action id + attempt, success criteria, max_attempts (§3.2) |
| 11 | Launch claim not atomic | `claim_drive_launch` single transaction, unique (job, action, attempt) (§3.2) |
| 12 | Wake events lost while lead runs | Id watermarks acknowledged per turn (§3.5) |
| 13 | Replacement writers overlap | Writer fencing on scope gone (§3.9) |
| 14 | Prose `DONE:` strands a seat | Only structured hand-offs count (§3.4) |
| 15 | Rebased hand-off loses author | Artifact-group owner seat (§3.6) |
| 16 | `coding_integrated` too weak | Integration receipt verified on every remote (§3.8) |
| 17 | Target serialization only a check | `drive_locks` land lease (§3.8) |
| 18 | Revisions/reactivation not isolated | Activation generation on everything (§3.1) |
| 19 | Per-job errors can starve the driver | Per-job isolation, deadline, rotation, DRIVER-ERROR → ruling (§3.2) |
| 20 | Blocker seat binding lives in plugin payload | Core `seat`/`generation` columns (§3.10) |
| 21 | Tests skip systemd, lack determinism | Clock, pass limit, crash barriers; box-only systemd test (§6) |
| 22 | File backend/old clients | File backend in E2E; Postgres refuses old clients on drive jobs (§6, §9) |
| 23 | Too many progress mechanisms | One counter + absolute caps (§3.3, R16) |

### Round 2 (Codex, 2026-10-10): CHANGES, 23 findings, all accepted (two as guardrail-only, stated in §4.0)

| # | Finding (short) | Resolution |
|---|-----------------|------------|
| 1 | `/dev/tty` is not person proof (agent can allocate a PTY) | Enrolled callers refused; tty is a guardrail; threat model states the limit (§3.4, §4.0) |
| 2 | Agents could halt indefinitely | Halt is person-path only; enrolled agents refused (§3.4, §4.3) |
| 3 | Hook re-read row can be swapped | Hash of exact tool-input arguments bound to row id + revision (§3.4) |
| 4 | Verdicts/ownership/posts forgeable | Host-private ledger + hook receipts for every control record (§4.0, R20) |
| 5 | Claim without run file never recovered | Reconciliation of every open ledger row incl. file-less claims (§4.5) |
| 6 | §4.5 contradicted §3.2 | §4.5 rewritten as the single protocol |
| 7 | `DONE:` still in recovery | Removed (§3.9) |
| 8 | `_unit_exists` false on systemctl error | Tri-state presence; relaunch only on confirmed absent (§3.9) |
| 9 | Generation does not stop old sessions; seat uniqueness | Driver-run rollover; seats keyed by generation (§3.1, §3.4) |
| 10 | Broken snapshot path blocks deadlines | `enforce_deadlines` from ledger + job row only (§3.2) |
| 11 | Unbounded reap before driver | Driver first; reconciliation and reap bounded and rotated (§3.2, §4.5) |
| 12 | Terminalization undefined with live seats/blockers | Six-step terminalization (§3.1) |
| 13 | Ruling races live work; ruling MET ungated | Quiesce + same closing gate (§3.6, R22) |
| 14 | Goal MET not bound to candidate | `--candidate`/`--req`, checked in the close transaction (§3.7) |
| 15 | Hook push matching is bypassable | `swarm land` trusted helper; read-only judges/reviewers (§3.8, §4.4, R21, R23) |
| 16 | `git fetch` in agent repo executes config | Private mirrors with pinned remotes and sanitized git (§3.8) |
| 17 | One-URL lease; expiry transfers ownership | Per-destination all-or-nothing leases; fenced reclaim (§3.8) |
| 18 | Seats can share a checkout | Exclusive writer worktree locks (§3.4) |
| 19 | No ownership transfer | Successor seat inherits ownership (§3.4) |
| 20 | Codex token formula; resume contract | Formula fixed; one launch contract for fresh and resume (§4.1) |
| 21 | Clocks and crash simulation | One injectable clock; SIGKILL barriers in child passes (§6.2) |
| 22 | Compat triggers incomplete; unverified SQLite/file claim | Ledger makes old writes noise; triggers on all tables; tested against 0.2.3 (§9) |
| 23 | `max_seat_launches` overlaps | Removed (§3.4) |

### Round 3 (Codex, 2026-10-10, final round): CHANGES, 9 findings, all accepted

| # | Finding (short) | Resolution |
|---|-----------------|------------|
| 1 | Unknown systemd state blocks terminalization forever | Bus-free cgroupfs check; user-manager-down stated as a limit (§2.2, §3.9) |
| 2 | Board closure by a DB writer drops the job | Driver iterates private ledgers; DRIVER-TAMPER repair/halt; availability limit stated (§2.2, §3.2, R24) |
| 3 | Unreceipted messages/questions/answers cause wakes | Receipts for every wake-causing record; driver-applied expiry (§3.5, §4.0, R25) |
| 4 | Ledger vs board claim durability | Write-ahead intent keyed by launch_id; two-sided reconciliation (§4.5) |
| 5 | Foreign lease reclaimed by timeout | Never reclaimed; hold → ruling deadline (§3.8) |
| 6 | Nested workdirs share a checkout | Worktree-root locks, overlap refused (§3.4) |
| 7 | Stale MET after target advance | Fresh head equality via mirrors before close; window stated (§3.7, §2.2) |
| 8 | events triage launches `claude -p` outside the ledger | Disabled for drive jobs (§3.2) |
| 9 | Operation timeouts exceed per-job deadline | Timeouts capped by remaining time; persisted continuations (§3.2, §3.8) |

The review cap (3 rounds) is reached. All findings of all rounds are resolved in this revision; the remaining
risk is carried by the implementation reviews and the tests of §6, which include every case these rounds raised.

### Design judge (Dr. Hibbert, 2026-10-10): APPROVE-WITH-FIXES; F1–F24 applied in revision 5

| Fix | Where (marker) |
|-----|----------------|
| F1 question-gated seats responsible; gated lead holds | §3.3 [F1] |
| F2 ledger first, enumerated by file, DRIVER-LEDGER-BAD/ORPHAN, job move | §3.1 [F2] |
| F3 one successor lead, then ruling | §3.4 [F3] |
| F4 lead acknowledges on any outcome with a turn | §3.5 [F4] |
| F5 halt: seats idle, attempts uncounted, deadlines shift, who may halt | §4.3 [F5], R15 |
| F6 ledger-first terminal intent; driver records via ledger | §3.1 [F6] |
| F7 lost races, local evidence before "never started", driver reaps, dir fsync | §4.5 [F7], §3.2 |
| F8 land journal as host-private receipt; driver completes; quiesce exemption | §3.8 [F8] |
| F9 withdraw a group | §3.7 [F9] |
| F10 pinned requirements | §3.1 [F10] |
| F11 "nothing else to do"; finalize cycles and max_seats wakes are no-progress rounds | §3.3 [F11] |
| F12 provider outages do not consume attempts | §3.3 [F12] |
| F13 clean rebase reuses review via patch-id | §3.8 [F13] |
| F14 report artifacts for non-writer seats | §3.4 [F14] |
| F15 questions/posts to a seat or role are wakes | §3.5 [F15] |
| F16 positive cgroup v2 containment check | §3.9 [F16] |
| F17 requester session receipts; tamper rule | §3.4 [F17], §3.13, §4.3 |
| F18 Codex seats must be drive-ready; no spooling of control commands | §3.12 [F18] |
| F19 land-lease hold → ruling; orphan lock report | §3.8 [F19] |
| F20 worktree-root overlap check in the claim | §3.4 [F20] |
| F21 extra barriers; doctor on receipt-write failure | §6.2 [F21] |
| F22 further tests; all backends; Postgres CI; real no-coordinator assertion | §6.2a [F22] |
| F23 staffing changes | plan |
| F24 close-time target audit; forgery-claim scope | §3.8 [F24], §4.0 [F24] |
| A drive mode default for goal jobs (PM ruling) | §2.1, §2.3, §3.1, §3.4, §3.10, §3.13, §6, §9, R1, D2 |

### Delta re-review (Codex, 2026-10-10, one focused round on rev 3→5): CHANGES, 8 findings, all accepted

| # | Finding | Resolution |
|---|---------|------------|
| RR1 | Owner record ordering; incomplete activation never driven | Five-step durable activation, `activating` ledger completed by the pass (§3.1) |
| RR2 | Rejected MET leaves a permanent terminal intent | Pending vs committed; rejection clears intent and closing (§3.1) |
| RR3 | Generic expiry applies defaults during halt | Drive questions expire only through the driver, never while halted (§3.10) |
| RR4 | Halt vs unknown-usage charging | Halt waives attempts only; cost rule unchanged (§4.3) |
| RR5 | Deadline terminalization interrupts landings | Terminalization step 0 completes land journals; partial lands recorded (§3.8) |
| RR6 | Nested-root claims race across supervisors | Host-wide lock around the overlap check (§3.4) |
| RR7 | Read-only Codex roles cannot write SQLite/file | Codex judges/reviewers only on Postgres; doctor per role (§3.12, R12) |
| RR8 | Codex cannot run `swarm land` privately | Land requests receipted; the driver executes landings (§3.8, R12) |

### Revision 5.2 (after Francesco's D1 answer)

D1 answered: the spending cap and the ruling reserve are configurable (config defaults, per-job activation
flags), 40/5 USD only shipped defaults; effective values shown per job by status and doctor (§4.1 [D1], §8).

### Re-judge (Dr. Hibbert, 3e1d54e): APPROVE-WITH-FIXES; G1–G12 applied in revision 6

| Fix | Where |
|-----|-------|
| G1 Codex judges/reviewers run on Claude in drive mode, every backend; doctor line | §3.12 [RR7 → G1], R12 |
| G2 pinned driver credentials; doctor ls-remote; land request is the hand-off; R12 corrected | §3.8 [G2], R12 |
| G3 per-recipe closing gate (PM ruling): artifact recipe reaches MET without target/CI/audit | §3.7 [G3] |
| G4 pending terminal intent bounded by `close_deadline_minutes` | §3.1 [RR2]/[G4] |
| G5 requester-spawn refusal is the third breaking change; skill rewrite | §9 [G5] |
| G6 option A text cleanups; `--drive` accepted no-op; person authority reconciled | §3.1, §3.4, §6.2, §6.3 |
| G7 cap ≥ reserve + min_session_usd; `swarm drive budget` without rollover | §4.1 [G7] |
| G8 stale landing text removed; "trusted" reworded | §3.8 |
| G9 missing harness binary is an error, not an outage | §3.3 [F12]/[G9] |
| G10 generated brief excluded from git | §3.1 [G10] |
| G11 requester session's person authority stated as an accepted trust choice | §4.0 [G11] |
| G12 no further review round; the judge re-checks G1–G4 | — |
