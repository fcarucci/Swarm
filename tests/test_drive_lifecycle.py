"""The drive job's life cycle (spec 3.1 [F6][RR2][G4], 3.2, 3.10 [RR3], 4.3 [F5], 4.5): deadlines, ledger-first
terminalization, rollover, halt, question expiry and the host lock sweep (lib/swarm/supervisor/driver_life.py).

A real (memory) board and a real private ledger under a temporary HOME; the runner, the closing gate and
the land journal are the injected `Hooks`, so no session is ever started or stopped."""
from __future__ import annotations

import contextlib
import datetime as dt
import os
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

from support import SANDBOX, add_backend_variants, drive_backends, home_env  # noqa: F401  (puts lib/ on sys.path)
import drive_fixtures as fx

from swarm import compat, receipts  # noqa: E402
from swarm.supervisor import driver_life as life, driver_model as dm, ledger  # noqa: E402

MIN = dt.timedelta(minutes=1)
CAND = "c" * 40
REQ = "r" * 16


class Crash(Exception):
    """Stands for the SIGKILL of a crash barrier."""


class FakeRunner:
    """The runner side of `Hooks`: which launches still have a live session, what was stopped, in order."""

    def __init__(self):
        self.live: set[str] = set()
        self.calls: list = []
        self.land = life.LandResult()
        self.gate = life.Gate()

    def stop_rows(self, board, cfg, rows):
        self.calls.append(("stop", sorted(r.launch_id for r in rows)))

    def rows_live(self, board, cfg, rows):
        return [r for r in rows if r.launch_id in self.live]

    def complete_land(self, board, cfg, led, now):
        self.calls.append(("land",))
        return self.land

    def gate_(self, board, cfg, led, intent, now):
        self.calls.append(("gate",))
        return self.gate

    def after_close(self, board, cfg, led):
        self.calls.append(("after_close",))

    def hooks(self, **over):
        return life.Hooks(stop_rows=self.stop_rows, rows_live=self.rows_live, complete_land=self.complete_land,
                          gate=self.gate_, after_close=self.after_close, **over)


class LifeCase(unittest.TestCase):
    harness_factory = staticmethod(drive_backends()["memory"])    # variants below swap it per backend
    CFG = {"max_wall_hours": 24, "ruling_deadline_minutes": 120, "close_deadline_minutes": 60,
           "question_expiry": "4h"}

    def setUp(self):
        self.home = Path(tempfile.mkdtemp(dir=SANDBOX, prefix="life-home-"))
        env = mock.patch.dict(os.environ, home_env(self.home))
        env.start()
        self.addCleanup(env.stop)
        self.h = self.harness_factory(self.id().rsplit(".", 1)[0])
        self.addCleanup(self.h.close)
        self.h.reset()
        self.b = self.h.board()
        self.addCleanup(self.b.close)
        self.b.open_job("J", None, None, None, "me", goal="G")
        self.b.set_job_data("J", "drive.enabled", "1")
        self.t0 = self.b.now()
        self.led = ledger.create("bk", "J", budgets={}, activated_at=fx.iso(self.t0))
        self.run = FakeRunner()
        self.cfg = dict(self.CFG)

    # ---- helpers
    def reload(self):
        return ledger.load("bk", "J")

    def job(self):
        return self.b.job_status("J")

    def at(self, minutes):
        return self.t0 + minutes * MIN

    def judge_verdict(self, verdict="met", scope="goal", gen=1, candidate=CAND, req=REQ):
        """A judge's record on the board, as a (possibly forged) writer would leave it."""
        key = "judge-" + uuid.uuid4().hex[:6]
        name = self.b.allocate_name(key, "J", "judge")
        self.assertTrue(self.b.claim_judge(key, "J"))
        kw = dict(scope=scope, generation=gen, candidate=candidate, req=req)
        if verdict == "unreachable":
            kw["details"] = "tried everything"
        if verdict == "not_met":
            kw["open_count"] = 2
        self.assertTrue(self.b.record_verdict("J", name, verdict, "all good", **kw))
        self.b.close_agent(key, "done")

    def pending_met(self, now=None, **kw):
        self.judge_verdict(**kw)
        self.assertTrue(life.begin_terminal(self.b, self.led, "met", "goal", "judge", "goal met",
                                            now or self.t0, candidate=CAND, req=REQ))

    def get_launch(self, name="eng-a", gen=1):
        s = self.b.queue_launch("J", gen, name, role="engineer", harness="claude", model="haiku",
                                workdir=f"/w/{name}", brief=f"/w/{name}/B.md", queued_by="lead")
        return s

    def launch(self, name="eng-a", gen=1, usd=5.0, locks=()):
        s = self.get_launch(name, gen)
        lid = f"L-{name}-{gen}"
        action = f"gen{gen}|seat|{name}|-|1|1"
        r = self.b.claim_drive_launch(job="J", generation=gen, action_id=action, attempt=1, launch_id=lid,
                                      launch_row_id=s.id, queue_version=s.revision, harness="claude", reserve_usd=usd,
                                      reserve_minutes=60, locks=list(locks), lock_seconds=4200, max_host_running=4)
        self.assertIsNotNone(r)
        self.led.intents.append(ledger.Intent(lid, action, 1, name, usd, 60.0, list(locks), "claimed"))
        self.led.save()
        return lid

    def row(self, lid):
        return next(r for r in self.b.restarts("J") if r.launch_id == lid)

    def barrier_crash(self, name):
        def hit(n):
            if n == name:
                raise Crash(name)
        return mock.patch("swarm.supervisor.barrier.hit", side_effect=hit)

    def posts(self, needle):
        return [m for m in self.b.recent_messages(50, job="J") if needle in m.message]


