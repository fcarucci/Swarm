"""Driver execution (plan Task 8a): claims, reconciliation, the runner's usage recording, the ledger
half of it, and the executor-side progress state. Memory board, real ledger/receipts/markers/run files
under the suite's private HOME, a fake `start_runner`."""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
import time
import unittest
from types import SimpleNamespace as NS
from unittest import mock

from support import MemoryHarness, SqliteHarness
from test_hooks_cli import Env
from drive_fixtures import ho, snap, v

from swarm import cli as swarm
from swarm import enrolment, receipts
from swarm.board import JobPaused
from swarm.board.autoinit import store_key
from swarm.supervisor import driver_exec, driver_model as dm, ledger, markers, privfs, runner
from swarm.supervisor import settings as st


def driver_exec_pkg():
    import swarm.supervisor as pkg
    return pkg


def iso(t):
    return t.isoformat()


@unittest.skipUnless(os.path.isdir("/proc"), "drive mode runs on Linux (runner locks, scopes)")
class ExecEnv(Env):
    """A drive job J with a ledger, an owner record, a work dir that is a git checkout, a fake runner."""

    def setUp(self):
        super().setUp()
        self.config.write_text(self.config.read_text() + "\n[supervise]\nenabled = true\n")
        self.cfg = swarm.load_config(self.config)
        self.cfg["supervise"]["allowed_workdirs"] = [str(self.tmp)]
        self.bk = store_key(self.cfg)
        self.sup = st.settings(self.cfg)
        self.b = self.board()
        self.addCleanup(self.b.close)
        self.b.open_job("J", "d", "t", "s", "me", goal="G")
        self.work = self.tmp / "work" / "repo"
        (self.work / ".git").mkdir(parents=True)
        self.brief = self.tmp / "brief.md"
        self.brief.write_text("brief")
        self.led = ledger.create(self.bk, "J", budgets={}, activated_at=iso(self.b.now()))
        enrolment.write_job_owner(self.bk, job="J", harness="claude", session_id="s", cwd=str(self.work))
        self.launched = []

    def tamper_seat(self, name, **cols):
        """Write columns straight into a seat row, as a database writer would (any backend)."""
        if hasattr(self.b, "_s"):
            with self.b._s().lock:
                next(r for r in self.b._s().seats if r["seat"] == name).update(cols)
        elif hasattr(self.b, "_tx"):
            keys = list(cols)
            with self.b._tx() as c:
                c.execute("UPDATE seats SET " + ", ".join(f"{k} = ?" for k in keys) + " WHERE job = ? AND seat = ?",
                          [cols[k] for k in keys] + ["J", name])
        else:
            self.skipTest("no raw row access on this backend")

    def delete_restarts(self):
        if hasattr(self.b, "_s"):
            with self.b._s().lock:
                self.b._s().restarts.clear()
        elif hasattr(self.b, "_tx"):
            with self.b._tx() as c:
                c.execute("DELETE FROM restarts")
        else:
            self.skipTest("no raw row access on this backend")

    # -- helpers
    def now(self, seconds=0):
        return self.b.now() + dt.timedelta(seconds=seconds)

    def seat(self, name="eng-a", role="engineer", workdir=None, authorize=True, **kw):
        wd = workdir or str(self.work / name)
        os.makedirs(wd, exist_ok=True)
        row = self.b.request_seat("J", self.led.generation, name, role=role, harness=kw.pop("harness", "claude"),
                                  model=kw.pop("model", "haiku"), workdir=wd, brief_path=str(self.brief),
                                  requested_by="lead")
        if authorize:
            driver_exec.authorize_seat(self.led, row, self.now())
        return row

    def act(self, seat="eng-a", kind="seat", group=None, trigger="1", **kw):
        aid = f"gen{self.led.generation}|{kind}|{seat or '-'}|{group or '-'}|0|{trigger}"
        return dm.Action(kind="launch", action_id=aid, seat=seat, prompt_kind=kind, **kw)

    def go(self, action, **kw):
        kw.setdefault("prompt", "PROMPT")
        return driver_exec.execute(self.b, self.cfg, self.sup, self.led, action,
                                   start_runner=kw.pop("start_runner", lambda c, r: self.launched.append(r)),
                                   now=kw.pop("now", None) or self.now(), **kw)

    def reload(self):
        self.led = ledger.load(self.bk, "J")
        return self.led

    def row_of(self, run):
        return next(r for r in self.b.restarts(job="J") if r.id == run["restart_id"])

    def write_tail(self, run, text):
        with runner._runs_fd(self.bk) as d:
            privfs.write_atomic(d, f"r{run['restart_id']}.tail.txt", "== stdout (last 32 KB, redacted)\n" + text + "\n")

    def end(self, run, outcome="failed", idle=True, **fields):
        """The end of a launch as the runner and reconcile leave it: row finished, locks released, the
        intent concluded, the seat idle (authorization carried)."""
        self.b.finish_restart(run["restart_id"], outcome)
        self.b.release_drive_locks(run["launch_id"], job="J", holder=run["action_id"])
        self.reload()
        self.conclude_intent(outcome)
        if idle:
            driver_exec._idle_seat(self.b, self.led, run["seat"], outcome, self.now(), **fields)

    def conclude_intent(self, outcome="failed", seat="eng-a", usd=0.0):
        for i in self.led.intents:
            if i.state != "concluded":
                i.state, i.outcome, i.success, i.charged_usd = "concluded", outcome, outcome == "completed", usd
        self.led.save()

    def spent(self, usd, outcome="completed"):
        self.led.intents.append(ledger.Intent(f"old{len(self.led.intents)}", f"gen1|seat|x{len(self.led.intents)}|-|0|1", 1, "x",
                                              usd, 1.0, [], "concluded", outcome, True, usd))
        self.led.save()


