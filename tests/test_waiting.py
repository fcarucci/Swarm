"""An open job that nobody is working on: `swarm wait --on` says what it waits for, and a job
with no agent at work and no reason given shows as idle. Stored status stays "active" (the
hooks and the marker don't change); status and watch show the derived word."""
from __future__ import annotations

import datetime as dt
import os
import re
import time
import unittest
from unittest import mock

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
        self.h.backdate_job(job, activated_at=600, created_at=600)   # idle, not yet an orphan (30 min)


if __name__ == "__main__":
    unittest.main()


class WaitUntilTests(RoutingEnv):
    def test_wait_deadline_forms(self):
        from swarm.cli import wait_deadline
        now = dt.datetime(2026, 10, 5, 12, 0).astimezone().timestamp()
        self.assertIsNone(wait_deadline(None, None, now))
        self.assertEqual(wait_deadline("90m", None, now), now + 5400)
        self.assertEqual(wait_deadline(None, "2h", now), now + 7200)       # --until takes a duration too
        self.assertEqual(wait_deadline(None, "17:30", now), dt.datetime(2026, 10, 5, 17, 30).astimezone().timestamp())
        self.assertEqual(wait_deadline(None, "09:00", now), dt.datetime(2026, 10, 6, 9, 0).astimezone().timestamp())   # the next one
        self.assertEqual(wait_deadline(None, "2026-10-06 09:00", now), dt.datetime(2026, 10, 6, 9, 0).astimezone().timestamp())
        self.assertEqual(wait_deadline(None, "2026-10-06T09:00:00+00:00", now),
                         dt.datetime(2026, 10, 6, 9, 0, tzinfo=dt.timezone.utc).timestamp())
        for for_, until in (("1h", "2h"), ("soon", None), (None, "tomorrow-ish"), (None, "2026-10-01 09:00"), (None, "25:99")):
            with self.subTest(for_=for_, until=until), self.assertRaises(ValueError):
                wait_deadline(for_, until, now)

    def test_wait_until_protects_and_shows_in_status_and_the_listing(self):
        self.activate("J")
        rc, out, _ = self.cli("wait", "--job", "J", "--until", "3h", "--on", "the", "build", "slot")
        self.assertEqual(rc, 0)
        self.assertIn("J is waiting on: the build slot (until ", out)
        with self.board() as b:
            js = b.job_status("J")
            self.assertAlmostEqual((js.waiting_until - b.now()).total_seconds(), 3 * 3600, delta=30)
        rc, out, _ = self.cli("status", "--job", "J", "--no-color")
        self.assertRegex(out, r"waiting    on the build slot, since \d+s ago, until \d{4}-\d\d-\d\d \d\d:\d\d \(protected from auto-close\)")
        rc, out, _ = self.cli("status", "--no-color")
        self.assertRegex(next(l for l in out.splitlines() if l.startswith("J ")), r"the build slot · \d+s · until \d\d:\d\d")

    def test_wait_rejects_for_with_until_and_a_past_time(self):
        self.activate("J")
        rc, _, err = self.cli("wait", "--job", "J", "--for", "1h", "--until", "2h", "--on", "x")
        self.assertEqual(rc, 2)
        self.assertIn("not both", err)
        rc, _, err = self.cli("wait", "--job", "J", "--until", "2000-01-01 00:00", "--on", "x")
        self.assertEqual(rc, 2)
        self.assertIn("in the past", err)
        with self.board() as b:
            self.assertIsNone(b.job_status("J").waiting_on)


class OrchestratorReadTests(RoutingEnv):
    def test_a_board_read_by_the_orchestrating_session_counts_as_contact(self):
        from swarm import cli as swarm_cli
        self.activate("J", session="sess-9")
        markers = [p for p in self.markers.glob("*.json")]
        self.assertEqual(len(markers), 1)
        seen = swarm_cli.orchestrator_seen_path(markers[0])
        swarm_cli.mark_orchestrator_seen(markers[0])
        old = time.time() - 3600
        os.utime(seen, (old, old))
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "sess-9"}):
            self.assertEqual(self.cli("status", "--job", "J")[0], 0)
        self.assertGreater(seen.stat().st_mtime, old + 3000)
        os.utime(seen, (old, old))
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "another-session"}):
            self.cli("who", "--job", "J")
        self.assertAlmostEqual(seen.stat().st_mtime, old, delta=2)   # someone else's read is no contact
