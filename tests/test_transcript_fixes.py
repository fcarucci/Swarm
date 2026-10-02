"""Fixes from the 3566c5b end-to-end run: fractional MB limits, the over-limit warning (human
units, rate-limited), and the snapshot stamp keyed by board."""
from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from support import MemoryHarness, base_config, home_env, posix_only  # noqa: F401  (sets sys.path)

from swarm import cli as swarm  # noqa: E402
from swarm import transcripts as T  # noqa: E402


def cfg_with(**tc) -> dict:
    cfg = base_config()
    cfg["transcripts"] = dict(T.DEFAULTS, enabled=True, **tc)
    return cfg


class LimitNoteTest(unittest.TestCase):
    def test_fractional_and_zero_limits(self):
        self.assertEqual(swarm._limit_note(cfg_with(max_total_mb=0.04)), "0.04 MB/30d")
        self.assertEqual(swarm._limit_note(cfg_with(max_total_mb=1.5)), "1.5 MB/30d")
        self.assertEqual(swarm._limit_note(cfg_with(max_total_mb=2048)), "2048 MB/30d")
        self.assertEqual(swarm._limit_note(cfg_with(max_total_mb=0)), "no size limit/30d")
        self.assertEqual(swarm._keep_note(cfg_with(max_total_mb=0.04)), "30 days / up to 0.04 MB")
        self.assertEqual(swarm._keep_note(cfg_with(max_total_mb=0)), "30 days / no size limit")


