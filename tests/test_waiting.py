"""An open job that nobody is working on: `swarm wait --on` says what it waits for, and a job
with no agent at work and no reason given shows as idle. Stored status stays "active" (the
hooks and the marker don't change); status and watch show the derived word."""
from __future__ import annotations

import datetime as dt
import re
import unittest

from test_routing import RoutingEnv  # noqa: E402  (sets sys.path)

from swarm.board import JobStatus, derive_job_status  # noqa: E402

NOW = dt.datetime(2026, 9, 24, 12, 0, tzinfo=dt.timezone.utc)


def js(status="active", waiting_on=None, started=0, running=0, last_minutes_ago=30.0) -> JobStatus:
    last = NOW - dt.timedelta(minutes=last_minutes_ago)
    return JobStatus(job="J", status=status, description=None, task=None, outcome=None,
                     created_by=None, session_id=None, created_at=last, activated_at=last,
                     finished_at=None, agents=1, started=started, running=running, idle=0,
                     completed=1, dead_or_left=0, messages=0, last_activity_at=last,
                     waiting_on=waiting_on)


class DeriveJobStatusTests(unittest.TestCase):
    def test_words(self):
        self.assertEqual(derive_job_status(js(status="completed", waiting_on="x"), 5, NOW), "completed")
        self.assertEqual(derive_job_status(js(waiting_on="the user"), 5, NOW), "waiting")
        self.assertEqual(derive_job_status(js(running=1), 5, NOW), "active")
        self.assertEqual(derive_job_status(js(started=1), 5, NOW), "active")
        self.assertEqual(derive_job_status(js(last_minutes_ago=2), 5, NOW), "active")   # just now
        self.assertEqual(derive_job_status(js(), 5, NOW), "idle")


class WaitCliTests(RoutingEnv):
    def status_row(self) -> str:
        rc, out, _ = self.cli("status", "--no-color")
        self.assertEqual(rc, 0)
        return next(line for line in out.splitlines() if line.startswith("J "))

    def test_wait_shows_in_status_and_detail_then_resume(self):
        self.activate("J")
        rc, out, _ = self.cli("wait", "--job", "J", "--on", "the", "user's", "answers")
        self.assertEqual(rc, 0)
        self.assertIn("J is waiting on: the user's answers", out)
        self.assertRegex(self.status_row(), r"^J\s+waiting\s.*the user's answers · \d+s")
        rc, out, _ = self.cli("status", "--job", "J", "--no-color")
        self.assertIn("job        J  [waiting]", out)
        self.assertRegex(out, r"waiting    on the user's answers, since \d+s ago")
        rc, out, _ = self.cli("resume", "--job", "J")
        self.assertEqual((rc, out), (0, "J is no longer waiting\n"))
        self.assertRegex(self.status_row(), r"^J\s+active\s")

    def test_wait_needs_an_open_job(self):
        rc, _, err = self.cli("wait", "--job", "nope", "--on", "x")
        self.assertEqual(rc, 1)
        self.assertIn("not an open job", err)

    def test_idle_when_nobody_works_and_no_reason(self):
        self.activate("J")
        self.backdate_job_activity("J")
        self.assertRegex(self.status_row(), r"^J\s+idle\s")

    def test_an_agent_joining_ends_the_wait(self):
        self.activate("J")
        self.cli("wait", "--job", "J", "--on", "review")
        self.spawn("a1", "[swarm job: J]\nwork")
        with self.board() as b:
            self.assertIsNone(b.job_status("J").waiting_on)

    def backdate_job_activity(self, job: str) -> None:
        self.h.backdate_job(job, activated_at=3600, created_at=3600)


if __name__ == "__main__":
    unittest.main()
