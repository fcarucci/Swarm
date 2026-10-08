"""`swarm upgrade` (formerly `swarm update`, kept as a hidden alias) and its automatic migrate --force."""
from __future__ import annotations

import io
import json
import os
import subprocess
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest import mock

from support import ROOT  # noqa: F401

from swarm import cli, update  # noqa: E402


class UpgradeCommandTests(unittest.TestCase):
    def _run(self, *argv):
        seen = {}
        with mock.patch.object(update, "run_update", side_effect=lambda *a, **k: seen.setdefault("a", a) and 0):
            rc = cli.main(["upgrade" if False else argv[0], *argv[1:]])
        return rc, seen

    def test_upgrade_runs_the_update_code(self):
        rc, seen = self._run("upgrade", "--host", "claude", "--force")
        self.assertEqual(rc, 0)
        self.assertEqual(seen["a"][:2], ("claude", True))

    def test_update_is_still_an_alias(self):
        rc, seen = self._run("update", "--host", "codex")
        self.assertEqual(rc, 0)
        self.assertEqual(seen["a"][:2], ("codex", False))

    def test_help_documents_upgrade_and_hides_update(self):
        out = io.StringIO()
        with redirect_stdout(out), self.assertRaises(SystemExit):
            cli.main(["--help"])
        text = out.getvalue()
        self.assertIn("upgrade", text)
        self.assertNotRegex(text, r"(?m)^\s+update\b")
        self.assertNotIn(",update", text)
        self.assertNotIn("update,", text)


OLD, TIP = "a" * 40, "b" * 40


