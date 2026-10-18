"""A goal job without a met verdict is not closed by an older client's "no live agents" sweep:
the Postgres board refuses that close itself (trigger jobs_keep_goal_jobs). Goal-less jobs and
met goals still close that way."""
from __future__ import annotations

import os
import unittest

from support import PostgresHarness  # noqa: F401  (also sets sys.path)

OLD_CLOSE = ("UPDATE jobs SET status = 'cancelled', outcome = 'auto-closed: no live agents for 30 min', "
             "closed_by = 'auto' WHERE job = %s")


@unittest.skipUnless(os.environ.get("SWARM_TEST_CONFIG"),
                     "set SWARM_TEST_CONFIG to a throwaway Postgres board config to run")
class OrphanGuardTests(unittest.TestCase):
    def setUp(self):
        self.h = PostgresHarness(os.environ["SWARM_TEST_CONFIG"])
        self.addCleanup(self.h.close)
        self.h.reset()
        self.board = self.h.board()

    def status(self, job):
        return self.h.conn.execute("SELECT status FROM jobs WHERE job = %s", (job,)).fetchone()[0]

    def test_old_orphan_close_of_a_goal_job_is_refused(self):
        self.board.open_job("g", None, "t", None, "u", goal="ship it")
        self.h.conn.execute(OLD_CLOSE, ("g",))
        self.assertEqual(self.status("g"), "active")

    def test_goal_less_job_still_closes(self):
        self.board.open_job("n", None, "t", None, "u")
        self.h.conn.execute(OLD_CLOSE, ("n",))
        self.assertEqual(self.status("n"), "cancelled")

    def test_met_goal_and_explicit_close_still_close(self):
        self.board.open_job("m", None, "t", None, "u", goal="ship it")
        self.h.conn.execute("UPDATE jobs SET verdict = 'met' WHERE job = 'm'")
        self.h.conn.execute(OLD_CLOSE, ("m",))
        self.assertEqual(self.status("m"), "cancelled")
        self.board.open_job("e", None, "t", None, "u", goal="ship it")
        self.assertTrue(self.board.close_job("e", "cancelled", "by a person"))
        self.assertEqual(self.status("e"), "cancelled")

    def test_the_stale_waiting_close_of_a_goal_job_or_a_zero_limit_job_is_refused(self):
        stale = "auto-closed: waiting with no live agents and no verdict"
        self.board.open_job("g2", None, "t", None, "u", goal="ship it")
        self.h.conn.execute("UPDATE jobs SET status='failed', outcome=%s, closed_by='auto' WHERE job='g2'", (stale,))
        self.assertEqual(self.status("g2"), "active")
        self.h.conn.execute("UPDATE jobs SET verdict='unreachable' WHERE job='g2'")
        self.h.conn.execute("UPDATE jobs SET status='failed', outcome=%s, closed_by='auto' WHERE job='g2'", (stale,))
        self.assertEqual(self.status("g2"), "failed")                  # a settled goal may close
        self.board.open_job("z", None, "t", None, "u")
        self.h.conn.execute("UPDATE jobs SET max_hours=0 WHERE job='z'")
        self.h.conn.execute("UPDATE jobs SET status='failed', outcome=%s, closed_by='auto' WHERE job='z'", (stale,))
        self.assertEqual(self.status("z"), "active")
        self.board.open_job("p", None, "t", None, "u")
        self.h.conn.execute("UPDATE jobs SET status='failed', outcome=%s, closed_by='auto' WHERE job='p'", (stale,))
        self.assertEqual(self.status("p"), "failed")                   # a plain stale job still closes

    def test_a_quiet_time_close_of_a_job_waiting_on_a_person_is_refused(self):
        self.board.open_job("w", None, "t", None, "u")
        self.board.post("w", "agent-1", "u, which one?", to="u")
        for outcome in ("auto-closed: no live agents for 30 min", "auto-closed: no progress for 4 h",
                        "auto-closed: waiting with no live agents and no verdict"):
            self.h.conn.execute("UPDATE jobs SET status='failed', outcome=%s, closed_by='auto' WHERE job='w'", (outcome,))
            self.assertEqual(self.status("w"), "active", outcome)
        self.board.post("w", "u", "the first")                 # answered: the sweep may close it again
        self.h.conn.execute("UPDATE jobs SET status='failed', outcome='auto-closed: no progress for 4 h', "
                            "closed_by='auto' WHERE job='w'")
        self.assertEqual(self.status("w"), "failed")

    def test_a_configured_owner_name_and_a_finished_agents_question_protect_on_postgres(self):
        self.board.open_job("o", None, "t", None, "u")
        self.board.set_job_data("o", "owner.names", "Marigold,Basil")
        self.board.post("o", "agent-1", "Basil, ok?", to="Basil")
        for outcome in ("auto-closed: 1/1 agents completed; last post agent-1: Basil, ok?",
                        "auto-closed: no live agents for 30 min"):
            self.h.conn.execute("UPDATE jobs SET status='completed', outcome=%s, closed_by='auto' WHERE job='o'", (outcome,))
            self.assertEqual(self.status("o"), "active", outcome)
        self.board.set_job_data("o", "owner.names", None)
        self.h.conn.execute("UPDATE jobs SET status='completed', outcome='auto-closed: no live agents for 30 min', "
                            "closed_by='auto' WHERE job='o'")
        self.assertEqual(self.status("o"), "completed")          # an unconfigured name protects nothing

    def test_the_view_and_python_agree_on_a_future_run_start(self):
        import datetime as dt
        from swarm.board import base
        self.board.open_job("f", None, "t", None, "u")
        self.h.conn.execute("UPDATE jobs SET created_at = now() - interval '10 hours', "
                            "activated_at = now() + interval '10 hours' WHERE job = 'f'")
        js = self.board.job_status("f")
        self.assertEqual(base.derive_job_status(js, 30, self.board.now()), "idle")
        self.assertEqual(self.h.shown_status("f"), "idle")


if __name__ == "__main__":
    unittest.main()
