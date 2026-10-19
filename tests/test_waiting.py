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


def js(status="active", waiting_on=None, started=0, running=0, last_minutes_ago=30.0, open_questions=0) -> JobStatus:
    last = NOW - dt.timedelta(minutes=last_minutes_ago)
    return JobStatus(job="J", status=status, description=None, task=None, outcome=None,
                     created_by=None, session_id=None, created_at=last, activated_at=last,
                     finished_at=None, agents=1, started=started, running=running, idle=0,
                     completed=1, dead_or_left=0, messages=0, last_activity_at=last,
                     waiting_on=waiting_on, open_questions=open_questions)


class DeriveJobStatusTests(unittest.TestCase):
    def test_live_agents_override_every_wait_input(self):
        from dataclasses import replace
        for live in ({"started": 1}, {"running": 1}):
            with self.subTest(live=live):
                state = replace(js(open_questions=2, waiting_on="owner decision", **live), open_blockers=3)
                self.assertEqual(derive_job_status(state, 5, NOW), "active")
                for terminal in ("paused", "completed", "cancelled", "failed"):
                    self.assertEqual(derive_job_status(replace(state, status=terminal), 5, NOW), "active")

    def test_words(self):
        self.assertEqual(derive_job_status(js(status="completed", waiting_on="x"), 5, NOW), "completed")
        self.assertEqual(derive_job_status(js(open_questions=1), 5, NOW), "waiting")   # a question to a person
        self.assertEqual(derive_job_status(js(waiting_on="the user"), 5, NOW), "idle")  # `wait --on` is not a state
        self.assertEqual(derive_job_status(js(running=1), 5, NOW), "active")
        self.assertEqual(derive_job_status(js(started=1), 5, NOW), "active")
        self.assertEqual(derive_job_status(js(last_minutes_ago=2), 5, NOW), "idle")   # just now
        self.assertEqual(derive_job_status(js(), 5, NOW), "idle")   # scheduling intention is not activity