class ExecuteTests(ExecEnv):
    def test_launches_an_authorized_seat(self):
        self.seat()
        run = self.go(self.act())
        self.assertIsNotNone(run)
        self.reload()
        (intent,) = self.led.intents
        self.assertEqual((intent.state, intent.seat, intent.attempt), ("claimed", "eng-a", 1))
        self.assertEqual((intent.reserved_usd, intent.reserved_minutes), (5.0, 60.0))   # session cap, session minutes
        self.assertTrue(intent.locks[0].startswith("wt:"))
        row = self.row_of(run)
        self.assertEqual((row.launch_id, row.action_id, row.reserved_usd), (intent.launch_id, intent.action_id, 5.0))
        self.assertEqual(self.b.seat("J", 1, "eng-a").state, "running")
        self.assertEqual([d.launch_id for d in self.b.drive_locks()], [intent.launch_id])
        self.assertEqual((run["launch_id"], run["action_id"], run["stdin"], run["cwd"]),
                         (intent.launch_id, intent.action_id, "PROMPT", str(self.work / "eng-a")))
        self.assertEqual(run["resume_of"], f"launch:{intent.launch_id}")
        self.assertTrue(runner.has_run(self.cfg, row.id))
        self.assertTrue(os.path.exists(run["marker"]))
        self.assertIn("--max-budget-usd", run["argv"])
        self.assertEqual(run["argv"][run["argv"].index("--max-budget-usd") + 1], "5")
        self.assertEqual(run["limit_seconds"], 3600.0)

    def test_unauthorized_seat_is_not_launched(self):
        self.seat(authorize=False)
        self.assertIsNone(self.go(self.act()))
        self.assertEqual((self.reload().intents, self.b.restarts(job="J")), ([], []))
        self.assertEqual(self.b.seat("J", 1, "eng-a").state, "requested")

    def test_a_row_changed_after_its_authorization_is_not_launched(self):
        row = self.seat()
        self.b.request_seat("J", 1, "eng-a", role="engineer", harness="claude", model="opus",
                            workdir=row.workdir, brief_path=row.brief_path, requested_by="lead")
        self.assertIsNone(self.go(self.act()))

    def test_a_role_forged_on_the_row_is_not_launched_and_takes_no_lock_shortcut(self):
        self.seat()
        self.tamper_seat("eng-a", role="reviewer")   # a read-only role: no lock
        self.assertIsNone(self.go(self.act()))
        self.assertEqual((self.reload().intents, self.b.drive_locks()), ([], []))

    def test_a_forged_role_on_an_idle_seat_is_not_carried_forward(self):
        self.seat()
        self.end_run = self.go(self.act())
        self.end(self.end_run, "completed")
        self.tamper_seat("eng-a", role="judge")
        self.assertIsNone(self.go(self.act(trigger="2")))

    def test_only_launch_actions_in_this_generation_run(self):
        self.seat()
        self.assertIsNone(self.go(dm.Action(kind="hold", action_id="gen1|hold|-|-|0|-", seat=None, prompt_kind=None)))
        self.assertIsNone(self.go(self.act().__class__(kind="launch", action_id="gen2|seat|eng-a|-|0|1",
                                                        seat="eng-a", prompt_kind="seat")))
        self.assertEqual(self.reload().intents, [])

    def test_intent_is_written_before_the_claim(self):
        self.seat()
        seen = []
        real = self.b.claim_drive_launch

        def claim(**kw):
            led = ledger.load(self.bk, "J")
            seen.append([(i.launch_id, i.state, i.seat) for i in led.intents])
            return real(**kw)
        with mock.patch.object(self.b, "claim_drive_launch", side_effect=claim):
            run = self.go(self.act())
        self.assertEqual(seen, [[(run["launch_id"], "claiming", "eng-a")]])

    def test_lost_race_intent_concluded_lost_uncounted(self):
        self.seat()
        with mock.patch.object(self.b, "claim_drive_launch", return_value=None):
            self.assertIsNone(self.go(self.act()))
        (i,) = self.reload().intents
        self.assertEqual((i.state, i.outcome, i.success, i.charged_usd), ("concluded", "lost", False, 0.0))
        self.assertEqual(self.led.charged_usd(), 0.0)
        self.assertEqual(dm._Ctx(snap(ledger=self.led), {}).attempts(i.action_id), 0)   # uncounted
        self.assertEqual(self.b.restarts(job="J"), [])

    def test_a_paused_job_launches_nothing_and_loses_no_attempt(self):
        self.seat()
        with mock.patch.object(self.b, "allocate_name", side_effect=JobPaused("J", None, None, None)):
            self.assertIsNone(self.go(self.act()))
        (i,) = self.reload().intents
        self.assertEqual((i.outcome, i.state), ("lost", "concluded"))
        self.assertEqual(self.b.restarts(job="J"), [])

    def test_a_claim_that_raises_concludes_its_intent_and_propagates(self):
        self.seat()
        with mock.patch.object(self.b, "claim_drive_launch", side_effect=ValueError("bad lock")):
            with self.assertRaises(ValueError):
                self.go(self.act())
        self.assertEqual(self.reload().intents[0].outcome, "lost")

    def test_an_open_attempt_of_the_same_action_blocks_a_second(self):
        self.seat()
        self.assertIsNotNone(self.go(self.act()))
        self.assertIsNone(self.go(self.act()))
        self.assertEqual(len(self.reload().intents), 1)

    def test_an_open_attempt_blocks_even_when_the_seat_is_launchable(self):
        self.seat()
        self.led.intents.append(ledger.Intent("t1-" + "0" * 32, self.act().action_id, 1, "other", 5.0, 60.0, [],
                                              "claimed"))
        self.led.save()
        self.assertIsNone(self.go(self.act()))
        self.assertEqual(self.b.restarts(job="J"), [])

    def test_a_row_altered_during_the_claim_loses_its_authorization(self):
        self.seat()
        real = self.b.claim_drive_launch

        def claim_then_alter(**kw):
            row = real(**kw)
            self.tamper_seat("eng-a", workdir="/elsewhere")
            return row
        with mock.patch.object(self.b, "claim_drive_launch", side_effect=claim_then_alter):
            run = self.go(self.act())
        self.end(run, "completed")
        self.assertEqual(self.b.seat("J", 1, "eng-a").state, "idle")
        self.assertIsNone(self.go(self.act(trigger="2")))            # not authorized at its new revision

    def test_next_attempt_numbers_never_reuse_and_stop_at_max_attempts(self):
        self.seat()
        for n in (1, 2, 3):
            run = self.go(self.act())
            self.assertIsNotNone(run, n)
            self.reload()
            self.assertEqual(self.led.intents[-1].attempt, n)
            self.end(run)
            self.reload()
        self.assertIsNone(self.go(self.act()))                       # max_attempts = 3 counted attempts

    def test_outage_and_lost_attempts_do_not_count_toward_max_attempts(self):
        self.seat()
        for outcome in ("outage", "lost", "outage"):
            self.led.intents.append(ledger.Intent(f"x{len(self.led.intents)}", self.act().action_id,
                                                  len(self.led.intents) + 1, "eng-a", 1.0, 1.0, [], "concluded",
                                                  outcome, False, 0.0))
        self.led.save()
        run = self.go(self.act())
        self.assertIsNotNone(run)
        self.assertEqual(self.reload().intents[-1].attempt, 4)

    def test_budget_keeps_the_ruling_reserve_and_a_ruling_draws_on_it(self):
        self.seat()
        self.spent(36.0)                      # 40 - 5 reserve - 36 < min_session_usd
        self.assertIsNone(self.go(self.act()))
        self.seat("ruling-judge", role="judge", workdir=str(self.work))
        run = self.go(self.act(seat="ruling-judge", kind="ruling", group="ruling", ruling=True))
        self.assertIsNotNone(run)
        self.assertEqual(self.reload().intents[-1].reserved_usd, 4.0)   # what is left of the cap

    def test_reservation_is_capped_by_the_session_cap_and_by_what_is_left(self):
        self.seat()
        self.spent(31.0)                      # 40 - 5 - 31 = 4 left for a seat
        run = self.go(self.act())
        self.assertEqual(self.reload().intents[-1].reserved_usd, 4.0)
        self.assertEqual(run["argv"][run["argv"].index("--max-budget-usd") + 1], "4")

    def test_the_job_budget_overrides_the_defaults(self):
        self.led.budgets = {"session_usd_cap": 2, "max_session_minutes": 10}
        self.led.save()
        self.seat()
        run = self.go(self.act())
        self.assertEqual((run["limit_seconds"], self.reload().intents[-1].reserved_usd), (600.0, 2.0))

    def test_max_sessions_stops_seats_but_a_ruling_may_exceed_it(self):
        self.led.budgets = {"max_sessions": 1}
        self.led.save()
        self.spent(0.1)
        self.seat()
        self.assertIsNone(self.go(self.act()))
        self.seat("ruling-judge", role="judge", workdir=str(self.work))
        self.assertIsNotNone(self.go(self.act(seat="ruling-judge", kind="ruling", group="ruling", ruling=True)))

    def test_lost_intents_do_not_count_as_sessions(self):
        self.led.budgets = {"max_sessions": 1}
        self.led.intents.append(ledger.Intent("L0", "gen1|seat|z|-|0|1", 1, "z", 1.0, 1.0, [], "concluded", "lost",
                                              False, 0.0))
        self.led.save()
        self.seat()
        self.assertIsNotNone(self.go(self.act()))

    def test_two_writers_never_share_a_checkout(self):
        self.seat("eng-a", workdir=str(self.work))
        self.seat("eng-b", workdir=str(self.work / "sub"))      # nested in the same checkout
        self.assertIsNotNone(self.go(self.act("eng-a")))
        self.assertIsNone(self.go(self.act("eng-b")))
        self.assertEqual([i.outcome for i in self.reload().intents], [None, "lost"])

    def test_read_only_seats_take_no_lock_and_run_read_only(self):
        self.seat("rev", role="reviewer", workdir=str(self.work))
        run = self.go(self.act("rev", kind="review", group="feat/a"))
        self.assertEqual(self.reload().intents[0].locks, [])
        self.assertEqual(self.b.drive_locks(), [])
        self.assertIn("--disallowedTools", run["argv"])

    def test_kill_switch_between_claim_and_start_cancels_the_launch(self):
        self.seat()
        real = self.b.claim_drive_launch

        def claim_then_close(**kw):
            row = real(**kw)
            self.b.close_job("J", "cancelled", "person", forced=True)
            return row
        started = []
        with mock.patch.object(self.b, "claim_drive_launch", side_effect=claim_then_close):
            run = self.go(self.act(), start_runner=lambda c, r: started.append(r))
        self.assertIsNone(run)
        self.assertEqual(started, [])
        (row,) = self.b.restarts(job="J")
        self.assertEqual(row.outcome, "cancelled")
        (i,) = self.reload().intents
        self.assertEqual((i.outcome, i.state, i.charged_usd), ("cancelled", "concluded", 0.0))
        self.assertFalse(runner.has_run(self.cfg, row.id))
        self.assertEqual(list(self.markers.glob("*--resume-r*.json")), [])
        self.assertEqual(self.b.drive_locks(), [])
        self.assertEqual(self.b.seat("J", 1, "eng-a").state, "idle")

    def test_a_halted_ledger_cancels_the_launch(self):
        self.seat()
        real = self.b.claim_drive_launch

        def claim_then_halt(**kw):
            row = real(**kw)
            other = ledger.load(self.bk, "J")
            other.halted = {"by": "person", "at": iso(self.now())}
            other.save()
            return row
        with mock.patch.object(self.b, "claim_drive_launch", side_effect=claim_then_halt):
            self.assertIsNone(self.go(self.act()))
        self.assertEqual(self.b.restarts(job="J")[0].outcome, "cancelled")
        self.assertEqual(self.launched, [])

    def test_a_failing_start_concludes_failed_cleans_up_and_raises(self):
        self.seat()

        def boom(cfg, run):
            raise OSError("no runner")
        with self.assertRaises(OSError):
            self.go(self.act(), start_runner=boom)
        (row,) = self.b.restarts(job="J")
        self.assertEqual(row.outcome, "failed")
        (i,) = self.reload().intents
        self.assertEqual((i.outcome, i.success, i.charged_usd), ("failed", False, 0.0))
        self.assertEqual((self.b.drive_locks(), list(self.markers.glob("*--resume-r*.json"))), ([], []))
        self.assertFalse(runner.has_run(self.cfg, row.id))

    def test_a_no_progress_launch_advances_the_counter_once_claimed(self):
        self.seat("lead", role="engineering_lead", workdir=str(self.work))
        self.assertIsNotNone(self.go(self.act("lead", kind="fallback", reason="invariant-fallback")))
        self.assertEqual(self.reload().rounds_without_progress, 1)
        self.seat("eng-a")
        self.go(self.act("eng-a", reason="wake"))
        self.assertEqual(self.reload().rounds_without_progress, 1)

    def test_a_lost_no_progress_launch_does_not_advance_the_counter(self):
        self.seat("lead", role="engineering_lead", workdir=str(self.work))
        with mock.patch.object(self.b, "claim_drive_launch", return_value=None):
            self.go(self.act("lead", kind="fallback", reason="stall"))
        self.assertEqual(self.reload().rounds_without_progress, 0)

    def test_a_seat_that_ran_before_resumes_its_session_and_a_new_one_does_not(self):
        self.seat()
        self.assertNotIn("--resume", self.go(self.act())["argv"])
        self.end(self.launched[-1], "completed", agent_key="sess-9", session_id="sess-9")
        again = self.go(self.act(trigger="2"))
        self.assertEqual(again["argv"][again["argv"].index("--resume") + 1], "sess-9")

    def test_each_launch_gets_a_placeholder_agent_and_a_marker(self):
        self.seat()
        run = self.go(self.act())
        marker = markers.read_marker(__import__("pathlib").Path(run["marker"]))
        self.assertEqual(marker["job"], "J")
        self.assertEqual(marker["resume"]["resume_of"], run["resume_of"]) if "resume" in marker else None
        self.assertTrue(run["name"])
        self.assertEqual(self.b.restarts(job="J")[0].old_agent_key, run["resume_of"])

    def test_the_launch_remembers_the_board_position_it_started_from(self):
        self.b.post("J", self.b.allocate_name("p", "J", "engineer"), "hello")
        self.seat()
        run = self.go(self.act())
        newest = self.b.recent_messages(1, job="J")[0].id
        self.assertEqual(self.reload().watermarks[f"launch-msg:{run['launch_id']}"], newest)
        self.assertIn(f"launch-event:{run['launch_id']}", self.led.watermarks)


