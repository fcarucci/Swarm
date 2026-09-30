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

    def frame(self, cols, rows=30, session=S1, color=False, view=None):
        view = view if view is not None else self.view(session)
        with self.board() as b, mock.patch("shutil.get_terminal_size", return_value=os.terminal_size((cols, rows))):
            return swarm._watch_frame(b, None, 2.0, color, True, view)

    def view(self, session=S1, offset=0):
        return {"offset": offset, "wrap": False, "max_offset": 0, "all_agents": False, "anchor": None,
                "scroll": 0, "mark": None, "page": 1, "recent_minutes": 10, "session": session,
                "compact": True}

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

    def test_colored_output_fits_and_has_no_broken_escape(self):
        import re
        for cols in range(12, 71):
            for ln in self.frame(cols, color=True):
                self.assertLessEqual(swarm._visible_len(ln), cols, (cols, ln))
                self.assertIsNone(re.search(r"\033(\[[0-9;?]*)?$", ln), (cols, ln))
                self.assertEqual(len(re.findall(r"\033(?!\[[0-9;?]*[A-Za-z])", ln)), 0, (cols, ln))

    def test_no_color_has_no_escapes(self):
        for cols in (20, 50, 70):
            self.assertNotIn("\033", "\n".join(self.frame(cols, color=False)))

    def test_names_use_the_tail_color(self):
        with self.board() as b:
            names = [b.active_agent_name(k) for k in ("w1", "w2")]
        text = "\n".join(self.frame(70, color=True))
        for n in names:
            painted = swarm._paint_name(n, True)
            self.assertGreaterEqual(text.count(painted), 2, n)  # agent line and MESSAGES line

    def test_every_open_job_of_the_session_is_shown(self):
        self.cli("activate", "--job", "JC", "--session", S1)
        self.cli("activate", "--job", "JD", "--session", S1)
        self.cli("join", "--job", "JC", "--key", "c1")
        self.cli("join", "--job", "JD", "--key", "d1")
        self.cli("wait", "--job", "JD", "--on", "a build")
        with self.board() as b:
            c1, d1 = b.active_agent_name("c1"), b.active_agent_name("d1")
        text = self.frame(70, rows=40)
        lines = [ln.strip() for ln in text]
        for want in ("a-rather-long-job-name", "JC", "JD"):
            self.assertTrue(any(ln.startswith(want) for ln in lines), want)
        self.assertTrue(any(ln.startswith(c1) for ln in lines))
        self.assertTrue(any(ln.startswith(d1) for ln in lines))
        self.assertFalse(any(ln.startswith("JB") for ln in lines))
        order = [i for w in ("JC", c1, "JD", d1) for i, ln in enumerate(lines) if ln.startswith(w)]
        self.assertEqual(order, sorted(order))  # each agent under its own job

    def test_offset_scrolls_by_visible_characters(self):
        import re
        strip = lambda ln: re.sub(r"\033\[[0-9;?]*[A-Za-z]", "", ln)  # noqa: E731
        for color in (False, True):
            wide = [strip(ln) for ln in self.frame(300, color=color)]
            moved = self.frame(40, color=color, view=self.view(offset=7))
            for full, ln in zip(wide, moved):
                self.assertLessEqual(swarm._visible_len(ln), 40)
                if len(full) > 47:  # long line: the text right of the offset, cut to the pane
                    self.assertEqual(strip(ln)[1:39], full[8:46])
                    self.assertTrue(strip(ln).startswith("…"))
                self.assertEqual(len(re.findall(r"\033(?!\[[0-9;?]*[A-Za-z])", ln)), 0, ln)

    def test_offset_is_clamped_to_the_longest_line(self):
        view = self.view(offset=10_000)
        lines = self.frame(40, view=view)
        longest = max(swarm._visible_len(ln) for ln in self.frame(10_000, rows=30, view=self.view()))
        self.assertEqual(view["max_offset"], longest - 40)
        self.assertEqual(view["offset"], view["max_offset"])
        self.assertTrue(any(ln.strip() for ln in lines))  # something is still on screen

    def test_offset_survives_redraws_and_keys_move_it(self):
        view = self.view()
        self.frame(40, view=view)
        swarm._apply_keys("\x1b[C", view)
        self.assertGreater(view["offset"], 0)
        first = view["offset"]
        self.frame(40, view=view)
        self.assertEqual(view["offset"], first)
        swarm._apply_keys("\x1b[D", view)
        self.assertLess(view["offset"], first)
        swarm._apply_keys("\x1b[C\x1b[C0", view)
        self.assertEqual(view["offset"], 0)
        swarm._apply_keys("\x1b[C\x1b[H", view)
        self.assertEqual(view["offset"], 0)

    def test_offset_zero_shows_a_marker_when_a_line_is_cut(self):
        lines = self.frame(30)
        self.assertTrue(any(ln.endswith("…") for ln in lines))

    def test_messages_dropped_when_no_room(self):
        text = "\n".join(self.frame(50, rows=6))
        self.assertNotIn("MESSAGES", text)
        self.assertLessEqual(len(self.frame(50, rows=6)), 5)


if __name__ == "__main__":
    unittest.main()


class LeaveSessionTests(Env):
    def test_leave_session_marks_every_unfinished_agent_of_that_session_left(self):
        self.cli("activate", "--job", "JA", "--session", S1)
        self.cli("activate", "--job", "JB", "--session", S1)
        self.cli("activate", "--job", "JX", "--session", S2)
        for job, key in (("JA", "a1"), ("JA", "a2"), ("JB", "b1"), ("JX", "x1")):
            self.cli("join", "--job", job, "--key", key)
        self.cli("leave", "--key", "a2")  # already gone: untouched
        rc, out, err = self.cli("leave", "--session", S1)
        self.assertEqual((rc, err), (0, ""), err)
        self.assertEqual(out, "left 2\n")
        with self.board() as b:
            status = {a.agent_key: a for j in ("JA", "JB", "JX") for a in b.agents(j)}
        self.assertIsNotNone(status["a1"].ended_at)
        self.assertEqual(status["a1"].left_reason, "session restarted")
        self.assertIsNotNone(status["b1"].ended_at)
        self.assertIsNone(status["a2"].left_reason)     # left by itself earlier
        self.assertIsNone(status["x1"].ended_at)        # another session's agent stays
        self.assertEqual(self.cli("leave", "--session", S1)[1], "left 0\n")  # idempotent

    def test_leave_session_excludes_name_and_key(self):
        rc, _, err = self.cli("leave", "--session", S1, "--key", "k")
        self.assertNotEqual(rc, 0)