class WaitCliTests(RoutingEnv):
    def test_only_owner_can_create_a_job_with_job_command(self):
        self.activate("J")
        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "agent-session"}):
            rc, _, err = self.cli("job", "NEW")
        self.assertNotEqual(rc, 0)
        self.assertIn("status", err)
        with self.board() as b:
            self.assertIsNone(b.job_status("NEW"))
        rc, _, err = self.cli("job", "NEW")
        self.assertEqual(rc, 0, err)
        with self.board() as b:
            self.assertEqual(b.job_status("NEW").status, "active")

    def test_agent_cannot_merge_jobs_to_close_one_and_clear_the_other_wait(self):
        self.activate("J")
        self.activate("K")
        with self.board() as b:
            b.set_waiting("K", "existing context")
            before = [b.job_status(job) for job in ("J", "K")]
            blockers = b.blockers(include_closed=True)
        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "agent-session"}):
            rc, _, err = self.cli("job", "merge", "J", "--into", "K")
        self.assertNotEqual(rc, 0)
        self.assertIn("status", err)
        with self.board() as b:
            self.assertEqual([b.job_status(job) for job in ("J", "K")], before)
            self.assertEqual(b.blockers(include_closed=True), blockers)

    def test_agent_lifecycle_commands_do_not_change_job_or_blockers(self):
        self.activate("J")
        with self.board() as b:
            b.set_waiting("J", "existing context")
            before = b.job_status("J")
            blockers = b.blockers("J", include_closed=True)
        for command, tail in (("wait", ["--on", "new context"]), ("resume", []),
                              ("activate", []), ("deactivate", []), ("pause", ["--wait", "0"])):
            for env in ("CODEX_THREAD_ID", "CLAUDE_CODE_SESSION_ID"):
                with self.subTest(command=command, env=env), mock.patch.dict(os.environ, {env: "agent-session"}):
                    rc, _, err = self.cli(command, "--job", "J", *tail)
                    self.assertNotEqual(rc, 0)
                    self.assertIn("status", err)
                with self.board() as b:
                    self.assertEqual(b.job_status("J"), before)
                    self.assertEqual(b.blockers("J", include_closed=True), blockers)

    def test_enrolled_agent_cannot_clear_identity_to_set_status(self):
        self.activate("J")
        self.spawn("a1", "[swarm job: J]\nwork")
        for command in ("swarm wait --job J --on pause", "env -i swarm resume --job J",
                        "env -u CODEX_THREAD_ID swarm --config /tmp/cfg deactivate --job J",
                        "sh -c 'swarm pause --job J'", "swarm activate --job J",
                        "env -i python3 -m swarm.cli wait --job J --on x",
                        "env -i python3 -B -m swarm.cli deactivate --job J",
                        "env -i swarm job merge J --into K",
                        "env -i swarm --con /tmp/cfg wait --job J --on x",
                        "env -i python3 -c 'import subprocess; subprocess.run([\"swarm\", \"wait\", \"--job\", \"J\", \"--on\", \"x\"])'",
                        "env -i python3 -c 'import subprocess; subprocess.run(args=[\"swarm\", \"wait\", \"--job\", \"J\", \"--on\", \"x\"])'",
                        "env -i python3 -c 'from swarm import cli; cli.main([\"resume\", \"--job\", \"J\"])'",
                        "env -i python3 -c 'import os; os.system(\"swarm resume --job J\")'"):
            with self.subTest(command=command):
                out = self.hook("turn", agent_id="a1", tool_name="Bash", tool_input={"command": command})
                self.assertIsNotNone(out)
                self.assertIn("hookSpecificOutput", out)
                self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        with self.board() as b:
            self.assertEqual(b.job_status("J").status, "active")
            self.assertEqual(b.blockers("J"), [])

    def test_who_shows_an_empty_goal_job_as_active(self):
        """A goal job nobody works on is not "waiting": `swarm pass` re-queues its lead or fails it."""
        rc, _, err = self.cli('activate', '--job', 'J', '--goal', 'deliver report')
        self.assertEqual(rc, 0, err)
        rc, out, err = self.cli('who', '--job', 'J')
        self.assertEqual(rc, 0, err)
        self.assertNotIn('waiting', out)

    def status_row(self) -> str:
        rc, out, _ = self.cli("status", "--no-color")
        self.assertEqual(rc, 0)
        return next(line for line in out.splitlines() if line.startswith("J "))

    def test_wait_shows_in_status_and_detail_then_resume(self):
        self.activate("J")
        rc, out, _ = self.cli("wait", "--job", "J", "--on", "the", "user's", "answers")
        self.assertEqual(rc, 0)
        self.assertIn("J is waiting on: the user's answers", out)
        self.assertRegex(self.status_row(), r"^J\s+idle\s.*wait -> external: the user's answers")
        rc, out, _ = self.cli("status", "--job", "J", "--no-color")
        self.assertIn("job        J  [idle]", out)
        self.assertRegex(out, r"waiting    on the user's answers, since \d+s ago")
        rc, out, _ = self.cli("resume", "--job", "J")
        self.assertEqual((rc, out), (0, "J is no longer waiting\n"))
        self.assertRegex(self.status_row(), r"^J\s+idle\s")

    def test_wait_needs_an_open_job(self):
        rc, _, err = self.cli("wait", "--job", "nope", "--on", "x")
        self.assertEqual(rc, 1)
        self.assertIn("not an open job", err)

    def test_idle_when_nobody_works_and_no_reason(self):
        self.activate("J")
        self.backdate_job_activity("J")
        self.assertRegex(self.status_row(), r"^J\s+idle\s")   # no agents have started work

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
        self.assertRegex(next(l for l in out.splitlines() if l.startswith("J ")), r"wait -> external: the build slot.*until \d\d:\d\d")

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
