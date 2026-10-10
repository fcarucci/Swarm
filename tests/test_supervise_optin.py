"""B3: the supervisor is opt-in. A new install leaves it off; an existing install whose timer is
already installed (and whose config never set `[supervise] enabled`) keeps it on with no action;
`swarm supervise enable|disable` switch it explicitly; `swarm uninstall` removes what Swarm set up."""
from __future__ import annotations

import sys as _sys
import unittest as _unittest
if _sys.platform == "win32":
    raise _unittest.SkipTest("swarm supervise and its systemd units are not supported on Windows")

import json
import os
import subprocess
import tempfile
import shutil
import tomllib
from pathlib import Path
from unittest import mock

from support import ROOT, base_config, home_env
from test_hooks_cli import Env
from swarm import cli as swarm, paths
from swarm.supervisor import settings as st, systemd


class FakeRun:
    def __init__(self, rc=0):
        self.calls, self.rc = [], rc

    def __call__(self, argv, **kw):
        self.calls.append(list(argv))
        return subprocess.CompletedProcess(argv, self.rc, "", "")


class DefaultTests(_unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="swarm-optin-", dir=os.environ.get("TMPDIR")))
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        env = {**home_env(self.home)}
        env.pop("XDG_CONFIG_HOME", None)
        p = mock.patch.dict(os.environ, env)
        p.start()
        self.addCleanup(p.stop)
        os.environ.pop("XDG_CONFIG_HOME", None)
        self.config = self.home / "config.toml"

    def install_timer(self):
        d = systemd.unit_dir()
        d.mkdir(parents=True, exist_ok=True)
        (d / systemd.TIMER).write_text("[Timer]\n")

    def test_off_with_no_config(self):
        self.assertFalse(st.settings(base_config())["enabled"])

    def test_off_for_a_new_config_written_from_the_example(self):
        shutil.copyfile(ROOT / "config.example.toml", self.config)   # what bootstrap/init write
        self.install_timer()                                          # even with a timer around
        self.assertFalse(st.settings(swarm.load_config(self.config))["enabled"])

    def test_existing_config_without_the_key_stays_on_without_a_timer(self):
        # Francesco's Mac, a Linux box without systemd: no timer file, a config from before the
        # opt-in. Stuck-agent closing (gated by enabled) must keep running.
        self.config.write_text('[board]\nbackend = "file"\n')
        with mock.patch("sys.platform", "darwin"):
            cfg = swarm.load_config(self.config)
            self.assertTrue(st.enabled(cfg))
            self.assertTrue(st.enabled_implied(cfg))

    def test_existing_config_without_the_key_and_a_timer_stays_on(self):
        self.config.write_text('[board]\nbackend = "file"\n')
        self.install_timer()
        cfg = swarm.load_config(self.config)
        self.assertTrue(st.settings(cfg)["enabled"])
        self.assertTrue(st.enabled_implied(cfg))

    def test_no_config_file_with_a_timer_is_off(self):
        # no config file at all is a new machine (bootstrap writes the example, enabled = false)
        self.install_timer()
        self.assertFalse(st.settings(swarm.load_config(self.home / "missing.toml"))["enabled"])

    def test_explicit_false_wins_over_an_installed_timer(self):
        self.config.write_text('[supervise]\nenabled = false\n')
        self.install_timer()
        self.assertFalse(st.settings(swarm.load_config(self.config))["enabled"])

    def test_explicit_true(self):
        self.config.write_text('[supervise]\nenabled = true\n')
        cfg = swarm.load_config(self.config)
        self.assertTrue(st.settings(cfg)["enabled"])
        self.assertFalse(st.enabled_implied(cfg))


class SafeEnv(Env):
    """Env with XDG_CONFIG_HOME unset, and a guard: unit_dir() must be inside the test home."""
    def setUp(self):
        super().setUp()
        p = mock.patch.dict(os.environ, {})
        p.start()
        self.addCleanup(p.stop)
        os.environ.pop("XDG_CONFIG_HOME", None)
        assert str(systemd.unit_dir()).startswith(str(self.tmp)), systemd.unit_dir()
        assert str(paths.home()).startswith(str(self.tmp)), paths.home()


