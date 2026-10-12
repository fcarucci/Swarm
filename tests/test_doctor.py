from __future__ import annotations

import datetime as dt
import json
import os
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from support import ROOT, tq, home_env, posix_only  # noqa: F401

from swarm import bootstrap, paths  # noqa: E402


class DoctorTests(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="swarm-doc-"))
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        import support
        p = mock.patch.dict(os.environ, {**home_env(self.home), "SWARM_VENV": str(support.temp_venv()),
                                         "SWARM_AUTO_INIT": "1"})   # a throwaway venv: never write the real one
        p.start(); self.addCleanup(p.stop)
        self.cfg = self.home / ".config/swarm/config.toml"
        self.cfg.parent.mkdir(parents=True)
        self.cfg.write_text(f'[board]\nbackend = "sqlite"\nspool_dir = "~/.local/state/swarm/spool"\n'
                            f'[sqlite]\npath = {tq(f"{self.home}/b.sqlite3")}\n')
        plugins = self.home / ".claude/plugins"; plugins.mkdir(parents=True)
        (plugins / "installed_plugins.json").write_text(json.dumps(
            {"version": 2, "plugins": {"swarm@swarm": [{"installPath": str(ROOT), "version": "0.9.0"}]}}))

    def by_name(self, checks):
        return {c.name: c for c in checks}

    def test_clean_install_passes(self):
        bootstrap.bootstrap("claude", config=self.cfg)
        checks = self.by_name(bootstrap.doctor("claude", config=self.cfg))
        failing = {n: c.detail for n, c in checks.items() if c.ok is False}
        self.assertEqual(failing, {})
        self.assertIsNone(checks["orchestrator model"].ok)
        # this config predates the opt-in (no [supervise] enabled): the supervisor stays on
        self.assertTrue(checks["supervise" if paths.IS_WINDOWS else "supervise config"].ok)

    @posix_only("supervisor uses Linux systemd")
    def test_clean_install_without_manager_or_harness_warns(self):
        with open(self.cfg, "a") as fh:
            fh.write("[supervise]\nenabled = true\n")
        bootstrap.bootstrap("claude", config=self.cfg)
        with mock.patch("swarm.supervisor.runner.scope_available", return_value=False), \
             mock.patch("swarm.bootstrap.shutil.which", return_value=None):
            checks = self.by_name(bootstrap.doctor("claude", config=self.cfg))
        self.assertIsNone(checks["replacement scope"].ok)
        self.assertIsNone(checks["supervise harness"].ok)
        self.assertIn("auto-restart inactive until", checks["replacement scope"].detail)
        self.assertIn("auto-restart inactive until", checks["supervise harness"].detail)
        self.assertFalse(any(c.ok is False for c in checks.values()))

    @posix_only("supervisor uses Linux systemd")
    def test_timer_unknown_from_shell_without_bus_is_not_fail(self):
        from swarm.supervisor import settings as st, systemd
        with open(self.cfg, "a") as fh:
            fh.write("[supervise]\nenabled = true\n")
        bootstrap.bootstrap("claude", config=self.cfg)
        st.save_state({"last_run_at": dt.datetime.now(dt.timezone.utc).isoformat()})
        with mock.patch("swarm.supervisor.runner.scope_available", return_value=True), \
             mock.patch.object(systemd, "timer_state", return_value={}), \
             mock.patch.object(systemd, "linger", return_value=None):
            checks = self.by_name(bootstrap.doctor("claude", config=self.cfg))
        self.assertIs(checks["supervise timer"].ok, True)
        self.assertIn("unknown (no user bus from this shell)", checks["supervise timer"].detail)

    def _timer_check_with_last_run(self, minutes_ago):
        from swarm.supervisor import settings as st, systemd
        with open(self.cfg, "a") as fh:
            fh.write("[supervise]\nenabled = true\ntimer_minutes = 2\n")
        bootstrap.bootstrap("claude", config=self.cfg)
        st.save_state({"last_run_at": (dt.datetime.now(dt.timezone.utc)
                                       - dt.timedelta(minutes=minutes_ago)).isoformat()})
        with mock.patch("swarm.supervisor.runner.scope_available", return_value=True), \
             mock.patch.object(systemd, "timer_state", return_value={}), \
             mock.patch.object(systemd, "linger", return_value=None):
            return self.by_name(bootstrap.doctor("claude", config=self.cfg))["supervise timer"]

    @posix_only("supervisor uses Linux systemd")
    def test_timer_unknown_is_not_ok_for_a_future_dated_last_run(self):
        self.assertIsNot(self._timer_check_with_last_run(-5).ok, True)

    @posix_only("supervisor uses Linux systemd")
    def test_timer_unknown_is_not_ok_for_a_stale_last_run(self):
        # timer_minutes = 2: the recency window is 2 x 2 = 4 minutes; 5 minutes ago is outside it
        check = self._timer_check_with_last_run(5)
        self.assertIs(check.ok, False)
        self.assertNotIn("unknown", check.detail)

    @posix_only("pre-0.1 skill hooks never existed on Windows")
    def test_doctor_flags_legacy_hooks_while_plugin_installed(self):
        bootstrap.bootstrap("claude", config=self.cfg)
        s = self.home / ".claude/settings.json"
        s.write_text(json.dumps({"hooks": {"PreToolUse": [{"matcher": "*", "hooks": [
            {"type": "command", "command": f"{self.home.as_posix()}/.claude/skills/swarm/bin/swarm-hook turn"}]}]}}))
        c = self.by_name(bootstrap.doctor("claude", config=self.cfg))["old hooks"]
        self.assertIs(c.ok, False)
        self.assertIn("swarm migrate", c.fix)

    def test_missing_launcher_and_plugin_have_fixes(self):
        (self.home / ".claude/plugins/installed_plugins.json").write_text('{"version": 2, "plugins": {}}')
        checks = self.by_name(bootstrap.doctor("claude", config=self.cfg))
        self.assertIs(checks["plugin"].ok, False)
        self.assertIn("/plugin install swarm@swarm", checks["plugin"].fix)
        self.assertIs(checks["launcher"].ok, False)
        self.assertIn("swarm bootstrap", checks["launcher"].fix)

    def test_unreachable_board_fails_without_secret(self):
        (self.cfg.parent / "pg.env").write_text("PGPASSWORD=hunter2-secret\n")
        self.cfg.write_text('[database]\nhost = "db.invalid"\nconnect_timeout = 1\npassword_env_file = "~/.config/swarm/pg.env"\n')
        checks = bootstrap.doctor("claude", config=self.cfg)
        self.assertIs(self.by_name(checks)["board"].ok, False)
        self.assertNotIn("hunter2", bootstrap.format_checks(checks))

    def test_no_config_values_in_output(self):
        self.cfg.write_text('[database]\nhost = "db-host-value.invalid"\nuser = "role-value"\ndbname = "db-name-value"\n'
                            'connect_timeout = 1\n')
        text = bootstrap.format_checks(bootstrap.doctor("claude", config=self.cfg))
        for value in ("db-host-value", "role-value", "db-name-value"):
            self.assertNotIn(value, text)

    def test_color_emits_codes_only_when_requested(self):
        bootstrap.bootstrap("claude", config=self.cfg)
        checks = bootstrap.doctor("claude", config=self.cfg)
        plain = bootstrap.format_checks(checks)
        self.assertNotIn("\033[", plain)
        colored = bootstrap.format_checks(checks, color=True)
        self.assertIn("\033[", colored)
        # the plain text itself (status words, names, details) must be identical either way
        strip = lambda s: s.replace("\033[0m", "").replace("\033[1m", "") \
            .replace("\033[32m", "").replace("\033[31m", "").replace("\033[33m", "")
        self.assertEqual(plain, strip(colored))

    def test_old_hooks_use_migrates_matcher(self):
        s = self.home / ".claude/settings.json"
        s.write_text(json.dumps({"hooks": {"PreToolUse": [{"matcher": "*", "hooks": [
            {"type": "command", "command": "/opt/tools/bin/swarm-hook turn"},              # not the old skill's
            {"type": "command", "command": f"{self.home.as_posix()}/.claude/skills/swarm/bin/swarm-hook turn --verbose"}]}]}}))
        self.assertIs(self.by_name(bootstrap.doctor("claude", config=self.cfg))["old hooks"].ok, True)

    @posix_only("pre-0.1 skill hooks never existed on Windows")
    def test_old_hooks_without_plugin_are_a_warning(self):
        (self.home / ".claude/plugins/installed_plugins.json").write_text('{"version": 2, "plugins": {}}')
        s = self.home / ".claude/settings.json"
        s.write_text(json.dumps({"hooks": {"SubagentStop": [{"hooks": [
            {"type": "command", "command": f"{self.home.as_posix()}/.claude/skills/swarm/bin/swarm-hook stop"}]}]}}))
        self.assertIsNone(self.by_name(bootstrap.doctor("claude", config=self.cfg))["old hooks"].ok)

    def test_cli_exit_code(self):
        import contextlib
        import io
        from swarm import cli
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = cli.main(["--config", str(self.cfg), "doctor", "--host", "claude"])
        self.assertEqual(rc, 1)                     # no launcher yet: a FAIL
        self.assertIn("FAIL launcher", out.getvalue())
        bootstrap.bootstrap("claude", config=self.cfg)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = cli.main(["--config", str(self.cfg), "doctor", "--host", "claude"])
        self.assertEqual(rc, 0, out.getvalue())

    @posix_only("needs Unix uids (no ownership checks on Windows)")
    def test_malformed_or_unreadable_settings_warn_not_ok(self):
        s = self.home / ".claude/settings.json"
        s.write_text('{"model": "secret-model-value", "hooks": {')
        c = self.by_name(bootstrap.doctor("claude", config=self.cfg))
        self.assertIsNone(c["old hooks"].ok)
        self.assertIn(f"can't read {s}", c["old hooks"].detail)
        self.assertIn("invalid JSON at line 1", c["old hooks"].detail)
        self.assertIn("valid JSON", c["old hooks"].fix)
        self.assertNotIn("secret-model-value", bootstrap.format_checks(list(c.values())))
        if os.geteuid() != 0:                       # root reads anything
            s.write_text("{}"); s.chmod(0)
            self.addCleanup(s.chmod, 0o600)
            c = self.by_name(bootstrap.doctor("claude", config=self.cfg))["old hooks"]
            self.assertIsNone(c.ok)
            self.assertIn("Permission denied", c.detail)

    def test_broken_config_is_a_fail_and_the_rest_still_runs(self):
        import contextlib
        import io
        from swarm import cli
        self.cfg.write_text('[database]\nhost = "config-value-secret"\nport = = 5\n')
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = cli.main(["--config", str(self.cfg), "doctor", "--host", "claude"])
        text = out.getvalue()
        self.assertEqual(rc, 1)
        self.assertRegex(text, rf"FAIL config +can't read {re.escape(str(self.cfg))}: invalid TOML at line 3 column \d+")
        self.assertNotIn("config-value-secret", text)
        self.assertNotRegex(text, r"(?m)^\w+ +board +(schema|unreachable)")                   # needs the config: not run
        for name in ("venv", "launcher", "plugin", "old hooks"):
            self.assertIn(name, text)

    def test_unreachable_board_fix_is_a_concrete_step(self):
        self.cfg.write_text('[database]\nhost = "db.invalid"\nconnect_timeout = 1\n')
        c = self.by_name(bootstrap.doctor("claude", config=self.cfg))["board"]
        self.assertIs(c.ok, False)
        self.assertIn("swarm status", c.fix)
        self.assertIn(str(self.cfg), c.fix)

    def test_empty_settings_warn_but_missing_is_ok(self):
        s = self.home / ".claude/settings.json"
        for text in ("", "  \n"):
            s.write_text(text)
            c = self.by_name(bootstrap.doctor("claude", config=self.cfg))["old hooks"]
            self.assertIsNone(c.ok, repr(text))
            self.assertEqual(c.detail, f"can't read {s}: empty or invalid JSON")
            self.assertIn("valid JSON", c.fix)
        s.unlink()
        self.assertIs(self.by_name(bootstrap.doctor("claude", config=self.cfg))["old hooks"].ok, True)

    def test_codex_checks_report_trust_sandbox_depth(self):
        codex = self.home / ".codex"; codex.mkdir()
        (codex / "config.toml").write_text("[agents]\nmax_depth = 1\n")
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(codex)}), \
                mock.patch.object(bootstrap, "_codex_plugin_listed", return_value=True):
            env = {k: v for k, v in os.environ.items() if not k.startswith("CODEX_") or k == "CODEX_HOME"}
            checks = self.by_name(bootstrap.doctor("codex", config=self.cfg, env=env))   # even if the suite runs inside Codex
        self.assertIs(checks["codex depth"].ok, False)
        self.assertIs(checks["codex sandbox"].ok, False)
        self.assertIsNone(checks["hooks trusted"].ok)
        self.assertIn("/hooks", checks["hooks trusted"].fix)
        self.assertNotIn("codex session", checks)                     # not run from inside Codex
        self.assertIsNone(checks["codex sandbox mode"].ok)             # unset: a warning

    def test_codex_read_only_mode_fails_even_with_roots(self):
        codex = self.home / ".codex"; codex.mkdir()
        from swarm import codex_config
        from swarm.cli import load_config
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(codex)}):
            codex_config.apply(codex / "config.toml", load_config(self.cfg))       # roots + network set
            cfgfile = codex / "config.toml"
            cfgfile.write_text('sandbox_mode = "read-only"\n' + cfgfile.read_text())
            with mock.patch.object(bootstrap, "_codex_plugin_listed", return_value=True):
                c = self.by_name(bootstrap.doctor("codex", config=self.cfg))["codex sandbox"]
        self.assertIs(c.ok, False)
        self.assertIn("workspace-write", c.fix)

    def test_codex_session_started_before_config_change_is_flagged(self):
        codex = self.home / ".codex"; codex.mkdir()
        from swarm import codex_config
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(codex)}):
            codex_config.apply(codex / "config.toml", __import__("swarm.cli", fromlist=["x"]).load_config(self.cfg))
            with mock.patch.object(bootstrap, "_codex_plugin_listed", return_value=True), \
                    mock.patch.object(bootstrap, "_sandbox_probe", return_value=["/some/root"]):
                checks = self.by_name(bootstrap.doctor("codex", config=self.cfg,
                                                       env={**os.environ, "CODEX_THREAD_ID": "t"}))
        self.assertIs(checks["codex sandbox"].ok, True)
        self.assertIs(checks["codex session"].ok, False)
        self.assertIn("new Codex session", checks["codex session"].fix)

    def test_codex_profile_override_warns_and_probe_decides(self):
        codex = self.home / ".codex"; codex.mkdir()
        (codex / "ro.config.toml").write_text('sandbox_mode = "read-only"\n')
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(codex)}), \
                mock.patch.object(bootstrap, "_codex_plugin_listed", return_value=True), \
                mock.patch.object(bootstrap, "_sandbox_probe", return_value=["/some/root"]):
            checks = self.by_name(bootstrap.doctor("codex", config=self.cfg, env={**os.environ, "CODEX_THREAD_ID": "t"}))
        self.assertIsNone(checks["codex profiles"].ok)
        self.assertIn("ro", checks["codex profiles"].detail)
        self.assertIs(checks["codex session"].ok, False)          # fails even though the base config isn't set up either

    def test_codex_bootstrap_change_asks_for_new_session(self):
        codex = self.home / ".codex"; codex.mkdir()
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(codex), "SWARM_CONFIG": str(self.cfg)}):
            from swarm.cli import load_config
            step = bootstrap.host_setup("codex", load_config(self.cfg))
        self.assertEqual(step.status, "manual")
        self.assertIn("new Codex session", step.detail)
        self.assertIn("/hooks", step.detail)

    def test_codex_plugin_list_table_fallback(self):
        out = ("Marketplace `openai-curated-remote`\nRemote catalog\n\nPLUGIN   STATUS   VERSION   SOURCE\n"
               "gmail@openai-curated-remote   not installed   0.1.10   plugin_connector_1p_0123456789abcdef\n")
        self.assertIs(bootstrap.plugin_table_state(out), False)
        self.assertIs(bootstrap.plugin_table_state(out + "swarm@swarm   not installed   0.9.0   ./\n"), False)
        self.assertIs(bootstrap.plugin_table_state(out + "swarm@swarm   disabled   0.9.0   ./\n"), False)
        self.assertIs(bootstrap.plugin_table_state(out + "swarm@swarm   installed   0.9.0   ./\n"), True)
        self.assertIsNone(bootstrap.plugin_table_state("swarm is great\n"))

    def test_codex_plugin_list_json(self):
        sample = (ROOT / "tests/fixtures/codex/0.157.1/plugin-list.json").read_text()
        self.assertIs(bootstrap.plugin_json_state(sample), True)
        data = json.loads(sample)
        swarm = data["installed"][1]
        swarm["enabled"] = False
        self.assertIs(bootstrap.plugin_json_state(json.dumps(data)), False)       # installed but disabled
        swarm.update(enabled=True, installed=True)
        data["available"], data["installed"] = [swarm], data["installed"][:1]
        self.assertIs(bootstrap.plugin_json_state(json.dumps(data)), False)       # only under "available": not installed
        other = dict(swarm, name="swarmish", pluginId="swarmish@x")
        self.assertIs(bootstrap.plugin_json_state(json.dumps({"installed": [other], "available": []})), False)
        by_id = dict(swarm, name="Swarm board", pluginId="swarm@elsewhere")
        self.assertIs(bootstrap.plugin_json_state(json.dumps({"installed": [by_id], "available": []})), True)
        self.assertIs(bootstrap.plugin_json_state('{"installed": [], "available": []}'), False)
        self.assertIsNone(bootstrap.plugin_json_state('{"plugins": ["swarm"]}'))  # unknown shape
        self.assertIsNone(bootstrap.plugin_json_state('{"installed": "swarm"}'))
        self.assertIsNone(bootstrap.plugin_json_state(json.dumps({"available": [swarm]})))   # no "installed" list
        self.assertIsNone(bootstrap.plugin_json_state("[]"))
        self.assertIsNone(bootstrap.plugin_json_state("not json"))

    def test_codex_plugin_command_exit_codes(self):
        import subprocess
        calls = []
        def fake(argv, **kw):
            calls.append(argv)
            if "--json" in argv:
                return subprocess.CompletedProcess(argv, 2, "", "unknown flag")   # older Codex: no --json
            return subprocess.CompletedProcess(argv, 0, "PLUGIN   STATUS   VERSION   SOURCE\n"
                                               "swarm@swarm   installed   0.9.0   ./\n", "")
        with mock.patch("subprocess.run", side_effect=fake):
            self.assertIs(bootstrap._codex_plugin_listed(), True)
        self.assertEqual(calls, [["codex", "plugin", "list", "--json"], ["codex", "plugin", "list"]])
        with mock.patch("subprocess.run", return_value=subprocess.CompletedProcess([], 1, "", "")):
            self.assertIsNone(bootstrap._codex_plugin_listed())                    # both fail: can't tell
        with mock.patch("subprocess.run", side_effect=FileNotFoundError):
            self.assertIs(bootstrap._codex_plugin_listed(), False)                 # no codex at all
        codex = self.home / ".codex"; codex.mkdir()
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(codex)}), \
                mock.patch.object(bootstrap, "_codex_plugin_listed", return_value=None):
            c = self.by_name(bootstrap.doctor("codex", config=self.cfg, env={}))["plugin"]
        self.assertIsNone(c.ok)
        self.assertIn("can't tell", c.detail)
        self.assertIn("codex plugin list --json", c.fix)

    @posix_only("needs Unix uids (no ownership checks on Windows)")
    def test_codex_session_probe_checks_every_root(self):
        ok = self.home / "ok"; ok.mkdir()
        locked = self.home / "locked"; locked.mkdir(); locked.chmod(0o500)
        self.addCleanup(locked.chmod, 0o700)
        if os.geteuid() == 0:
            self.skipTest("root writes anywhere")
        self.assertEqual(bootstrap._sandbox_probe([str(ok), str(locked)]), [str(locked)])
        self.assertEqual(list(ok.iterdir()), [])                      # the probe cleans up
        codex = self.home / ".codex"; codex.mkdir()
        self.cfg.write_text(f'[board]\nbackend = "sqlite"\nspool_dir = {tq(f"{self.home}/spool")}\n'
                            f'[sqlite]\npath = {tq(f"{self.home}/b.sqlite3")}\n[hook]\nmarker_dir = {tq(f"{self.home}/markers")}\n')
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(codex)}), \
                mock.patch.object(bootstrap, "_codex_plugin_listed", return_value=True), \
                mock.patch.object(bootstrap, "_sandbox_probe", return_value=[str(locked)]) as probe:
            c = self.by_name(bootstrap.doctor("codex", config=self.cfg, env={"CODEX_THREAD_ID": "t"}))["codex session"]
        self.assertIs(c.ok, False)
        self.assertIn(str(locked), c.detail)
        from swarm import codex_config
        from swarm.cli import load_config
        roots = probe.call_args.args[0]
        self.assertEqual(roots, codex_config.required(load_config(self.cfg))[("sandbox_workspace_write", "writable_roots")])
        self.assertEqual(roots, [str(self.home / "spool"), str(self.home / "markers")])     # no state dir

    def test_sandbox_probe_reports_a_missing_root_without_creating_it(self):
        """A session whose sandbox was set up before a granted root existed has already failed to
        bind-mount it; the probe must report that, not quietly create the root after the fact."""
        missing = self.home / "not-there-yet"
        self.assertEqual(bootstrap._sandbox_probe([str(missing)]), [str(missing)])
        self.assertFalse(missing.exists())

    def test_codex_sandbox_check_flags_a_granted_root_missing_on_disk(self):
        codex = self.home / ".codex"; codex.mkdir()
        from swarm import codex_config
        from swarm.cli import load_config
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(codex)}):
            codex_config.apply(codex / "config.toml", load_config(self.cfg))   # roots created + granted
            for root in codex_config.required(load_config(self.cfg))[("sandbox_workspace_write", "writable_roots")]:
                __import__("shutil").rmtree(root)                             # ...then removed from disk
            with mock.patch.object(bootstrap, "_codex_plugin_listed", return_value=True):
                env = {k: v for k, v in os.environ.items() if not k.startswith("CODEX_") or k == "CODEX_HOME"}
                c = self.by_name(bootstrap.doctor("codex", config=self.cfg, env=env))["codex sandbox"]
        self.assertIs(c.ok, False)
        self.assertIn("not on disk", c.detail)
        self.assertIn("missing roots: none", c.detail)                        # declared in config.toml, just gone