class ForcePullTests(unittest.TestCase):
    """`swarm upgrade --force` reinstalls at an unchanged version; a plain upgrade on main warns."""

    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="swarm-force-"))
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        self.plugins = self.home / ".claude" / "plugins"
        self.plugins.mkdir(parents=True)
        self.tree = self.home / "tree"
        (self.tree / ".claude-plugin").mkdir(parents=True)
        (self.tree / ".claude-plugin" / "plugin.json").write_text('{"name":"swarm","version":"0.2.0"}')
        self.set_installed(OLD)
        (self.plugins / "known_marketplaces.json").write_text(json.dumps(
            {"swarm": {"source": {"source": "git", "url": "https://example.com/s.git"}}}))
        self.calls = []
        env = mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.home / ".claude"),
                                           "CODEX_HOME": str(self.home / ".codex"), "HOME": str(self.home)})
        env.start(); self.addCleanup(env.stop)

    def set_installed(self, sha):
        entry = {"installPath": str(self.tree), "version": "0.2.0"}
        if sha:
            entry["gitCommitSha"] = sha
        (self.plugins / "installed_plugins.json").write_text(json.dumps({"plugins": {"swarm@swarm": [entry]}}))

    def fake_run(self, bin_, args, timeout=180):
        self.calls.append((bin_, list(args)))
        if args[:2] == ["plugin", "--help"]:
            return subprocess.CompletedProcess([], 0, "  update  x\n", "")
        if args[:2] in (["plugin", "install"], ["plugin", "add"]):
            self.set_installed(TIP)   # the reinstall refetches the tip
        return subprocess.CompletedProcess([], 0, "", "")

    def go(self, force, channel="main", hosts=("claude",), tip=TIP, ref_tag=None, commits=None):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(update, "_run", self.fake_run), \
                mock.patch.object(update.channels, "tip_commit", return_value=tip), \
                mock.patch.object(update.channels, "latest_tag", return_value=ref_tag), \
                mock.patch.object(update, "_child", return_value=(0, "", "")), \
                mock.patch.object(update, "_codex_plugin_version", return_value="0.2.0"), \
                mock.patch.object(update, "_codex_plugin_commit", side_effect=commits or [OLD, TIP]), \
                mock.patch.object(update, "_host_source", return_value=("https://example.com/s.git", True)), \
                mock.patch.object(update, "newest_installed_plugin_root", return_value=self.tree), \
                mock.patch("os.access", return_value=True), \
                mock.patch("sys.stderr", err):
            rc = update.run_update(hosts[0] if len(hosts) == 1 else None, force, False,
                                   which=lambda h: f"/bin/{h}" if h in hosts else None, out=out,
                                   channel=channel)
        return rc, out.getvalue(), err.getvalue()

    def verbs(self):
        return [a[1] for _, a in self.calls if a[:1] == ["plugin"]]

    def test_same_version_newer_commit_warns_without_force(self):
        rc, out, _ = self.go(False)
        self.assertEqual(rc, 0)
        self.assertIn("behind main", out)
        self.assertIn("swarm upgrade --force", out)
        self.assertNotIn("up to date", out)
        self.assertNotIn("uninstall", self.verbs())

    def test_unknown_installed_commit_is_said(self):
        self.set_installed(None)
        rc, out, _ = self.go(False)
        self.assertIn("can't be determined", out)
        self.assertNotIn("up to date", out)

    def test_at_the_tip_is_up_to_date(self):
        self.set_installed(TIP)
        rc, out, _ = self.go(False)
        self.assertIn("up to date", out)

    def test_force_reinstalls_and_reports_commits(self):
        rc, out, _ = self.go(True)
        self.assertEqual(rc, 0)
        self.assertEqual(self.verbs(), ["marketplace", "--help", "uninstall", "install"])
        self.assertIn(["plugin", "uninstall", "--keep-data", "swarm@swarm"], [a for _, a in self.calls])
        self.assertIn(f"[claude] commit: {OLD[:7]} -> {TIP[:7]} (tip of main {TIP[:7]})", out)

    def test_force_on_release_channel_reinstalls_current_release(self):
        (self.plugins / "known_marketplaces.json").write_text(json.dumps(
            {"swarm": {"source": {"source": "git", "url": "https://example.com/s.git", "ref": "v0.2.0"}}}))
        rc, out, _ = self.go(True, channel="release", ref_tag="v0.2.0")
        self.assertEqual(rc, 0)
        self.assertIn("uninstall", self.verbs())
        self.assertIn("install", self.verbs())
        self.assertIn("channel: release (v0.2.0)", out)
        self.assertNotIn("tip of main", out)

    def test_release_channel_never_warns_behind(self):
        rc, out, _ = self.go(False, channel="release", ref_tag="v0.2.0")
        self.assertNotIn("behind", out)
        self.assertIn("up to date", out)

    def test_force_codex_removes_then_adds(self):
        rc, out, _ = self.go(True, hosts=("codex",))
        self.assertEqual(rc, 0)
        self.assertEqual([v for v in self.verbs() if v in ("remove", "add")], ["remove", "add"])
        self.assertIn(f"[codex] commit: {OLD[:7]} -> {TIP[:7]}", out)

    def test_codex_marketplace_revision_is_not_the_installed_commit(self):
        # config.toml's last_revision follows the marketplace (already at the tip after the refresh),
        # while the installed copy is stale: a plain upgrade must not say "up to date".
        codex_home = self.home / ".codex"
        codex_home.mkdir()
        (codex_home / "config.toml").write_text(
            f'[marketplaces.swarm]\nsource = "https://example.com/s.git"\nlast_revision = "{TIP}"\n')
        stale = self.home / "codex-copy"          # installed copy: no .git, commit not recorded
        stale.mkdir()
        self.assertIsNone(update._codex_plugin_commit(stale))
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(update, "_run", self.fake_run), \
                mock.patch.object(update.channels, "tip_commit", return_value=TIP), \
                mock.patch.object(update, "_codex_plugin_version", return_value="0.2.0"), \
                mock.patch.object(update, "_host_source", return_value=("https://example.com/s.git", True)), \
                mock.patch.object(update, "newest_installed_plugin_root", return_value=stale), \
                mock.patch("sys.stderr", err):
            update.run_update("codex", False, False, which=lambda h: f"/bin/{h}", out=out, channel="main")
        self.assertNotIn("up to date", out.getvalue())
        self.assertIn("can't be determined", out.getvalue())

    def test_codex_commit_read_from_the_installed_copy(self):
        repo = self.home / "copy"
        repo.mkdir()
        run = lambda *a: subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True,
                                        env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                                             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})
        run("init", "-q"); run("commit", "-q", "--allow-empty", "-m", "x")
        sha = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
        self.assertEqual(update._codex_plugin_commit(repo), sha)

    def test_force_failure_of_uninstall_is_an_error(self):
        real = self.fake_run
        def failing(bin_, args, timeout=180):
            if args[:2] == ["plugin", "uninstall"]:
                return subprocess.CompletedProcess([], 1, "", "nope")
            return real(bin_, args, timeout)
        self.fake_run = failing
        rc, out, err = self.go(True)
        self.assertEqual(rc, 1)
        self.assertIn("plugin uninstall", err)


class ChannelTipTests(unittest.TestCase):
    def test_tip_commit_parses_ls_remote(self):
        from swarm import channel
        res = mock.Mock(returncode=0, stdout=f"{TIP}\trefs/heads/main\n")
        with mock.patch.object(channel.subprocess, "run", return_value=res):
            self.assertEqual(channel.tip_commit("u"), TIP)
        with mock.patch.object(channel.subprocess, "run", return_value=mock.Mock(returncode=2, stdout="")):
            self.assertIsNone(channel.tip_commit("u"))


if __name__ == "__main__":
    unittest.main()