class DeadlineTests(LifeCase):
    def test_deadline_records_mechanical_unreachable_without_snapshot(self):
        self.led.ruling_due_at = fx.iso(self.at(-121))
        self.led.save()
        try:
            import swarm.supervisor.driver_snapshot  # noqa: F401
            guard = mock.patch("swarm.supervisor.driver_snapshot.read", side_effect=RuntimeError)
        except ImportError:                       # Task 7b not integrated yet: nothing to forbid
            guard = contextlib.nullcontext()
        with guard:
            self.assertTrue(life.enforce_deadlines(self.b, self.cfg, self.led, self.t0))
        js = self.job()
        self.assertEqual((js.verdict, js.verdict_scope, js.verdict_generation), ("unreachable", "ruling", 1))
        self.assertIn("mechanical", js.verdict_reason)
        led = self.reload()
        self.assertEqual((led.terminal_intent["verdict"], led.terminal_intent["scope"],
                          led.terminal_intent["by"], led.terminal_intent["generation"]),
                         ("unreachable", "ruling", life.MECHANICAL, 1))
        self.assertEqual(led.closing, "unreachable: ruling deadline passed")
        self.assertIsNone(led.terminal)
        self.assertEqual(len(self.posts("DRIVER-MECHANICAL-UNREACHABLE")), 1)

    def test_ruling_deadline_boundary(self):
        self.led.ruling_due_at = fx.iso(self.at(-120))
        self.led.save()
        self.assertFalse(life.enforce_deadlines(self.b, self.cfg, self.led, self.at(-0.001)))
        self.assertIsNone(self.reload().terminal_intent)
        self.assertTrue(life.enforce_deadlines(self.b, self.cfg, self.led, self.t0))

    def test_ruling_deadline_comes_from_config_and_the_jobs_budget(self):
        self.led.ruling_due_at = fx.iso(self.at(-31))
        self.assertFalse(life.enforce_deadlines(self.b, dict(self.cfg), self.led, self.t0))
        self.assertTrue(life.enforce_deadlines(self.b, {**self.cfg, "ruling_deadline_minutes": 30}, self.led, self.t0))

    def test_budget_overrides_config(self):
        self.led.ruling_due_at = fx.iso(self.at(-31))
        self.led.budgets = {"ruling_deadline_minutes": 30}
        self.assertTrue(life.enforce_deadlines(self.b, self.cfg, self.led, self.t0))

    def test_wall_clock_deadline_includes_the_ruling_deadline(self):
        wall = 24 * 60
        self.assertFalse(life.enforce_deadlines(self.b, self.cfg, self.led, self.at(wall + 119)))
        self.assertTrue(life.enforce_deadlines(self.b, self.cfg, self.led, self.at(wall + 120)))
        self.assertIn("wall-clock", self.reload().closing)

    def test_no_deadline_no_record(self):
        self.assertFalse(life.enforce_deadlines(self.b, self.cfg, self.led, self.at(60)))
        self.assertIsNone(self.job().verdict)
        self.assertIsNone(self.reload().terminal_intent)

    def test_enforce_deadlines_ignores_board_verdict_column(self):
        self.judge_verdict("met", scope="goal")            # a forged (or any) board met does not stop the deadline
        self.led.ruling_due_at = fx.iso(self.at(-121))
        self.led.save()
        self.assertTrue(life.enforce_deadlines(self.b, self.cfg, self.led, self.t0))
        self.assertEqual(self.reload().terminal_intent["verdict"], "unreachable")
        self.assertEqual(self.job().verdict, "unreachable")

    def test_enforce_deadlines_skips_halted_jobs(self):
        self.led.ruling_due_at = fx.iso(self.at(-500))
        self.led.halted = {"by": "me", "at": fx.iso(self.at(-10))}
        self.led.save()
        self.assertFalse(life.enforce_deadlines(self.b, self.cfg, self.led, self.t0))
        self.assertIsNone(self.reload().terminal_intent)

    def test_enforce_deadlines_does_nothing_once_terminal_or_pending(self):
        self.led.ruling_due_at = fx.iso(self.at(-500))
        self.led.terminal = {"verdict": "met", "at": fx.iso(self.t0)}
        self.assertFalse(life.enforce_deadlines(self.b, self.cfg, self.led, self.t0))
        self.led.terminal = None
        self.led.terminal_intent = {"verdict": "met", "scope": "goal", "generation": 1, "candidate": None,
                                    "req": None, "by": "judge", "at": fx.iso(self.t0)}
        self.assertFalse(life.enforce_deadlines(self.b, self.cfg, self.led, self.t0))
        self.assertEqual(self.led.terminal_intent["verdict"], "met")      # the pending MET is not replaced

    def test_halted_time_shifts_the_ruling_deadline(self):
        self.led.ruling_due_at = fx.iso(self.at(-110))
        self.led.save()
        life.halt(self.b, self.cfg, self.led, "me", self.at(0), hooks=self.run.hooks())
        life.unhalt(self.b, self.cfg, self.led, "me", self.at(30))
        self.assertEqual(self.led.halted_total_seconds, 1800)
        self.assertFalse(life.enforce_deadlines(self.b, self.cfg, self.led, self.at(39)))   # 10 + 30 min after
        self.assertTrue(life.enforce_deadlines(self.b, self.cfg, self.led, self.at(40)))

    def test_halted_time_shifts_the_wall_clock_deadline(self):
        self.led.halted_total_seconds = 3600
        wall = 24 * 60 + 120 + 60
        self.assertFalse(life.enforce_deadlines(self.b, self.cfg, self.led, self.at(wall - 1)))
        self.assertTrue(life.enforce_deadlines(self.b, self.cfg, self.led, self.at(wall)))

    def test_mechanical_verdict_not_recorded_while_a_live_judge_holds_the_seat(self):
        key = "judge-live"
        self.b.allocate_name(key, "J", "judge")
        self.assertTrue(self.b.claim_judge(key, "J"))
        self.b.tool_started(key, "Bash")
        self.led.ruling_due_at = fx.iso(self.at(-121))
        self.assertTrue(life.enforce_deadlines(self.b, self.cfg, self.led, self.t0))
        self.assertIsNone(self.job().verdict)                     # the ledger decided; the board write waits
        self.assertEqual(self.reload().terminal_intent["verdict"], "unreachable")

    def test_terminal_intent_fsynced_before_board_verdict(self):
        self.led.ruling_due_at = fx.iso(self.at(-121))
        self.led.save()
        with self.barrier_crash("terminal_intent_saved"), self.assertRaises(Crash):
            life.enforce_deadlines(self.b, self.cfg, self.led, self.t0)
        self.assertIsNone(self.job().verdict)                                  # the board knows nothing yet
        led = self.reload()                                                    # the ledger does
        self.assertEqual(led.terminal_intent["verdict"], "unreachable")
        self.assertTrue(led.closing)
        self.assertTrue(life.terminalize(self.b, self.cfg, led, self.at(1), hooks=self.run.hooks()))
        self.assertEqual((self.job().status, self.job().verdict), ("failed", "unreachable"))
        self.assertEqual(self.reload().terminal["verdict"], "unreachable")

    def test_mechanical_verdict_recognised_from_ledger_not_receipts(self):
        self.led.ruling_due_at = fx.iso(self.at(-121))
        self.led.save()
        life.enforce_deadlines(self.b, self.cfg, self.led, self.t0)
        # no receipt exists anywhere and the verdict has no judge: the ledger alone closes the job
        self.assertTrue(life.terminalize(self.b, self.cfg, self.led, self.at(1), hooks=self.run.hooks()))
        js = self.job()
        self.assertEqual((js.status, js.closed_by), ("failed", life.CLOSED_BY))
        self.assertTrue(js.outcome.startswith("unreachable: "))
        self.assertEqual(self.reload().terminal["scope"], "ruling")


