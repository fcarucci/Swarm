"""A closed drive job deletes its launch homes, deterministically (2026-10-15).
Run: cd tests && python3 -m unittest -v test_launch_home_cleanup
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))
from support import SANDBOX  # noqa: E402  (canonical temp root, including macOS /private/var)
from swarm.supervisor import launchhome  # noqa: E402


class TestRemoveJobHomes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=SANDBOX)
        self.addCleanup(self.tmp.cleanup)
        self.priv = Path(self.tmp.name) / "supervisor"
        p = mock.patch("swarm.supervisor.settings.private_dir", return_value=self.priv)
        p.start()
        self.addCleanup(p.stop)

    def _launch(self, job, launch, size=1000):
        d = launchhome.home_dir(job, launch) / "home"
        d.mkdir(parents=True)
        (d / "cache.bin").write_bytes(b"x" * size)
        return d

    def test_removes_only_the_closed_jobs_homes(self):
        self._launch("J", "lead", 1000)
        self._launch("J", "eng", 2000)
        keep = self._launch("OTHER", "lead", 10)
        freed = launchhome.remove_job_homes("J")
        self.assertEqual(freed, 3000)
        self.assertFalse((self.priv / launchhome.LAUNCH_HOME / launchhome._slug("J")).exists())
        self.assertTrue(keep.exists(), "another job's homes are untouched")

    def test_nothing_to_remove_is_zero(self):
        self.assertEqual(launchhome.remove_job_homes("NONE"), 0)

    def test_legacy_home_moves_once_and_keeps_contents(self):
        old = self.priv / ("se" "at-home")
        cache = old / launchhome._slug("J") / launchhome._slug("lead") / "home"
        cache.mkdir(parents=True)
        self.priv.chmod(0o700)
        (cache / "transcript").write_text("keep")
        first = launchhome.home_dir("J", "lead")
        self.assertFalse(old.exists())
        self.assertEqual((first / "home" / "transcript").read_text(), "keep")
        self.assertEqual(launchhome.home_dir("J", "lead"), first)

    def test_legacy_home_never_overwrites_current_home(self):
        old = self.priv / ("se" "at-home")
        old.mkdir(parents=True)
        self.priv.chmod(0o700)
        (self.priv / launchhome.LAUNCH_HOME).mkdir()
        with self.assertRaises(launchhome.LaunchIsolationError):
            launchhome.migrate_home()
        self.assertTrue(old.exists())

    def test_prepare_migrates_before_creating_current_home(self):
        old = self.priv / ("se" "at-home")
        cache = old / launchhome._slug("J") / launchhome._slug("lead") / "home"
        cache.mkdir(parents=True)
        for directory in (self.priv, old, cache.parent.parent, cache.parent, cache):
            directory.chmod(0o700)
        (cache / "transcript").write_text("keep")
        cfg = {"drive": {"launch_git_name": "Engineer", "launch_git_email": "engineer@example.test"}}
        current = launchhome.prepare(cfg, "J", "lead")
        self.assertFalse(old.exists())
        self.assertEqual((current / "home" / "transcript").read_text(), "keep")

    def test_legacy_config_keys_and_explicit_new_values(self):
        old = "se" "at_"
        legacy = {"drive": {old + "ro_paths": ["/read"], old + "rw_paths": ["/write"],
                            old + "ssh_key": "/key", old + "git_name": "Legacy",
                            old + "git_email": "legacy@example.test"}}
        self.assertEqual(launchhome._list(legacy, "launch_ro_paths"), [os.path.normpath("/read")])
        self.assertEqual(launchhome._list(legacy, "launch_rw_paths"), [os.path.normpath("/write")])
        self.assertIn('/key', launchhome.ssh_config_text(legacy))
        self.assertEqual(launchhome._git_identity(legacy), ("Legacy", "legacy@example.test"))
        legacy["drive"].update(launch_ro_paths=[], launch_rw_paths=[], launch_ssh_key="",
                               launch_git_name="Current", launch_git_email="current@example.test")
        self.assertEqual(launchhome._list(legacy, "launch_ro_paths"), [])
        self.assertEqual(launchhome._list(legacy, "launch_rw_paths"), [])
        self.assertNotIn('/key', launchhome.ssh_config_text(legacy))
        self.assertEqual(launchhome._git_identity(legacy), ("Current", "current@example.test"))

    def test_a_symlink_is_unlinked_never_followed(self):
        outside = Path(self.tmp.name) / "outside"
        outside.mkdir()
        (outside / "precious").write_text("keep")
        root = self.priv / launchhome.LAUNCH_HOME
        root.mkdir(parents=True)
        os.symlink(outside, root / launchhome._slug("J"))
        self.assertEqual(launchhome.remove_job_homes("J"), 0)
        self.assertFalse((root / launchhome._slug("J")).exists())
        self.assertTrue((outside / "precious").exists())


if __name__ == "__main__":
    unittest.main()
