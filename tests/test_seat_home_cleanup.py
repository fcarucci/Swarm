"""A closed drive job deletes its seat homes, deterministically (2026-10-15).
Run: cd tests && python3 -m unittest -v test_seat_home_cleanup
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))
from swarm.supervisor import seathome  # noqa: E402


class TestRemoveJobHomes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.priv = Path(self.tmp.name) / "supervisor"
        p = mock.patch("swarm.supervisor.settings.private_dir", return_value=self.priv)
        p.start()
        self.addCleanup(p.stop)

    def _seat(self, job, seat, size=1000):
        d = seathome.home_dir(job, seat) / "home"
        d.mkdir(parents=True)
        (d / "cache.bin").write_bytes(b"x" * size)
        return d

    def test_removes_only_the_closed_jobs_homes(self):
        self._seat("J", "lead", 1000)
        self._seat("J", "eng", 2000)
        keep = self._seat("OTHER", "lead", 10)
        freed = seathome.remove_job_homes("J")
        self.assertEqual(freed, 3000)
        self.assertFalse((self.priv / seathome.SEAT_HOME / seathome._slug("J")).exists())
        self.assertTrue(keep.exists(), "another job's homes are untouched")

    def test_nothing_to_remove_is_zero(self):
        self.assertEqual(seathome.remove_job_homes("NONE"), 0)

    def test_a_symlink_is_unlinked_never_followed(self):
        outside = Path(self.tmp.name) / "outside"
        outside.mkdir()
        (outside / "precious").write_text("keep")
        root = self.priv / seathome.SEAT_HOME
        root.mkdir(parents=True)
        os.symlink(outside, root / seathome._slug("J"))
        self.assertEqual(seathome.remove_job_homes("J"), 0)
        self.assertFalse((root / seathome._slug("J")).exists())
        self.assertTrue((outside / "precious").exists())


if __name__ == "__main__":
    unittest.main()