class TerminalizeTests(LifeCase):
    def test_terminalize_settles_questions_and_closes(self):
        self.pending_met()
        qb = self.b.open_blocker("J", "question", "human", "which way?", until=self.t0 + 240 * MIN,
                                 default_value="left", created_by="a", seat="a", generation=1)
        self.assertTrue(life.terminalize(self.b, self.cfg, self.led, self.at(1), hooks=self.run.hooks()))
        js = self.job()
        self.assertEqual((js.status, js.closed_by), ("completed", life.CLOSED_BY))
        self.assertTrue(js.outcome.startswith("met: "))
        self.assertEqual(self.b.blocker(qb.id).state, "expired")
        self.assertIn("job concluded: met", self.b.blocker_events(qb.id)[-1].detail or "")
        led = self.reload()
        self.assertEqual(led.terminal["verdict"], "met")
        self.assertEqual(led.terminal["scope"], "goal")
        self.assertIn(("after_close",), self.run.calls)

    def test_terminalization_clears_the_board_drive_claim(self):
        self.pending_met()
        self.assertEqual(self.b.job_data("J")["drive.enabled"], "1")
        self.assertTrue(life.terminalize(self.b, self.cfg, self.led, self.at(1), hooks=self.run.hooks()))
        self.assertNotIn("drive.enabled", self.b.job_data("J"))

    def test_claim_is_kept_while_the_close_is_pending(self):
        self.pending_met()
        self.run.gate = life.Gate("unknown")
        life.terminalize(self.b, self.cfg, self.led, self.at(1), hooks=self.run.hooks())
        self.assertEqual(self.b.job_data("J")["drive.enabled"], "1")

    def test_terminalize_without_intent_or_when_halted_does_nothing(self):
        self.assertFalse(life.terminalize(self.b, self.cfg, self.led, self.t0, hooks=self.run.hooks()))
        self.pending_met()
        self.led.halted = {"by": "me", "at": fx.iso(self.t0)}
        self.assertFalse(life.terminalize(self.b, self.cfg, self.led, self.t0, hooks=self.run.hooks()))
        self.assertEqual(self.job().status, "active")

    def test_terminalize_waits_for_scopes_to_be_absent(self):
        lid = self.launch()
        self.pending_met()
        self.run.live = {lid}
        self.assertFalse(life.terminalize(self.b, self.cfg, self.led, self.at(1), hooks=self.run.hooks()))
        self.assertIn(("stop", [lid]), self.run.calls)
        self.assertEqual(self.job().status, "active")
        self.assertIsNone(self.reload().terminal)
        self.assertIsNone(self.row(lid).ended_at)                 # nothing is finished while a scope is present
        self.run.live = set()
        self.assertTrue(life.terminalize(self.b, self.cfg, self.led, self.at(2), hooks=self.run.hooks()))
        self.assertEqual(self.job().status, "completed")

    def test_terminalize_ends_sessions_charges_reservation_releases_locks_and_seats(self):
        lid = self.launch(locks=("land:x:main",))
        lid2 = self.launch("eng-b")
        self.b.finish_restart(self.row(lid2).id, "completed", cost_usd=1.25)
        self.pending_met()
        self.assertTrue(life.terminalize(self.b, self.cfg, self.led, self.at(1), hooks=self.run.hooks()))
        led = self.reload()
        first = next(i for i in led.intents if i.launch_id == lid)
        self.assertEqual((first.state, first.outcome, first.success, first.charged_usd), ("concluded", "cancelled", False, 5.0))
        self.assertEqual(self.row(lid).outcome, "cancelled")
        self.assertEqual(self.b.drive_locks(), [])
        self.assertEqual({s.state for s in self.b.launches("J")}, {"done"})

    def guard_refuses(self, **board_verdict):
        """The pending MET names (goal, CAND, REQ, generation 1); the board holds a different verdict."""
        rejected = self.led.watermarks.get("close_rejected", 0)
        self.judge_verdict(**board_verdict)
        self.led.terminal_intent = {"verdict": "met", "scope": "goal", "generation": 1, "candidate": CAND,
                                    "req": REQ, "by": "judge", "at": fx.iso(self.t0)}
        self.led.closing = "met: x"
        self.led.save()
        self.assertFalse(life.terminalize(self.b, self.cfg, self.led, self.at(1), hooks=self.run.hooks()))
        self.assertEqual(self.job().status, "active")
        self.assertIsNone(self.led.terminal_intent)
        self.assertIsNone(self.led.terminal)
        self.assertEqual(self.led.watermarks["close_rejected"], rejected + 1)

    def test_driver_close_guard_checks_generation_scope_candidate_req_and_verdict(self):
        for field, board_verdict in (("generation", dict(gen=2)), ("scope", dict(scope="artifact")),
                                     ("candidate", dict(candidate="d" * 40)), ("req", dict(req="x" * 16)),
                                     ("verdict", dict(verdict="not_met", scope="goal"))):
            with self.subTest(field):
                self.guard_refuses(**board_verdict)

    def test_driver_close_guard_refuses_when_the_board_has_no_verdict(self):
        self.led.terminal_intent = {"verdict": "met", "scope": "goal", "generation": 1, "candidate": CAND,
                                    "req": REQ, "by": "judge", "at": fx.iso(self.t0)}
        self.assertFalse(life.terminalize(self.b, self.cfg, self.led, self.at(1), hooks=self.run.hooks()))
        self.assertIsNone(self.led.terminal_intent)

    def test_rejected_met_clears_terminal_intent_and_rejudges(self):
        self.pending_met()
        self.run.gate = life.Gate("reject", "the target moved")
        self.assertFalse(life.terminalize(self.b, self.cfg, self.led, self.at(1), hooks=self.run.hooks()))
        led = self.reload()
        self.assertIsNone(led.terminal_intent)
        self.assertIsNone(led.closing)
        self.assertIsNone(led.terminal)
        self.assertEqual(led.watermarks["close_rejected"], 1)
        self.assertEqual(led.watermarks[f"rejected:{CAND}"], 1)           # the goal judge reruns on a new candidate
        self.assertIsNone(led.ruling_due_at)                             # a plain rejection is not a ruling
        self.assertEqual(self.job().status, "active")
        self.assertEqual(len(self.posts("DRIVER-CLOSE-REJECTED the target moved")), 1)
        # the deadlines keep running: a ruling can still be due and enforce_deadlines still fires
        led.ruling_due_at = fx.iso(self.at(-121))
        self.assertTrue(life.enforce_deadlines(self.b, self.cfg, led, self.at(1)))

    def test_unevaluable_pending_met_rejected_after_close_deadline(self):
        self.pending_met()
        self.run.gate = life.Gate("unknown")
        self.assertFalse(life.terminalize(self.b, self.cfg, self.led, self.at(59), hooks=self.run.hooks()))
        self.assertIsNotNone(self.reload().terminal_intent)               # still pending before the deadline
        self.assertFalse(life.terminalize(self.b, self.cfg, self.led, self.at(60), hooks=self.run.hooks()))
        led = self.reload()
        self.assertIsNone(led.terminal_intent)                           # rejected: gate not evaluable
        self.assertEqual(led.ruling_due_at, fx.iso(self.at(60)))         # a ruling is due now
        self.assertEqual(len(self.posts("gate not evaluable")), 1)
        self.assertEqual(self.job().status, "active")
        # later enforce_deadlines applies normally
        self.assertFalse(life.enforce_deadlines(self.b, self.cfg, led, self.at(60 + 119)))
        self.assertTrue(life.enforce_deadlines(self.b, self.cfg, led, self.at(60 + 120)))
        self.assertEqual(self.reload().terminal_intent["by"], life.MECHANICAL)

    def test_close_deadline_comes_from_config(self):
        self.pending_met()
        self.run.gate = life.Gate("unknown")
        cfg = {**self.cfg, "close_deadline_minutes": 5}
        self.assertFalse(life.terminalize(self.b, cfg, self.led, self.at(4), hooks=self.run.hooks()))
        self.assertIsNotNone(self.reload().terminal_intent)
        life.terminalize(self.b, cfg, self.led, self.at(5), hooks=self.run.hooks())
        self.assertIsNone(self.reload().terminal_intent)

    def test_mechanical_unreachable_has_no_gate(self):
        self.led.ruling_due_at = fx.iso(self.at(-121))
        life.enforce_deadlines(self.b, self.cfg, self.led, self.t0)
        self.run.gate = life.Gate("reject", "would refuse a met")
        self.assertTrue(life.terminalize(self.b, self.cfg, self.led, self.at(1), hooks=self.run.hooks()))
        self.assertNotIn(("gate",), self.run.calls)

    def test_terminalization_completes_land_journal_first(self):
        lid = self.launch()
        self.pending_met()
        self.run.land = life.LandResult("pending", "push in flight")
        self.assertFalse(life.terminalize(self.b, self.cfg, self.led, self.at(1), hooks=self.run.hooks()))
        self.assertEqual(self.run.calls, [("land",)])                    # nothing was stopped before the journal
        self.assertEqual(self.job().status, "active")
        self.run.land = life.LandResult("done")
        self.assertTrue(life.terminalize(self.b, self.cfg, self.led, self.at(2), hooks=self.run.hooks()))
        names = [c[0] for c in self.run.calls]
        self.assertLess(names.index("land"), names.index("stop"))
        self.assertNotIn("reasons", self.reload().terminal)

    def test_partial_land_recorded_on_timeout(self):
        self.pending_met()
        self.run.land = life.LandResult("pending", "pushed 1 of 2 branches")
        self.assertFalse(life.terminalize(self.b, self.cfg, self.led, self.at(59), hooks=self.run.hooks()))
        self.assertTrue(life.terminalize(self.b, self.cfg, self.led, self.at(60), hooks=self.run.hooks()))
        self.assertEqual(self.job().status, "completed")
        reasons = self.reload().terminal["reasons"]
        self.assertEqual(len(reasons), 1)
        self.assertIn("pushed 1 of 2 branches", reasons[0])
        self.assertEqual(len(self.posts("DRIVER-LAND-PARTIAL")), 1)

    def test_partial_land_reported_by_the_journal_is_recorded(self):
        self.pending_met()
        self.run.land = life.LandResult("partial", "branch b not pushed")
        self.assertTrue(life.terminalize(self.b, self.cfg, self.led, self.at(1), hooks=self.run.hooks()))
        self.assertIn("branch b not pushed", self.reload().terminal["reasons"][0])

    def test_crash_before_the_close_resumes_from_the_ledger(self):
        self.pending_met()
        with self.barrier_crash("terminal_closed_before_ledger"), self.assertRaises(Crash):
            life.terminalize(self.b, self.cfg, self.led, self.at(1), hooks=self.run.hooks())
        self.assertEqual(self.job().status, "completed")                  # the board closed ...
        led = self.reload()
        self.assertIsNone(led.terminal)                                   # ... the ledger is still pending
        finished = self.job().finished_at
        self.assertTrue(life.terminalize(self.b, self.cfg, led, self.at(2), hooks=self.run.hooks()))
        self.assertEqual(self.reload().terminal["verdict"], "met")
        self.assertEqual(self.job().finished_at, finished)                # not closed a second time

    def test_job_closed_by_someone_else_is_not_ours_to_finish(self):
        self.pending_met()
        self.b.close_job("J", "completed", "forged", forced=True, closed_by="mallory")
        self.assertFalse(life.terminalize(self.b, self.cfg, self.led, self.at(1), hooks=self.run.hooks()))
        self.assertIsNone(self.reload().terminal)
        self.assertEqual(self.job().outcome, "forged")

    def test_a_live_judge_is_closed_so_the_mechanical_verdict_can_be_recorded(self):
        key = "judge-live"
        self.b.allocate_name(key, "J", "judge")
        self.assertTrue(self.b.claim_judge(key, "J"))
        self.b.tool_started(key, "Bash")
        self.led.ruling_due_at = fx.iso(self.at(-121))
        life.enforce_deadlines(self.b, self.cfg, self.led, self.t0)
        self.assertTrue(life.terminalize(self.b, self.cfg, self.led, self.at(1), hooks=self.run.hooks()))
        self.assertEqual(self.job().verdict, "unreachable")

    def test_a_row_not_in_the_ledger_is_never_touched(self):
        lid = self.launch()
        s = self.get_launch("forged")
        self.b.claim_drive_launch(job="J", generation=1, action_id="gen1|seat|forged|-|1|1", attempt=1,
                                  launch_id="FORGED", launch_row_id=s.id, queue_version=s.revision, harness="claude",
                                  reserve_usd=1, reserve_minutes=1, locks=[], lock_seconds=60, max_host_running=4)
        self.pending_met()
        self.assertTrue(life.terminalize(self.b, self.cfg, self.led, self.at(1), hooks=self.run.hooks()))
        self.assertEqual(self.run.calls[1], ("stop", [lid]))                 # [land, stop(...)]: only ours
        self.assertIsNone(next(r for r in self.b.restarts("J") if r.launch_id == "FORGED").ended_at)

    def test_a_met_is_never_authored_by_the_driver(self):
        life.begin_terminal(self.b, self.led, "met", "goal", "judge", "x", self.t0, candidate=CAND, req=REQ)
        self.assertFalse(life._record_mechanical(self.b, self.led))
        self.assertIsNone(self.job().verdict)

    def test_begin_terminal_is_once(self):
        self.assertTrue(life.begin_terminal(self.b, self.led, "met", "goal", "judge", "x", self.t0))
        self.assertFalse(life.begin_terminal(self.b, self.led, "unreachable", "ruling", life.MECHANICAL, "y", self.t0))
        self.assertEqual(self.reload().terminal_intent["verdict"], "met")