class DriverSeatTests(ExecEnv):
    def test_judge_seat_is_created_authorized_and_launched_read_only_on_the_judge_model(self):
        run = self.go(self.act(seat=None, kind="judge", group="feat/a"))
        self.assertIsNotNone(run)
        seat = self.b.seat("J", 1, "judge")
        self.assertEqual((seat.role, seat.harness, seat.model, seat.requested_by, seat.state),
                         ("judge", "claude", "opus", "driver", "running"))
        self.assertEqual(seat.workdir, str(self.work))
        self.assertIn("--disallowedTools", run["argv"])
        self.assertEqual(run["argv"][run["argv"].index("--model") + 1], "opus")
        self.assertEqual(self.b.drive_locks(), [])

    def test_reviewer_and_integrator_names_are_valid_deterministic_and_per_group(self):
        a = driver_exec.seat_name("reviewer", "feat/Auth Login")
        self.assertRegex(a, r"^reviewer-feat-auth-login-[0-9a-f]{8}$")
        self.assertEqual(a, driver_exec.seat_name("reviewer", "feat/Auth Login"))
        self.assertNotEqual(a, driver_exec.seat_name("reviewer", "feat/auth-login"))   # same slug, another group
        self.assertNotEqual(driver_exec.seat_name("reviewer", "g"), driver_exec.seat_name("integrator", "g"))
        self.assertEqual(driver_exec.seat_name("goal-judge"), "goal-judge")
        run = self.go(self.act(seat=None, kind="review", group="feat/a"))
        self.assertIsNotNone(run)
        seat = self.b.seat("J", 1, driver_exec.seat_name("reviewer", "feat/a"))
        self.assertEqual((seat.role, seat.model), ("reviewer", "sonnet"))

    def test_integrator_is_a_writer_on_the_haiku_model(self):
        run = self.go(self.act(seat=None, kind="integrate", group="feat/a"))
        seat = self.b.seat("J", 1, driver_exec.seat_name("integrator", "feat/a"))
        self.assertEqual((seat.role, seat.model), ("integrator", "haiku"))
        self.assertTrue(self.reload().intents[0].locks[0].startswith("wt:"))
        self.assertNotIn("--disallowedTools", run["argv"])

    def test_goal_and_ruling_judges_have_their_own_seats(self):
        self.assertIsNotNone(self.go(self.act(seat=None, kind="goal", group="goal")))
        self.assertIsNotNone(self.go(self.act(seat=None, kind="ruling", group="ruling", ruling=True)))
        self.assertEqual({s.seat for s in self.b.seats("J")}, {"goal-judge", "ruling-judge"})

    def test_driver_seats_never_resume_an_old_session(self):
        run = self.go(self.act(seat=None, kind="judge", group="a"))
        self.end(run, "completed", agent_key="old", session_id="old")
        again = self.go(self.act(seat=None, kind="judge", group="b"))
        self.assertNotIn("--resume", again["argv"])

    def test_the_jobs_model_table_wins_over_the_drive_config_and_the_defaults(self):
        self.cfg["drive"] = {"models": {"judge": "haiku", "qa": "opus"}}
        job = NS(models={"judge": "sonnet"})                  # the ledger's `models` field (Task 6a)
        table = driver_exec._models(self.cfg, job)
        self.assertEqual((table["judge"], table["qa"], table["reviewer"], table["engineer"]),
                         ("sonnet", "opus", "sonnet", "haiku"))
        self.assertEqual(driver_exec._models({}, NS())["judge"], "opus")

    def test_the_drive_config_overrides_the_default_model(self):
        self.cfg["drive"] = {"models": {"reviewer": "haiku"}}
        self.go(self.act(seat=None, kind="review", group="feat/a"))
        self.assertEqual(self.b.seat("J", 1, driver_exec.seat_name("reviewer", "feat/a")).model, "haiku")

    def test_no_owner_record_means_no_driver_seat(self):
        os.unlink(next(p for p in (enrolment.enrolled_dir()).glob("owner-*.json")))
        self.assertIsNone(self.go(self.act(seat=None, kind="judge", group="a")))
        self.assertEqual(self.b.seats("J"), [])

    def test_an_action_with_no_seat_and_no_driver_kind_launches_nothing(self):
        self.assertIsNone(self.go(self.act(seat=None, kind="lead")))
        self.assertEqual(self.b.seats("J"), [])

    def test_lead_successor_copies_the_failed_lead_and_is_authorized(self):
        lead = self.seat("lead", role="engineering_lead", workdir=str(self.work))
        self.go(self.act("lead", kind="lead"))
        self.end(self.launched[-1], "failed", idle=False)
        cur = self.b.seat("J", 1, "lead")
        self.b.set_seat_state(cur.id, cur.revision, "failed", job="J")
        run = self.go(self.act("lead~1", kind="lead", trigger="2"))
        self.assertIsNotNone(run)
        succ = self.b.seat("J", 1, "lead~1")
        self.assertEqual((succ.role, succ.model, succ.workdir, succ.brief_path, succ.requested_by),
                         (lead.role, lead.model, lead.workdir, lead.brief_path, "driver"))
        self.assertNotIn("--resume", run["argv"])

    def test_owner_successor_copies_the_owner(self):
        owner = self.seat("eng-a")
        self.go(self.act("eng-a"))
        self.end(self.launched[-1], "failed", idle=False)
        cur = self.b.seat("J", 1, "eng-a")
        self.b.set_seat_state(cur.id, cur.revision, "failed", job="J")
        self.assertIsNotNone(self.go(self.act("eng-a~1", trigger="2")))
        self.assertEqual(self.b.seat("J", 1, "eng-a~1").workdir, owner.workdir)

    def test_an_unknown_seat_that_is_not_a_successor_is_not_created(self):
        self.assertIsNone(self.go(self.act("ghost")))
        self.assertEqual(self.b.seats("J"), [])

    def test_driver_authorization_binds_the_row_it_wrote(self):
        self.go(self.act(seat=None, kind="judge", group="a"))
        row = self.b.seat("J", 1, "judge")
        r = receipts.find(self.bk, "J", "seat_request", str(row.id), rev=row.revision,
                          incarnation=self.led.incarnation, generation=1, now=time.time())
        self.assertIsNotNone(r)
        self.assertEqual(r.action_id, "driver|authorize|judge")
        self.assertGreater(r.expires_at, time.time() + 3600)