class SwitchTests(SafeEnv):
    def test_enable_writes_the_key_and_installs_the_timer(self):
        run = FakeRun()
        os.environ.pop("SWARM_NO_SYSTEMD", None)   # SafeEnv restores the environment; run is fake
        with mock.patch.object(systemd.shutil, "which", return_value="/usr/bin/systemctl"), \
                mock.patch("swarm.supervisor.systemd.subprocess.run", run):
            rc, out, err = self.cli("supervise", "enable")
        self.assertEqual(rc, 0, err)
        data = tomllib.loads(self.config.read_text())
        self.assertIs(data["supervise"]["enabled"], True)
        self.assertIn("[board]", self.config.read_text())   # the rest of the file is kept
        self.assertIn(["systemctl", "--user", "enable", "--now", systemd.TIMER], run.calls)
        self.assertTrue((systemd.unit_dir() / systemd.TIMER).exists())

    def test_enable_without_systemctl_exits_zero_and_says_so(self):
        os.environ.pop("SWARM_NO_SYSTEMD", None)
        with mock.patch.object(systemd.shutil, "which", return_value=None), \
                mock.patch("sys.platform", "linux"):
            rc, out, err = self.cli("supervise", "enable")
        self.assertEqual(rc, 0, err)
        self.assertIn("no systemctl here", out)
        self.assertIs(tomllib.loads(self.config.read_text())["supervise"]["enabled"], True)

    def test_enable_on_macos_suggests_no_cron(self):
        os.environ.pop("SWARM_NO_SYSTEMD", None)
        run = FakeRun()
        with mock.patch("sys.platform", "darwin"), \
                mock.patch("swarm.supervisor.systemd.subprocess.run", run):
            rc, out, err = self.cli("supervise", "enable")
        self.assertEqual(rc, 0, err)
        self.assertIn("macOS", out)
        self.assertNotIn("cron", out)
        self.assertEqual(run.calls, [])
        self.assertIs(tomllib.loads(self.config.read_text())["supervise"]["enabled"], True)

    def test_disable_writes_false_and_stops_the_timer(self):
        run = FakeRun()
        with mock.patch.object(systemd.shutil, "which", return_value="/usr/bin/systemctl"), \
                mock.patch("swarm.supervisor.systemd.subprocess.run", run):
            rc, out, err = self.cli("supervise", "disable")
        self.assertEqual(rc, 0, err)
        self.assertIs(tomllib.loads(self.config.read_text())["supervise"]["enabled"], False)
        self.assertIn(["systemctl", "--user", "disable", "--now", systemd.TIMER], run.calls)