class ExposureDoctorTests(unittest.TestCase):
    """The security checks: what a sandbox may write, the spool, the DB
    socket and shared-role transcripts, and loose ~/.local dirs (safefs refuses them)."""

    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="swarm-doc-"))
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        self.codex = self.home / ".codex"; self.codex.mkdir()
        import support
        p = mock.patch.dict(os.environ, {**home_env(self.home), "SWARM_VENV": str(support.temp_venv()),
                                         "CODEX_HOME": str(self.codex), "SWARM_AUTO_INIT": "1"})
        p.start(); self.addCleanup(p.stop)
        os.environ.pop("CLAUDE_SETTINGS", None)
        self.cfg = self.home / ".config/swarm/config.toml"
        self.cfg.parent.mkdir(parents=True)
        self.write_cfg()

    def write_cfg(self, extra="", spool="~/.local/state/swarm/spool", board=None):
        board = board or f"{self.home}/.local/share/swarm-board/board.sqlite3"
        self.cfg.write_text(f'[board]\nbackend = "sqlite"\nspool_dir = {tq(spool)}\n'
                            f'[sqlite]\npath = {tq(board)}\n' + extra)

    def roots(self, *roots, where="config.toml", table="sandbox_workspace_write", extra=""):
        body = extra + f"[{table}]\nwritable_roots = {json.dumps([str(r) for r in roots])}\n"
        (self.codex / where).write_text(body)

    def checks(self, host="claude"):
        with mock.patch.object(bootstrap, "_codex_plugin_listed", return_value=True):
            return {c.name: c for c in bootstrap.doctor(host, config=self.cfg, env={})}

    def test_state_dir_writable_root_fails(self):
        self.roots(self.home / ".local/state/swarm")
        c = self.checks()["sandbox roots"]
        self.assertIs(c.ok, False)
        self.assertIn(str(self.home / ".local/state/swarm"), c.detail)
        self.assertIn("writable_roots", c.fix)
        for h in ("codex", None):
            self.assertIs(self.checks(h)["sandbox roots"].ok, False)

    def test_spool_and_marker_roots_pass(self):
        self.roots(self.home / ".local/state/swarm/spool", self.home / ".local/state/swarm/active")
        self.assertIs(self.checks()["sandbox roots"].ok, True)

    def test_exposed_share_dir_fails_from_every_source(self):
        """All of ~/.local/share/swarm (venv, host/, supervisor/): the root itself, an
        ancestor, a dir inside it; base table, legacy profile, profile file, Claude allowWrite."""
        cases = [dict(roots=[self.home / ".local/share/swarm"]),
                 dict(roots=[self.home / ".local"]),
                 dict(roots=[self.home]),
                 dict(roots=[self.home / ".local/share/swarm/venv"]),
                 dict(roots=[self.home / ".local/share/swarm/host"], table="profiles.p.sandbox_workspace_write"),
                 dict(roots=[self.home / ".local/share/swarm/supervisor"], where="p.config.toml")]
        for case in cases:
            for f in self.codex.glob("*.toml"):
                f.unlink()
            self.roots(*case.pop("roots"), **case)
            c = self.checks()["sandbox roots"]
            self.assertIs(c.ok, False, case)
            self.assertIn(os.path.normpath(".local/share/swarm"), c.detail)
        for f in self.codex.glob("*.toml"):
            f.unlink()
        s = self.home / ".claude/settings.json"; s.parent.mkdir()
        s.write_text(json.dumps({"sandbox": {"filesystem": {"allowWrite": [str(self.home / ".local/share")]}}}))
        c = self.checks()["sandbox roots"]
        self.assertIs(c.ok, False)
        self.assertIn("allowWrite", c.detail + c.fix)

    def test_board_under_writable_root_fails(self):
        work = self.home / "work"; work.mkdir()
        self.write_cfg(board=f"{work}/b.sqlite3")
        self.roots(work)
        c = self.checks()["board location"]
        self.assertIs(c.ok, False)
        self.assertIn(str(work), c.detail)
        self.assertIn("fake", c.detail)
        self.roots(self.home / ".local/state/swarm/spool")
        self.assertIs(self.checks()["board location"].ok, True)
        self.write_cfg(extra="[codex]\nboard_writable = true\n")           # opted in: still a FAIL, explained
        from swarm import codex_config
        from swarm.cli import load_config
        self.roots(*codex_config.required(load_config(self.cfg))[("sandbox_workspace_write", "writable_roots")])
        c = self.checks()["board location"]
        self.assertIs(c.ok, False)
        self.assertIn("board_writable", c.detail + c.fix)

    def test_postgres_board_has_no_location_check(self):
        self.cfg.write_text('[board]\nspool_dir = "~/.local/state/swarm/spool"\n'
                            '[database]\nhost = "db.invalid"\nconnect_timeout = 1\n')
        self.roots(self.home)
        self.assertNotIn("board location", self.checks())

    def test_spool_shared_default_fails(self):
        self.write_cfg(spool=bootstrap.OLD_SPOOL)   # the old shared default (a test path: support.py)
        c = self.checks()["spool dir"]
        self.assertIs(c.ok, False)
        self.assertIn("shared", c.detail)
        self.assertIn("~/.local/state/swarm/spool", c.fix)

    @posix_only("needs Unix uids (no ownership checks on Windows)")
    def test_spool_in_tmp_must_name_the_uid(self):
        self.write_cfg(spool="/tmp/swarm-spool-shared")
        self.assertIs(self.checks()["spool dir"].ok, False)
        self.write_cfg(spool=f"/tmp/claude-{os.getuid()}7/swarm-spool")          # a longer number: not the uid
        self.assertIs(self.checks()["spool dir"].ok, False)
        self.write_cfg(spool=f"/tmp/claude-{os.getuid()}/swarm-spool")
        self.assertIs(self.checks()["spool dir"].ok, True)

    @posix_only("needs Unix uids (no ownership checks on Windows)")
    def test_spool_component_owned_by_another_uid_fails(self):
        real = os.getuid()
        with mock.patch("os.getuid", return_value=real + 4242):
            c = self.checks()["spool dir"]
        self.assertIs(c.ok, False)
        self.assertIn("another user", c.detail)

    def test_spool_symlink_component_fails(self):
        elsewhere = self.home / "elsewhere"; elsewhere.mkdir()
        (self.home / ".local/state").mkdir(parents=True)
        (self.home / ".local/state/swarm").symlink_to(elsewhere)
        c = self.checks()["spool dir"]
        self.assertIs(c.ok, False)
        self.assertIn("symlink", c.detail)

    def test_default_spool_passes(self):
        self.assertIs(self.checks()["spool dir"].ok, True)

    def test_base_network_access_warns(self):
        (self.codex / "config.toml").write_text("[sandbox_workspace_write]\nnetwork_access = true\n")
        c = self.checks("codex")["codex network"]
        self.assertIsNone(c.ok)
        self.assertIn("network_access", c.fix)
        (self.codex / "config.toml").write_text("[sandbox_workspace_write]\nnetwork_access = false\n")
        self.assertIs(self.checks("codex")["codex network"].ok, True)

    def test_codex_sandbox_no_longer_needs_network(self):
        from swarm import codex_config
        from swarm.cli import load_config
        codex_config.apply(self.codex / "config.toml", load_config(self.cfg))
        c = self.checks("codex")["codex sandbox"]
        self.assertIs(c.ok, True, c.detail)

    def test_unix_socket_db_warn(self):
        """The Codex Linux sandbox may still permit AF_UNIX connects. Not verified: no codex
        binary or source on this machine, and tests/fixtures/codex/0.157.1 records nothing about
        socket policy; so doctor warns for any socket-path host until it is checked."""
        self.cfg.write_text('[board]\nspool_dir = "~/.local/state/swarm/spool"\n'
                            '[database]\nhost = "/var/run/postgresql"\nconnect_timeout = 1\n')
        c = self.checks()["db socket"]
        self.assertIsNone(c.ok)
        self.assertIn("Unix socket", c.detail)
        self.cfg.write_text('[board]\nspool_dir = "~/.local/state/swarm/spool"\n'
                            '[database]\nhost = "db.invalid"\nconnect_timeout = 1\n'
                            '[watch_database]\nhost = "/run/pg"\n')
        self.assertIsNone(self.checks()["db socket"].ok)
        self.cfg.write_text('[board]\nspool_dir = "~/.local/state/swarm/spool"\n'
                            '[database]\nhost = "db.invalid"\nconnect_timeout = 1\n')
        self.assertNotIn("db socket", self.checks())

    def test_transcripts_multi_user_warn(self):
        import sqlite3
        from swarm.board import open_board
        from swarm.cli import load_config
        self.write_cfg(extra="[transcripts]\nenabled = true\n")
        cfg = load_config(self.cfg)
        with open_board(cfg) as b:
            b.ensure_job("J")
            b.allocate_name("k1", "J")
            b.allocate_name("k2", "J")
        self.assertIs(self.checks()["transcripts users"].ok, True)
        db = sqlite3.connect(cfg["sqlite"]["path"])
        db.execute("UPDATE agents SET os_user = os_user || '-other' WHERE agent_key = 'k2'"); db.commit(); db.close()
        c = self.checks()["transcripts users"]
        self.assertIsNone(c.ok)
        self.assertIn("2 OS users", c.detail)
        self.assertIn("read", c.detail)
        self.write_cfg(extra="[provenance]\nenabled = false\n")              # both off: no check
        self.assertNotIn("transcripts users", self.checks())

    def _two_users(self, extra: str):
        import sqlite3
        from swarm.board import open_board
        from swarm.cli import load_config
        self.write_cfg(extra=extra)
        cfg = load_config(self.cfg)
        with open_board(cfg) as b:
            b.ensure_job("J")
            b.allocate_name("k1", "J")
            b.allocate_name("k2", "J")
        db = sqlite3.connect(cfg["sqlite"]["path"])
        db.execute("UPDATE agents SET os_user = os_user || '-other' WHERE agent_key = 'k2'"); db.commit(); db.close()
        return self.checks()

    EXCERPTS = "memory excerpts are readable by every OS user sharing the board role"

    def test_provenance_multi_user_warn(self):
        """memory excerpts sit in the shared board too, so the
        warning fires with [provenance] enabled (the default) even when transcripts are off."""
        c = self._two_users("")                                             # provenance by default
        self.assertIsNone(c["transcripts users"].ok)
        self.assertIn(self.EXCERPTS, c["transcripts users"].detail)
        self.assertIn("2 OS users", c["transcripts users"].detail)
        self.assertIn("[provenance] enabled", c["transcripts users"].fix)

    def test_provenance_single_user_ok(self):
        self.write_cfg(extra="[provenance]\nenabled = true\n")
        from swarm.board import open_board
        from swarm.cli import load_config
        with open_board(load_config(self.cfg)) as b:
            b.ensure_job("J")
            b.allocate_name("k1", "J")
        self.assertIs(self.checks()["transcripts users"].ok, True)

    def test_transcripts_only_multi_user_warn_leaves_excerpts_out(self):
        c = self._two_users("[transcripts]\nenabled = true\n[provenance]\nenabled = false\n")
        self.assertIsNone(c["transcripts users"].ok)
        self.assertNotIn("excerpts", c["transcripts users"].detail)
        self.assertIn("transcripts", c["transcripts users"].detail)

    def test_both_on_multi_user_names_both(self):
        c = self._two_users("[transcripts]\nenabled = true\n")
        self.assertIn(self.EXCERPTS, c["transcripts users"].detail)
        self.assertIn("archived transcripts", c["transcripts users"].detail)

    @posix_only("needs POSIX file modes (Windows has ACLs)")
    def test_group_writable_local_dirs_fail(self):
        for d in (".local/share", ".local/state"):
            (self.home / d).mkdir(parents=True, exist_ok=True)
        self.assertIs(self.checks()["local dirs"].ok, True)
        (self.home / ".local").chmod(0o775)
        (self.home / ".local/state").chmod(0o777)
        c = self.checks()["local dirs"]
        self.assertIs(c.ok, False)
        self.assertIn(str(self.home / ".local"), c.detail)
        self.assertIn(str(self.home / ".local/state"), c.detail)
        self.assertNotIn(str(self.home / ".local/share"), c.detail)
        self.assertTrue(c.fix.startswith("chmod go-w"))