class ReconcileTests(ExecEnv):
    def claim_only(self, seat="eng-a", age=0.0):
        """A claim whose process crashed after the claim: an intent and a board row, no run file."""
        self.seat(seat)
        row = self.b.seat("J", 1, seat)
        lid = driver_exec.new_launch_id(self.now())
        self.led.intents.append(ledger.Intent(lid, self.act(seat).action_id, 1, seat, 5.0, 60.0, [], "claiming"))
        self.led.save()
        self.b.allocate_name(f"launch:{lid}", "J", "engineer")
        self.b.claim_drive_launch(job="J", generation=1, action_id=self.act(seat).action_id, attempt=1, launch_id=lid,
                                  seat_id=row.id, seat_revision=row.revision, harness="claude", reserve_usd=5,
                                  reserve_minutes=60, locks=[], lock_seconds=4200, max_host_running=4)
        return lid

    def rec(self, seconds=0):
        driver_exec.reconcile(self.b, self.cfg, self.led, self.now(seconds), time.monotonic() + 20)
        return self.reload()

    def test_claim_without_run_file_is_concluded_after_grace(self):
        lid = self.claim_only()
        led = self.rec(91)
        (i,) = led.intents
        self.assertEqual((i.state, i.success, i.outcome, i.charged_usd), ("concluded", False, "failed", 0.0))
        self.assertEqual(self.b.restarts(job="J")[-1].outcome, "failed")
        self.assertEqual(self.b.seat("J", 1, "eng-a").state, "idle")

    def test_a_claim_within_the_grace_period_is_left_alone(self):
        self.claim_only()
        led = self.rec(80)
        self.assertEqual([i.state for i in led.intents], ["claimed"])
        self.assertIsNone(self.b.restarts(job="J")[-1].outcome)

    def test_never_started_claim_releases_locks_and_removes_its_marker(self):
        lid = self.claim_only()
        row = self.b.restarts(job="J")[0]
        marker = markers.write_resume_marker(self.cfg, "J", row.id, resume_of=f"launch:{lid}", name="n", harness="claude")
        self.rec(91)
        self.assertFalse(marker.exists())

    def test_a_claim_with_a_run_file_is_left_to_the_reaper(self):
        self.seat()
        run = self.go(self.act())
        with mock.patch.object(runner, "reap") as reap:
            led = self.rec(500)
        reap.assert_called_once()
        self.assertEqual([i.state for i in led.intents], ["claimed"])
        self.assertIsNone(self.row_of(run).outcome)

    def test_claiming_with_a_board_row_becomes_claimed(self):
        self.claim_only()
        self.led.intents[0].state = "claiming"
        self.led.save()
        self.assertEqual(self.rec(1).intents[0].state, "claimed")

    def test_forged_restart_row_ignored(self):
        self.seat()
        row = self.b.seat("J", 1, "eng-a")
        self.b.claim_drive_launch(job="J", generation=1, action_id="gen1|seat|eng-a|-|0|9", attempt=1,
                                  launch_id="FORGED", seat_id=row.id, seat_revision=row.revision, harness="claude",
                                  reserve_usd=5, reserve_minutes=60, locks=[], lock_seconds=60, max_host_running=4)
        led = self.rec(500)
        self.assertEqual(led.charged_usd(), 0.0)
        self.assertEqual(led.intents, [])
        self.assertIsNone(self.b.restarts(job="J")[0].outcome)          # not touched either

    def test_a_row_without_a_matching_intent_and_an_intent_without_a_row_after_grace(self):
        lid = driver_exec.new_launch_id(self.now(-200))
        self.led.intents.append(ledger.Intent(lid, "gen1|seat|a|-|0|1", 1, "a", 5.0, 60.0, [], "claiming"))
        self.led.save()
        led = self.rec(0)
        self.assertEqual((led.intents[0].state, led.intents[0].outcome, led.intents[0].charged_usd),
                         ("concluded", "failed", 0.0))

    def test_an_intent_without_a_row_inside_the_grace_period_waits(self):
        lid = driver_exec.new_launch_id(self.now(-10))
        self.led.intents.append(ledger.Intent(lid, "gen1|seat|a|-|0|1", 1, "a", 5.0, 60.0, [], "claiming"))
        self.led.save()
        self.assertEqual(self.rec(0).intents[0].state, "claiming")

    def test_deleted_board_row_with_live_lock_is_not_restarted(self):
        self.seat()
        run = self.go(self.act())
        fd = runner.own(run["restart_id"], self.bk)                       # a live runner holds the run lock
        self.addCleanup(os.close, fd)
        self.delete_restarts()
        led = self.rec(1000)
        self.assertEqual(led.intents[0].state, "claimed")
        self.assertEqual(self.launched, [run])

    def test_deleted_board_row_with_a_present_unit_is_live(self):
        self.seat()
        run = self.go(self.act())
        run["unit"] = f"swarm-r{run['restart_id']}-ab.scope"
        runner.write_run(run)
        self.delete_restarts()
        with mock.patch.object(runner, "unit_presence", return_value="present"):
            led = self.rec(1000)
        self.assertEqual(led.intents[0].state, "claimed")

    def test_deleted_board_row_with_nothing_alive_is_concluded_after_grace(self):
        self.seat()
        run = self.go(self.act())
        self.delete_restarts()
        runner.run_file(run["restart_id"], self.bk)       # the run file stays but no lock is held, no unit
        led = self.rec(1000)
        self.assertEqual(led.intents[0].state, "concluded")

    def test_a_row_ended_elsewhere_concludes_its_intent_with_the_cost_it_recorded(self):
        self.seat()
        run = self.go(self.act())
        self.b.finish_restart(run["restart_id"], "completed", cost_usd=0.75)
        os.unlink(runner.run_file(run["restart_id"], self.bk))
        (i,) = self.rec(1).intents
        self.assertEqual((i.state, i.outcome, i.success, i.charged_usd), ("concluded", "completed", True, 0.75))

    def test_an_ended_row_whose_run_file_is_still_there_is_left_to_its_finish(self):
        self.seat()
        run = self.go(self.act())
        self.b.finish_restart(run["restart_id"], "failed")
        fd = runner.own(run["restart_id"], self.bk)               # the runner is still finishing
        self.addCleanup(os.close, fd)
        self.assertEqual(self.rec(1).intents[0].state, "claimed")

    def test_an_ended_row_without_cost_charges_the_reservation(self):
        self.seat()
        run = self.go(self.act())
        self.b.finish_restart(run["restart_id"], "timeout")
        os.unlink(runner.run_file(run["restart_id"], self.bk))
        (i,) = self.rec(1).intents
        self.assertEqual((i.outcome, i.success, i.charged_usd), ("timeout", False, 5.0))

    def test_a_refused_row_charges_nothing(self):
        self.seat()
        run = self.go(self.act())
        self.b.finish_restart(run["restart_id"], "refused")
        os.unlink(runner.run_file(run["restart_id"], self.bk))
        (i,) = self.rec(1).intents
        self.assertEqual((i.outcome, i.charged_usd), ("refused", 0.0))

    def test_a_late_claim_for_a_concluded_intent_is_cancelled_and_its_locks_released(self):
        self.seat()
        run = self.go(self.act())
        self.conclude_intent("failed")                      # concluded while its claim had not committed yet
        os.unlink(runner.run_file(run["restart_id"], self.bk))
        self.rec(1)
        self.assertEqual(self.row_of(run).outcome, "cancelled")
        self.assertEqual(self.b.drive_locks(), [])

    def test_the_driver_reaps_drive_runs(self):
        self.seat()
        self.go(self.act())
        with mock.patch.object(runner, "reap", return_value=0) as reap:
            self.rec(1)
        reap.assert_called_once_with(self.cfg, self.b)

    def test_reaping_finishes_a_dead_runs_row_and_concludes_its_intent_with_the_reservation(self):
        self.seat()
        run = self.go(self.act())
        led = self.rec(1)                                    # no runner holds the lock: the real reap finishes it
        self.assertEqual(self.row_of(run).outcome, "failed")
        (i,) = led.intents
        self.assertEqual((i.state, i.outcome, i.charged_usd), ("concluded", "failed", 5.0))
        self.assertFalse(runner.has_run(self.cfg, run["restart_id"]))
        self.assertEqual(self.b.drive_locks(), [])

    def test_a_crashed_activation_is_finished_by_reconcile(self):
        self.led.state = "activating"
        calls = []
        stub = NS(finish_activation=lambda cfg, board, job: calls.append(job) or NS(state="active"))
        with mock.patch.dict(sys.modules, {"swarm.drive_activate": stub}), mock.patch.object(swarm, "drive_activate", stub, create=True):
            driver_exec.reconcile(self.b, self.cfg, self.led, self.now(1), time.monotonic() + 20)
        self.assertEqual(calls, ["J"])
        self.assertEqual(self.led.state, "active")                   # the caller's copy is refreshed

    def test_an_active_ledger_is_not_re_activated(self):
        calls = []
        stub = NS(finish_activation=lambda cfg, board, job: calls.append(job))
        self.led.state = "active"
        with mock.patch.dict(sys.modules, {"swarm.drive_activate": stub}), mock.patch.object(swarm, "drive_activate", stub, create=True):
            self.rec(1)
        self.assertEqual(calls, [])

    def test_a_failed_activation_recovery_does_not_stop_reconcile(self):
        self.led.state = "activating"
        self.claim_only()

        def boom(cfg, board, job):
            raise RuntimeError("DRIVER-ORPHAN")
        stub = NS(finish_activation=boom)
        with mock.patch.dict(sys.modules, {"swarm.drive_activate": stub}), mock.patch.object(swarm, "drive_activate", stub, create=True):
            led = self.rec(91)
        self.assertEqual(led.intents[0].state, "concluded")

    def test_reconcile_runs_the_host_lock_sweep_and_survives_its_failure(self):
        calls = []
        life = NS(sweep_locks_by_host=lambda board, cfg, now: calls.append((board, cfg, now)))
        with mock.patch.dict(sys.modules, {"swarm.supervisor.driver_life": life}), \
                mock.patch.object(driver_exec_pkg(), "driver_life", life, create=True):
            self.rec(1)
            life.sweep_locks_by_host = mock.Mock(side_effect=RuntimeError("boom"))
            self.claim_only()
            led = self.rec(91)
        self.assertEqual([c[0] for c in calls], [self.b])
        self.assertEqual(led.intents[0].state, "concluded")          # a failing sweep does not stop reconcile

    def test_nothing_is_reaped_when_there_is_no_open_intent(self):
        with mock.patch.object(runner, "reap") as reap:
            self.rec(1)
        reap.assert_not_called()

    def test_an_expired_deadline_does_no_work(self):
        self.claim_only()
        driver_exec.reconcile(self.b, self.cfg, self.led, self.now(500), time.monotonic() - 1)
        self.assertEqual(self.reload().intents[0].state, "claiming")
        self.assertIsNone(self.b.restarts(job="J")[0].outcome)

    def test_a_row_of_another_host_is_not_touched(self):
        self.claim_only()
        with mock.patch.object(driver_exec.compat, "node", return_value="elsewhere"):
            self.rec(500)
        self.assertIsNone(self.b.restarts(job="J")[0].outcome)

    def test_a_seat_whose_launch_ended_goes_idle_and_keeps_its_session(self):
        self.seat()
        run = self.go(self.act())
        self.b.set_restart_agent(run["restart_id"], "sess-1")
        self.b.finish_restart(run["restart_id"], "completed", cost_usd=0.1)
        os.unlink(runner.run_file(run["restart_id"], self.bk))
        self.rec(1)
        seat = self.b.seat("J", 1, "eng-a")
        self.assertEqual((seat.state, seat.last_outcome, seat.agent_key, seat.session_id),
                         ("idle", "completed", "sess-1", "sess-1"))

    def test_max_attempts_comes_from_the_drive_config(self):
        self.cfg["drive"] = {"max_attempts": 1}
        self.seat()
        run = self.go(self.act())
        self.assertTrue(runner.finish(self.cfg, run, "failed"))
        self.rec(1)
        self.assertEqual(self.b.seat("J", 1, "eng-a").state, "failed")

    def test_a_seat_that_ran_out_of_attempts_fails(self):
        self.seat()
        for n in range(3):
            run = self.go(self.act())
            self.assertTrue(runner.finish(self.cfg, run, "failed"))
            self.rec(1)
            if n < 2:
                seat = self.b.seat("J", 1, "eng-a")
                self.assertEqual(seat.state, "idle", n)
        self.assertEqual(self.b.seat("J", 1, "eng-a").state, "failed")

    def test_a_running_seat_with_a_live_launch_is_left_alone(self):
        self.seat()
        run = self.go(self.act())
        fd = runner.own(run["restart_id"], self.bk)
        self.addCleanup(os.close, fd)
        self.rec(1)
        self.assertEqual(self.b.seat("J", 1, "eng-a").state, "running")

    def test_reconcile_ends_running_seats_with_no_launch(self):
        self.seat()
        self.tamper_seat("eng-a", state="running", launch_id=None)
        self.rec(1)
        seat = self.b.seat("J", 1, "eng-a")
        self.assertEqual((seat.state, seat.last_outcome), ("idle", "no launch"))


