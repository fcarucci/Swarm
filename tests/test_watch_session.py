"""`watch --session S` shows only S's jobs; `--exit-when-idle` lingers, then exits (injectable clock)."""
from __future__ import annotations

import os
import unittest
from unittest import mock

from support import ROOT  # noqa: F401  (sets sys.path)

from swarm import cli as swarm  # noqa: E402
from test_hooks_cli import Env  # noqa: E402

S1 = "11111111-aaaa-bbbb-cccc-000000000001"
S2 = "22222222-aaaa-bbbb-cccc-000000000002"


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


class IdleExitTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.idle = swarm.IdleExit(30, self.clock)

    def test_session_that_never_had_a_job_expires_after_the_linger(self):
        self.idle.observe(False)
        self.clock.t += 29.9
        self.idle.observe(False)
        self.assertFalse(self.idle.expired())
        self.clock.t += 0.1
        self.assertTrue(self.idle.expired())

    def test_job_appearing_before_the_linger_ends_keeps_it_going(self):
        self.idle.observe(False)
        self.clock.t += 20
        self.idle.observe(True)
        self.clock.t += 1000
        self.assertFalse(self.idle.expired())

    def test_active_job_never_expires(self):
        self.idle.observe(True)
        self.clock.t += 1000
        self.idle.observe(True)
        self.assertFalse(self.idle.expired())

    def test_lingers_then_expires(self):
        self.idle.observe(True)
        self.idle.observe(False)
        self.clock.t += 29.9
        self.idle.observe(False)  # more frames do not restart the clock
        self.assertFalse(self.idle.expired())
        self.clock.t += 0.1
        self.assertTrue(self.idle.expired())

    def test_new_job_during_linger_cancels_and_restarts(self):
        self.idle.observe(False)
        self.clock.t += 20
        self.idle.observe(True)
        self.clock.t += 100
        self.assertFalse(self.idle.expired())
        self.idle.observe(False)
        self.clock.t += 29
        self.assertFalse(self.idle.expired())
        self.clock.t += 1
        self.assertTrue(self.idle.expired())


class SessionWatchTests(Env):
    def activate(self, job, session):
        rc, _, err = self.cli("activate", "--job", job, "--session", session)
        self.assertEqual(rc, 0, err)

    def frame(self, session, idle=None):
        view = {"offset": 0, "wrap": False, "max_offset": 0, "all_agents": False, "anchor": None,
                "scroll": 0, "mark": None, "page": 1, "recent_minutes": 10, "session": session,
                "idle_exit": idle}
        with self.board() as b, mock.patch("shutil.get_terminal_size", return_value=os.terminal_size((160, 40))):
            return "\n".join(swarm._watch_frame(b, None, 2.0, False, True, view))

    def test_filter_and_header(self):
        self.activate("JA", S1)
        self.activate("JB", S2)
        text = self.frame(S1)
        self.assertIn("session 11111111", text.splitlines()[0])
        self.assertIn("JA", text)
        self.assertNotIn("JB", text)
        self.assertNotIn("22222222", text)

    def test_new_job_of_same_session_appears(self):
        self.activate("JA", S1)
        self.assertNotIn("JC", self.frame(S1))
        self.activate("JC", S1)
        text = self.frame(S1)
        self.assertIn("JA", text)
        self.assertIn("JC", text)

    def test_messages_only_from_the_sessions_jobs(self):
        self.activate("JA", S1)
        self.activate("JB", S2)
        self.cli("join", "--job", "JA", "--key", "ka")
        self.cli("join", "--job", "JB", "--key", "kb")
        with self.board() as b:
            b.post("JA", b.active_agent_name("ka"), "hello-from-a")
            b.post("JB", b.active_agent_name("kb"), "hello-from-b")
        text = self.frame(S1)
        self.assertIn("hello-from-a", text)
        self.assertNotIn("hello-from-b", text)

    def test_closed_job_stays_shown_and_starts_the_linger(self):
        clock = Clock()
        idle = swarm.IdleExit(30, clock)
        self.activate("JA", S1)
        self.frame(S1, idle)
        self.assertFalse(idle.expired())
        rc, _, err = self.cli("deactivate", "--job", "JA")
        self.assertEqual(rc, 0, err)
        text = self.frame(S1, idle)
        self.assertIn("JA", text)          # the final state stays on screen
        clock.t += 30
        self.assertTrue(idle.expired())
        self.activate("JA", S1)            # reactivated during the linger
        self.frame(S1, idle)
        self.assertFalse(idle.expired())

    def test_no_session_shows_everything(self):
        self.activate("JA", S1)
        self.activate("JB", S2)
        text = self.frame(None)
        self.assertIn("JA", text)
        self.assertIn("JB", text)
        self.assertIn("all active jobs", text.splitlines()[0])

    def test_loop_returns_when_expired(self):
        clock = Clock()
        idle = swarm.IdleExit(30, clock)
        idle.observe(False)
        clock.t += 31
        view = {"idle_exit": idle}
        board = mock.Mock()
        board.wait_for_change.return_value = False
        out = mock.Mock()
        swarm._watch_loop(board, out, None, 2.0, view, lambda: ["x"])  # returns, no sleeping
        board.subscribe.assert_called_once()


class CompactTests(Env):
    def setUp(self):
        super().setUp()
        self.cli("activate", "--job", "a-rather-long-job-name-for-a-narrow-pane", "--session", S1)
        self.cli("activate", "--job", "JB", "--session", S2)
        job = "a-rather-long-job-name-for-a-narrow-pane"
        for key, role in (("w1", None), ("w2", "verifier")):
            self.cli("join", "--job", job, "--key", key, *(["--role", role] if role else []))
        self.cli("join", "--job", "JB", "--key", "other")
        self.h.update_agent("w1", model="claude-sonnet-5-5", current_tool="Bash: some very long command line here")
        with self.board() as b:
            for key in ("w1", "w2"):
                b.post(job, b.active_agent_name(key), "a long message " * 20)

    def frame(self, cols, rows=30, session=S1, color=False):
        view = {"offset": 0, "wrap": False, "max_offset": 0, "all_agents": False, "anchor": None,
                "scroll": 0, "mark": None, "page": 1, "recent_minutes": 10, "session": session,
                "compact": True}
        with self.board() as b, mock.patch("shutil.get_terminal_size", return_value=os.terminal_size((cols, rows))):
            return swarm._watch_frame(b, None, 2.0, color, True, view)

    def test_no_line_wider_than_the_pane(self):
        for cols in (30, 40, 50, 60, 70):
            for color in (False, True):
                lines = self.frame(cols, color=color)
                self.assertLessEqual(len(lines), 29)
                for ln in lines:
                    self.assertLessEqual(swarm._visible_len(ln), cols, (cols, ln))

    def test_content_at_50_columns(self):
        text = "\n".join(self.frame(50))
        self.assertIn("session 11111111", text)
        self.assertIn("a-rather-long-job-name", text)
        self.assertIn("[verifier]", text)
        self.assertIn("sonnet-5-5", text)
        self.assertIn("MESSAGES", text)
        self.assertNotIn("JB", text)
        self.assertNotIn("LAST CONTACT", text)

    def test_messages_dropped_when_no_room(self):
        text = "\n".join(self.frame(50, rows=6))
        self.assertNotIn("MESSAGES", text)
        self.assertLessEqual(len(self.frame(50, rows=6)), 5)


if __name__ == "__main__":
    unittest.main()