class HomeTest(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="swarm-fix-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.home, ignore_errors=True))
        p = mock.patch.dict(os.environ, {**home_env(self.home)})
        p.start()
        self.addCleanup(p.stop)
        self.log = self.home / ".local/share/swarm/host/hook-errors.log"


class OverLimitWarningTest(HomeTest):
    def setUp(self):
        super().setUp()
        self.h = MemoryHarness(f"fix-warn-{id(self)}")
        self.h.reset()
        self.b = self.h.board()
        self.addCleanup(self.b.close)
        self.cfg = self.h.cfg
        self.cfg["transcripts"] = dict(T.DEFAULTS, enabled=True, max_total_mb=0.001)
        self.b.open_job("act", None, None, None, "me")
        self.b.save_transcript(T.make_row("act", "k", "n", "subagent", '{"x":"%s"}\n' % os.urandom(3000).hex()))

    def lines(self) -> list[str]:
        return self.log.read_text().splitlines() if self.log.exists() else []

    def test_human_units(self):
        warned = []
        T.rotate(self.b, self.cfg, warn=warned.append)
        self.assertRegex(warned[0], r"\d+\.\d KB stored by active jobs alone, over max_total_mb = 0\.001 \(1\.0 KB\)")

    def test_logged_once_until_the_active_jobs_change_or_an_hour_passes(self):
        for _ in range(5):
            T.rotate(self.b, self.cfg)
        self.assertEqual(len(self.lines()), 1)
        self.b.open_job("act2", None, None, None, "me")      # the set of active jobs changed
        T.rotate(self.b, self.cfg)
        self.assertEqual(len(self.lines()), 2)
        with mock.patch.object(T.time, "time", return_value=time.time() + 3700):
            T.rotate(self.b, self.cfg)
        self.assertEqual(len(self.lines()), 3)

    def test_explicit_warn_always_hears_it(self):
        warned = []
        T.rotate(self.b, self.cfg, warn=warned.append)
        T.rotate(self.b, self.cfg, warn=warned.append)
        self.assertEqual(len(warned), 2)


class SnapshotStampTest(HomeTest):
    def test_keyed_by_board(self):
        a, b = cfg_with(), cfg_with()
        a["board"]["backend"] = b["board"]["backend"] = "sqlite"
        a["sqlite"] = {"path": "/tmp/one.sqlite3"}
        b["sqlite"] = {"path": "/tmp/two.sqlite3"}
        sa, sb = T.snapshot_stamp(a), T.snapshot_stamp(b)
        self.assertNotEqual(sa, sb)
        self.assertEqual(sa.parent, self.home / ".local/share/swarm/host")
        self.assertIn("sqlite", sa.name)
        m1, m2 = cfg_with(), cfg_with()
        m1["memory"], m2["memory"] = {"store": "x"}, {"store": "y"}
        self.assertNotEqual(T.snapshot_stamp(m1), T.snapshot_stamp(m2))

    def test_a_round_on_one_board_does_not_delay_another(self):
        h1, h2 = MemoryHarness("fix-snap-1"), MemoryHarness("fix-snap-2")
        for h in (h1, h2):
            h.reset()
            h.cfg["transcripts"] = dict(T.DEFAULTS, enabled=True)
        with h1.board() as b1, h2.board() as b2:
            self.assertTrue(T.snapshot_due(b1, h1.cfg))
            T.run_snapshots(b1, h1.cfg)
            self.assertFalse(T.snapshot_due(b1, h1.cfg))
            self.assertTrue(T.snapshot_due(b2, h2.cfg))

    def test_the_old_shared_stamp_is_removed(self):
        old = self.home / ".local/state/swarm" / T.SNAPSHOT_STAMP
        old.parent.mkdir(parents=True)
        old.touch()
        h = MemoryHarness("fix-snap-old")
        h.reset()
        h.cfg["transcripts"] = dict(T.DEFAULTS, enabled=True)
        with h.board() as b:
            T.run_snapshots(b, h.cfg)
        self.assertFalse(old.exists())
        self.assertTrue(T.snapshot_stamp(h.cfg).exists())


class HostFileSafetyTest(HomeTest):
    """The transcript log, stamps and lost-rollouts list live in the host-private dir
    (~/.local/share/swarm/host, not a sandbox writable root) and are never followed through a
    planted symlink, hard link or FIFO."""

    def setUp(self):
        super().setUp()
        from swarm import paths, safefs
        from swarm.board.autoinit import store_key
        self.host = paths.host_dir()
        os.close(safefs.open_base(self.host, strict_mode=0o700))
        self.victim = self.home / "victim.pth"
        self.victim.write_text("original\n")
        self.h = MemoryHarness(f"fix-safety-{id(self)}")
        self.h.reset()
        self.cfg = self.h.cfg
        self.cfg["transcripts"] = dict(T.DEFAULTS, enabled=True, max_total_mb=0.001)
        self.key = store_key(self.cfg)

    def plant(self, name: str) -> Path:
        """name -> victim in the host dir and in the old, sandbox-writable state dir."""
        old = self.home / ".local/state/swarm"
        old.mkdir(parents=True, exist_ok=True)
        (old / name).symlink_to(self.victim)
        p = self.host / name
        p.symlink_to(self.victim)
        return p

    def assert_victim_untouched(self):
        self.assertEqual(self.victim.read_text(), "original\n")

    @posix_only("needs POSIX file modes (Windows has ACLs)")
    def test_log_is_one_escaped_line_in_the_host_dir(self):
        T.log("x\nimport os; os.system('id')\r\x1b[2J")
        lines = self.log.read_text().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertIn("x\\x0aimport os", lines[0])
        self.assertNotIn("\x1b", lines[0])
        self.assertEqual(oct(self.log.stat().st_mode & 0o777), "0o600")
        self.assertFalse((self.home / ".local/state/swarm/hook-errors.log").exists())

    def test_log_symlink_not_followed(self):
        self.plant("hook-errors.log")
        T.log("x\nimport os\n#")
        self.assert_victim_untouched()
        dangling = self.home / "created-by-log"
        for d in (self.host, self.home / ".local/state/swarm"):
            (d / "hook-errors.log").unlink()
            (d / "hook-errors.log").symlink_to(dangling)
        T.log("y")
        self.assertFalse(dangling.exists())

    @posix_only("needs os.mkfifo (POSIX FIFOs)")
    def test_log_hard_link_and_fifo_refused(self):
        os.link(self.victim, self.host / "hook-errors.log")
        T.log("z")
        self.assert_victim_untouched()
        (self.host / "hook-errors.log").unlink()
        os.mkfifo(self.host / "hook-errors.log")
        self.assertTrue(self._returns_quickly(lambda: T.log("fifo")))

    def test_a_symlinked_host_dir_is_refused(self):
        elsewhere = self.home / "elsewhere"
        elsewhere.mkdir(mode=0o700)
        os.rmdir(self.host)
        self.host.symlink_to(elsewhere)
        T.log("q")
        with self.h.board() as b:
            self.assertFalse(T.snapshot_due(b, self.cfg))
            T.run_snapshots(b, self.cfg)
        self.assertEqual(os.listdir(elsewhere), [])

    def test_over_limit_stamp_symlink_not_truncated(self):
        self.plant(f"transcripts-over-limit-{self.key}")
        with self.h.board() as b:
            b.open_job("act", None, None, None, "me")
            b.save_transcript(T.make_row("act", "k", "n", "subagent", '{"x":"%s"}\n' % os.urandom(3000).hex()))
            T.rotate(b, self.cfg)
            T.rotate(b, self.cfg)
        self.assert_victim_untouched()
        self.assertEqual(len(self.log.read_text().splitlines()), 1)   # still rate-limited

    def test_snapshot_stamp_symlink_not_followed(self):
        stamp = self.plant(f"transcripts-snapshot-{self.key}.stamp")
        old = time.time() - 7200
        os.utime(self.victim, (old, old))
        with self.h.board() as b:
            T.run_snapshots(b, self.cfg)
            T.snapshot_due(b, self.cfg)
        self.assert_victim_untouched()
        self.assertLess(self.victim.stat().st_mtime, time.time() - 3600)
        self.assertTrue(stamp.is_symlink() or not stamp.exists() or stamp.is_file())

    @posix_only("needs os.mkfifo (POSIX FIFOs)")
    def test_snapshot_stamp_fifo_does_not_block(self):
        old = self.home / ".local/state/swarm"
        old.mkdir(parents=True, exist_ok=True)
        for d in (old, self.host):
            os.mkfifo(d / f"transcripts-snapshot-{self.key}.stamp")
        with self.h.board() as b:
            self.assertTrue(self._returns_quickly(lambda: T.run_snapshots(b, self.cfg)))

    def test_lost_rollouts_symlink_neither_read_nor_written(self):
        self.victim.write_text("J\tforged\n")
        f = T._lost_rollouts_file(self.cfg)
        self.assertEqual(f.parent, self.host)
        f.symlink_to(self.victim)
        self.assertEqual(T._read_lost(f), set())
        T._write_lost(f, {"J\tk1"})
        self.assertEqual(self.victim.read_text(), "J\tforged\n")
        self.assertEqual(T._read_lost(f), {"J\tk1"})
        self.assertFalse(f.is_symlink())

    @staticmethod
    def _returns_quickly(fn, timeout: float = 5.0) -> bool:
        import threading
        t = threading.Thread(target=fn, daemon=True)
        t.start()
        t.join(timeout)
        return not t.is_alive()


if __name__ == "__main__":
    unittest.main()
