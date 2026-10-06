"""_Recorder/_Replay against the real frames: every key and a resize render from the snapshot
with no board call. A frame change that put something time-dependent into a query argument would
turn each render into a SnapshotMiss (a refresh per render), and fails here."""
from __future__ import annotations

import datetime as dt
import unittest
from unittest import mock

from support import MemoryHarness  # noqa: F401  (sets sys.path)

from swarm import cli as swarm  # noqa: E402

SESSION = "11111111-aaaa-bbbb-cccc-000000000001"


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.h = MemoryHarness("watch-snapshot")
        self.addCleanup(self.h.close)
        self.h.reset()
        self.board = self.h.board()
        self.addCleanup(self.board.close)
        self.board.ensure_job("J1", "d")
        self.board.bind_job_session("J1", SESSION)
        for i in range(40):
            self.board.post("J1", "Alice", f"message number {i} " + "word " * 30)

    def view(self, compact):
        return {"offset": 0, "wrap": False, "max_offset": 0, "all_agents": False, "anchor": None,
                "scroll": 0, "mark": None, "page": 1, "recent_minutes": 60, "db_label": None,
                "session": SESSION, "compact": compact, "idle_exit": None}

    def snapshot(self, compact):
        view = self.view(compact)
        with mock.patch("shutil.get_terminal_size", return_value=(60, 40)):
            return swarm._take_snapshot(
                self.board, lambda b: swarm._watch_frame(b, None, 2.0, False, True, dict(view)), view, None)

    def render(self, snap, view, size):
        with mock.patch("shutil.get_terminal_size", return_value=size):
            return swarm._watch_frame(swarm._Replay(snap), None, 2.0, False, True, view)

    def test_postgres_json_waiting_since_is_a_datetime(self):
        import dataclasses
        from swarm.watchdata import SnapshotBoard
        self.board.set_waiting("J1", "30-minute watch")
        job = self.board.job_status("J1")
        data = dataclasses.asdict(job)
        data = {k: v.isoformat() if isinstance(v, dt.datetime) else v for k, v in data.items()}
        snapshot = SnapshotBoard(self.board, [self.board.now(), [data], [], [], [], {}, {}, [], {}])
        self.assertEqual(snapshot.job_status("J1").waiting_since, job.waiting_since)
        self.assertIn("waiting", swarm.jobs_overview(snapshot, False, False))

    def test_every_key_and_a_resize_render_from_the_snapshot(self):
        for compact in (True, False):
            snap = self.snapshot(compact)
            calls = mock.Mock(side_effect=AssertionError("board queried"))
            snap._board = mock.Mock(**{"now.side_effect": calls})   # a Replay never goes near it
            for keys in ("\x1b[C", "\x1b[D", "w", "a", "v", "0", "$"):
                view = self.view(compact)
                swarm._apply_keys(keys, view)
                self.assertTrue(self.render(snap, view, (60, 40)), (compact, keys))
            for size in ((60, 25), (80, 60), (40, 10)):
                self.assertTrue(self.render(snap, self.view(compact), size), (compact, size))

    def test_history_keys_render_from_the_snapshot_in_the_full_view(self):
        snap = self.snapshot(False)
        for keys in ("\x1b[A", "\x1b[5~", "k" * 5, "\x1b[5~\x1b[5~"):
            view = self.view(False)
            swarm._apply_keys(keys, view)
            lines = self.render(snap, view, (60, 40))
            self.assertTrue(any("message number" in ln for ln in lines), keys)

    def test_a_shorter_message_window_is_a_slice_of_the_recorded_one(self):
        snap = self.snapshot(True)
        replay = swarm._Replay(snap)
        full = replay.recent_messages(60, job="J1")
        self.assertEqual(replay.recent_messages(5, job="J1"), full[-5:])

    def test_unrecorded_questions_miss(self):
        replay = swarm._Replay(self.snapshot(True))
        with self.assertRaises(swarm.SnapshotMiss):
            replay.recent_messages(5000, job="J1")
        with self.assertRaises(swarm.SnapshotMiss):
            replay.agents("no-such-job")

    def test_replay_clock_runs_from_the_start_of_the_refresh(self):
        snap = self.snapshot(True)
        t = snap.taken_mono
        with mock.patch.object(swarm.time, "monotonic", return_value=t + 7.0):
            self.assertEqual(swarm._Replay(snap).now() - snap.taken, dt.timedelta(seconds=7))

    def test_replay_hands_out_copies(self):
        replay = swarm._Replay(self.snapshot(True))
        replay.recent_messages(5, job="J1").clear()
        self.assertEqual(len(replay.recent_messages(5, job="J1")), 5)


if __name__ == "__main__":
    unittest.main()