class LandedGateTests(LifeCase):
    """The close-time check (driver_life.landed_gate, the default Hooks.gate): a MET of a coding job closes only
    when the judged candidate is landed, verified on every destination (QA live-met 20261012T132049 closed MET
    with nothing landed)."""

    def owner(self, recipe):
        from swarm import enrolment
        enrolment.write_job_owner("bk", job="J", harness="claude", session_id=None, cwd=str(self.home),
                                  roots=[str(self.home)], recipe=recipe, goal="G", review_role="reviewer",
                                  review_gates="standard")

    def land(self, sha=CAND, state="verified"):
        dest = {"https://example.test/r.git": {"state": state, "sha": sha},
                "https://example.test/backup.git": {"state": state, "sha": sha}}
        self.led.integrated = {"feat/a": {"landed": sha, "land_id": 7, "land_rev": 1}}
        self.led.save()
        receipts.write("bk", receipts.Receipt(
            board_key="bk", job="J", generation=1, incarnation=self.led.incarnation, action_id="t|land", attempt=0,
            seat=None, kind="land", record_id="7", rev=1,
            content_sha=receipts.content_sha("land", {"destinations": dest}), args_sha="0" * 64,
            expires_at=time.time() + 3600, destinations=dest))

    def test_unlanded_coding_met_is_refused_at_close(self):
        self.owner("coding")
        self.pending_met()
        self.assertFalse(life.terminalize(self.b, self.cfg, self.led, self.at(1)))
        led = self.reload()
        self.assertIsNone(led.terminal)
        self.assertIsNone(led.terminal_intent)
        self.assertEqual(self.job().status, "active")
        self.assertEqual(len(self.posts("not landed")), 1)

    def test_a_land_not_verified_on_every_destination_is_refused(self):
        self.owner("coding")
        self.land(state="pushed")
        self.pending_met()
        self.assertEqual(life.landed_gate(self.b, self.cfg, self.led, self.led.terminal_intent, self.at(1)).verdict,
                         "reject")

    def test_landed_candidate_passes_the_gate(self):
        self.owner("coding")
        self.land()
        self.pending_met()
        self.assertEqual(life.landed_gate(self.b, self.cfg, self.led, self.led.terminal_intent, self.at(1)).verdict,
                         "pass")

    def test_no_owner_record_fails_closed(self):
        """Review of 7d1e8ba: with no readable owner record the gate is "unknown", never a pass."""
        self.pending_met()
        self.assertEqual(life.landed_gate(self.b, self.cfg, self.led, self.led.terminal_intent, self.at(1)).verdict,
                         "unknown")

    def test_artifact_job_met_is_not_checked(self):
        self.owner("artifact")
        self.pending_met()
        self.assertEqual(life.landed_gate(self.b, self.cfg, self.led, self.led.terminal_intent, self.at(1)).verdict,
                         "pass")


