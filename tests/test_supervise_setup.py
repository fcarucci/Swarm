"""Activation switches, marker cwd, user timer setup, and supervisor doctor checks."""
from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
from pathlib import Path
from unittest import mock

from test_hooks_cli import Env
from swarm import bootstrap, cli as swarm, paths
from swarm.supervisor import settings as st, systemd


class FakeRun:
    def __init__(self, show="ActiveState=active\nUnitFileState=enabled\n", linger="Linger=yes\n", rc=0, err=""):
        self.calls, self.show, self.linger, self.rc, self.err = [], show, linger, rc, err

    def __call__(self, argv, **kw):
        self.calls.append(list(argv))
        out = self.show if "show" in argv and "systemctl" in argv[0] else self.linger if "loginctl" in argv[0] else ""
        return subprocess.CompletedProcess(argv, self.rc, out, self.err)


class ActivateTests(Env):
    def test_no_supervise_flag_and_reset_on_new_run(self):
        self.assertEqual(self.cli("activate", "--job", "J", "--no-supervise")[0], 0)
        with self.board() as b:
            self.assertFalse(b.job_status("J").supervise)
        self.assertEqual(self.cli("activate", "--job", "J")[0], 0)
        with self.board() as b:
            self.assertTrue(b.job_status("J").supervise)

    def test_attach_keeps_flag(self):
        self.cli("activate", "--job", "J", "--no-supervise")
        self.cli("activate", "--job", "J", "--attach", "--session", "other")
        with self.board() as b:
            self.assertFalse(b.job_status("J").supervise)

    def test_markers_record_cwd(self):
        self.cli("activate", "--job", "J")
        self.cli("activate", "--job", "J", "--attach", "--session", "other")
        self.assertEqual(len(list(self.markers.glob("J*.json"))), 2)
        for marker in self.markers.glob("J*.json"):
            self.assertEqual(json.loads(marker.read_text())["cwd"], os.getcwd())
            self.assertEqual(marker.stat().st_mode & 0o777, 0o600)


class SystemdTests(Env):
    def setUp(self):
        super().setUp()
        env = mock.patch.dict(os.environ, {"SWARM_NO_SYSTEMD": "0", "XDG_CONFIG_HOME": str(self.tmp / "xdg")})
        env.start()
        self.addCleanup(env.stop)

    def test_units_text(self):
        service = systemd.service_text(Path("/h/.local/bin/swarm"), Path("/h/.config/swarm/config.toml"))
        self.assertIn("ExecStart=/h/.local/bin/swarm supervise", service)
        self.assertIn("Environment=SWARM_CONFIG=/h/.config/swarm/config.toml", service)
        self.assertIn("KillMode=process", service)
        self.assertIn("Type=oneshot", service)
        self.assertIn("OnUnitActiveSec=2min", systemd.timer_text(2))
        self.assertIn("WantedBy=timers.target", systemd.timer_text(2))

    def test_escapes_spaces_and_percent_in_unit_values(self):
        launcher = Path("/h/my dir/100%cpu/swarm")
        config = Path("/h/my config/50%done/config.toml")
        service = systemd.service_text(launcher, config)
        # a space in a value must be quoted; every % must be doubled (systemd specifier escaping)
        self.assertIn('ExecStart="/h/my dir/100%%cpu/swarm" supervise', service)
        self.assertIn('Environment="SWARM_CONFIG=/h/my config/50%%done/config.toml"', service)
        # no unescaped single % or unquoted space slipped through
        self.assertNotIn("100%cpu", service)
        self.assertNotIn("50%done", service)

    def test_install_writes_private_units_and_enables(self):
        run = FakeRun()
        with mock.patch("shutil.which", return_value="/usr/bin/systemctl"):
            step = systemd.install(self.config, 2, run=run)
        self.assertEqual(step.status, "changed")
        directory = self.tmp / "xdg/systemd/user"
        self.assertEqual(oct((directory / systemd.SERVICE).stat().st_mode & 0o777), "0o600")
        self.assertEqual(oct((directory / systemd.TIMER).stat().st_mode & 0o777), "0o600")
        self.assertIn(f"ExecStart={paths.agent_bin()} supervise", (directory / systemd.SERVICE).read_text())
        self.assertEqual(run.calls, [["systemctl", "--user", "daemon-reload"],
                                     ["systemctl", "--user", "enable", "--now", systemd.TIMER]])
        again = FakeRun()
        with mock.patch("shutil.which", return_value="/usr/bin/systemctl"):
            self.assertEqual(systemd.install(self.config, 2, run=again).status, "ok")
        self.assertEqual(again.calls, [["systemctl", "--user", "enable", "--now", systemd.TIMER]])

    def test_install_disabled_in_tests_and_without_systemctl(self):
        with mock.patch.dict(os.environ, {"SWARM_NO_SYSTEMD": "1"}):
            self.assertEqual(systemd.install(self.config, 2, run=FakeRun()).status, "skipped")
        with mock.patch("shutil.which", return_value=None):
            self.assertEqual(systemd.install(self.config, 2, run=FakeRun()).status, "manual")

    def test_bus_error_gets_a_friendly_hint(self):
        run = FakeRun(rc=1, err="Failed to connect to bus: No such file or directory\n")
        with mock.patch("shutil.which", return_value="/usr/bin/systemctl"):
            step = systemd.install(self.config, 2, run=run)
        self.assertEqual(step.status, "failed")
        self.assertEqual(step.detail, "no user systemd manager (fix: loginctl enable-linger $USER)")

    def test_bootstrap_step_only_when_enabled(self):
        self.assertEqual(bootstrap.supervisor_step(self.cfg, self.config, run=FakeRun()).status, "skipped")
        self.cfg["supervise"] = {"enabled": True}
        with mock.patch("shutil.which", return_value="/usr/bin/systemctl"):
            self.assertEqual(bootstrap.supervisor_step(self.cfg, self.config, run=FakeRun()).status, "changed")