class UnlaunchedSeatSweepTests(unittest.TestCase):
    """A seat `running` with no launch_id can be ended on every backend (board/seats.py)."""

    def plant(self, h, b):
        b.open_job("J", None, None, None, "me", goal="G")
        s = b.request_seat("J", 1, "a", role="engineer", harness="claude", model="haiku", workdir="/w/a",
                           brief_path="/w/a/B.md", requested_by="lead")
        if h.name == "memory":
            with h.store.lock:
                next(r for r in h.store.seats if r["id"] == s.id).update(state="running", launch_id=None)
        else:
            h._db().execute("UPDATE seats SET state = 'running', launch_id = NULL WHERE id = ?", (s.id,))
        return s

    def run_on(self, h):
        h.reset()
        b = h.board()
        self.addCleanup(b.close)
        s = self.plant(h, b)
        self.assertEqual(driver_exec.sweep_unlaunched_seats(b, "J"), 1)
        seat = b.seat("J", 1, "a")
        self.assertEqual((seat.state, seat.last_outcome), ("idle", "no launch"))
        self.assertEqual(driver_exec.sweep_unlaunched_seats(b, "J"), 0)
        return s

    def test_memory(self):
        self.run_on(MemoryHarness("t8a-sweep"))

    def test_sqlite(self):
        h = SqliteHarness()
        self.addCleanup(h.close)
        self.run_on(h)

    def test_a_running_seat_with_an_open_launch_still_cannot_leave_running(self):
        h = MemoryHarness("t8a-open")
        h.reset()
        b = h.board()
        self.addCleanup(b.close)
        b.open_job("J", None, None, None, "me", goal="G")
        s = b.request_seat("J", 1, "a", role="engineer", harness="claude", model="haiku", workdir="/w/a",
                           brief_path="/w/a/B.md", requested_by="lead")
        b.claim_drive_launch(job="J", generation=1, action_id="1|seat|a", attempt=1, launch_id="L1", seat_id=s.id,
                             seat_revision=s.revision, harness="claude", reserve_usd=1, reserve_minutes=1, locks=[],
                             lock_seconds=60, max_host_running=4)
        cur = b.seat("J", 1, "a")
        with self.assertRaisesRegex(ValueError, "still open"):
            b.set_seat_state(cur.id, cur.revision, "idle", job="J")
        self.assertEqual(driver_exec.sweep_unlaunched_seats(b, "J"), 0)


