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


if __name__ == "__main__":
    unittest.main()
