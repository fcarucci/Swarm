"""`watch` scrolls the messages pane back into history: ↑/↓ (k/j), PgUp/PgDn, G for live."""
from __future__ import annotations

import contextlib
import io
import os
import re
import unittest
from types import SimpleNamespace
from unittest import mock

from support import ROOT  # noqa: F401  (sets sys.path)

from swarm import board as swarm_board  # noqa: E402
from swarm import cli as swarm  # noqa: E402
from test_hooks_cli import Env  # noqa: E402

UP, DOWN, PGUP, PGDN = "\x1b[A", "\x1b[B", "\x1b[5~", "\x1b[6~"


class WatchScrollTests(Env):
    """Job J with one agent and 40 messages m00..m39; a 200x30 terminal."""

    ROWS = 30

    def setUp(self):
        super().setUp()
        self.cli("activate", "--job", "J")
        self.cli("join", "--job", "J", "--key", "a1")
        with self.board() as b:
            self.name = b.active_agent_name("a1")
        self.view = {"offset": 0, "wrap": False, "max_offset": 0, "all_agents": False,
                     "anchor": None, "scroll": 0, "mark": None, "page": 1, "recent_minutes": 10}
        self.post(range(40))

    def post(self, numbers, text=""):
        with self.board() as b:
            for i in numbers:
                b.post("J", self.name, f"m{i:02d}{text}")

    def frame(self, keys="", cols=200):
        if keys:
            self.assertTrue(swarm._apply_keys(keys, self.view))
        with self.board() as b, mock.patch("shutil.get_terminal_size",
                                           return_value=os.terminal_size((cols, self.ROWS))):
            return swarm._watch_frame(b, "J", 2.0, False, True, self.view)

    def pane(self, lines):
        """(MESSAGES header, message numbers shown in order)."""
        at = next(i for i, ln in enumerate(lines) if ln.startswith("MESSAGES"))
        nums = [int(m) for ln in lines[at + 1:] for m in re.findall(r": m(\d\d)", ln)]
        return lines[at], nums

    def test_live_tail_unchanged(self):
        head, nums = self.pane(self.frame())
        self.assertEqual(head, "MESSAGES")
        self.assertEqual(nums[-1], 39)
        self.assertEqual(nums, list(range(nums[0], 40)))
        self.assertIsNone(self.view["anchor"])

    def test_up_and_down_move_one_message(self):
        _, live = self.pane(self.frame())
        for keys, shift in ((UP, 1), ("k", 2), (DOWN, 1), ("j", 0)):
            _, nums = self.pane(self.frame(keys))
            self.assertEqual(nums, [n - shift for n in live], keys)
        self.assertIsNone(self.view["anchor"])  # back at the tail

    def test_page_keys_move_by_the_messages_on_screen(self):
        _, live = self.pane(self.frame())
        page = len(live)
        self.assertEqual(self.view["page"], page)
        _, nums = self.pane(self.frame(PGUP))
        self.assertEqual(nums[-1], 39 - page)
        _, nums = self.pane(self.frame(PGDN))
        self.assertEqual(nums, live)

    def test_clamps_at_oldest_and_newest(self):
        _, live = self.pane(self.frame())
        _, nums = self.pane(self.frame(PGUP * 10))
        self.assertEqual(nums, list(range(len(live))))  # full pane from m00, no empty space
        head, again = self.pane(self.frame(UP))
        self.assertEqual(again, nums)
        self.assertIn(f"scrolled back {40 - len(live)}", head)
        _, nums = self.pane(self.frame(PGDN * 10 + DOWN))
        self.assertEqual(nums, live)
        self.assertIsNone(self.view["anchor"])

    def test_nothing_to_scroll_when_everything_fits(self):
        self.cli("activate", "--job", "K")
        with self.board() as b:
            for i in range(3):
                b.post("K", self.name, f"m{i:02d}")
            with mock.patch("shutil.get_terminal_size", return_value=os.terminal_size((200, self.ROWS))):
                swarm._apply_keys(UP, self.view)
                lines = swarm._watch_frame(b, "K", 2.0, False, True, self.view)
        self.assertEqual(self.pane(lines), ("MESSAGES", [0, 1, 2]))
        self.assertIsNone(self.view["anchor"])

    def test_new_messages_do_not_move_a_scrolled_back_view(self):
        _, before = self.pane(self.frame(UP * 12))
        self.post(range(40, 43))
        head, after = self.pane(self.frame())
        self.assertEqual(after, before)
        self.assertEqual(head, "MESSAGES  (scrolled back 12 · 3 newer · G for live)")

    def test_header_without_newer(self):
        head, _ = self.pane(self.frame(UP * 2))
        self.assertEqual(head, "MESSAGES  (scrolled back 2 · G for live)")

    def test_G_returns_to_live_tail_including_new_messages(self):
        self.frame(UP * 5)
        self.post(range(40, 42))
        head, nums = self.pane(self.frame("G"))
        self.assertEqual(head, "MESSAGES")
        self.assertEqual(nums[-1], 41)
        self.assertIsNone(self.view["anchor"])

    def test_down_walks_into_newer_messages_then_goes_live(self):
        self.frame(UP)
        self.post([40])
        head, nums = self.pane(self.frame(DOWN))
        self.assertEqual(nums[-1], 39)
        self.assertEqual(head, "MESSAGES  (scrolled back 0 · 1 newer · G for live)")
        head, nums = self.pane(self.frame(DOWN))
        self.assertEqual((head, nums[-1]), ("MESSAGES", 40))

    def test_home_end_still_horizontal(self):
        self.post([40], " " + "x" * 400)
        self.frame(UP * 3)
        anchor = self.view["anchor"]
        self.frame("$")
        self.assertEqual(self.view["anchor"], anchor)  # End is not the live tail
        self.assertEqual(self.view["offset"], self.view["max_offset"])

    def test_scroll_counts_messages_in_wrap_mode(self):
        self.post(range(40, 60), " " + "word " * 60)  # each wraps onto 2 lines at 200 cols
        self.view["wrap"] = True
        lines = self.frame()
        _, live = self.pane(lines)
        self.assertEqual(live[-1], 59)
        _, nums = self.pane(self.frame(UP))
        self.assertEqual(nums[-1], 58)
        self.assertEqual(self.view["page"], len(live))
        _, nums = self.pane(self.frame(PGUP))
        self.assertEqual(nums[-1], 58 - len(live))
        self.assertIn("(wrap)  (scrolled back", self.pane(self.frame())[0])

    def test_horizontal_offset_applies_while_scrolled_back(self):
        self.post(range(40, 45), " " + "y" * 300)
        self.frame(UP * 5)  # bottom is m39: only short messages on screen
        self.assertEqual(self.view["max_offset"], 0)
        self.frame(DOWN * 2)
        self.frame("l")
        self.assertGreater(self.view["offset"], 0)
        lines = self.frame()
        self.assertTrue(any("…" in ln for ln in lines))

    def test_history_fetch_is_bounded(self):
        calls = []
        with self.board() as b:
            real = b.recent_messages
            b.recent_messages = lambda limit, **kw: calls.append(limit) or real(limit, **kw)
            with mock.patch("shutil.get_terminal_size", return_value=os.terminal_size((200, self.ROWS))):
                swarm._watch_frame(b, "J", 2.0, False, True, self.view)
                swarm._apply_keys(UP, self.view)
                swarm._watch_frame(b, "J", 2.0, False, True, self.view)
        self.assertLess(calls[0], 30)
        self.assertEqual(calls[1], swarm.WATCH_HISTORY)

    def test_title_lists_the_keys(self):
        title = self.frame()[0]
        for k in ("↑/↓", "PgUp/PgDn", "G live", "←/→", "Home/End", "w wrap", "a all agents", "q quit"):
            self.assertIn(k, title)



