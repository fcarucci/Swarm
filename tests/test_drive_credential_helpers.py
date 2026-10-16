"""Landing helper trust from the person's [drive] configuration, through the driver to the mirror."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import support
from swarm import cli, land
from swarm.supervisor import driver, mirror
from test_drive_mirror_land import HelperFixtures


@support.posix_only("absolute POSIX helper pins and file modes")
class CredentialHelpers(HelperFixtures):
    def env(self, entries=None):
        path = Path(self.root) / "config.toml"
        if entries is not None:
            path.write_text("[drive]\ncredential_helpers = " + json.dumps(entries) + "\n")
        cfg = cli.load_config(path)
        mods = SimpleNamespace(land=SimpleNamespace(ci_status_cli=lambda repo: lambda sha: "green"))
        return driver._land_env(cfg, SimpleNamespace(), SimpleNamespace(owner={}),
                                SimpleNamespace(verdicts={}), lambda text: None,
                                time.monotonic() + 30, mods)

    def mirrors(self, env, command):
        dest = land.Destination("origin", "https://example.test/repo.git",
                                land.credential_from_pin("helper:" + command, config_dir=self.home))
        return land._mirrors(env, [dest])

    def test_configured_absolute_command_allows_landing_helper(self):
        gh = self.gh()
        command = gh + " auth git-credential"
        result = self.mirrors(self.env([command]), command)
        self.assertIn("https://example.test/repo.git", result)

    def test_configured_executable_path_allows_landing_helper(self):
        gh = self.gh()
        self.mirrors(self.env([gh]), gh + " auth git-credential")

    def test_system_directory_helper_needs_no_config_entry(self):
        gh = self.gh()
        with mock.patch.object(mirror, "HELPER_SYSTEM_DIRS", (*mirror.HELPER_SYSTEM_DIRS, os.path.dirname(gh))):
            self.mirrors(self.env([]), gh + " auth git-credential")

    def test_git_core_system_directory_helper_needs_no_config_entry(self):
        helper = "/usr/lib/git-core/git-credential-store"
        if not os.path.isfile(helper):
            self.skipTest("system git credential helper is not installed here")
        self.mirrors(self.env([]), helper)

    def test_unlisted_helper_refusal_names_config_key_and_exact_path(self):
        gh = self.gh()
        with self.assertRaises(land.LandRefused) as cm:
            self.mirrors(self.env([gh + "x"]), gh + " auth git-credential")
        self.assertIn("destination origin:", str(cm.exception))
        self.assertIn("[drive] credential_helpers", str(cm.exception))
        self.assertIn(gh, str(cm.exception))

    def test_relative_helper_is_refused_even_when_listed(self):
        command = "./gh auth git-credential"
        with self.assertRaises(land.LandRefused) as cm:
            self.mirrors(self.env([command]), command)
        self.assertIn("./gh", str(cm.exception))
        self.assertIn("[drive] credential_helpers", str(cm.exception))
        self.assertIn("absolute", str(cm.exception))

    def test_relative_config_entry_does_not_trust_an_absolute_pin(self):
        gh = self.gh()
        with self.assertRaises(land.LandRefused):
            self.mirrors(self.env(["gh auth git-credential"]), gh + " auth git-credential")

    def test_config_loading_supplies_the_builtin_default_list(self):
        cfg = cli.load_config(Path(self.root) / "absent.toml")
        self.assertEqual(cfg.get("drive", {}).get("credential_helpers"),
                         ["gh auth git-credential", "git-credential-manager",
                          "git-credential-store", "git-credential-cache"])
