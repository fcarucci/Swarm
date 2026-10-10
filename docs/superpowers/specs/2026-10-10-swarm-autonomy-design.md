# Swarm autonomy: a goal job runs to MET or UNREACHABLE with zero coordinator turns

Status: design, revision 2 (EL, job swarm-autonomy, 2026-10-10). Base: github/main 2f194bb.
Revision 2 resolves Codex review round 1 (23 findings; see §10).

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
  with zero coordinator turns.
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
- *Budgets.* "Guaranteed to complete" means guaranteed to reach a *ruling*: when budgets run out the outcome
  may be UNREACHABLE ("budget exhausted before the goal was met", with the judge's assessment), not MET.
- *Judgment quality.* MET and UNREACHABLE are a model judge's rulings; the design guarantees one is recorded with
  reasons and evidence, not that it is right. A person may reopen with a new goal revision.
- *Harness/provider outages* (API down, quota exhausted) burn launch attempts and backoff; sustained failure
  ends in the mechanical UNREACHABLE with the cause named, rather than an endless loop.
- *halt* (§4.3) is a person stopping the job on purpose; the job is then neither progressing nor terminal until
  the person unhalts or cancels. That is deliberate.

### 2.3 Non-goals

- Replacing Claude Code's in-session subagents for small jobs; drive mode is opt-in per job (default for the
  engineering-team skill's moderate/complex jobs).
- Cross-host failover of the driver; multi-owner jobs.
- Changing judge semantics beyond adding UNREACHABLE and the ruling constraint.

## 3. Design

### 3.1 Drive mode and terminal states

- `swarm activate --job J --goal G --drive --lead-brief PATH --workdir DIR [--lead-harness claude|codex]
  [--budget KEY=VALUE ...]` from any shell (a person's terminal, a coordinator, a script). It writes the job,
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
- **Terminal transitions** (driver only, under the existing close guard):
  - current-generation `goal` or `ruling` verdict `met` (§3.7) → job `completed`, outcome `met: <reason>`;
  - current-generation `unreachable` → job `failed`, outcome `unreachable: <reason>`, `closed_by='driver'`;
  - a person's `swarm deactivate` → `cancelled` (existing).
  `goal_is_unmet` treats `unreachable` as settled for closing; the `jobs_keep_goal_jobs` trigger is unchanged
  (it only refuses the old auto-close outcome).
- A drive-mode job never shows `idle`, `stalled` or `waiting for the coordinator`: `swarm status` shows
  `driving: <next action or the running seats>` or `ruling`, or `halted by <person>`.

### 3.2 The job driver

New module `lib/swarm/supervisor/driver.py`, called from `_pass` right after `runner.reap` and before
`pipeline.run`; it **owns** drive-mode jobs (the pipeline/orphan/candidate passes skip them, the way
`pipeline.run` already returns `managed`). It reuses the pipeline's transition logic, the launch/runner/marker
machinery, `record_restart` as the atomic launch ledger, and the stuck sweep.

For each active job with `drive.enabled` whose owner record is on this host+user (`find_job_owner` or
`find_job`):

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

**Failure isolation.** Each job is driven inside its own try/except with a per-job deadline (15 s of the 95 s
pass budget); jobs are visited in least-recently-driven order (job data `drive.last_pass`), so one slow or
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
9. **Goal judgment** (§3.7): lead has posted `GOAL-READY` and every hand-off is integrated and nothing is live
   → launch the goal judge.
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

### 3.4 Seats

**Table `seats`** (schema 25; memory/file/SQLite/Postgres):
`id, job, seat (stable name, unique per job, e.g. "eng-auth"), role, title, harness, model, workdir,
brief_path, task_ref, complexity (simple|standard|complex), requested_by, requested_at, state, agent_key,
session_id, launches, last_outcome, wake_reason, updated_at`, `UNIQUE(job, seat)`.

States: `requested → running → idle ⇄ running … → released | failed | cancelled | refused`.
- `idle`: the session exited; the seat persists and can be resumed. Resume = same harness session when
  available (`claude -p --resume <session_id>`, `codex exec resume <thread>`, via `hosts/resume.py`), else a new
  session with the seat brief + the existing `supervisor/brief.py` brief (posts, transcript tail).
- `released`: the lead (or a person) ended it (`swarm seat release`); also on job terminal.
- `failed`: `max_seat_launches` (4) used without a hand-off or release; wakes the lead.
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
  → lead seat) ran a successful `swarm seat request` (the hook re-reads the row and hashes it); or by
  `swarm activate --drive` for the initial lead seat; or by a person through the person path below. A changed
  row no longer matches its hash and is not launched.
- **Person authority** (unhalt, a person's seat request, a person's job-level wait) is not inferred from missing
  agent environment variables (spoofable, as `ask-answer.identity` shows). It requires an interactive
  confirmation read from `/dev/tty` (the command prints the action and waits for the typed job name); a
  headless agent session has no controlling terminal. Stopping is always allowed: `swarm halt` needs no person
  proof (§4.3).
- **Job workdir scope.** `swarm activate --drive --workdir DIR [--allow-workdir DIR ...]` records the job's
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

**Hand-backs are board events.** A seat's work product is `swarm done --artifact REF` (pipeline), `DONE:` posts,
`swarm seat release` by the lead, questions, and verdicts. Nothing returns to a parent session.

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
  runs are never lost. A turn acknowledges its watermark by ending normally (runner outcome `completed`); a
  crashed turn does not, and its events stay pending. Wakes are coalesced: one resume carries all pending ids.
- A lost lead (stuck, crashed, exited without a turn summary) is relaunched by §3.9 with the lead brief + board
  history; `max_seat_launches` does not apply to the lead (its turns are bounded by the job budgets and the
  progress counter).
- The PM duties of `skills/engineering-team` (NEEDS-REVIEW dispatch, CI-FAILED triage, READY-TO-LAND landing,
  staffing waves) move: review dispatch, fix rounds and landing to the driver pipeline (§3.6, §3.8); CI-FAILED
  triage to the fix round's brief (the integrator's FINALIZE_BLOCKED text and the CI logs pointer); staffing to
  the lead.

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
- **Ruling judge.** Same judge seat, prompt says it must rule `met` or `unreachable`; the board sets job data
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
lead names, and the evidence (CI on that exact SHA, QA reports). Outcomes: `met` → completed;
`not_met --open N --next` → lead wake with the next steps (a **goal round**, counted like §3.6);
`unreachable` → failed. The lead may also post `UNREACHABLE-PROPOSED <reason file>` to request an early ruling
(the judge decides; the lead never records a verdict). In drive mode `complete_finalized` no longer closes the
job on artifact finalization alone; it waits for the goal verdict.

