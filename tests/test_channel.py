"""Release channels: install/upgrade follow the newest vX.Y.Z tag by default, or main."""
from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from support import ROOT  # noqa: F401  (puts lib/ on sys.path)

from swarm import channel, update


def git(cwd, *args):
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "-c", "commit.gpgsign=false",
                    "-c", "tag.gpgsign=false", *args], cwd=cwd, check=True, capture_output=True)


def make_repo(root: Path) -> Path:
    """A marketplace repo: tags v0.1.1 and v0.1.2, then one more commit (0.1.3-dev) after the last tag."""
    repo = root / "repo"
    (repo / ".claude-plugin").mkdir(parents=True)
    (repo / "bin").mkdir()
    (repo / "bin" / "swarm").write_text("#!/bin/sh\nexit 0\n")
    (repo / "bin" / "swarm").chmod(0o755)
    (repo / "bin" / "swarm.cmd").write_text("@echo off\r\nexit /b 0\r\n")   # for the Windows installer test
    (repo / ".claude-plugin" / "marketplace.json").write_text(json.dumps(
        {"name": "swarm", "owner": {"name": "t"}, "plugins": [{"name": "swarm", "source": "./"}]}))
    git(repo, "init", "-q", "-b", "main")
    for version, tag in (("0.1.1", "v0.1.1"), ("0.1.2", "v0.1.2"), ("0.1.3-dev", None)):
        (repo / ".claude-plugin" / "plugin.json").write_text(json.dumps({"name": "swarm", "version": version}))
        git(repo, "add", "-A")
        git(repo, "commit", "-qm", version)
        if tag:
            git(repo, "tag", tag)
    git(repo, "tag", "vnext-not-a-release")
    git(repo, "tag", "v0.1.10-rc1")
    return repo


class TagResolutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="swarm-chan-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.repo = make_repo(self.tmp)

    def test_latest_tag_is_the_newest_release_tag_not_main(self):
        self.assertEqual(channel.latest_tag(str(self.repo)), "v0.1.2")

    def test_numeric_not_lexical_order(self):
        git(self.repo, "tag", "v0.1.10")
        self.assertEqual(channel.latest_tag(str(self.repo)), "v0.1.10")

    def test_release_resolves_to_the_tag_and_its_version(self):
        chan, ref, warning = channel.resolve("release", str(self.repo))
        self.assertEqual((chan, ref, warning), ("release", "v0.1.2", None))
        shown = subprocess.run(["git", "show", f"{ref}:.claude-plugin/plugin.json"], cwd=self.repo,
                               capture_output=True, text=True).stdout
        self.assertEqual(json.loads(shown)["version"], "0.1.2")

    def test_main_has_no_ref_and_is_the_tip(self):
        self.assertEqual(channel.resolve("main", str(self.repo)), ("main", None, None))
        tip = (self.repo / ".claude-plugin" / "plugin.json").read_text()
        self.assertEqual(json.loads(tip)["version"], "0.1.3-dev")

    def test_explicit_ref_wins(self):
        self.assertEqual(channel.resolve("release", str(self.repo), "v0.1.1"), ("release", "v0.1.1", None))

    def test_no_tag_or_unreachable_falls_back_to_main_with_a_warning(self):
        for url in (str(self.tmp / "nowhere"), str(self.tmp)):
            chan, ref, warning = channel.resolve("release", url)
            self.assertEqual((chan, ref), ("main", None))
            self.assertIn("falling back to the tip of main", warning)


class ConfigChannelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="swarm-chan-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.cfg = self.tmp / "config.toml"

    def test_round_trip_keeps_the_rest_of_the_file(self):
        self.cfg.write_text('[board]\nbackend = "file"\n\n[hook]\nmarker_dir = "x"\n')
        self.assertIsNone(channel.read_channel(self.cfg))
        self.assertTrue(channel.write_channel("main", self.cfg))
        self.assertEqual(channel.read_channel(self.cfg), "main")
        self.assertTrue(channel.write_channel("release", self.cfg))
        text = self.cfg.read_text()
        self.assertEqual(channel.read_channel(self.cfg), "release")
        self.assertEqual(text.count("[upgrade]"), 1)
        self.assertIn('[board]\nbackend = "file"\n\n[hook]\nmarker_dir = "x"\n', text)

    def test_existing_section_is_edited_in_place(self):
        self.cfg.write_text('[upgrade]\nother = 1\nchannel = "release"\n[board]\nbackend = "file"\n')
        channel.write_channel("main", self.cfg)
        self.assertEqual(self.cfg.read_text(), '[upgrade]\nother = 1\nchannel = "main"\n[board]\nbackend = "file"\n')

    def test_no_config_file_is_not_created(self):
        self.assertFalse(channel.write_channel("main", self.cfg))
        self.assertFalse(self.cfg.exists())