class WatchTablesScrollTests(Env):
    """←/→ scroll the job and agents tables too, not just the messages (job J, a long
    description, one agent, short messages; a 100x40 terminal)."""

    DESC = "the job description " + "x" * 150 + " TAILEND"

    def setUp(self):
        super().setUp()
        self.cli("activate", "--job", "J", "--description", self.DESC)
        self.cli("join", "--job", "J", "--key", "a1")
        with self.board() as b:
            b.post("J", b.active_agent_name("a1"), "hi")
        self.view = {"offset": 0, "wrap": False, "max_offset": 0, "all_agents": False,
                     "anchor": None, "scroll": 0, "mark": None, "page": 1, "recent_minutes": 10}

    def frame(self, keys="", job=None, cols=100):
        with mock.patch("shutil.get_terminal_size", return_value=os.terminal_size((cols, 40))):
            if keys:
                self.assertTrue(swarm._apply_keys(keys, self.view))
            with self.board() as b:
                return swarm._watch_frame(b, job, 2.0, False, True, self.view)

    def test_status_shows_the_whole_description(self):
        out = self.cli("status")[1]
        self.assertIn(self.DESC, out)

    def test_description_tail_visible_after_scrolling_right(self):
        lines = self.frame()  # the frame sets how far → can go
        self.assertFalse(any("TAILEND" in ln for ln in lines))
        self.assertGreater(self.view["max_offset"], 0)  # the job table is wider than the screen
        lines = self.frame("$")
        self.assertTrue(any(ln.rstrip().endswith("TAILEND") for ln in lines), lines)
        self.assertTrue(all(len(ln) <= 100 for ln in lines))

    def test_right_steps_scroll_every_table_by_the_same_offset(self):
        full = self.frame(cols=1000)  # nothing clipped
        self.frame()
        after = self.frame("l")
        off = self.view["offset"]
        self.assertGreater(off, 0)
        self.assertIn("(scrolled 33 →)", next(ln for ln in after if ln.startswith("MESSAGES")))
        for head in ("JOB ", "J ", "AGENT "):
            at = next(i for i, ln in enumerate(full) if ln.startswith(head))
            self.assertEqual(after[at], ("…" + full[at][off + 1:])[:100], head)

    def test_titles_and_section_headings_stay_put(self):
        self.frame()
        lines = self.frame("l")
        self.assertTrue(lines[0].startswith("swarm watch"))
        for heading in ("JOBS", "AGENTS · J"):
            self.assertIn(heading, lines)
        self.assertTrue(any(ln.startswith("MESSAGES") for ln in lines))
        self.assertEqual(lines[-1].count(":"), 3)  # the message prefix (time, author) never scrolls

    def test_single_job_head_scrolls_too(self):
        lines = self.frame(job="J")
        self.assertFalse(any("TAILEND" in ln for ln in lines))
        self.frame("$", job="J")
        self.assertTrue(any(ln.rstrip().endswith("TAILEND") for ln in self.frame(job="J")))

    def test_home_returns_to_the_left_edge(self):
        before = self.frame()
        self.frame("$")
        self.assertEqual(self.frame("0")[1:], before[1:])


    def test_shift_keeps_colour_codes(self):
        line = "ab \033[32mactive\033[0m  tail"
        self.assertEqual(swarm._shift(line, 4), "\033[32m…tive\033[0m  tail")
        self.assertEqual(swarm._shift(line, 0), line)
        self.assertEqual(swarm._shift("abc", 5), "")