class RolloverTests(LifeCase):
    def request(self, n=2):
        self.led.generation_requested = n
        self.led.save()

    def test_rollover_stops_old_generation_first(self):
        lid = self.launch("lead")
        self.request()
        self.run.live = {lid}
        self.assertFalse(life.rollover(self.b, self.cfg, self.led, self.at(1), hooks=self.run.hooks()))
        self.assertEqual(self.run.calls, [("stop", [lid])])
        self.assertEqual(self.reload().generation, 1)
        self.assertNotIn("drive.generation", self.b.job_data("J"))
        self.run.live = set()
        self.assertTrue(life.rollover(self.b, self.cfg, self.led, self.at(2), hooks=self.run.hooks()))
        led = self.reload()
        self.assertEqual(led.generation, 2)
        self.assertEqual(self.b.job_data("J")["drive.generation"], "2")
        self.assertEqual(self.b.get_launch("J", 2, "lead").state, "queued")
        self.assertEqual(self.b.get_launch("J", 1, "lead").state, "done")
        self.assertEqual(led.intents[0].outcome, "cancelled")
        self.assertEqual(led.intents[0].charged_usd, 5.0)

    def test_rollover_releases_locks_and_expires_blockers_of_the_old_generation_only(self):
        self.launch("lead", locks=("land:x:main",))
        old = self.b.open_blocker("J", "question", "human", "q1", seat="lead", generation=1, default_value="d")
        self.request()
        self.assertTrue(life.rollover(self.b, self.cfg, self.led, self.at(1), hooks=self.run.hooks()))
        self.assertEqual(self.b.drive_locks(), [])
        self.assertEqual(self.b.blocker(old.id).state, "expired")

    def test_rollover_writes_ledger_before_board(self):
        self.launch("lead")
        self.request()
        self.led.rounds_without_progress, self.led.ruling_due_at = 2, fx.iso(self.at(-5))
        self.led.save()
        with self.barrier_crash("mid_rollover_after_ledger"), self.assertRaises(Crash):
            life.rollover(self.b, self.cfg, self.led, self.at(1), hooks=self.run.hooks())
        led = self.reload()
        self.assertEqual(led.generation, 2)                                # the ledger moved first ...
        self.assertEqual((led.rounds_without_progress, led.ruling_due_at), (0, None))
        self.assertNotEqual(self.b.job_data("J").get("drive.generation"), "2")      # ... the board did not
        self.assertIsNone(self.b.get_launch("J", 2, "lead"))
        self.assertTrue(life.rollover(self.b, self.cfg, led, self.at(2), hooks=self.run.hooks()))
        self.assertEqual(self.b.job_data("J")["drive.generation"], "2")
        self.assertEqual(self.b.get_launch("J", 2, "lead").state, "queued")
        self.assertFalse(life.rollover(self.b, self.cfg, led, self.at(3), hooks=self.run.hooks()))   # then idle

    def test_rollover_does_nothing_unless_requested(self):
        self.launch("lead")
        self.assertFalse(life.rollover(self.b, self.cfg, self.led, self.at(1), hooks=self.run.hooks()))
        self.assertEqual(self.run.calls, [])
        self.assertEqual(self.reload().generation, 1)

    def test_rollover_waits_while_halted_or_closing(self):
        self.request()
        self.led.halted = {"by": "me", "at": fx.iso(self.t0)}
        self.assertFalse(life.rollover(self.b, self.cfg, self.led, self.at(1), hooks=self.run.hooks()))
        self.led.halted = None
        self.led.terminal_intent = {"verdict": "met", "scope": "goal", "generation": 1, "candidate": None,
                                    "req": None, "by": "judge", "at": fx.iso(self.t0)}
        self.assertFalse(life.rollover(self.b, self.cfg, self.led, self.at(1), hooks=self.run.hooks()))
        self.assertEqual(self.led.generation, 1)

    def test_rollover_touches_only_the_old_generation(self):
        old, new = self.launch("lead", gen=1), self.launch("lead2", gen=2)
        self.request(3)
        self.run.live = set()
        life.rollover(self.b, self.cfg, self.led, self.at(1), hooks=self.run.hooks())
        self.assertEqual(self.run.calls, [("stop", [old])])
        self.assertIsNotNone(self.row(old).ended_at)
        self.assertIsNone(self.row(new).ended_at)
        by = {i.launch_id: i.state for i in self.reload().intents}
        self.assertEqual(by, {old: "concluded", new: "claimed"})
        self.assertEqual(self.b.get_launch("J", 2, "lead2").state, "running")

    def test_rollover_keeps_charges_and_budgets(self):
        self.launch("lead")
        self.led.budgets = {"max_cost_usd": 12}
        self.request()
        life.rollover(self.b, self.cfg, self.led, self.at(1), hooks=self.run.hooks())
        led = self.reload()
        self.assertEqual((led.charged_usd(), led.budgets), (5.0, {"max_cost_usd": 12}))


