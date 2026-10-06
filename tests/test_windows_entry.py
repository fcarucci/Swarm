"""lib/swarm/winlaunch.py and winhook.py: the Windows launcher and hook entry (stdlib Python, so
they are exercised here on any platform with a fake HOME and a fake venv interpreter)."""
from __future__ import annotations

import io
import json
import os
import shutil
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from support import ROOT  # noqa: F401

from swarm import winhook, winlaunch


def fake_python(path: Path, log: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'#!/bin/sh\necho "$@" >> "{log}"\n')
    path.chmod(0o755)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="swarm-win-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.venv = self.tmp / "venv"
        env = mock.patch.dict(os.environ, {"HOME": str(self.tmp), "USERPROFILE": str(self.tmp),
                                           "SWARM_VENV": str(self.venv),
                                           "SWARM_CONFIG": str(self.tmp / "none.toml")})
        env.start()
        self.addCleanup(env.stop)
        self.log = self.tmp / "py.log"
        self.py = winlaunch.venv_python(self.venv)


class WinLaunchTests(Base):
    def test_requirements_stamp_ignores_line_endings(self):
        a, b = self.tmp / "a", self.tmp / "b"
        a.mkdir(); b.mkdir()
        (a / "requirements.txt").write_bytes(b"psycopg\nx\n")
        (b / "requirements.txt").write_bytes(b"psycopg\r\nx\r\n")
        self.assertEqual(winlaunch.requirements_stamp(a), winlaunch.requirements_stamp(b))

    def test_up_to_date_venv_is_not_rebuilt(self):
        fake_python(self.py, self.log)
        (self.venv / ".swarm-requirements").write_text(winlaunch.requirements_stamp() + "\n")
        with mock.patch.object(winlaunch.subprocess, "run", side_effect=AssertionError("rebuilt")):
            self.assertEqual(winlaunch.ensure_venv(), self.py)

    def test_changed_requirements_rebuild_under_a_lock(self):
        fake_python(self.py, self.log)
        (self.venv / ".swarm-requirements").write_text("stale\n")
        calls = []
        def run(cmd, **kw):
            calls.append(cmd)
            self.assertTrue(Path(str(self.venv) + ".building").is_dir())   # lock held
            return mock.Mock(returncode=0)
        with mock.patch.object(winlaunch.subprocess, "run", run):
            winlaunch.ensure_venv()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1:4], ["-m", "pip", "install"])           # venv already exists: pip only
        self.assertEqual((self.venv / ".swarm-requirements").read_text().strip(), winlaunch.requirements_stamp())
        self.assertFalse(Path(str(self.venv) + ".building").exists())      # lock released

    def test_failed_setup_exits_with_a_message(self):
        with mock.patch.object(winlaunch.subprocess, "run", return_value=mock.Mock(returncode=1)):
            with self.assertRaises(SystemExit) as cm:
                winlaunch.ensure_venv()
        self.assertIn("could not set up", str(cm.exception))

    @unittest.skipIf(os.name == "nt", "the fake venv interpreter is an sh script")
    def test_main_runs_the_swarm_package_from_the_plugin(self):
        fake_python(self.py, self.log)
        (self.venv / ".swarm-requirements").write_text(winlaunch.requirements_stamp() + "\n")
        self.assertEqual(winlaunch.main(["status", "--all"]), 0)
        self.assertEqual(self.log.read_text().split(), ["-B", "-m", "swarm.cli", "status", "--all"])