# --------------------------------------------------------------------------- controls

# What a forged, old or other-backend row can carry in any text field: an OSC title set ended by
# BEL, a screen clear, a C1 CSI, a right-to-left override, NUL, and a newline (a forged line).
EVIL = "\x1b]0;pwn\x07\x1b[2J\x9b31m‮\x00\n[swarm] forged"
# No byte a terminal interprets as a command may be left in rendered board text.
FORBIDDEN = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f  ‪-‮⁦-⁩]")
OUR_SGR = re.compile(r"\x1b\[[0-9;]*m|\x1b\[K|\x1b\[J|\x1b\[H")


def tainted(value) -> str:
    return (value or "x") + EVIL


def untaint(value):
    return value.replace(EVIL, "") if isinstance(value, str) else value


class Tainted:
    """A board whose rows carry EVIL in every free-text field, as rows written before the
    write-time strip, by another backend or through the shared database role would. Job names
    passed back in are untainted, so lookups still find the job."""

    def __init__(self, inner, stop_on_wait: bool = False):
        self.inner, self.stop_on_wait = inner, stop_on_wait

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.inner.close()

    @staticmethod
    def _t(row, *fields):
        import dataclasses
        return dataclasses.replace(row, **{f: tainted(getattr(row, f)) for f in fields})

    def _msgs(self, rows):
        return [self._t(m, "job", "agent_name", "to_agent", "message") for m in rows]

    def recent_messages(self, *a, **kw):
        kw = {k: untaint(v) for k, v in kw.items()}
        return self._msgs(self.inner.recent_messages(*a, **kw))

    def messages_after(self, after_id, job=None):
        return self._msgs(self.inner.messages_after(after_id, untaint(job)))

    def read_unread(self, *a, **kw):
        import dataclasses
        res = self.inner.read_unread(*a, **kw)
        return dataclasses.replace(res, messages=self._msgs(res.messages))

    def agents(self, job, *a, **kw):
        return [self._t(r, "job", "name", "role", "current_tool", "harness", "model", "host")
                for r in self.inner.agents(untaint(job), *a, **kw)]

    def agent_events(self, since, job=None):
        return [self._t(e, "name", "job", "role") for e in self.inner.agent_events(since, untaint(job))]

    _JOB_TEXT = ("description", "task", "outcome", "created_by", "session_id", "project", "goal",
                 "verdict_reason", "verdict_by", "judge", "waiting_on", "closed_by")

    def job_status(self, job):
        js = self.inner.job_status(untaint(job))
        return None if js is None else self._t(js, *self._JOB_TEXT)

    def jobs(self, *a, **kw):
        return [self._t(j, "job", *self._JOB_TEXT) for j in self.inner.jobs(*a, **kw)]

    def restarts(self, job=None, *a, **kw):
        return self.inner.restarts(untaint(job), *a, **kw)

    def verification_counts(self, job):
        return self.inner.verification_counts(untaint(job))

    def transcripts(self, *a, **kw):
        return self.inner.transcripts(*a, **{k: untaint(v) for k, v in kw.items()})

    def now(self):
        """With stop_on_wait (tail): an hour back, so `tail` shows the agents' joins too."""
        import datetime as dt
        return self.inner.now() - (dt.timedelta(hours=1) if self.stop_on_wait else dt.timedelta(0))

    def wait_for_change(self, timeout):
        if self.stop_on_wait:
            raise KeyboardInterrupt
        return self.inner.wait_for_change(timeout)