class HaltTests(LifeCase):
    def test_halt_idles_seats_and_shifts_deadlines(self):
        lid = self.launch()
        life.halt(self.b, self.cfg, self.led, "francesco", self.at(60), reason="stop", hooks=self.run.hooks())
        self.assertIn(("stop", [lid]), self.run.calls)
        seat = self.b.get_launch("J", 1, "eng-a")
        self.assertEqual((seat.state, seat.last_outcome), ("failed", "halted"))
        led = self.reload()
        self.assertEqual(led.halted, {"by": "francesco", "at": fx.iso(self.at(60)), "reason": "stop"})
        self.assertEqual(self.b.job_data("J")["drive.halted"], "francesco")
        intent = led.intents[0]
        self.assertEqual((intent.state, intent.outcome, intent.success), ("concluded", "halted", False))
        self.assertEqual(self.row(lid).outcome, "cancelled")
        self.assertTrue(life.unhalt(self.b, self.cfg, led, "francesco", self.at(70)))
        led = self.reload()
        self.assertEqual((led.halted, led.halted_total_seconds), (None, 600.0))
        self.assertNotIn("drive.halted", self.b.job_data("J"))
        self.assertEqual(self.b.get_launch("J", 1, "eng-a").state, "failed")      # the next pass wakes it

    def test_halt_waits_for_the_session_to_be_gone(self):
        lid = self.launch()
        self.run.live = {lid}
        life.halt(self.b, self.cfg, self.led, "me", self.at(0), hooks=self.run.hooks())
        self.assertEqual(self.b.get_launch("J", 1, "eng-a").state, "running")
        self.assertIsNone(self.row(lid).ended_at)
        self.assertEqual(self.reload().halted["by"], "me")                # halted already (ledger first)
        self.run.live = set()
        self.assertTrue(life.settle_halt(self.b, self.cfg, self.led, self.at(1), hooks=self.run.hooks()))
        self.assertEqual(self.b.get_launch("J", 1, "eng-a").state, "failed")

    def test_halt_cancelled_attempts_not_counted(self):
        self.launch()
        aid = self.led.intents[0].action_id
        life.halt(self.b, self.cfg, self.led, "me", self.at(0), hooks=self.run.hooks())
        ctx = dm._Ctx(fx.snap(ledger=self.reload()), {})
        self.assertEqual(ctx.attempts(aid), 0)                            # not an attempt
        self.assertEqual(ctx.sessions(), 0)                               # not a session

    def test_a_cancel_concluded_by_reconcile_after_the_halt_is_relabelled_halted(self):
        lid = self.launch()
        life.halt(self.b, self.cfg, self.led, "me", self.at(0), hooks=self.run.hooks())
        led = self.reload()
        led.intents[0].outcome = "cancelled"                              # as a reconcile would have concluded it
        led.save()
        life.settle_halt(self.b, self.cfg, led, self.at(1), hooks=self.run.hooks())
        self.assertEqual(self.reload().intents[0].outcome, "halted")

    def test_an_earlier_cancel_is_not_relabelled(self):
        lid = self.launch()
        self.b.finish_restart(self.row(lid).id, "cancelled")
        self.led.intents[0].state, self.led.intents[0].outcome = "concluded", "cancelled"
        self.led.intents[0].success, self.led.intents[0].charged_usd = False, 5.0
        self.led.save()
        life.halt(self.b, self.cfg, self.led, "me", self.at(60), hooks=self.run.hooks())
        self.assertEqual(self.reload().intents[0].outcome, "cancelled")

    def test_halt_with_unknown_usage_charges_reservation(self):
        a, c = self.launch("eng-a", usd=5.0), self.launch("eng-b", usd=4.0)
        self.b.finish_restart(self.row(c).id, "cancelled", cost_usd=1.25)
        life.halt(self.b, self.cfg, self.led, "me", self.at(0), hooks=self.run.hooks())
        by = {i.launch_id: i.charged_usd for i in self.reload().intents}
        self.assertEqual(by, {a: 5.0, c: 1.25})                           # [RR4]: usage when known, else the ceiling

    def test_second_halt_keeps_the_first(self):
        self.assertTrue(life.halt(self.b, self.cfg, self.led, "first", self.at(0), hooks=self.run.hooks()))
        self.assertFalse(life.halt(self.b, self.cfg, self.led, "second", self.at(5), hooks=self.run.hooks()))
        self.assertEqual(self.reload().halted["by"], "first")

    def test_unhalt_when_not_halted_is_a_noop(self):
        self.assertFalse(life.unhalt(self.b, self.cfg, self.led, "me", self.at(5)))
        self.assertEqual(self.reload().halted_total_seconds, 0.0)

    def test_halted_time_accumulates(self):
        for start, end in ((0, 10), (20, 25)):
            life.halt(self.b, self.cfg, self.led, "me", self.at(start), hooks=self.run.hooks())
            life.unhalt(self.b, self.cfg, self.led, "me", self.at(end))
        self.assertEqual(self.reload().halted_total_seconds, 900.0)