class WinHookTests(Base):
    def setUp(self):
        super().setUp()
        self.plugin = self.tmp / "plugin"
        (self.plugin / ".claude-plugin").mkdir(parents=True)
        (self.plugin / ".claude-plugin" / "plugin.json").write_text('{"version": "1.2.3"}')
        p = mock.patch.object(winhook, "PLUGIN_ROOT", self.plugin)
        p.start(); self.addCleanup(p.stop)
        self.host = self.tmp / ".local/share/swarm/host"

    def run_hook(self, *argv, stdin="{}"):
        with mock.patch("sys.stdin", io.StringIO(stdin)), mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            rc = winhook.main(list(argv))
        return rc, out.getvalue()

    def test_session_start_stamps_and_starts_a_detached_bootstrap_once(self):
        started = []
        with mock.patch.object(winhook, "_detached", lambda cmd, log: started.append(cmd)):
            self.assertEqual(self.run_hook("--host", "codex", "session-start")[0], 0)
            ran = list(self.host.glob("hooks-ran-codex-1.2.3-*"))
            self.assertEqual(len(ran), 1)
            self.assertEqual(len(started), 1)
            self.assertEqual(started[0][2:6], ["bootstrap", "--host", "codex", "--quiet"])
            stamp = Path(started[0][7])
            self.assertEqual(stamp.parent, self.host)
            stamp.touch()                                   # bootstrap finished
            self.run_hook("--host", "codex", "session-start")
            self.assertEqual(len(started), 1)                # not again

    def test_other_events_do_nothing_without_a_job_marker(self):
        fake_python(self.py, self.log)
        with mock.patch.object(winhook.subprocess, "run", side_effect=AssertionError("ran")):
            self.assertEqual(self.run_hook("--host", "claude", "turn"), (0, ""))

    @unittest.skipIf(os.name == "nt", "the fake venv interpreter is an sh script")
    def test_other_events_run_the_hook_in_the_venv_when_a_job_is_active(self):
        fake_python(self.py, self.log)
        md = self.tmp / ".local/state/swarm/active"; md.mkdir(parents=True)
        (md / "J.json").write_text("{}")
        self.assertEqual(self.run_hook("--host", "claude", "turn")[0], 0)
        self.assertEqual(self.log.read_text().split(), ["-B", "-m", "swarm.cli", "hook", "--host", "claude", "turn"])

    def test_codex_session_end_runs_without_a_marker(self):
        fake_python(self.py, self.log)
        with mock.patch.object(winhook.subprocess, "run", return_value=mock.Mock(returncode=0)) as run:
            self.assertEqual(self.run_hook("--host", "codex", "session-end"), (0, ""))
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0][-3:], ["--host", "codex", "session-end"])

    @unittest.skipIf(os.name == "nt", "POSIX shell entry")
    def test_posix_codex_session_end_runs_without_a_marker(self):
        import subprocess
        fake_python(self.venv / "bin/python", self.log)
        out = subprocess.run([str(ROOT / "bin/swarm-hook"), "--host", "codex", "session-end"],
                             input="{}", text=True, capture_output=True, timeout=10)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(self.log.read_text().splitlines()[-1].split(),
                         ["-B", "-m", "swarm.cli", "hook", "--host", "codex", "session-end"])

    def test_marker_dir_comes_from_the_config(self):
        cfg = self.tmp / "c.toml"
        cfg.write_text('[hook]\nmarker_dir = "~/m"\n')
        with mock.patch.dict(os.environ, {"SWARM_CONFIG": str(cfg)}):
            self.assertEqual(winhook.marker_dir(), Path(os.path.expanduser("~/m")))

    def test_no_venv_means_no_build_and_exit_zero(self):
        md = self.tmp / ".local/state/swarm/active"; md.mkdir(parents=True)
        (md / "J.json").write_text("{}")
        self.assertEqual(self.run_hook("--host", "claude", "turn"), (0, ""))
        self.assertFalse(self.venv.exists())

    def test_bad_arguments_never_fail(self):
        for argv in ((), ("--host",), ("--host", "x"), ("--host", "bad name", "turn"), ("--host", "a/b", "turn")):
            self.assertEqual(self.run_hook(*argv)[0], 0)

    def test_a_symlinked_host_dir_is_refused_with_a_fixed_message(self):
        if os.name == "nt":
            self.skipTest("needs symlink privilege")
        target = self.tmp / "elsewhere"; target.mkdir()
        self.host.parent.mkdir(parents=True)
        self.host.symlink_to(target)
        rc, out = self.run_hook("--host", "claude", "session-start")
        self.assertEqual(rc, 0)
        self.assertIn("setup problem", json.loads(out)["systemMessage"])
        self.assertEqual(list(target.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
