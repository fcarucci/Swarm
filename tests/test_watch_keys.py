"""watch is event driven: a key is dispatched by the keymap and rendered from the cached snapshot,
with no board call on the input path, even while a refresh (a slow query) is still running."""
from __future__ import annotations

import io
import os
import threading
import time
import unittest

from support import ROOT  # noqa: F401  (sets sys.path)

from swarm import cli as swarm  # noqa: E402


class SlowBoard:
    """subscribe/wait_for_change/now like a board; `query` blocks while `gate` is cleared."""

    degraded = None

    def __init__(self):
        self.gate = threading.Event()
        self.gate.set()
        self.queries = 0

    def subscribe(self, messages_only=False):
        pass

    def wait_for_change(self, timeout):
        time.sleep(min(timeout, 0.02))
        return False

    def now(self):
        import datetime as dt
        return dt.datetime.now(dt.timezone.utc)

    def query(self):
        self.queries += 1
        self.gate.wait(10)
        return ["row"]


class KeymapTests(unittest.TestCase):
    def test_keymap_is_declarative_and_every_action_has_a_handler(self):
        for seq, action in swarm.WATCH_KEYS:
            self.assertIn(action, swarm.VIEW_ACTIONS if action != "quit" else {"quit": 1}, seq)

    def test_right_and_left_move_the_offset(self):
        view = {"offset": 0, "max_offset": 100}
        self.assertTrue(swarm._apply_keys("\x1b[C", view))
        self.assertGreater(view["offset"], 0)
        swarm._apply_keys("\x1b[D", view)
        self.assertEqual(view["offset"], 0)
        self.assertFalse(swarm._apply_keys("q", view))


class EventLoopTests(unittest.TestCase):
    def run_loop(self, script):
        board = SlowBoard()
        view = {"offset": 0, "max_offset": 100}
        rfd, wfd = os.pipe()
        out = io.StringIO()
        out.flush = lambda: None
        draws = []

        def refresh():
            return {"rows": board.query()}

        def draw(snap):
            draws.append((view["offset"], board.queries))
            snap.now()   # replay answers locally
            return [f"offset={view['offset']}"]

        result = {}

        def loop():
            try:
                swarm._watch_loop(board, out, rfd, 60.0, view, draw,
                                  lambda: _Snap(refresh()))
            except BaseException as exc:  # pragma: no cover
                result["error"] = exc
        t = threading.Thread(target=loop)
        t.start()
        try:
            script(board, view, wfd, out, draws)
        finally:
            board.gate.set()
            os.write(wfd, b"q")
            t.join(5)
            os.close(rfd)
            os.close(wfd)
        self.assertFalse(t.is_alive())
        self.assertNotIn("error", result)

    def wait_for(self, cond, what, limit=3.0):
        end = time.monotonic() + limit
        while time.monotonic() < end:
            if cond():
                return
            time.sleep(0.005)
        self.fail(what)

    def test_key_renders_at_once_while_a_refresh_is_blocked_in_a_query(self):
        def script(board, view, wfd, out, draws):
            self.wait_for(lambda: draws, "first frame")
            board.gate.clear()                      # the next refresh will hang in its query
            os.write(wfd, b"a")                     # any key: wakes a refresh that blocks
            self.wait_for(lambda: board.queries >= 2, "refresh started")
            n, queries = len(draws), board.queries
            t0 = time.monotonic()
            os.write(wfd, b"\x1b[C")                # Right
            self.wait_for(lambda: len(draws) > n and draws[-1][0] > 0, "render after key")
            self.assertLess(time.monotonic() - t0, 0.2)
            self.assertEqual(board.queries, queries)   # the render made no query
            self.assertIn("offset=", out.getvalue())

        self.run_loop(script)


class _Snap(swarm._Recorder):
    def __init__(self, data):
        class B:
            def now(self_inner):
                import datetime as dt
                return dt.datetime.now(dt.timezone.utc)
        super().__init__(B())


if __name__ == "__main__":
    unittest.main()