class RenderControlsTests(Env):
    """Board text never reaches the terminal as control input: tail, watch and read show
    each control as visible notation, with and without colour."""

    def setUp(self):
        super().setUp()
        self.cli("activate", "--job", "J", "--description", "d")
        self.cli("join", "--job", "J", "--key", "a1")
        self.cli("join", "--job", "J", "--key", "a2")
        with self.board() as b:
            self.name = b.active_agent_name("a1")
            b.post("J", self.name, "hello", b.active_agent_name("a2"))
            b.set_waiting("J", "input")
        self.view = {"offset": 0, "wrap": False, "max_offset": 0, "all_agents": True,
                     "anchor": None, "scroll": 0, "mark": None, "page": 1, "recent_minutes": 10}

    def assert_clean(self, text: str, color: bool):
        if color:
            text = OUR_SGR.sub("", text)
        self.assertIsNone(FORBIDDEN.search(text), repr(text))
        self.assertIn(r"\x1b]0;pwn\x07", text)   # shown, not dropped
        self.assertIn(r"\u{202E}", text)

    def frame(self, job, color, wrap=False):
        self.view["wrap"] = wrap
        with Tainted(self.board()) as b, mock.patch("shutil.get_terminal_size",
                                                    return_value=os.terminal_size((400, 60))):
            return swarm._watch_frame(b, job, 2.0, color, True, self.view)

    def test_watch_frame_all_jobs(self):
        for color in (False, True):
            for wrap in (False, True):
                self.assert_clean("\n".join(self.frame(None, color, wrap)), color)

    def test_watch_frame_one_job(self):
        for color in (False, True):
            self.assert_clean("\n".join(self.frame("J", color)), color)

    def test_forged_newline_does_not_start_a_line(self):
        lines = self.frame(None, False)
        self.assertFalse(any(ln.startswith("[swarm] forged") for ln in lines))

    def tail(self, color, job):
        real_open = swarm_board.open_board
        out = io.StringIO()
        with mock.patch.object(swarm_board, "open_board",
                               lambda cfg, **kw: Tainted(real_open(cfg, **kw), stop_on_wait=True)), \
                contextlib.redirect_stdout(out):
            swarm.cmd_tail(self.cfg, job, 5, 0.01, True, color)
        return out.getvalue()

    def test_tail(self):
        for color in (False, True):
            for job in (None, "J"):
                out = self.tail(color, job)
                self.assertIn("hello", out)
                self.assertIn("*** joined", out)
                self.assert_clean(out, color)
                self.assertFalse(any(ln.startswith("[swarm] forged") for ln in out.splitlines()))

    def test_read(self):
        with Tainted(self.board()) as b, contextlib.redirect_stdout(io.StringIO()) as out:
            swarm._board_read(b, self.cfg, SimpleNamespace(key="a2", name=None, job="J", peek=True))
        self.assertIn("hello", out.getvalue())
        self.assert_clean(out.getvalue(), False)
        self.assertEqual(len(out.getvalue().strip().splitlines()), 1)

    def test_fmt_escapes_every_field(self):
        import datetime as dt
        from swarm.board.base import Message
        m = Message(1, dt.datetime.now(dt.timezone.utc), "J", tainted("Homer"), tainted("Bob"), tainted("hi"))
        text = swarm.fmt([m])
        self.assertEqual(len(text.splitlines()), 1)
        self.assert_clean(text, False)

    def test_msg_prefix_escapes_job_and_names(self):
        import datetime as dt
        for color in (False, True):
            text = swarm._msg_prefix(dt.datetime.now(dt.timezone.utc), tainted("J"), tainted("Homer"),
                                     tainted("Bob"), color)
            self.assert_clean(text, color)

    def test_clip_width_still_counts_only_our_colour_codes(self):
        line = swarm._paint_name("Homer", True) + ": " + swarm._paint_status("active", "active", True)
        self.assertEqual(swarm._visible_len(line), len("Homer: active"))
        self.assertEqual(swarm._visible_len(swarm._clip(line, 8)), 8)


if __name__ == "__main__":
    unittest.main()