class DoctorSupervisorTests(Env):
    def checks(self, run=None, which=None, host="claude", scope=True):
        which = which or (lambda b, path=None: f"/usr/bin/{b}")
        with mock.patch("swarm.supervisor.runner.scope_available", return_value=scope):
            return {c.name: c for c in bootstrap.supervisor_checks(self.cfg, host, run=run or FakeRun(), which=which)}

    def test_off_is_one_ok_line(self):
        checks = self.checks()
        self.assertEqual(list(checks), ["supervise"])
        self.assertTrue(checks["supervise"].ok)

    def test_enabled_all_good(self):
        self.cfg["supervise"] = {"enabled": True}
        st.save_state({"last_run_at": dt.datetime.now(dt.timezone.utc).isoformat()})
        checks = self.checks()
        for name in ("supervise config", "supervise timer", "supervise last run", "linger", "supervise harness",
                     "replacement scope"):
            self.assertTrue(checks[name].ok, name)

    def test_enabled_problems_have_fixes(self):
        self.cfg["supervise"] = {"enabled": True}
        checks = self.checks(run=FakeRun(show="ActiveState=inactive\nUnitFileState=disabled\n", linger="Linger=no\n"),
                             which=lambda b, path=None: None)
        self.assertFalse(checks["supervise timer"].ok)
        self.assertIn("enable --now swarm-supervise.timer", checks["supervise timer"].fix)
        self.assertFalse(checks["supervise last run"].ok)
        self.assertIn("never", checks["supervise last run"].detail)
        self.assertIsNone(checks["linger"].ok)
        self.assertFalse(checks["supervise harness"].ok)

    def test_off_file_and_stale_run(self):
        self.cfg["supervise"] = {"enabled": True, "timer_minutes": 2}
        off_file = st.off_file()
        off_file.parent.mkdir(parents=True, exist_ok=True)
        off_file.touch()
        st.save_state({"last_run_at": (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=7)).isoformat()})
        checks = self.checks()
        self.assertIsNone(checks["supervise off file"].ok)
        self.assertIn(str(off_file), checks["supervise off file"].fix)
        self.assertFalse(checks["supervise last run"].ok)
        self.assertIn("7 min ago", checks["supervise last run"].detail)

    def test_timer_and_linger_unknown_when_queries_fail(self):
        self.cfg["supervise"] = {"enabled": True}
        checks = self.checks(run=FakeRun(rc=1))
        self.assertFalse(checks["supervise timer"].ok)
        self.assertIsNone(checks["linger"].ok)

    def test_bad_settings_fail(self):
        self.cfg["supervise"] = {"enabled": True, "claude_permission_mode": "bypassPermissions"}
        self.assertFalse(self.checks()["supervise config"].ok)

    def test_codex_host_warns_about_hook_trust(self):
        self.cfg["supervise"] = {"enabled": True}
        self.assertIsNone(self.checks(host="codex")["supervise codex hooks"].ok)

    def test_no_user_manager_is_one_fail_and_skips_bus_dependent_checks(self):
        """Reconciles with the containment check: with no user manager, doctor prints one
        FAIL (fix: loginctl enable-linger $USER), not a contradictory pile of timer/last-run/
        linger lines that can never succeed without a manager."""
        self.cfg["supervise"] = {"enabled": True}
        checks = self.checks(scope=False)
        self.assertNotIn("supervise timer", checks)
        self.assertNotIn("supervise last run", checks)
        self.assertNotIn("linger", checks)
        self.assertIs(checks["replacement scope"].ok, False)
        self.assertIn("loginctl enable-linger $USER", checks["replacement scope"].fix)
        # unrelated checks (not bus-dependent) still run
        self.assertIn("supervise harness", checks)
        self.assertIn("supervise config", checks)

    def test_scope_ok_line_when_manager_reachable(self):
        self.cfg["supervise"] = {"enabled": True}
        checks = self.checks(scope=True)
        self.assertIs(checks["replacement scope"].ok, True)
        self.assertIn("supervise timer", checks)
        self.assertIn("linger", checks)

    # ---- without --host, the harness of the user running doctor

    def _hooks_ran(self, host):
        from swarm import paths
        d = paths.host_dir()                       # the stamps are host-only now
        d.mkdir(parents=True, mode=0o700, exist_ok=True)
        (d / f"hooks-ran-{host}-{paths.plugin_version()}-x").touch()

    def test_no_host_on_a_codex_user_checks_codex(self):
        self.cfg["supervise"] = {"enabled": True}
        self._hooks_ran("codex")
        only_codex = lambda b, path=None: "/usr/bin/codex" if b == "codex" else None
        c = self.checks(host=None, which=only_codex)["supervise harness"]
        self.assertTrue(c.ok, c.detail)
        self.assertIn("codex", c.detail)

    def test_no_host_and_no_hooks_yet_passes_with_either_harness(self):
        self.cfg["supervise"] = {"enabled": True}
        only_codex = lambda b, path=None: "/usr/bin/codex" if b == "codex" else None
        self.assertTrue(self.checks(host=None, which=only_codex)["supervise harness"].ok)

    def test_no_host_and_no_harness_at_all_fails(self):
        self.cfg["supervise"] = {"enabled": True}
        c = self.checks(host=None, which=lambda b, path=None: None)["supervise harness"]
        self.assertFalse(c.ok)
        self.assertIn("claude", c.detail)
        self.assertIn("codex", c.detail)

    def test_no_host_checks_the_harness_whose_hooks_ran(self):
        self.cfg["supervise"] = {"enabled": True}
        self._hooks_ran("claude")
        only_codex = lambda b, path=None: "/usr/bin/codex" if b == "codex" else None
        self.assertFalse(self.checks(host=None, which=only_codex)["supervise harness"].ok)

    def test_harness_check_uses_the_service_units_path(self):
        self.cfg["supervise"] = {"enabled": True}
        seen = {}

        def which(binary, path=None):
            seen["path"] = path
            return None
        self.checks(which=which)
        self.assertEqual(seen["path"], systemd.service_path())
        self.assertIsNotNone(seen["path"])
