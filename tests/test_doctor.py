from __future__ import annotations

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
        self.assertTrue(checks["supervise"].ok)

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
        self.assertIn("forge", c.detail)
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
        db.execute("UPDATE agents SET os_user = 'codex' WHERE agent_key = 'k2'"); db.commit(); db.close()
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
        db.execute("UPDATE agents SET os_user = 'codex' WHERE agent_key = 'k2'"); db.commit(); db.close()
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
