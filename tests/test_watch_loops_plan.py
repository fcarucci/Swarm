"""Characterization of the watch loops with a _RefreshPlan (pinned before the complexity refactor):
which refresh kinds the plain and the threaded loop ask for, when, what a None result (a matching
fingerprint check) leaves on screen, and how a SnapshotMiss is requested."""
from __future__ import annotations

import io
import unittest
from unittest import mock

from support import MemoryHarness  # noqa: F401  (sets sys.path)

from swarm import cli  # noqa: E402


class Loops(unittest.TestCase):
    def setUp(self):
        self.h = MemoryHarness("watch-loops-plan")
        self.addCleanup(self.h.close)
        self.h.reset()
        self.board = self.h.board()
        self.addCleanup(self.board.close)
        self.now = [0.0]
        self.kinds: list[tuple[float, str]] = []

    def refresh_fn(self, results):
        def refresh(kind):
            self.kinds.append((round(self.now[0], 1), kind))
            return results.pop(0) if results else None
        return refresh

    def test_plain_loop_kinds_and_none_keeps_the_screen(self):
        out = io.StringIO()
        plan = cli._RefreshPlan(interval=10, check_s=5, minimum=2, clock=lambda: self.now[0], push_fn=lambda: True)
        wakes = {"n": 0}

        def wait(timeout):
            self.now[0] = round(self.now[0] + .5, 1)
            wakes["n"] += 1
            return 4 <= self.now[0] <= 4.5          # one notification burst around t=4
        keys = iter([None] * 80 + ["q"])
        drawn = []
        with mock.patch.object(self.board, "wait_for_change", side_effect=wait), \
             mock.patch.object(self.board, "subscribe"), \
             mock.patch.object(cli.time, "monotonic", side_effect=lambda: self.now[0]), \
             mock.patch.object(cli, "_read_keys", side_effect=lambda fd: next(keys)):
            cli._watch_loop(self.board, out, None, 10, {"min_redraw": 2},
                            lambda snap: drawn.append(1) or ["frame"],
                            self.refresh_fn(["S1", "S2"]), plan)
        self.assertEqual(self.kinds[:4], [(0.0, "full"), (4.0, "incremental"), (6.0, "incremental"), (10.0, "check")])
        self.assertGreaterEqual(len(drawn), 3)      # at least the first draw, the incremental, the ageing redraws

    def test_plain_loop_snapshot_miss_asks_for_a_full_read(self):
        plan = cli._RefreshPlan(interval=10, check_s=60, minimum=2, clock=lambda: self.now[0], push_fn=lambda: True)
        calls = {"n": 0}

        def draw(snap):
            calls["n"] += 1
            if calls["n"] == 2:
                raise cli.SnapshotMiss("history")
            return ["frame"]

        def wait(timeout):
            self.now[0] = round(self.now[0] + 1, 1)
            return False
        keys = iter([None] * 7 + ["q"])
        with mock.patch.object(self.board, "wait_for_change", side_effect=wait), \
             mock.patch.object(self.board, "subscribe"), \
             mock.patch.object(cli.time, "monotonic", side_effect=lambda: self.now[0]), \
             mock.patch.object(cli, "_read_keys", side_effect=lambda fd: next(keys)):
            cli._watch_loop(self.board, io.StringIO(), None, 3, {"min_redraw": 2}, draw,
                            self.refresh_fn(["S1", "S2", "S3"]), plan)
        self.assertEqual([k for _, k in self.kinds[:2]], ["full", "full"])

    def test_threaded_worker_asks_plan_for_kinds(self):
        plan = cli._RefreshPlan(interval=10, check_s=5, minimum=2, clock=lambda: self.now[0], push_fn=lambda: True)

        def wait(timeout):
            self.now[0] = round(self.now[0] + .5, 1)
            if self.now[0] > 12:
                raise RuntimeError("clock finished")
            return self.now[0] in (3.0, 3.5)
        with mock.patch.object(self.board, "wait_for_change", side_effect=wait), \
             mock.patch.object(cli.time, "monotonic", side_effect=lambda: self.now[0]):
            with self.assertRaisesRegex(RuntimeError, "clock finished"):
                cli._watch_loop_threaded(self.board, io.StringIO(), None, 10, {"min_redraw": 2},
                                         lambda snap: ["x"], self.refresh_fn(["S1", None, "S3"]), plan)
        self.assertEqual(self.kinds, [(0.5, "full"), (3.0, "incremental"), (5.0, "incremental"), (10.5, "check")])


if __name__ == "__main__":
    unittest.main()