### 3.8 Integration and landing gate

Unchanged in substance from the coding recipe, now always run by the driver:
- `met` artifact → integrator seat (Sonnet; harness Claude unless the Codex network profile is configured,
  §3.12): fetch, rebase onto the latest target (rebase only; never merge commits, squash only if the project's
  `[land] strategy = squash-ff`), push the rebased branch, open/reuse the PR, `swarm ci wait --repo R --sha
  <exact rebased sha> --timeout 540` in the foreground (repeated while pending, within the seat's wall clock).
- Rebased SHA differs from the judged SHA → new hand-off; the old verdict and CI do not cover it (existing).
- Green on the exact SHA + `met` on that SHA → fast-forward the target on every remote, verify the landed tree,
  delete the branch, `swarm learn`, `INTEGRATED <artifact>`.
- Red CI or a non-trivial conflict → `FINALIZE_BLOCKED <artifact> <instructions>` → fix round (§3.6).
  CI red repeatedly is a no-progress round like any other and so ends in a ruling.
- **Integration receipt, not inference.** Today `coding_integrated` treats "source branch gone remotely or its
  SHA an ancestor of a local tracking ref" as integrated. In drive mode an artifact group is integrated only with
  a receipt: the integrator's `INTEGRATED <artifact>` from the integrator seat of the current generation, plus a
  driver check (`git fetch` + `merge-base --is-ancestor <rebased sha> <remote>/<target>`) on **every**
  configured remote, recorded as job data `drive.integrated.<gen>.<group>` with the landed SHA. A partial push
  (some remotes advanced) is journalled per remote by the integrator (`swarm seat note`), and the next
  integrator attempt reconciles the missing remotes first.
- **Landings are serialized by a lease.** New table `drive_locks(key PRIMARY KEY, holder, generation,
  expires_at)` (schema 25); the key is `land:<sha256 of the normalized remote URL>:<target>`. The integrator
  launch claims it inside `claim_drive_launch`; the runner renews it while the session lives; it expires
  (session wall clock + 10 min) if the runner dies. Two supervisors (two hosts or users) cannot land on the
  same target at once.

### 3.9 Stall recovery

- **Lost/silent seats**: the existing stuck sweep (`stuck_reason`: dead, tool, silent) closes the row; in drive
  mode the driver relaunches the *seat* (not a coordinator) with the seat brief + `brief.build_brief` context
  (posts, transcript tail). The question gate in `stuck_reason` becomes per seat (§3.10).
- **Writer fencing.** A seat is never relaunched while its previous attempt may still write: the relaunch
  requires the previous attempt's restart row to be ended **and** its scope unit to be gone
  (`runner._unit_exists` false). If the row is ended by the sweep but the runner has not yet stopped the scope,
  the driver stops it (`runner._stop_session`) and relaunches on a later pass.
- **Exited without a hand-off**: a seat whose session ended `completed` with no `swarm done`, `DONE:` post,
  release, or open question since its launch is an unsuccessful attempt of its action (§3.2): retried with "you
  ended without a hand-off: finish, or ask with a default"; after `max_attempts` → seat `failed` + lead wake.
- **Lead lost** → relaunched (§3.5). **No progress for T**: `drive.stall_minutes` (90) without a new hand-off,
  verdict, seat-state change or lead turn while seats are live → the lead is woken with "no progress for T";
  is a round without progress (§3.3).
- **Supervisor killed or restarted**: every launch is a `claim_drive_launch` row inserted atomically with its caps;
  the runner owns it by flock; `reap` finishes dead runners' runs; seats in `running` whose restart row ended are
  reconciled to `idle`/`failed` at the next pass; tokens make every action idempotent (§4.5).

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
- **Models** (`[models.claude]` defaults written by bootstrap; drive mode enforces lead and judge):
  `engineering_lead = judge = "opus"`; `engineer = qa = reviewer = integrator = worker = "sonnet"`; a seat with
  `--complexity simple` gets `"haiku"`. `[drive] allowed_models` limits what a request may name; leads and judges
  are clamped to Opus. `[models.codex]` likewise for Codex.
- Every seat prompt carries: targeted tests only, through `/var/tmp/test-slots/test-slot -- <cmd>` when it
  exists; full suites only in CI; `swarm ci wait --repo R --sha S --timeout 540` in the foreground, never
  `gh run watch`; never put work trees or builds under /tmp; rebase-only landing; post DONE with evidence.

### 3.13 The coordinator becomes optional

- A coordinator session may activate a drive job and then do anything or nothing; its turns, blocking or absence
  change nothing. It may observe (`swarm watch`, `swarm snapshot --follow`) and answer questions.
- `events_safety`: `waiter-stale` and `unacked` alerts are not raised for drive jobs (no one is expected to
  wait); `stalled` for drive jobs means the invariant check (§3.3) failed and is raised as `DRIVER-STALLED`.
- `orphans.run` skips drive jobs (there is no coordinator to replace; the lead is a seat).
- `skills/engineering-team/SKILL.md` and `references/hosts.md`, `pm-events.md`, `recovery.md` are rewritten:
  moderate/complex jobs are activated with `--drive`; the invoking session's role is "requester/observer";
  the PM-per-event procedure applies only to non-drive jobs.

## 4. Safety

### 4.1 Budgets (per job, `[drive]` defaults, overridable at activation within host maxima)

| Key | Default | On exhaustion |
|-----|---------|---------------|
| `max_sessions` | 60 launches (all seats, all kinds) | ruling |
| `max_seats` | 12 distinct seats | further requests `refused` (lead woken) |
| `max_rounds` | 8 judged rounds per artifact group | ruling |
| `no_progress_rounds` | 3 (the one progress counter, §3.3) | ruling |
| `max_attempts` | 3 per logical action | action fails; lead woken |
| `max_cost_usd` | 40 (Claude `total_cost_usd` + Codex tokens × `[drive] codex_usd_per_mtok`) | ruling |
| `max_session_minutes` | 60 per session (wall clock, runner) | session ends; seat idle |
| `max_wall_hours` | 24 from activation | ruling |
| `ruling_reserve` | 2 sessions, 60 min, 5 USD kept back for rulings | — |

- Each budget check happens in `next_action` from the ledger; launches that would eat into `ruling_reserve`
  are held, and a ruling becomes due. A ruling never waits on a spent budget: it draws on the reserve. If the
  reserve is gone too, mechanical UNREACHABLE.
- **Reserve before launch, reconcile after.** `claim_drive_launch` atomically reserves the session's ceiling
  (`reserved_usd` on the restart row): `min([drive] session_usd_cap (5), budget − charged − open reservations −
  ruling reserve)`; a launch with less than `min_session_usd` (0.5) available is held and a ruling becomes due.
  The ceiling is enforced per session: Claude `--max-budget-usd <reserved>`; Codex
  `features.rollout_budget.limit_tokens = reserved / codex_usd_per_mtok` (drive mode refuses Codex seats when the
  rollout budget feature is unavailable in the installed Codex, doctor checks it). Concurrent sessions can
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

- `swarm halt [--job J] [--reason TEXT]` (anyone may stop; recorded with the actor): job-level sets
  `drive.halted` and stops every live seat session of the job (`systemctl --user kill` of its scopes via the
  runner, which finishes rows `cancelled`); global writes the private `supervise.off` (existing kill switch,
  which a sandboxed agent cannot delete) and stops all runs. `swarm unhalt` reverses and needs person proof
  (§3.4, `/dev/tty` confirmation). Halting advances no counter and no budget clock except `max_wall_hours`
  (deliberately: a halted job past its wall time gets a ruling on unhalt). Every launch path rechecks
  the switches immediately before starting (existing `_switched_off_now`).
- Halt is not a terminal state (deviation from "only three states", by design: a person's emergency brake);
  it is shown as `halted by <person>` and ends with `unhalt` or `deactivate` (CANCELLED).

### 4.4 Permissions

Unchanged and reaffirmed: never a bypass flag (`check_safe`), Claude `--permission-mode auto`,
`--setting-sources user`, Codex `--sandbox workspace-write`, minimal environment allowlist. Seats are started
only by the supervisor's systemd unit; agents never start sessions (the classifier's "Create Unsafe Agents"
denial stays correct, and the hook refuses agent-launched `claude -p`/`codex exec` loops with a pointer to
`swarm seat request`).

**Role limits are enforced in hooks, not prompts.** Prompts and fencing are not permission boundaries, and a
brief is agent-written. In drive jobs the PreToolUse hook enforces: judges and reviewers may not edit files,
commit or push (existing judge rule, extended to reviewers); only the integrator seat of the current
generation holding the land lease may push to the target branch (`git push` to `<target>` or `HEAD:<target>`
is refused for every other seat; force pushes are refused for everyone); no seat may run `swarm verdict` except
the judge seat; no seat may launch `claude`/`codex` sessions. The driver independently verifies landing (the
integration receipt, §3.8) instead of trusting the integrator's post.

### 4.5 Idempotence and crash recovery

- Every driver action has a deterministic token (`job|kind|seat|artifact|event id|round`), stored as the
  `record_restart` reason; a token with an open or completed row is never launched twice. Seat state changes
  are compare-and-set on `(id, state, updated_at)`.
- Launch sequence (existing order kept): decide → recheck fresh → `record_restart` (atomic with caps) → marker →
  kill-switch recheck → `runner.start`; any failure finishes the row `failed`/`cancelled` and removes the marker.
- Pass killed mid-way: the next pass reaps (runner flock), reconciles seats with their restart rows, and
  continues. Supervisor binary upgraded mid-job: the driver reads only board state + private files.

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

**Determinism.** The E2E harness uses an injected clock (`now=`), a pass limit (a scenario that is not
terminal after `max_passes` fails the test, printing the snapshot), and named **crash barriers**: hooks in
`driver.execute` and `claim_drive_launch`/runner start (`before_claim`, `after_claim`, `after_marker`,
`after_runner_start`, `runner_mid_session`, `before_finish`) where a test raises `SystemExit` or SIGKILLs a
process. "Every snapshot" means: the invariant is asserted after every pass of every scenario, and the chaos
suite enumerates every barrier × each of the scenario's actions (bounded by the scenario size).

`tests/test_drive_e2e.py` runs real `run_pass` loops on SQLite, Postgres and the file backend (Actions matrix,
the file backend including close-and-reopen of the store between passes) with fake
`claude`/`codex` executables on PATH (`tests/fixtures/drive/fake_harness.py`), started by the real runner
through an injectable `start_runner` that executes them as subprocesses and drives the real hooks (as
`test_pause_e2e` does). Each fake reads its prompt, identifies its role, and follows a script file. No
coordinator session exists in any of these tests; `activate --drive` is called once from the test's shell.

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
5. **Forgery**: a seat row inserted directly into the database (no authorization record), a modified row after
   authorization, a `swarm seat request` from a non-lead seat, and an agent-environment-cleared "person" unhalt
   are all refused; a seat workdir outside the job roots and one under /tmp are refused.

### 6.2a Real systemd (box-only, targeted)

`tests/test_drive_systemd.py` (skipped unless `SWARM_TEST_SYSTEMD=1` and a user manager answers), run on the
coder box through test-slot: real `systemd-run --scope` units with a fake harness that forks a grandchild;
asserts SIGKILL of the runner → reap stops the scope and its descendants; `swarm halt` racing a launch leaves
no live scope and no open row; writer fencing waits for the scope to disappear before a relaunch.

### 6.3 Live acceptance on the coder box (manual gate before landing)

`e2e/drive_live.sh`: a toy repository under `~/work/drive-accept/` with a small, real goal (e.g. "add a
`--version` flag with tests"), `activate --drive` from a plain shell, real Claude (Opus lead/judge, Sonnet
engineers, Haiku for a `simple` seat) and one Codex engineer, the real systemd timer, **no coordinator
session**, budget `max_cost_usd=10`. Pass: MET with an integrated rebase-only commit on the toy repo's main and a
green (local fake or GitHub) CI on the exact SHA; a second run with an impossible goal reaches UNREACHABLE.
Transcripts and the board export are attached to the judge's evidence.

## 7. Rulings (made by EL, recorded)

- R1 Drive mode is opt-in per job (`--drive`); the engineering-team skill turns it on for moderate/complex jobs.
  Non-drive jobs keep today's behavior, so nothing existing regresses.
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
- R10 Halt is a person's brake, not a terminal state.
- R11 No automatic cross-host failover; `swarm job move` remains the person's tool.
- R12 Integrator/pushing seats run on Claude unless Codex network access is configured.
- R13 waiter-stale/unacked alerts are suppressed for drive jobs; a failed invariant raises DRIVER-STALLED.
- R14 Model policy as Francesco set it (Opus leads/judges, Sonnet coding, Haiku simple), enforced for lead and
  judge, configurable in `[models]`.
- R15 Seat requests are authenticated by host-private authorization records written by hooks, never by board
  fields; person authority needs a `/dev/tty` confirmation; stopping (halt) needs no proof.
- R16 One progress counter plus absolute caps; no idle-round, lead-turn or adaptive-timer counters (cut).
- R17 Budgets are reserved before launch and charged at the reserved ceiling when usage is unknown.
- R18 Verdicts are typed (artifact/goal/ruling) and bound to an activation generation.

## 8. Decisions only Francesco can make

- D1 Default per-job spend ceiling `max_cost_usd` (proposed 40 USD; ruling reserve 5 USD) and whether it is
  enforced on subscription accounts where cost is notional (proposed: yes, as a usage proxy).

## 9. Compatibility and rollout

- Schema 25: `seats` and `drive_locks` tables; `verdict` CHECK adds `unreachable`; `verdict_scope` column;
  blockers gain `seat`, `generation`; restart rows gain `action_id`, `cost_usd`, `reserved_usd`, `tokens_in`,
  `tokens_out` with `UNIQUE(job, action_id, attempt)`.
- **Older clients are refused, not warned.** On Postgres, schema-25 clients set `swarm.client_schema = 25` on
  every connection; triggers on `seats`, `drive_locks`, `blockers` and `jobs` refuse writes touching a drive job
  (job data `drive.enabled`) from a connection without it (an older client gets an error, never a silent
  half-write). SQLite and file stores are local to one installation, which upgrades as a whole; their
  migration bumps `user_version`/the state document version, which older clients already refuse to open.
- Order: doctor fix (independent) → schema + verdict → seats + driver core → pipeline extensions → questions →
  kicks/NOTIFY → halt/budgets/cost → skill rewrite → E2E/chaos → live acceptance → release notes.

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