class QuestionTests(LifeCase):
    def ask(self, seat="a", until=240, default="left", receipt=1, created=None):
        b = self.b.open_blocker("J", "question", "human", "which way?", until=self.t0 + until * MIN,
                                default_value=default, created_by=seat, seat=seat, generation=1)
        return b, fx.q(b.id, seat=seat, default=default, until=self.t0 + until * MIN, receipt_id=receipt)

    def test_default_applied_once_after_the_deadline(self):
        b, q = self.ask(until=30)
        self.assertEqual(life.apply_question_expiry(self.b, self.cfg, self.led, self.at(29), [q]), [])
        self.assertEqual(self.b.blocker(b.id).state, "open")
        self.assertEqual(life.apply_question_expiry(self.b, self.cfg, self.led, self.at(30), [q]), [b.id])
        got = self.b.blocker(b.id)
        self.assertEqual((got.state, got.resolved_how), ("expired", "left"))
        self.assertEqual(len(self.posts(f"Blocker {b.id} expired: left")), 1)
        self.assertEqual(life.apply_question_expiry(self.b, self.cfg, self.led, self.at(31), [q]), [])
        self.assertEqual(len(self.posts(f"Blocker {b.id} expired")), 1)

    def test_expiry_after_downtime_applies_once(self):
        b, q = self.ask(until=30)
        # the supervisor was down for two days; two passes see the same question
        for minutes in (2 * 24 * 60, 2 * 24 * 60 + 2):
            life.apply_question_expiry(self.b, self.cfg, self.led, self.at(minutes), [q])
        events = [e.event for e in self.b.blocker_events(b.id)]
        self.assertEqual(events.count("expired"), 1)
        self.assertEqual(len(self.posts("expired: left")), 1)
        self.assertEqual(self.reload().watermarks[f"qdone:{b.id}"], 1)
        # a fresh process that reloads the ledger does not apply it again either
        self.assertEqual(life.apply_question_expiry(self.b, self.cfg, self.reload(), self.at(3 * 24 * 60), [q]), [])

    def test_halted_job_question_default_not_applied(self):
        b, q = self.ask(until=30)
        self.led.halted = {"by": "me", "at": fx.iso(self.at(10))}
        self.assertEqual(life.apply_question_expiry(self.b, self.cfg, self.led, self.at(500), [q]), [])
        self.assertEqual(self.b.blocker(b.id).state, "open")

    def test_applied_question_is_not_applied_again_even_if_reopened(self):
        b, q = self.ask(until=30)
        life.apply_question_expiry(self.b, self.cfg, self.led, self.at(31), [q])
        self.assertTrue(self.b.reopen_blocker(b.id, "again", actor="me"))
        self.assertEqual(life.apply_question_expiry(self.b, self.cfg, self.led, self.at(40), [q]), [])
        self.assertEqual(self.b.blocker(b.id).state, "open")

    def test_halted_time_shifts_the_question_deadline(self):
        b, q = self.ask(until=30)
        self.led.halted_total_seconds = 3600
        self.assertEqual(life.apply_question_expiry(self.b, self.cfg, self.led, self.at(89), [q]), [])
        self.assertEqual(life.apply_question_expiry(self.b, self.cfg, self.led, self.at(90), [q]), [b.id])

    def test_unreceipted_question_is_ignored(self):
        b, q = self.ask(until=30, receipt=None)
        self.assertEqual(life.apply_question_expiry(self.b, self.cfg, self.led, self.at(500), [q]), [])
        self.assertEqual(self.b.blocker(b.id).state, "open")

    def test_far_future_until_is_capped_at_question_expiry(self):
        b, q = self.ask(until=24 * 60)
        opened = self.t0
        q = fx.q(b.id, default="left", until=self.t0 + 24 * 60 * MIN, receipt_id=1, opened_at=opened)
        self.assertEqual(life.apply_question_expiry(self.b, self.cfg, self.led, opened + 239 * MIN, [q]), [])
        self.assertEqual(life.apply_question_expiry(self.b, self.cfg, self.led, opened + 240 * MIN, [q]), [b.id])

    def test_answered_question_is_left_alone(self):
        b, q = self.ask(until=30)
        self.assertTrue(self.b.resolve_blocker(b.id, "right", actor="human"))
        self.assertEqual(life.apply_question_expiry(self.b, self.cfg, self.led, self.at(500), [q]), [])
        self.assertEqual(self.b.blocker(b.id).resolved_how, "right")
        self.assertNotIn(f"qdone:{b.id}", self.led.watermarks)

    def test_question_whose_blocker_is_gone_is_skipped(self):
        q = fx.q(999, seat="a", default="left", until=self.t0 + 30 * MIN, receipt_id=1)
        self.assertEqual(life.apply_question_expiry(self.b, self.cfg, self.led, self.at(500), [q]), [])

    def test_question_without_default_is_not_applied(self):
        b, q = self.ask(until=30, default=None)
        self.assertEqual(life.apply_question_expiry(self.b, self.cfg, self.led, self.at(500), [q]), [])

    def test_closing_job_applies_nothing(self):
        b, q = self.ask(until=30)
        self.pending = life.begin_terminal(self.b, self.led, "met", "goal", "judge", "x", self.t0)
        self.assertEqual(life.apply_question_expiry(self.b, self.cfg, self.led, self.at(500), [q]), [])

    def test_other_seats_progress_while_question_open(self):
        b, q = self.ask(seat="a", until=30)
        s = fx.snap(launches={"a": fx.seat("a", "done"), "b": fx.seat("b", "queued")}, questions={"a": [q]},
                    wakes={"a": [5]})
        launches = lambda: [x.seat for x in dm.next_actions(s, dm.DEFAULT_CFG) if x.kind == "launch"]
        self.assertEqual(launches(), ["b"])                               # a waits for its answer, b runs
        other, qo = self.ask(seat="b", until=30)
        for minute in (10, 29):                                           # before the deadline: nothing, for either seat
            self.assertEqual(life.apply_question_expiry(self.b, self.cfg, self.led, self.at(minute), [q, qo]), [])
            self.assertEqual(self.b.blocker(b.id).state, "open")          # and nothing was applied behind its back
        self.assertEqual(life.apply_question_expiry(self.b, self.cfg, self.led, self.at(31), [q]), [b.id])
        self.assertEqual(self.b.blocker(b.id).state, "expired")           # at expiry only that seat's question settles
        self.assertEqual(self.b.blocker(other.id).state, "open")
        s2 = fx.snap(launches={"a": fx.seat("a", "done"), "b": fx.seat("b", "queued")}, questions={},
                     wakes={"a": [5]})
        self.assertEqual(sorted(x.seat for x in dm.next_actions(s2, dm.DEFAULT_CFG) if x.kind == "launch"), ["a", "b"])