class UpgradeChannelTests(unittest.TestCase):
    """swarm upgrade against a stub claude: re-pins the marketplace to the channel's ref."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="swarm-chan-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.repo = make_repo(self.tmp)
        self.cc = self.tmp / "cc"
        (self.cc / "plugins").mkdir(parents=True)
        self.env = mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.cc)})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.calls = []

    def known(self, **src):
        (self.cc / "plugins" / "known_marketplaces.json").write_text(json.dumps({"swarm": {"source": src}}))

    def fake_run(self, bin_, args, timeout=0):
        self.calls.append(args)
        return subprocess.CompletedProcess([bin_, *args], 0, "  update  Update a plugin\n", "")

    def test_claude_repins_to_the_latest_tag_with_hash_ref(self):
        self.known(source="git", url=str(self.repo))
        with mock.patch.object(update, "_run", self.fake_run):
            update.update_claude("claude", "v0.1.2", str(self.repo))
        self.assertIn(["plugin", "marketplace", "remove", "swarm"], self.calls)
        self.assertIn(["plugin", "marketplace", "add", f"{self.repo}#v0.1.2"], self.calls)
        self.assertIn(["plugin", "install", "swarm@swarm"], self.calls)   # remove uninstalls the plugin

    def test_claude_main_drops_the_pin(self):
        self.known(source="git", url=str(self.repo), ref="v0.1.2")
        with mock.patch.object(update, "_run", self.fake_run):
            update.update_claude("claude", None, str(self.repo))
        self.assertIn(["plugin", "marketplace", "add", str(self.repo)], self.calls)

    def test_claude_already_on_the_ref_just_updates(self):
        self.known(source="git", url=str(self.repo), ref="v0.1.2")
        with mock.patch.object(update, "_run", self.fake_run):
            update.update_claude("claude", "v0.1.2", str(self.repo))
        self.assertNotIn(["plugin", "marketplace", "remove", "swarm"], self.calls)
        self.assertIn(["plugin", "marketplace", "update", "swarm"], self.calls)

    def test_codex_uses_ref_flag(self):
        with mock.patch.object(update, "_run", self.fake_run), \
                mock.patch.object(update, "_codex_config_marketplace_field", return_value=None):
            update.update_codex("codex", "v0.1.2", str(self.repo))
        self.assertIn(["plugin", "marketplace", "add", str(self.repo), "--ref", "v0.1.2"], self.calls)

    def run_upgrade(self, **kw):
        changed = {"changed": False, "old_version": "0.1.2", "new_version": "0.1.2",
                   "old_root": None, "new_root": None, "verb": "update"}
        seen = {}
        def fake_claude(bin_, ref=update._UNSET, url=None):
            seen["ref"], seen["url"] = ref, url
            return changed
        out = io.StringIO()
        with mock.patch.object(update, "update_claude", fake_claude):
            rc = update.run_update("claude", False, False, which=lambda h: f"/bin/{h}", out=out, **kw)
        return rc, seen, out.getvalue()

    def test_default_channel_is_release_and_main_is_remembered(self):
        cfg = self.tmp / "config.toml"
        cfg.write_text('[board]\nbackend = "file"\n')
        self.known(source="git", url=str(self.repo))
        rc, seen, out = self.run_upgrade(config_path=cfg)
        self.assertEqual((rc, seen["ref"]), (0, "v0.1.2"))
        self.assertIn("channel: release (v0.1.2)", out)
        self.assertIsNone(channel.read_channel(cfg))               # nothing chosen: nothing stored
        rc, seen, out = self.run_upgrade(config_path=cfg, channel="main")
        self.assertIsNone(seen["ref"])
        self.assertIn("channel: main (tip of main)", out)
        self.assertEqual(channel.read_channel(cfg), "main")
        rc, seen, out = self.run_upgrade(config_path=cfg)           # a plain upgrade keeps following main
        self.assertIsNone(seen["ref"])

    def test_local_directory_marketplace_is_followed_as_is(self):
        self.known(source="directory", path=str(self.repo))
        rc, seen, out = self.run_upgrade()
        self.assertIs(seen.get("ref", update._UNSET), update._UNSET)
        self.assertIn("local path", out)


if __name__ == "__main__":
    unittest.main()
