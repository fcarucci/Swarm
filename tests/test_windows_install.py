"""Windows install layout: the swarm.cmd launcher text and parser, the venv interpreter path, the
channel step of bootstrap. The last class runs the real launcher and only on Windows."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from support import ROOT  # noqa: F401

from swarm import bootstrap, channel, paths


class LauncherCmdTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="swarm-wininst-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_target_round_trips_even_with_spaces(self):
        root = r"C:\Users\First Last\.claude\plugins\cache\swarm\swarm\0.1.13"
        f = self.tmp / "swarm.cmd"
        f.write_text(bootstrap.LAUNCHER_CMD.format(root=root), newline="")
        self.assertEqual(str(bootstrap.launcher_target(f)), root)

    def test_launcher_is_crlf_and_has_the_heal_fallback(self):
        text = bootstrap.LAUNCHER_CMD.format(root=r"C:\p")
        self.assertNotIn("\n", text.replace("\r\n", ""))
        self.assertIn(":heal", text)
        self.assertIn("exit /b 127", text)
        self.assertIn("rerun install.ps1", text)

    def test_sh_launcher_still_parses(self):
        f = self.tmp / "swarm"
        f.write_text(bootstrap.LAUNCHER.format(root="/opt/p"))
        self.assertEqual(bootstrap.launcher_target(f), Path("/opt/p"))

    def test_the_heal_line_is_not_taken_for_the_target(self):
        f = self.tmp / "swarm.cmd"
        f.write_text(bootstrap.LAUNCHER_CMD.format(root=r"C:\p"), newline="")
        self.assertEqual(str(bootstrap.launcher_target(f)), r"C:\p")


class VenvPathTests(unittest.TestCase):
    def test_posix_interpreter_is_bin_python(self):
        with mock.patch.object(paths, "IS_WINDOWS", False):
            self.assertEqual(paths.venv_python(Path("/v")), Path("/v/bin/python"))

    def test_windows_interpreter_is_scripts_python_exe(self):
        tmp = Path(tempfile.mkdtemp(prefix="swarm-wininst-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        with mock.patch.object(paths, "IS_WINDOWS", True):
            self.assertEqual(paths.venv_python(tmp), tmp / "Scripts" / "python.exe")
            (tmp / "bin").mkdir()
            (tmp / "bin" / "python.exe").write_text("")      # a venv made by an MSYS python
            self.assertEqual(paths.venv_python(tmp), tmp / "bin" / "python.exe")

    def test_windows_launcher_and_agent_bin_are_cmd_files(self):
        with mock.patch.object(paths, "IS_WINDOWS", True):
            self.assertEqual(paths.launcher_path().name, "swarm.cmd")
            self.assertEqual(paths.agent_bin().name, "swarm.cmd")


class ChannelStepTests(unittest.TestCase):
    def test_bootstrap_records_the_channel_from_the_installer(self):
        tmp = Path(tempfile.mkdtemp(prefix="swarm-wininst-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        cfg = tmp / "config.toml"
        cfg.write_text('[board]\nbackend = "file"\n')
        env = {"HOME": str(tmp), "USERPROFILE": str(tmp), "SWARM_CHANNEL": "main", "SWARM_NO_MIGRATE": "1",
               "SWARM_VENV": str(tmp / "venv")}
        with mock.patch.dict(os.environ, env):
            steps = bootstrap.bootstrap(None, config=cfg)
        self.assertIn("channel", [s.name for s in steps])
        self.assertEqual(channel.read_channel(cfg), "main")


@unittest.skipUnless(sys.platform == "win32", "runs the real swarm.cmd launcher")
class RealLauncherTests(unittest.TestCase):
    def test_launcher_heals_to_the_newest_installed_plugin(self):
        tmp = Path(tempfile.mkdtemp(prefix="swarm-wininst-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        cache = tmp / "claude" / "plugins" / "cache" / "swarm" / "swarm"
        for v in ("0.1.9", "0.1.12"):
            (cache / v / "bin").mkdir(parents=True)
            (cache / v / "bin" / "swarm.cmd").write_text(f"@echo off\r\necho plugin {v} %*\r\n")
        launcher = tmp / "swarm.cmd"
        launcher.write_text(bootstrap.LAUNCHER_CMD.format(root=str(tmp / "gone")), newline="")
        env = {**os.environ, "CLAUDE_CONFIG_DIR": str(tmp / "claude"), "CODEX_HOME": str(tmp / "nocodex")}
        res = subprocess.run(["cmd", "/c", str(launcher), "status"], capture_output=True, text=True, env=env)
        self.assertEqual(res.stdout.strip(), "plugin 0.1.12 status", res.stderr)

    def test_launcher_without_any_plugin_exits_127(self):
        tmp = Path(tempfile.mkdtemp(prefix="swarm-wininst-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        launcher = tmp / "swarm.cmd"
        launcher.write_text(bootstrap.LAUNCHER_CMD.format(root=str(tmp / "gone")), newline="")
        env = {**os.environ, "CLAUDE_CONFIG_DIR": str(tmp / "c"), "CODEX_HOME": str(tmp / "x")}
        res = subprocess.run(["cmd", "/c", str(launcher)], capture_output=True, text=True, env=env)
        self.assertEqual(res.returncode, 127)

    def test_swarm_cmd_runs_the_cli(self):
        env = {**os.environ, "SWARM_VENV": str(Path(tempfile.mkdtemp(prefix="swarm-wininst-")) / "venv")}
        res = subprocess.run(["cmd", "/c", str(ROOT / "bin" / "swarm.cmd"), "--help"],
                             capture_output=True, text=True, env=env, timeout=600)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("usage: swarm", res.stdout)


if __name__ == "__main__":
    unittest.main()