class FinishTests(ExecEnv):
    """runner.finish() records what a drive session cost and concludes the ledger intent."""

    def finished(self, tail, outcome="completed", harness="claude", seat="eng-a"):
        self.seat(seat, harness=harness)
        run = self.go(self.act(seat))
        if tail is not None:
            self.write_tail(run, tail)
        self.assertTrue(runner.finish(self.cfg, run, outcome))
        return run

    def claude_tail(self, cost=0.42, tin=10, tout=5):
        return json.dumps({"type": "result", "total_cost_usd": cost, "usage": {"input_tokens": tin, "output_tokens": tout}})

    def test_claude_usage_lands_on_the_row_and_the_intent_is_charged_it(self):
        run = self.finished(self.claude_tail())
        row = self.row_of(run)
        self.assertEqual((row.outcome, row.cost_usd, row.tokens_in, row.tokens_out), ("completed", 0.42, 10, 5))
        (i,) = self.reload().intents
        self.assertEqual((i.state, i.outcome, i.success, i.charged_usd), ("concluded", "completed", True, 0.42))

    def test_missing_usage_charges_reservation(self):
        run = self.finished("killed")
        row = self.row_of(run)
        self.assertEqual((row.cost_usd, row.tokens_in), (None, None))
        (i,) = self.reload().intents
        self.assertEqual((i.outcome, i.success, i.charged_usd), ("completed", True, 5.0))
        self.assertEqual(self.led.charged_usd(), 5.0)

    def test_no_output_tail_charges_the_reservation(self):
        self.finished(None, outcome="failed")
        (i,) = self.reload().intents
        self.assertEqual((i.outcome, i.success, i.charged_usd), ("failed", False, 5.0))

    def test_codex_tokens_are_priced_by_the_drive_rate(self):
        tail = "\n".join(json.dumps({"type": "turn.completed", "usage": {"input_tokens": t, "output_tokens": o}})
                         for t, o in ((600_000, 100_000), (200_000, 100_000)))
        run = self.finished(tail, harness="codex")
        row = self.row_of(run)
        self.assertEqual((row.tokens_in, row.tokens_out), (800_000, 200_000))
        self.assertAlmostEqual(row.cost_usd, 10.0)                       # 1M tokens at 10 USD / Mtok
        self.assertAlmostEqual(self.reload().intents[0].charged_usd, 10.0)

    def test_codex_rate_follows_the_drive_config(self):
        self.cfg["drive"] = {"codex_usd_per_mtok": 2}
        tail = json.dumps({"type": "turn.completed", "usage": {"input_tokens": 500_000, "output_tokens": 0}})
        run = self.finished(tail, harness="codex")
        self.assertAlmostEqual(self.row_of(run).cost_usd, 1.0)

    def test_an_unsuccessful_outcome_is_recorded_and_not_a_success(self):
        run = self.finished(self.claude_tail(1.5), outcome="timeout")
        (i,) = self.reload().intents
        self.assertEqual((i.outcome, i.success, i.charged_usd), ("timeout", False, 1.5))

    def test_a_provider_outage_concludes_outage(self):
        tail = json.dumps({"type": "result", "subtype": "success", "is_error": True, "api_error_status": 529,
                           "result": "API Error: 529 overloaded_error"})
        self.finished(tail, outcome="failed")
        (i,) = self.reload().intents
        self.assertEqual((i.outcome, i.success), ("outage", False))

    def test_an_outage_text_does_not_turn_a_completed_run_into_an_outage(self):
        tail = json.dumps({"type": "result", "is_error": True, "api_error_status": 529, "result": "overloaded",
                           "total_cost_usd": 0.1, "usage": {}})
        self.finished(tail, outcome="completed")
        self.assertEqual(self.reload().intents[0].outcome, "completed")

    def test_a_refused_launch_without_usage_charges_nothing(self):
        self.finished(None, outcome="refused")
        self.assertEqual(self.reload().intents[0].charged_usd, 0.0)

    def test_finish_releases_the_launchs_drive_locks(self):
        self.finished(self.claude_tail())
        self.assertEqual(self.b.drive_locks(), [])

    def test_locks_stay_while_the_scope_unit_is_present_or_unknown(self):
        self.seat()
        run = self.go(self.act())
        run["unit"] = f"swarm-r{run['restart_id']}-ab.scope"
        for presence in ("present", "unknown"):
            with mock.patch.object(runner, "unit_presence", return_value=presence):
                self.assertTrue(runner.finish(self.cfg, dict(run), "completed"))
            self.assertEqual(len(self.b.drive_locks()), 1, presence)
            runner.write_run(run)
        with mock.patch.object(runner, "unit_presence", return_value="absent"):
            self.assertTrue(runner.finish(self.cfg, dict(run), "completed"))
        self.assertEqual(self.b.drive_locks(), [])

    def test_a_retried_finish_concludes_once_and_keeps_the_first_charge(self):
        self.seat()
        run = self.go(self.act())
        self.write_tail(run, self.claude_tail(0.5))
        marker_busy = {"n": 0}
        real = markers.remove_resume_marker

        def busy_once(path):
            marker_busy["n"] += 1
            return False if marker_busy["n"] == 1 else real(path)
        with mock.patch.object(markers, "remove_resume_marker", side_effect=busy_once):
            self.assertFalse(runner.finish(self.cfg, run, "completed"))
            first = self.reload().intents[0].charged_usd
            self.assertTrue(runner.finish(self.cfg, run, "completed"))
        self.assertEqual((first, self.reload().intents[0].charged_usd), (0.5, 0.5))
        self.assertEqual(self.row_of(run).cost_usd, 0.5)

    def test_a_run_without_a_launch_id_is_a_plain_replacement(self):
        self.seat()
        run = self.go(self.act())
        plain = {k: v for k, v in run.items() if k not in ("launch_id", "action_id")}
        self.write_tail(run, self.claude_tail())
        self.assertTrue(runner.finish(self.cfg, plain, "completed"))
        self.assertIsNone(self.row_of(run).cost_usd)
        self.assertEqual(self.reload().intents[0].state, "claimed")

    def test_finish_acknowledges_the_wake_watermarks_of_a_completed_turn(self):
        self.b.post("J", self.b.allocate_name("p", "J", "engineer"), "hello")
        newest = self.b.recent_messages(1, job="J")[0].id
        self.finished(self.claude_tail())
        led = self.reload()
        self.assertEqual(led.watermarks["msg:eng-a"], newest)
        self.assertIn("event:eng-a", led.watermarks)
        self.assertFalse([k for k in led.watermarks if k.startswith("launch-")])

    def test_a_crashed_turn_leaves_its_events_pending(self):
        self.b.post("J", self.b.allocate_name("p", "J", "engineer"), "hello")
        self.finished(self.claude_tail(), outcome="not_enrolled")
        led = self.reload()
        self.assertNotIn("msg:eng-a", led.watermarks)
        self.assertFalse([k for k in led.watermarks if k.startswith("launch-")])

    def test_max_turns_and_timeout_still_acknowledge(self):
        self.b.post("J", self.b.allocate_name("p", "J", "engineer"), "hello")
        self.finished(self.claude_tail(), outcome="max_turns")
        self.assertIn("msg:eng-a", self.reload().watermarks)

    def test_an_acknowledged_mark_never_moves_back(self):
        self.led.watermarks["msg:eng-a"] = 10 ** 6
        self.led.save()
        self.finished(self.claude_tail())
        self.assertEqual(self.reload().watermarks["msg:eng-a"], 10 ** 6)