class OrphanQuestionTests(LifeCase):
    """A board job claiming drive mode with no ledger: the driver applies its overdue defaults itself."""

    def ask(self, minutes=30, default="left", kind="question"):
        return self.b.open_blocker("J", kind, "human", "which way?", until=self.t0 + minutes * MIN,
                                   default_value=default, created_by="a", seat="a", generation=1)

    def test_overdue_default_applied_once(self):
        b = self.ask()
        self.assertEqual(life.expire_orphan_questions(self.b, "J", self.t0 + 29 * MIN), [])
        self.assertEqual(life.expire_orphan_questions(self.b, "J", self.t0 + 30 * MIN), [b.id])
        self.assertEqual(self.b.blocker(b.id).resolved_how, "left")
        self.assertEqual(life.expire_orphan_questions(self.b, "J", self.t0 + 31 * MIN), [])
        self.assertEqual(len(self.posts(f"Blocker {b.id} expired: left")), 1)

    def test_halted_board_claim_applies_nothing(self):
        b = self.ask()
        self.b.set_job_data("J", "drive.halted", "me")
        self.assertEqual(life.expire_orphan_questions(self.b, "J", self.at(500)), [])
        self.assertEqual(self.b.blocker(b.id).state, "open")

    def test_job_without_the_claim_is_left_to_the_generic_sweep(self):
        b = self.ask()
        self.b.set_job_data("J", "drive.enabled", None)
        self.assertEqual(life.expire_orphan_questions(self.b, "J", self.at(500)), [])
        self.assertEqual(self.b.blocker(b.id).state, "open")

    def test_closed_job_is_left_alone_and_a_paused_one_is_not(self):
        b, c = self.ask(minutes=30), self.ask(minutes=60)
        self.b.pause_job("J", "me", "hold")
        self.assertEqual(life.expire_orphan_questions(self.b, "J", self.at(45)), [b.id])
        self.b.close_job("J", "cancelled", "x", forced=True)
        self.assertEqual(life.expire_orphan_questions(self.b, "J", self.at(500)), [])
        self.assertEqual(self.b.blocker(c.id).state, "open")

    def test_waits_and_defaultless_questions_are_not_touched(self):
        w, d = self.ask(kind="wait"), self.ask(default=None)
        self.assertEqual(life.expire_orphan_questions(self.b, "J", self.at(500)), [])
        self.assertEqual({self.b.blocker(w.id).state, self.b.blocker(d.id).state}, {"open"})




# one subclass per backend of support.drive_backends() beyond memory (Postgres only with SWARM_TEST_CONFIG)
add_backend_variants(globals(), DeadlineTests, TerminalizeTests, RolloverTests, HaltTests, QuestionTests,
                     OrphanQuestionTests, LandedGateTests)


if __name__ == "__main__":
    unittest.main()
