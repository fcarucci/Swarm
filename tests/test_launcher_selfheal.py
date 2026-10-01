"""The ~/.local/bin/swarm launcher falls back to the newest installed plugin when its target is gone."""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from support import ROOT  # noqa: F401

from swarm import bootstrap  # noqa: E402


class LauncherSelfHealTests(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="swarm-heal-"))
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        self.env = {"PATH": os.environ["PATH"], "HOME": str(self.home)}

    def _plugin(self, base: Path, market: str, version: str) -> Path:
        root = base / "plugins" / "cache" / market / "swarm" / version
        (root / "bin").mkdir(parents=True)
        s = root / "bin" / "swarm"
        s.write_text(f'#!/bin/sh\necho "ran {version} $*"\n')
        s.chmod(0o755)
        return root

    def _launcher(self, target: Path) -> Path:
        lp = self.home / "swarm"
        lp.write_text(bootstrap.LAUNCHER.format(root=target))
        lp.chmod(0o755)
        return lp

    def _run(self, lp, **extra):
        env = dict(self.env, **extra)
        return subprocess.run(["/bin/sh", str(lp), "hi", "there"], capture_output=True, text=True, env=env)

    def test_deleted_target_runs_newest_of_two_installed_versions(self):
        claude, codex = self.home / ".claude", self.home / ".codex"
        old = self._plugin(codex, "swarm", "0.1.7")
        self._plugin(claude, "swarm", "0.1.9")
        self._plugin(codex, "swarm", "0.1.10")          # 0.1.10 > 0.1.9 numerically, not as text
        gone = self.home / ".codex/plugins/cache/swarm/swarm/0.1.6"
        res = self._run(self._launcher(gone))
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.stdout.strip(), "ran 0.1.10 hi there")
        self.assertTrue(old.exists())

    def test_env_overrides_locate_the_caches(self):
        cdir, xdir = self.home / "cc", self.home / "cx"
        self._plugin(cdir, "m", "1.2.0")
        self._plugin(xdir, "m", "1.10.0")
        res = self._run(self._launcher(self.home / "gone"), CLAUDE_CONFIG_DIR=str(cdir), CODEX_HOME=str(xdir))
        self.assertEqual(res.stdout.strip(), "ran 1.10.0 hi there")

    def test_non_executable_target_also_falls_back(self):
        bad = self._plugin(self.home / ".claude", "swarm", "0.1.5")
        (bad / "bin" / "swarm").chmod(0o644)
        self._plugin(self.home / ".claude", "swarm", "0.1.4")
        res = self._run(self._launcher(bad))
        self.assertEqual(res.stdout.strip(), "ran 0.1.4 hi there")

    def test_live_target_is_used_even_if_older(self):
        live = self._plugin(self.home / ".claude", "swarm", "0.1.2")
        self._plugin(self.home / ".codex", "swarm", "0.1.9")
        res = self._run(self._launcher(live))
        self.assertEqual(res.stdout.strip(), "ran 0.1.2 hi there")

    def test_nothing_installed_prints_one_clear_line(self):
        res = self._run(self._launcher(self.home / "gone"))
        self.assertNotEqual(res.returncode, 0)
        self.assertEqual(len(res.stderr.strip().splitlines()), 1)
        self.assertIn("install.sh", res.stderr)

    def test_launcher_target_still_parses_new_script(self):
        lp = self._launcher(Path("/some/where/0.1.10"))
        self.assertEqual(bootstrap.launcher_target(lp), Path("/some/where/0.1.10"))

    def test_runs_under_dash_if_present(self):
        dash = shutil.which("dash")
        if not dash:
            self.skipTest("no dash")
        self._plugin(self.home / ".claude", "swarm", "0.1.3")
        r = subprocess.run([dash, str(self._launcher(self.home / "gone")), "x"], capture_output=True, text=True, env=self.env)
        self.assertEqual(r.stdout.strip(), "ran 0.1.3 x")


if __name__ == "__main__":
    unittest.main()