class ProgressTests(ExecEnv):
    def now_(self):
        return dt.datetime(2026, 10, 10, 12, 0, tzinfo=dt.timezone.utc)

    def settle(self, **kw):
        s = snap(ledger=self.led, **kw)
        changed = driver_exec.settle_progress(self.led, s, self.now_())
        return changed, self.reload()

    def test_ruling_due_is_recorded_once(self):
        t = self.now_()
        self.assertTrue(driver_exec.note_ruling_due(self.led, t))
        self.assertEqual(self.reload().ruling_due_at, iso(t))
        self.assertFalse(driver_exec.note_ruling_due(self.led, t + dt.timedelta(hours=1)))
        self.assertEqual(self.reload().ruling_due_at, iso(t))

    def test_a_not_met_round_without_progress_advances_the_counter_once(self):
        art = "feat/a@" + "a" * 40
        vd = v("not_met", artifact=art, open=3, receipt_id=5)
        changed, led = self.settle(verdicts={art: vd})
        self.assertEqual((changed, led.rounds_without_progress), (True, 1))
        changed, led = self.settle(verdicts={art: vd})                 # the same verdict again: nothing
        self.assertEqual((changed, led.rounds_without_progress), (False, 1))

    def test_a_round_whose_open_count_fell_on_a_new_artifact_is_progress(self):
        a1, a2 = "feat/a@" + "a" * 40, "feat/a@" + "b" * 40
        self.settle(verdicts={a1: v("not_met", artifact=a1, open=3, receipt_id=5)})
        self.assertEqual(self.led.rounds_without_progress, 1)
        _, led = self.settle(verdicts={a1: v("not_met", artifact=a1, open=3, receipt_id=5),
                                       a2: v("not_met", artifact=a2, open=2, receipt_id=6)})
        self.assertEqual(led.rounds_without_progress, 0)
        self.assertIsNotNone(led.last_progress_at)

    def test_open_that_did_not_fall_is_no_progress_even_on_a_new_artifact(self):
        a1, a2 = "feat/a@" + "a" * 40, "feat/a@" + "b" * 40
        self.settle(verdicts={a1: v("not_met", artifact=a1, open=3, receipt_id=5)})
        _, led = self.settle(verdicts={a1: v("not_met", artifact=a1, open=3, receipt_id=5),
                                       a2: v("not_met", artifact=a2, open=3, receipt_id=6)})
        self.assertEqual(led.rounds_without_progress, 2)

    def test_a_judge_progress_flag_is_progress(self):
        art = "feat/a@" + "a" * 40
        self.led.rounds_without_progress = 2
        self.led.save()
        _, led = self.settle(verdicts={art: v("not_met", artifact=art, open=3, progress=True, receipt_id=5)})
        self.assertEqual(led.rounds_without_progress, 0)

    def test_rounds_of_different_groups_do_not_share_their_history(self):
        a, b = "feat/a@" + "a" * 40, "feat/b@" + "b" * 40
        self.settle(verdicts={a: v("not_met", artifact=a, open=5, receipt_id=5)})
        _, led = self.settle(verdicts={a: v("not_met", artifact=a, open=5, receipt_id=5),
                                       b: v("not_met", artifact=b, open=1, receipt_id=6)})
        self.assertEqual(led.rounds_without_progress, 2)                  # b has no earlier round to beat

    def test_goal_verdicts_compare_open_counts(self):
        self.settle(goal_verdict=v("not_met", scope="goal", open=4, receipt_id=5))
        self.assertEqual(self.led.rounds_without_progress, 1)
        _, led = self.settle(goal_verdict=v("not_met", scope="goal", open=2, receipt_id=6))
        self.assertEqual(led.rounds_without_progress, 0)

    def test_a_met_verdict_is_not_a_round(self):
        art = "feat/a@" + "a" * 40
        changed, led = self.settle(verdicts={art: v("met", artifact=art, receipt_id=5)})
        self.assertEqual(led.rounds_without_progress, 0)
        self.assertIsNone(led.last_progress_at)
        self.assertEqual(led.watermarks["p.verdict"], 5)

    def test_a_new_handoff_moves_last_progress_at_but_not_the_counter(self):
        self.led.rounds_without_progress = 2
        self.led.save()
        t = self.now_() - dt.timedelta(minutes=7)
        _, led = self.settle(handoffs=[ho(at=t, receipt_id=9)])
        self.assertEqual((led.rounds_without_progress, led.last_progress_at), (2, iso(t)))   # the hand-off's own time
        again_changed, _ = self.settle(handoffs=[ho(at=t, receipt_id=9)])
        self.assertFalse(again_changed)

    def test_an_integration_is_progress(self):
        self.led.rounds_without_progress = 2
        self.led.save()
        _, led = self.settle(integrated={"feat/a": {"landed": "abc"}})
        self.assertEqual(led.rounds_without_progress, 0)
        self.assertIsNotNone(led.last_progress_at)
        again, _ = self.settle(integrated={"feat/a": {"landed": "abc"}})
        self.assertFalse(again)

    def test_nothing_new_changes_nothing(self):
        changed, led = self.settle()
        self.assertFalse(changed)
        self.assertEqual(led.ledger_rev, self.led.ledger_rev)

    def test_progress_survives_a_conflicting_save(self):
        art = "feat/a@" + "a" * 40
        other = ledger.load(self.bk, "J")
        other.errors = 2
        other.save()
        self.settle(verdicts={art: v("not_met", artifact=art, open=3, receipt_id=5)})
        led = self.reload()
        self.assertEqual((led.errors, led.rounds_without_progress), (2, 1))


if __name__ == "__main__":
    unittest.main()