class UninstallTests(SafeEnv):
    def setUp(self):
        super().setUp()
        self.home = paths.home()
        self.units = systemd.unit_dir()
        self.units.mkdir(parents=True, exist_ok=True)
        for name in (systemd.TIMER, systemd.SERVICE):
            (self.units / name).write_text("x\n")
        self.launcher = paths.launcher_path()
        self.launcher.parent.mkdir(parents=True, exist_ok=True)
        self.launcher.write_text(f'#!/bin/sh\nexec "{paths.PLUGIN_ROOT}/bin/swarm" "$@"\n')
        (paths.share_dir() / "venv").mkdir(parents=True, exist_ok=True)
        (paths.state_dir()).mkdir(parents=True, exist_ok=True)
        self.claude_settings = Path(os.environ["CLAUDE_SETTINGS"])
        spool = str(self.spool_dir)
        self.claude_settings.write_text(json.dumps(
            {"model": "x", "sandbox": {"filesystem": {"allowWrite": ["/keep", spool]}}}))

    def uninstall(self, *extra, rc=0):
        run = FakeRun(rc)
        with mock.patch.object(systemd.shutil, "which", return_value="/usr/bin/systemctl"), \
                mock.patch("swarm.supervisor.systemd.subprocess.run", run):
            rc, out, err = self.cli("uninstall", *extra)
        return rc, out, err, run

    def test_removes_units_launcher_venv_and_settings_entries(self):
        rc, out, err, run = self.uninstall()
        self.assertEqual(rc, 0, err)
        self.assertIn(["systemctl", "--user", "disable", "--now", systemd.TIMER], run.calls)
        self.assertFalse((self.units / systemd.TIMER).exists())
        self.assertFalse((self.units / systemd.SERVICE).exists())
        self.assertFalse(self.launcher.exists())
        self.assertFalse(paths.share_dir().exists())
        s = json.loads(self.claude_settings.read_text())
        self.assertEqual(s["sandbox"]["filesystem"]["allowWrite"], ["/keep"])
        self.assertEqual(s["model"], "x")
        self.assertTrue(paths.state_dir().exists())      # boards and config stay without --purge
        self.assertTrue(self.config.exists())

    def test_removes_only_swarms_codex_writable_roots(self):
        codex = Path(os.environ.get("CODEX_HOME") or self.home / ".codex") / "config.toml"
        codex.parent.mkdir(parents=True, exist_ok=True)
        ours = [str(self.spool_dir), str(self.markers)]
        codex.write_text('model = "m"\n[sandbox_workspace_write]\nwritable_roots = '
                         + json.dumps(["/keep"] + ours) + '\n')
        rc, out, err, run = self.uninstall()
        self.assertEqual(rc, 0, err)
        data = tomllib.loads(codex.read_text())
        self.assertEqual(data["sandbox_workspace_write"]["writable_roots"], ["/keep"])
        self.assertEqual(data["model"], "m")

    def test_a_failed_timer_stop_removes_nothing(self):
        rc, out, err, run = self.uninstall(rc=1)   # e.g. no user systemd bus in this shell
        self.assertEqual(rc, 1)
        self.assertTrue((self.units / systemd.TIMER).exists())
        self.assertTrue((self.units / systemd.SERVICE).exists())
        self.assertTrue(paths.share_dir().exists())
        self.assertTrue(self.launcher.exists())
        self.assertIn("systemctl --user disable --now", err)

    def test_codex_roots_all_swarms_drops_the_key(self):
        codex = self.home / ".codex" / "config.toml"
        codex.parent.mkdir(parents=True, exist_ok=True)
        ours = [str(self.spool_dir), str(self.markers)]
        codex.write_text('model = "m"\n[sandbox_workspace_write]\nwritable_roots = ' + json.dumps(ours) + '\n')
        rc, out, err, run = self.uninstall()
        self.assertEqual(rc, 0, err)
        data = tomllib.loads(codex.read_text())
        self.assertNotIn("writable_roots", data.get("sandbox_workspace_write", {}))
        self.assertEqual(data["model"], "m")

    def test_purge_removes_state_and_a_default_location_config(self):
        default = self.home / ".config" / "swarm" / "config.toml"
        default.parent.mkdir(parents=True, exist_ok=True)
        default.write_text(self.config.read_text())
        self.config = default
        rc, out, err, run = self.uninstall("--purge")
        self.assertEqual(rc, 0, err)
        self.assertFalse(paths.state_dir().exists())
        self.assertFalse(default.exists())

    def test_purge_keeps_a_config_outside_the_default_location(self):
        rc, out, err, run = self.uninstall("--purge")
        self.assertEqual(rc, 0, err)
        self.assertTrue(self.config.exists())
        self.assertIn("not Swarm's default location", out)

    def test_purge_removes_the_default_sqlite_dir_only(self):
        board_dir = self.home / ".local" / "share" / "swarm-board"
        board_dir.mkdir(parents=True)
        (board_dir / "board.sqlite3").write_text("x")
        rc, out, err, run = self.uninstall("--purge")
        self.assertEqual(rc, 0, err)
        self.assertFalse(board_dir.exists())

    def test_purge_keeps_a_custom_sqlite_board(self):
        board_dir = self.home / ".local" / "share" / "swarm-board"
        board_dir.mkdir(parents=True)
        custom = self.tmp / "elsewhere" / "b.sqlite3"
        custom.parent.mkdir()
        custom.write_text("x")
        with open(self.config, "a") as fh:
            fh.write(f'[sqlite]\npath = {json.dumps(str(custom))}\n')
        rc, out, err, run = self.uninstall("--purge")
        self.assertEqual(rc, 0, err)
        self.assertTrue(custom.exists())
        self.assertTrue(board_dir.exists())   # not the configured board: not Swarm's to delete

    def test_dry_run_changes_nothing(self):
        rc, out, err, run = self.uninstall("--dry-run")
        self.assertEqual(rc, 0, err)
        self.assertEqual(run.calls, [])
        self.assertTrue((self.units / systemd.TIMER).exists())
        self.assertTrue(self.launcher.exists())
        self.assertIn(str(self.launcher), out)

    def test_foreign_launcher_is_kept(self):
        self.launcher.write_text("#!/bin/sh\necho someone else's swarm\n")
        rc, out, err, run = self.uninstall()
        self.assertEqual(rc, 0, err)
        self.assertTrue(self.launcher.exists())


if __name__ == "__main__":
    _unittest.main()
