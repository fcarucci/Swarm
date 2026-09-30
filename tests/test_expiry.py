"""Jobs don't stay open forever: [job] max_hours caps a job's life (failed), [job] orphan_minutes
closes one nobody works on (cancelled), and `swarm wait --for` bounds a wait
(Board.sweep_expiry, driven by swarm.sweep_jobs).

ExpiryContract runs on every backend (memory, sqlite, file; Postgres with SWARM_TEST_CONFIG).
ExpiryCliTests drive the CLI and the hooks end to end on SWARM_TEST_BACKEND.
"""
from __future__ import annotations

import os
import unittest

from support import FileHarness, MemoryHarness, PostgresHarness, SqliteHarness  # noqa: F401  (sets sys.path)
from test_hooks_cli import Env  # noqa: E402

from swarm import cli as swarm  # noqa: E402
from swarm.board import base  # noqa: E402

MIN = 60
HOUR = 3600
CAP, ORPHAN = 4, 30   # the defaults


class ExpiryContract:
    harness_factory = None

    @classmethod
    def setUpClass(cls):
        cls.h = cls.harness_factory()

    @classmethod
    def tearDownClass(cls):
        cls.h.close()

    def setUp(self):
        self.h.reset()
        self.b = self.h.board()
        self.addCleanup(self.b.close)

    def job(self, name="j", age=0, agents=(), goal=None):
        """An open job whose run started `age` seconds ago, with agents (key -> seconds of
        silence; still active, never stopped)."""
        self.b.open_job(name, "d", None, None, "me", goal=goal)
        self.h.backdate_job(name, created_at=age + 60, activated_at=age)
        for key, quiet in dict(agents).items():
            self.b.allocate_name(key, name)
            self.h.backdate_agent(key, joined_at=max(age, quiet) + 1, last_seen=quiet)

    def sweep(self, cap=CAP, orphan=ORPHAN, watch=None):
        return self.b.sweep_expiry(cap, orphan, watch)

    def status(self, name="j"):
        return self.b.job_status(name)

    # ---- lifetime cap
    def test_cap_closes_an_old_job_failed_even_with_live_agents(self):
        self.job(age=5 * HOUR, agents={"a": 0})
        closed = self.sweep()
        self.assertEqual([c.job for c in closed], ["j"])
        s = self.status()
        self.assertEqual((s.status, s.closed_by, s.outcome),
                         ("failed", base.AUTO_CLOSED_BY, "auto-closed: open longer than 4 h"))
        self.assertIsNotNone(s.finished_at)
        self.assertEqual(self.b.agents("j", include_departed=False), [])   # left, as deactivate does
        self.assertEqual(self.sweep(), [])   # closed once

    def test_cap_outcome_carries_the_last_verdict(self):
        self.job(age=5 * HOUR, agents={"j1": 0}, goal="ship it")
        self.assertTrue(self.b.claim_judge("j1", "j"))
        self.assertTrue(self.b.record_verdict("j", self.b.active_agent_name("j1"), "not_met",
                                              "tests fail", "fix them"))
        self.sweep()
        self.assertEqual(self.status().outcome,
                         "auto-closed: open longer than 4 h; last verdict not_met: tests fail")

    def test_cap_counts_from_the_current_run_and_not_before_it(self):
        self.job(age=3 * HOUR, agents={"a": 0})
        self.assertEqual(self.sweep(), [])
        self.assertEqual(self.status().status, "active")

    def test_a_per_job_cap_overrides_the_default(self):
        self.job("short", age=2 * HOUR, agents={"a": 0})
        self.b.set_job_max_hours("short", 1.5)
        self.job("long", age=9 * HOUR, agents={"b": 0})
        self.b.set_job_max_hours("long", 10)
        self.job("never", age=99 * HOUR, agents={"c": 0})
        self.b.set_job_max_hours("never", 0)
        self.assertEqual([c.job for c in self.sweep()], ["short"])
        self.assertEqual(self.status("short").outcome, "auto-closed: open longer than 1.5 h")
        self.assertEqual(self.status("long").status, "active")
        self.assertEqual(self.status("never").status, "active")
        self.assertEqual(self.status("never").max_hours, 0)

    def test_default_cap_zero_is_off_and_reactivation_resets_the_override(self):
        self.job(age=99 * HOUR, agents={"a": 0})
        self.assertEqual(self.sweep(cap=0), [])
        self.b.set_job_max_hours("j", 7)
        self.assertEqual(self.status().max_hours, 7)
        self.b.open_job("j", None, None, None, None)
        self.assertIsNone(self.status().max_hours)

    def test_max_hours_of_a_missing_job(self):
        self.assertFalse(self.b.set_job_max_hours("nope", 3))

    # ---- orphans
    def test_no_agents_and_quiet_is_cancelled(self):
        self.job(age=31 * MIN)
        self.assertEqual([c.job for c in self.sweep()], ["j"])
        s = self.status()
        self.assertEqual((s.status, s.closed_by, s.outcome),
                         ("cancelled", base.AUTO_CLOSED_BY, "auto-closed: no live agents for 30 min"))

    def test_not_before_orphan_minutes(self):
        self.job(age=20 * MIN)
        self.assertEqual(self.sweep(), [])

    def test_a_post_counts_as_activity(self):
        self.job(age=2 * HOUR)
        self.h.backdate_message(self.b.post("j", "Someone", "still around").id, 10 * MIN)
        self.assertEqual(self.sweep(), [])

    def test_a_live_agent_keeps_it_open(self):
        for state, quiet in (("started", 2 * MIN), ("idle", 10 * MIN)):
            with self.subTest(state=state):
                self.h.reset()
                self.job(age=2 * HOUR, agents={"a": quiet})
                self.assertEqual(next(a.status for a in self.b.agents("j")), state)
                self.assertEqual(self.sweep(), [])

    def test_dead_and_done_agents_do_not_keep_it_open(self):
        self.job(age=2 * HOUR, agents={"gone": 40 * MIN, "done": 40 * MIN})
        self.b.agent_stopped("done")
        self.h.backdate_agent("done", last_seen=40 * MIN, left_at=40 * MIN)
        self.assertEqual({a.status for a in self.b.agents("j")}, {"dead", "completed"})
        self.assertEqual([c.job for c in self.sweep()], ["j"])
        self.assertEqual(self.status().status, "cancelled")

    def test_an_unmet_goal_does_not_shield_an_orphan(self):
        self.job(age=2 * HOUR, goal="ship it")
        self.assertEqual([c.job for c in self.sweep()], ["j"])

    def test_orphan_rule_off_at_zero(self):
        self.job(age=2 * HOUR)
        self.assertEqual(self.sweep(orphan=0), [])

    def test_watch_of_the_orchestrating_session_keeps_it_open_but_not_past_the_cap(self):
        class Active:
            def __init__(self, job):
                pass

            def active(self):
                return True

        self.job("a", age=2 * HOUR)
        self.job("b", age=5 * HOUR)
        self.assertEqual([c.job for c in self.sweep(watch=Active)], ["b"])
        self.assertEqual(self.status("a").status, "active")

    def test_an_agent_joining_meanwhile_keeps_it_open(self):
        self.job(age=2 * HOUR)
        real = type(self.b).job_status
        joined = []

        def racing(board, job):
            if not joined:   # the sweep's re-read right before the close
                joined.append(board.allocate_name("late", job))
            return real(board, job)

        from unittest import mock
        with mock.patch.object(type(self.b), "job_status", racing):
            self.assertEqual(self.sweep(), [])
        self.assertEqual(self.status().status, "active")

    def test_closed_jobs_are_left_alone(self):
        self.job(age=9 * HOUR)
        self.b.close_job("j", "completed", "done by hand", closed_by="me")
        self.assertEqual(self.sweep(), [])
        self.assertEqual(self.status().outcome, "done by hand")

    # ---- bounded waits
    def test_a_waiting_job_is_covered_by_both_rules(self):
        self.job("orphaned", age=2 * HOUR)
        self.b.set_waiting("orphaned", "the user")
        self.job("old", age=5 * HOUR, agents={"a": 0})
        self.b.set_waiting("old", "the user")
        self.assertEqual(sorted(c.job for c in self.sweep()), ["old", "orphaned"])
        self.assertEqual((self.status("orphaned").status, self.status("old").status), ("cancelled", "failed"))
        self.assertIsNone(self.status("old").waiting_on)

    def test_a_bounded_wait_shields_from_the_orphan_rule_until_it_expires(self):
        self.job(age=2 * HOUR)
        until = self.b.now() + base._dt.timedelta(minutes=45)
        self.assertTrue(self.b.set_waiting("j", "CI", until))
        self.assertEqual(self.status().waiting_until, until)
        self.assertEqual(self.sweep(), [])
        self.assertEqual(self.status().waiting_on, "CI")
        self.h.update_job("j", waiting_until=self.b.now() - base._dt.timedelta(minutes=10))
        # expired: the wait is cleared, and the orphan clock restarts from its end
        self.assertEqual(self.sweep(), [])
        s = self.status()
        self.assertEqual((s.status, s.waiting_on, s.waiting_until), ("active", None, None))
        self.h.update_job("j", waiting_until=None)
        self.b.set_waiting("j", "again", self.b.now() - base._dt.timedelta(minutes=31))
        self.assertEqual([c.job for c in self.sweep()], ["j"])
        self.assertEqual(self.status().outcome, "auto-closed: no live agents for 30 min")

    def test_resume_and_close_clear_the_bound(self):
        self.job(age=MIN)
        until = self.b.now() + base._dt.timedelta(hours=1)
        self.b.set_waiting("j", "CI", until)
        self.b.set_waiting("j", None)
        self.assertIsNone(self.status().waiting_until)
        self.b.set_waiting("j", "CI", until)
        self.b.close_job("j", "completed", None)
        self.assertIsNone(self.status().waiting_until)


