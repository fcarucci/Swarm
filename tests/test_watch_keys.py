"""watch is event driven: a key is dispatched by the keymap and rendered from the last snapshot, with
no board call on the input path, even while a refresh (a slow query) is in flight. The behavioural
tests run the real `cmd_watch` on a pty, so against the old single-thread loop they fail on the
assertion (the key waits for the query), not on an import."""
from __future__ import annotations

import io
import os
import sys
import threading
import time
import unittest
from unittest import mock

from support import MemoryHarness  # noqa: F401  (also sets sys.path)

from swarm import cli as swarm  # noqa: E402
from swarm.board import BoardUnavailable  # noqa: E402
from swarm.board.memory import MemoryBoard  # noqa: E402

JOB = "ABCDEFGH-" + "x" * 80
SESSION = "11111111-aaaa-bbbb-cccc-000000000001"


WIN = "Windows has no pty (os.openpty) and cannot select() on a pipe or stdin; cmd_watch is not interactive there (fd None) and uses the plain redraw loop"


@unittest.skipIf(sys.platform == "win32", WIN)
class PtyWatch(unittest.TestCase):
    """cmd_watch --compact --session on a pty; recent_messages blocks on `gate` once it is cleared."""

    def setUp(self):
        self.h = MemoryHarness("watch-keys")
        self.addCleanup(self.h.close)
        self.h.reset()
        with self.h.board() as b:
            b.ensure_job(JOB, "d")
            b.bind_job_session(JOB, SESSION)
        self.gate = threading.Event()
        self.gate.set()
        real = MemoryBoard.recent_messages
        gate = self.gate

        def slow(board, *a, **k):
            gate.wait(20)
            return real(board, *a, **k)
        for p in (mock.patch.object(MemoryBoard, "recent_messages", slow),
                  mock.patch.object(swarm, "Sweeper", lambda cfg, *a, **k: (lambda board: [])),
                  mock.patch.dict(os.environ, {"COLUMNS": "60", "LINES": "40"})):
            p.start()
            self.addCleanup(p.stop)
        self.master, slave = os.openpty()
        self.stdin = os.fdopen(os.dup(slave), "r")
        self.stdout = os.fdopen(slave, "w")
        self.addCleanup(self.stdin.close)
        self.addCleanup(self.stdout.close)
        self.addCleanup(os.close, self.master)
        self.seen = b""
        self.result = {}

        def run():
            with mock.patch("sys.stdin", self.stdin), mock.patch("sys.stdout", self.stdout):
                self.result["rc"] = swarm.cmd_watch(self.h.cfg, None, 60.0, False, session=SESSION, compact=True)
        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()
        self.addCleanup(self.shutdown)

    def shutdown(self):
        import select
        self.gate.set()
        if self.thread.is_alive():
            os.write(self.master, b"q")
        end = time.monotonic() + 5
        while self.thread.is_alive() and time.monotonic() < end:   # drain: tcsetattr(DRAIN) waits for the reader
            if select.select([self.master], [], [], 0.01)[0]:
                os.read(self.master, 65536)

    def frames(self):
        return [f for f in self.seen.split(b"\x1b[H")[1:] if f.endswith(b"\x1b[J")]

    def wait_screen(self, pred, limit, what):
        end = time.monotonic() + limit
        import select
        while time.monotonic() < end:
            if select.select([self.master], [], [], 0.01)[0]:
                self.seen += os.read(self.master, 65536)
            fr = self.frames()
            if fr and pred(fr[-1]):
                return time.monotonic()
        self.fail(f"{what}: no such frame within {limit}s; last: {self.frames()[-1:]}")

    def type(self, data: bytes):
        os.write(self.master, data)

    def test_key_shifts_the_view_at_once_while_a_query_is_in_flight(self):
        self.wait_screen(lambda f: b"ABCDEFGH" in f, 10, "first frame")
        self.gate.clear()                                    # the next refresh hangs in its query
        with self.h.board() as b:
            b.post(JOB, "Alice", "wakes the refresh")        # a change: refresh (old code: redraw) starts
        time.sleep(0.3)
        self.seen = b""
        t0 = time.monotonic()
        self.type(b"\x1b[C")                                 # Right
        t1 = self.wait_screen(lambda f: b"ABCDEFGH" not in f and b"xxxx" in f, 3, "shifted frame")
        self.assertLess(t1 - t0, 0.3)

    def test_quit_does_not_wait_for_a_query_in_flight(self):
        self.wait_screen(lambda f: b"ABCDEFGH" in f, 10, "first frame")
        self.gate.clear()
        with self.h.board() as b:
            b.post(JOB, "Alice", "wakes the refresh")
        time.sleep(0.3)
        t0 = time.monotonic()
        self.type(b"q")
        import select
        end = time.monotonic() + 3
        while self.thread.is_alive() and time.monotonic() < end:   # drain: tcsetattr(DRAIN) waits for the reader
            if select.select([self.master], [], [], 0.01)[0]:
                os.read(self.master, 65536)
        self.assertFalse(self.thread.is_alive())
        self.assertEqual(self.result.get("rc"), 0)