class FakeRun:
    """A stand-in for subprocess.run: answers systemctl, git and codex from a table, records calls."""

    def __init__(self, *, systemctl="ActiveState=active\nUnitFileState=enabled\n", git="git version 2.43.0",
                 features="rollout_budget  experimental  true\n", systemctl_rc=0):
        self.systemctl, self.git, self.features, self.systemctl_rc = systemctl, git, features, systemctl_rc
        self.calls = []

    def __call__(self, argv, **kw):
        self.calls.append((list(argv), kw))
        out, rc = "", 0
        if argv[0] == "systemctl":
            out, rc = self.systemctl, self.systemctl_rc
        elif argv[0] == "git":
            out = self.git
        elif argv[0] == "codex":
            out = self.features
        return mock.Mock(returncode=rc, stdout=out, stderr="")


class DriveDoctorTests(unittest.TestCase):
    NOW = dt.datetime(2026, 10, 11, 12, 0, tzinfo=dt.timezone.utc)

    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="swarm-ddoc-"))
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        p = mock.patch.dict(os.environ, {**home_env(self.home),
                                         "CLAUDE_SETTINGS": str(self.home / "claude-settings.json"),
                                         "SWARM_AUTO_INIT": "1"})
        p.start(); self.addCleanup(p.stop)
        self.cfg_path = self.home / ".config/swarm/config.toml"
        self.cfg_path.parent.mkdir(parents=True)
        self.write_cfg("")
        self.state = {"last_run_at": (self.NOW - dt.timedelta(minutes=3)).isoformat()}
        q = mock.patch("swarm.supervisor.settings.load_state", side_effect=lambda: self.state)
        q.start(); self.addCleanup(q.stop)

    def write_cfg(self, extra):
        self.cfg_path.write_text(f'[board]\nbackend = "sqlite"\nspool_dir = "~/.local/state/swarm/spool"\n'
                                 f'[sqlite]\npath = {tq(f"{self.home}/b.sqlite3")}\n{extra}')

    def cfg(self):
        from swarm.cli import load_config
        return load_config(self.cfg_path)

    def key(self):
        from swarm import drive_activate
        return drive_activate.board_key(self.cfg())

    def ledger(self, job="J", **kw):
        from swarm.supervisor import ledger
        kw.setdefault("budgets", {})
        kw.setdefault("activated_at", (self.NOW - dt.timedelta(hours=1)).isoformat())
        return ledger.create(self.key(), job, **kw)

    def run_checks(self, run=None, which=None, platform="linux", now=None, **kw):
        run = run or FakeRun()
        which = which or (lambda name, path=None: None)
        checks = bootstrap.drive_checks(self.cfg(), run=run, which=which, now=now or self.NOW, platform=platform, **kw)
        return {c.name: c for c in checks}

    # ---- relevance
    def test_nothing_without_drive_jobs_or_config(self):
        self.assertEqual(bootstrap.drive_checks(self.cfg(), run=FakeRun(), which=lambda n, path=None: None,
                                                now=self.NOW, platform="linux"), [])

    # ---- running timer
    @posix_only("systemd")
    def test_timer_running_and_recent_pass_is_ok(self):
        self.ledger()
        c = self.run_checks()["drive timer"]
        self.assertTrue(c.ok)
        self.assertIn("3 min ago", c.detail)
        self.assertIn("active/enabled", c.detail)
        self.assertNotIn("no user bus", c.detail)

    @posix_only("systemd")
    def test_timer_not_active_fails(self):
        self.ledger()
        run = FakeRun(systemctl="ActiveState=inactive\nUnitFileState=enabled\n")
        c = self.run_checks(run)["drive timer"]
        self.assertIs(c.ok, False)
        self.assertIn("inactive/enabled", c.detail)

    @posix_only("systemd")
    def test_timer_not_enabled_fails(self):
        self.ledger()
        run = FakeRun(systemctl="ActiveState=active\nUnitFileState=disabled\n")
        self.assertIs(self.run_checks(run)["drive timer"].ok, False)

    @posix_only("systemd")
    def test_stale_last_pass_fails_beyond_two_periods(self):
        self.ledger()
        self.state["last_run_at"] = (self.NOW - dt.timedelta(minutes=10.5)).isoformat()   # timer_minutes 5
        c = self.run_checks()["drive timer"]
        self.assertIs(c.ok, False)
        self.assertIn("last pass", c.detail)
        self.state["last_run_at"] = (self.NOW - dt.timedelta(minutes=9.5)).isoformat()
        self.assertTrue(self.run_checks()["drive timer"].ok)

    @posix_only("systemd")
    def test_never_run_or_future_dated_pass_fails(self):
        self.ledger()
        self.state.clear()
        self.assertIs(self.run_checks()["drive timer"].ok, False)
        self.state["last_run_at"] = (self.NOW + dt.timedelta(minutes=30)).isoformat()
        self.assertIs(self.run_checks()["drive timer"].ok, False)

    @posix_only("systemd")
    def test_unreadable_bus_with_recent_pass_is_not_a_false_fail(self):
        self.ledger()
        c = self.run_checks(FakeRun(systemctl="", systemctl_rc=1))["drive timer"]
        self.assertTrue(c.ok)
        self.assertNotIn("?/?", c.detail)
        self.assertIn("no user bus", c.detail)
        self.state["last_run_at"] = (self.NOW - dt.timedelta(hours=2)).isoformat()
        self.assertIs(self.run_checks(FakeRun(systemctl="", systemctl_rc=1))["drive timer"].ok, False)

    @posix_only("systemd")
    def test_timer_is_read_with_the_user_managers_environment(self):
        self.ledger()
        run = FakeRun()
        with mock.patch("swarm.supervisor.systemd.manager_env", return_value={"XDG_RUNTIME_DIR": "/run/user/4242"}):
            self.run_checks(run)
        shown = [kw for argv, kw in run.calls if argv[0] == "systemctl"]
        self.assertTrue(shown)
        self.assertEqual(shown[0]["env"], {"XDG_RUNTIME_DIR": "/run/user/4242"})

    @posix_only("systemd")
    def test_disabled_supervisor_fails_drive_timer(self):
        self.write_cfg("[supervise]\nenabled = false\n")
        self.ledger()
        c = self.run_checks()["drive timer"]
        self.assertIs(c.ok, False)
        self.assertIn("disabled", c.detail)

    @posix_only("systemd")
    def test_drive_config_alone_adds_no_timer_line(self):
        self.write_cfg("[drive]\nmax_cost_usd = 10\n")
        checks = self.run_checks()
        self.assertNotIn("drive timer", checks)
        self.assertIn("drive git", checks)

    def test_other_platforms_say_drive_is_unavailable(self):
        self.write_cfg("[drive]\nmax_cost_usd = 10\n")
        for plat in ("darwin", "win32"):
            checks = self.run_checks(platform=plat)
            self.assertEqual(list(checks), ["drive mode"])
            self.assertTrue(checks["drive mode"].ok)
            self.assertEqual(checks["drive mode"].detail, "drive mode unavailable: no supervisor timer")

    # ---- git
    def test_git_below_2_40_fails_with_drive_jobs(self):
        self.ledger()
        for version in ("2.39.5", "2.9.1", "1.8.3"):
            c = self.run_checks(FakeRun(git=f"git version {version}"))["drive git"]
            self.assertIs(c.ok, False, version)
            self.assertIn(version, c.detail)
            self.assertIn("2.40", c.fix)

    def test_git_2_40_and_newer_pass(self):
        self.ledger()
        for version in ("2.40.0", "2.43.0", "2.100.1", "3.0.0", "2.50.1.windows.1"):
            self.assertTrue(self.run_checks(FakeRun(git=f"git version {version}"))["drive git"].ok, version)

    def test_old_git_only_warns_without_drive_jobs(self):
        self.write_cfg("[drive]\nmax_cost_usd = 10\n")
        self.assertIsNone(self.run_checks(FakeRun(git="git version 2.30.2"))["drive git"].ok)

    def test_git_missing_or_unreadable_version_is_a_warning(self):
        self.ledger()
        self.assertIsNone(self.run_checks(FakeRun(git="what"))["drive git"].ok)
        run = mock.Mock(side_effect=lambda argv, **kw: (_ for _ in ()).throw(OSError("no git")) if argv[0] == "git"
                        else FakeRun()(argv, **kw))
        self.assertIsNone(self.run_checks(run)["drive git"].ok)

    # ---- Claude Bash sandbox
    def settings(self, data):
        Path(os.environ["CLAUDE_SETTINGS"]).write_text(data if isinstance(data, str) else json.dumps(data))

    def test_bash_sandbox_enabled_is_a_boundary(self):
        self.ledger()
        self.settings({"sandbox": {"enabled": True}})
        c = self.run_checks()["claude sandbox"]
        self.assertTrue(c.ok)
        self.assertIn("boundary", c.detail)
        self.assertNotIn("guardrail only", c.detail)

    def test_bash_sandbox_off_missing_or_unreadable_is_guardrail_only(self):
        self.ledger()
        for data in ({"sandbox": {"enabled": False}}, {"sandbox": {"enabled": "true"}}, {}, {"sandbox": []},
                     "{not json", None):
            if data is not None:
                self.settings(data)
            c = self.run_checks()["claude sandbox"]
            self.assertIsNone(c.ok, data)
            self.assertIn("guardrail only", c.detail)
            self.assertNotIn("boundary", c.detail)

    # ---- Codex
    def codex_home(self, text):
        d = self.home / ".codex"
        d.mkdir(exist_ok=True)
        (d / "config.toml").write_text(text)
        p = mock.patch.dict(os.environ, {"CODEX_HOME": str(d)}); p.start(); self.addCleanup(p.stop)

    def test_no_codex_lines_without_codex(self):
        self.ledger()
        names = self.run_checks()
        self.assertNotIn("codex board access", names)
        self.assertNotIn("codex rollout budget", names)

    def test_codex_writer_ready_when_the_store_dir_is_a_writable_root(self):
        self.ledger()
        which = lambda name, path=None: "/usr/bin/codex" if name == "codex" else None   # noqa: E731
        self.codex_home(f'[sandbox_workspace_write]\nwritable_roots = [{tq(self.home)}]\n')
        c = self.run_checks(which=which)["codex board access"]
        self.assertTrue(c.ok)
        self.assertEqual(c.detail, "codex writer seats reach the board")
        self.codex_home('[sandbox_workspace_write]\nwritable_roots = ["/elsewhere"]\n')
        c = self.run_checks(which=which)["codex board access"]
        self.assertIsNone(c.ok)
        self.assertIn("cannot reach the board", c.detail)

    def test_codex_writer_on_postgres_needs_network_access(self):
        self.ledger()
        which = lambda name, path=None: "/usr/bin/codex" if name == "codex" else None   # noqa: E731
        cfg = self.cfg()
        with mock.patch("swarm.codex_config.board_dir", return_value=None):
            self.codex_home("[sandbox_workspace_write]\nnetwork_access = true\n")
            cfg["codex"] = {"network_access": True}
            yes = {c.name: c for c in bootstrap.drive_checks(cfg, run=FakeRun(), which=which, now=self.NOW,
                                                             platform="linux")}["codex board access"]
            self.assertEqual(yes.detail, "codex writer seats reach the board")
            cfg["codex"] = {}
            no = {c.name: c for c in bootstrap.drive_checks(cfg, run=FakeRun(), which=which, now=self.NOW,
                                                            platform="linux")}["codex board access"]
            self.assertIn("cannot reach the board", no.detail)
            self.assertIn("network_access", no.detail)
            cfg["codex"] = {"network_access": True}
            self.codex_home("[sandbox_workspace_write]\n")
            half = {c.name: c for c in bootstrap.drive_checks(cfg, run=FakeRun(), which=which, now=self.NOW,
                                                              platform="linux")}["codex board access"]
            self.assertIn("cannot reach the board", half.detail)

    # ---- budget and models
    def test_budget_line_per_active_job(self):
        from swarm.supervisor import ledger
        led = self.ledger(budgets={"max_cost_usd": 10, "ruling_reserve_usd": 2, "max_sessions": 5,
                                   "max_wall_hours": 3})
        led.intents.append(ledger.Intent("L1", "a1", 1, "a", 2.0, 60.0, [], "concluded", "ok", True, 1.5))
        led.intents.append(ledger.Intent("L2", "a2", 1, "b", 0.5, 60.0, [], "claimed"))
        led.save()
        c = self.run_checks()["drive budget J"]
        self.assertTrue(c.ok)
        self.assertEqual(c.detail, "cost 2.00/10.00 USD (reserve 2.00), sessions 2/5, wall 1.0/3 h")

    def test_budget_line_defaults_and_config_overlay(self):
        self.write_cfg("[drive]\nmax_cost_usd = 25\n")
        self.ledger()
        self.assertEqual(self.run_checks()["drive budget J"].detail,
                         "cost 0.00/25.00 USD (reserve 5.00), sessions 0/60, wall 1.0/24 h")

    def test_budget_wall_clock_excludes_halted_time(self):
        led = self.ledger(budgets={"max_wall_hours": 3})
        led.halted_total_seconds = 1800.0
        led.save()
        self.assertIn("wall 0.5/3 h", self.run_checks()["drive budget J"].detail)

    def test_exhausted_budget_warns(self):
        from swarm.supervisor import ledger
        led = self.ledger(budgets={"max_cost_usd": 3, "ruling_reserve_usd": 1, "max_sessions": 9})
        led.intents.append(ledger.Intent("L1", "a1", 1, "a", 3.0, 60.0, [], "concluded", "ok", True, 3.0))
        led.save()
        self.assertIsNone(self.run_checks()["drive budget J"].ok)
        led = self.ledger("K", budgets={"max_sessions": 1})
        led.intents.append(ledger.Intent("L1", "a1", 1, "a", 1.0, 60.0, [], "concluded", "ok", True, 0.1))
        led.save()
        self.assertIsNone(self.run_checks()["drive budget K"].ok)
        self.ledger("W", budgets={"max_wall_hours": 0.5})
        self.assertIsNone(self.run_checks()["drive budget W"].ok)

    def test_model_table_line_uses_job_overrides_then_config_then_defaults(self):
        self.write_cfg('[drive.models]\nreviewer = "opus"\n')
        self.ledger(models={"engineer": "sonnet"})
        c = self.run_checks()["drive models J"]
        self.assertTrue(c.ok)
        for want in ("engineer=sonnet", "reviewer=opus", "judge=opus", "worker=haiku", "lead=opus", "qa=sonnet"):
            self.assertIn(want, c.detail)

    def test_bad_model_config_is_a_warning_not_a_crash(self):
        self.write_cfg('[drive.models]\nreviewer = "gpt-9"\n')
        self.ledger()
        checks = self.run_checks()
        self.assertIsNone(checks["drive models J"].ok)
        self.assertIn("allowed_models", checks["drive models J"].detail)

    def test_finished_jobs_get_no_lines(self):
        done = self.ledger("DONE")
        done.terminal = {"verdict": "met", "at": self.NOW.isoformat()}
        done.save()
        self.ledger("LIVE")
        names = list(self.run_checks())
        self.assertIn("drive budget LIVE", names)
        self.assertNotIn("drive budget DONE", names)
        self.assertNotIn("drive models DONE", names)

    # ---- credential helpers
    def owner(self, credentials, job="J"):
        from swarm import enrolment
        enrolment.write_job_owner(self.key(), job=job, harness="claude", session_id=None, cwd=str(self.home),
                                  roots=[str(self.home)], remotes={}, credentials=credentials, requirements={},
                                  recipe="coding", goal="G")

    @posix_only("executable bits")
    def test_credential_helper_resolvable_or_not_per_destination(self):
        self.ledger()
        good = self.home / "bin/gh"
        good.parent.mkdir()
        good.write_text("#!/bin/sh\n"); good.chmod(0o755)
        noexec = self.home / "bin/git-credential-store"
        noexec.write_text("#!/bin/sh\n"); noexec.chmod(0o644)
        self.owner({"origin": f"helper:{good} auth git-credential",
                    "mirror": f"helper:{self.home}/gone/gh auth git-credential",
                    "other": f"helper:{noexec}"})
        checks = self.run_checks()
        self.assertTrue(checks["drive credential origin"].ok)
        self.assertIn(str(good), checks["drive credential origin"].detail)
        gone = checks["drive credential mirror"]
        self.assertIs(gone.ok, False)
        self.assertIn(f"{self.home}/gone/gh", gone.detail)
        self.assertIn("--credential", gone.fix)
        self.assertIs(checks["drive credential other"].ok, False)
        (self.home / "bin/adir/gh").mkdir(mode=0o755, parents=True)   # an allowlisted name that is a directory
        self.owner({"d": f"helper:{self.home}/bin/adir/gh auth git-credential"})
        self.assertIs(self.run_checks()["drive credential d"].ok, False)

    def test_non_helper_credentials_and_missing_owner_add_no_helper_line(self):
        self.ledger()
        self.owner({"ssh": "ssh-agent:/run/user/1/agent.sock", "key": f"ssh-key:{self.home}/k"})
        self.assertFalse([n for n in self.run_checks() if n.startswith("drive credential")])
        self.ledger("NOOWNER")
        self.assertFalse([n for n in self.run_checks() if n.startswith("drive credential")])

    def test_land_lines_are_added_for_each_jobs_pinned_destinations(self):
        import sys
        import types
        from collections import namedtuple
        Line = namedtuple("Line", "name ok detail fix")
        seen = []
        fake = types.ModuleType("swarm.land_doctor")
        fake.checks = lambda cfg, owners=None: seen.append(owners) or [Line("land x ls-remote", False, "boom", None)]
        self.ledger()
        self.owner({"origin": "ssh-agent:/run/user/1/a.sock"})
        import swarm
        with mock.patch.dict(sys.modules, {"swarm.land_doctor": fake}), \
                mock.patch.object(swarm, "land_doctor", fake, create=True):
            c = self.run_checks()["land x ls-remote"]
        self.assertIs(c.ok, False)
        self.assertEqual((c.detail, c.fix), ("boom", ""))
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0][0]["credentials"], {"origin": "ssh-agent:/run/user/1/a.sock"})

    def test_no_land_lines_without_drive_jobs(self):
        import sys
        import types
        fake = types.ModuleType("swarm.land_doctor")
        fake.checks = lambda cfg, owners=None: self.fail("called without drive jobs")
        self.write_cfg("[drive]\nmax_cost_usd = 10\n")
        import swarm
        with mock.patch.dict(sys.modules, {"swarm.land_doctor": fake}), \
                mock.patch.object(swarm, "land_doctor", fake, create=True):
            self.run_checks()

    # ---- bad ledgers
    def test_unreadable_ledger_fails_as_driver_ledger_bad(self):
        from swarm.supervisor import privfs
        self.ledger()
        with privfs.dir_fd("drive") as d:
            privfs.write_atomic(d, "ab" * 32 + ".json", "{not json")
        checks = self.run_checks()
        bad = [c for n, c in checks.items() if n.startswith("drive ledger")]
        self.assertEqual([c.ok for c in bad], [False])
        self.assertIn("DRIVER-LEDGER-BAD", bad[0].detail)
        self.assertIn("drive timer", checks)       # an unreadable ledger still means drive jobs here

    # ---- orphans, claims, goal jobs ([A], DRIVER-ORPHAN)
    def board_job(self, job, *, claim=False, goal="G"):
        from swarm.board import open_board
        with open_board(self.cfg()) as b:
            b.open_job(job, "d", None, None, "me", goal=goal)
            if claim:
                b.set_job_data(job, "drive.enabled", "1")

    def later(self, seconds):
        return dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=seconds)

    def test_ledger_without_owner_record_is_a_driver_orphan(self):
        from swarm.supervisor import driver_events
        self.ledger("J")
        c = self.run_checks()["drive orphan J"]
        self.assertIs(c.ok, False)
        self.assertIn(driver_events.DRIVER_ORPHAN, c.detail)
        self.assertIn("owner record", c.detail)
        self.owner({})
        self.assertNotIn("drive orphan J", self.run_checks())

    def test_board_claim_without_ledger_fails_only_past_the_grace(self):
        from swarm.supervisor import driver_events
        self.board_job("C", claim=True)
        self.write_cfg("[drive]\nmax_cost_usd = 10\n")
        within = bootstrap.drive_checks(self.cfg(), run=FakeRun(), which=lambda n, path=None: None,
                                        now=self.later(100), platform="linux")
        self.assertNotIn("drive orphan C", {c.name for c in within})
        past = {c.name: c for c in bootstrap.drive_checks(self.cfg(), run=FakeRun(), which=lambda n, path=None: None,
                                                          now=self.later(125), platform="linux")}
        self.assertIs(past["drive orphan C"].ok, False)
        self.assertIn(driver_events.DRIVER_ORPHAN, past["drive orphan C"].detail)
        self.assertIn("claim", past["drive orphan C"].detail)

    def test_claim_with_a_ledger_or_a_terminal_ledger_is_not_an_orphan(self):
        self.board_job("L", claim=True)
        self.ledger("L")
        self.owner({}, job="L")
        done = self.ledger("T")
        done.terminal = {"verdict": "met", "at": self.NOW.isoformat()}
        done.save()
        self.board_job("T", claim=True)
        names = {c.name for c in bootstrap.drive_checks(self.cfg(), run=FakeRun(), which=lambda n, path=None: None,
                                                        now=self.later(3600), platform="linux")}
        self.assertNotIn("drive orphan L", names)
        self.assertNotIn("drive orphan T", names)

    def test_goal_jobs_activated_before_drive_mode_are_listed(self):
        self.board_job("OLD", claim=False, goal="G")
        self.board_job("NOGOAL", claim=False, goal=None)
        self.board_job("DRIVEN", claim=True, goal="G")
        self.ledger("DRIVEN")
        self.owner({}, job="DRIVEN")
        self.board_job("NOCLAIM", claim=False, goal="G")   # a ledger, but the claim was lost: not "old"
        self.ledger("NOCLAIM")
        self.owner({}, job="NOCLAIM")
        c = self.run_checks(now=self.later(0))["drive not autonomous"]
        self.assertNotIn("NOCLAIM", c.detail)
        self.assertIsNone(c.ok)
        self.assertIn("activated before drive mode", c.detail)
        self.assertIn("OLD", c.detail)
        self.assertNotIn("NOGOAL", c.detail)
        self.assertNotIn("DRIVEN", c.detail)

    def test_no_list_when_every_goal_job_is_driven_or_there_is_no_board(self):
        self.ledger("J")
        self.assertNotIn("drive not autonomous", self.run_checks())   # no board store: nothing read

    def test_board_unreadable_is_a_warning_line(self):
        from swarm.board import BoardUnavailable
        self.board_job("X")
        with mock.patch("swarm.board.open_board", side_effect=BoardUnavailable("down")):
            c = self.run_checks()["drive board"]
        self.assertIsNone(c.ok)

    # ---- read-only, and loud when a ledger can't be read
    def tree(self):
        return sorted(str(p.relative_to(self.home)) for p in self.home.rglob("*"))

    def test_doctor_creates_nothing_on_a_fresh_home(self):
        self.write_cfg("[drive]\nmax_cost_usd = 10\n")
        before = self.tree()
        checks = self.run_checks()
        self.assertIn("drive git", checks)
        self.assertEqual(self.tree(), before)
        self.assertFalse((self.home / ".local/share/swarm/supervisor").exists())

    @posix_only("symlinks")
    def test_unreadable_ledger_dir_is_a_fail_line_not_silence(self):
        from swarm.supervisor import driver_events, ledger, settings
        self.ledger("J")
        d = settings.private_dir() / ledger.SUBDIR
        lock = d / ledger._lock_name(ledger._name(self.key(), "J"))
        lock.unlink(missing_ok=True)
        lock.symlink_to(self.home / "nowhere")
        checks = self.run_checks()
        c = checks["drive ledgers"]
        self.assertIs(c.ok, False)
        self.assertIn(driver_events.DRIVER_LEDGER_BAD, c.detail)
        self.assertIn("held", c.detail)
        self.assertIn("drive timer", checks)

    # ---- wiring
    @posix_only("systemd")
    def test_doctor_includes_the_drive_lines(self):
        self.ledger()
        import support
        with mock.patch.dict(os.environ, {"SWARM_VENV": str(support.temp_venv()), "SWARM_AUTO_INIT": "1"}), \
             mock.patch("swarm.bootstrap.drive_checks", return_value=[bootstrap.Check("drive probe", True, "x")]) as m:
            names = [c.name for c in bootstrap.doctor("claude", config=self.cfg_path)]
        self.assertIn("drive probe", names)
        self.assertEqual(m.call_count, 1)