class MemoryExpiry(ExpiryContract, unittest.TestCase):
    harness_factory = MemoryHarness


class SqliteExpiry(ExpiryContract, unittest.TestCase):
    harness_factory = SqliteHarness


class FileExpiry(ExpiryContract, unittest.TestCase):
    harness_factory = FileHarness


@unittest.skipUnless(os.environ.get("SWARM_TEST_CONFIG"),
                     "set SWARM_TEST_CONFIG to a throwaway Postgres board config to run")
class PostgresExpiry(ExpiryContract, unittest.TestCase):
    harness_factory = staticmethod(lambda: PostgresHarness(os.environ["SWARM_TEST_CONFIG"]))


# --------------------------------------------------------------------------- CLI and hooks

class ExpiryCliTests(Env):
    def activate(self, job="J", *extra):
        rc, _, err = self.cli("activate", "--job", job, *extra)
        self.assertEqual(rc, 0, err)

    def job(self, job="J"):
        with self.board() as b:
            return b.job_status(job)

    def age(self, job="J", seconds=5 * HOUR):
        self.h.backdate_job(job, created_at=seconds + 60, activated_at=seconds)

    def test_defaults(self):
        self.assertEqual(self.cfg["job"]["max_hours"], 4)
        self.assertEqual(self.cfg["job"]["orphan_minutes"], 30)

    def test_status_closes_an_old_job_and_all_shows_it(self):
        self.activate()
        self.hook("start", agent_id="agent-1")
        self.age()
        self.h.backdate_agent("agent-1", joined_at=5 * HOUR, last_seen=0)
        rc, out, _ = self.cli("status")
        self.assertIn("J: auto-closed: open longer than 4 h", out)
        s = self.job()
        self.assertEqual((s.status, s.closed_by), ("failed", "auto"))
        self.assertFalse(self.markers.joinpath("J.json").exists())
        _, out, _ = self.cli("status", "--all", "--no-color")
        row = next(line for line in out.splitlines() if line.startswith("J "))
        self.assertIn("failed", row)

    def test_activate_max_hours(self):
        self.activate("J", "--max-hours", "10")
        self.assertEqual(self.job().max_hours, 10)
        self.activate("K", "--max-hours", "0")
        self.assertEqual(self.job("K").max_hours, 0)
        self.activate("L")
        self.assertIsNone(self.job("L").max_hours)
        rc, _, err = self.cli("activate", "--job", "M", "--max-hours", "-1")
        self.assertEqual(rc, 2)
        self.assertIn("--max-hours", err)

    def test_config_keys_apply(self):
        self.config.write_text(self.config.read_text() + "[job]\nmax_hours = 1\norphan_minutes = 0\n")
        self.cfg = swarm.load_config(self.config)
        self.activate()
        self.hook("start", agent_id="agent-1")
        self.h.backdate_agent("agent-1", joined_at=2 * HOUR, last_seen=0)
        self.age("J", 2 * HOUR)
        self.cli("purge")
        self.assertEqual(self.job().status, "failed")
        self.assertEqual(self.job().outcome, "auto-closed: open longer than 1 h")

    def test_purge_closes_an_orphan(self):
        self.activate()
        self.age("J", 40 * MIN)
        rc, out, _ = self.cli("purge")
        self.assertIn("J: auto-closed: no live agents for 30 min", out)
        self.assertEqual(self.job().status, "cancelled")

    def test_wait_for_bounds_the_wait(self):
        self.activate()
        rc, out, _ = self.cli("wait", "--job", "J", "--for", "90m", "--on", "the", "review")
        self.assertEqual(rc, 0)
        self.assertIn("J is waiting on: the review (for up to 1h30m; ", out)
        with self.board() as b:
            js = b.job_status("J")
            self.assertAlmostEqual((js.waiting_until - b.now()).total_seconds(), 90 * MIN, delta=30)
        rc, _, err = self.cli("wait", "--job", "J", "--for", "soon", "--on", "x")
        self.assertEqual(rc, 2)
        self.assertIn("--for", err)

    def test_wait_for_units(self):
        self.assertEqual(swarm.parse_duration("45"), 45 * MIN)   # bare number: minutes
        self.assertEqual(swarm.parse_duration("45m"), 45 * MIN)
        self.assertEqual(swarm.parse_duration("2h"), 2 * HOUR)
        self.assertEqual(swarm.parse_duration("1h30m"), 90 * MIN)
        self.assertEqual(swarm.parse_duration("30s"), 30)
        for bad in ("", "0", "soon", "-5m", "1x"):
            with self.assertRaises(ValueError):
                swarm.parse_duration(bad)

    def test_a_queued_bounded_wait_is_delivered_with_its_bound(self):
        import time
        from swarm import spool
        self.activate()
        spool.spool_wait(self.cfg, "J", "CI", time.time() + 3600)
        with self.board() as b:
            spool.flush_spool(b, self.cfg)
            js = b.job_status("J")
            self.assertEqual(js.waiting_on, "CI")
            self.assertAlmostEqual((js.waiting_until - b.now()).total_seconds(), 3600, delta=30)

    def test_a_failing_expiry_sweep_does_not_break_the_caller(self):
        from unittest import mock
        self.activate()
        with mock.patch("swarm.board.base.Board.sweep_expiry", side_effect=RuntimeError("boom")):
            rc, _, _ = self.cli("status")
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