class KeymapTests(unittest.TestCase):
    def test_keymap_is_declarative_and_every_action_has_a_handler(self):
        for seq, action in swarm.WATCH_KEYS:
            self.assertTrue(action == "quit" or action in swarm.VIEW_ACTIONS, seq)

    def test_right_and_left_move_the_offset(self):
        view = {"offset": 0, "max_offset": 100}
        self.assertTrue(swarm._apply_keys("\x1b[C", view))
        self.assertGreater(view["offset"], 0)
        swarm._apply_keys("\x1b[D", view)
        self.assertEqual(view["offset"], 0)
        self.assertFalse(swarm._apply_keys("q", view))


@unittest.skipIf(sys.platform == "win32", WIN)
class LoopTests(unittest.TestCase):
    """_watch_loop_threaded with a stub board and refresh."""

    class Board:
        degraded = None

        def subscribe(self, messages_only=False):
            pass

        def wait_for_change(self, timeout):
            time.sleep(min(timeout, 0.02))
            return False

    def run_loop(self, refresh, draw=None, keys=b"", quit_after=0.5):
        rfd, wfd = os.pipe()
        out = io.StringIO()
        out.flush = lambda: None
        view = {"offset": 0, "max_offset": 100}
        err = {}

        def loop():
            try:
                swarm._watch_loop(self.Board(), out, rfd, 60.0, view, draw or (lambda snap: ["x"]), refresh)
            except BaseException as exc:
                err["e"] = exc
        t = threading.Thread(target=loop)
        t.start()
        time.sleep(quit_after)
        if keys:
            os.write(wfd, keys)
        if t.is_alive():
            os.write(wfd, b"q")
        t.join(5)
        os.close(rfd)
        os.close(wfd)
        self.assertFalse(t.is_alive())
        return out.getvalue(), err.get("e")

    def test_board_unavailable_in_the_refresh_reaches_the_caller(self):
        def refresh():
            raise BoardUnavailable("gone")
        _out, err = self.run_loop(refresh, quit_after=0.3)
        self.assertIsInstance(err, BoardUnavailable)

    def test_other_refresh_errors_propagate_too(self):
        def refresh():
            raise RuntimeError("boom")
        _out, err = self.run_loop(refresh, quit_after=0.3)
        self.assertIsInstance(err, RuntimeError)

    def test_snapshot_miss_requests_a_refresh_and_keeps_the_old_frame(self):
        n = {"refreshes": 0, "draws": 0}

        def refresh():
            n["refreshes"] += 1
            return swarm._Recorder(mock.Mock(now=lambda: __import__("datetime").datetime.now(__import__("datetime").timezone.utc)))

        def draw(snap):
            n["draws"] += 1
            if n["refreshes"] < 3:
                raise swarm.SnapshotMiss("recent_messages")
            return ["fresh"]
        out, err = self.run_loop(refresh, draw, quit_after=1.0)
        self.assertIsNone(err)
        self.assertGreaterEqual(n["refreshes"], 3)       # each miss woke a refresh
        self.assertIn("fresh", out)

    def test_a_key_does_not_trigger_a_refresh(self):
        n = {"refreshes": 0}

        def refresh():
            n["refreshes"] += 1
            return swarm._Recorder(mock.Mock(now=lambda: __import__("datetime").datetime.now(__import__("datetime").timezone.utc)))
        self.run_loop(refresh, keys=b"\x1b[C\x1b[D\x1b[C", quit_after=0.5)
        self.assertEqual(n["refreshes"], 1)              # the first, at start; keys added none


if __name__ == "__main__":
    unittest.main()
