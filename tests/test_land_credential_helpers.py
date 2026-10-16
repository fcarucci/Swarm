"""[drive] credential_helpers: the listed absolute helpers flow from the config into the landing's mirrors."""
from __future__ import annotations

import os
import unittest
from unittest import mock

import support  # noqa: F401
from swarm import land
from swarm.land import Destination, LandEnv, LandRefused, credential_from_pin
from swarm.supervisor import driver, mirror
from test_drive_mirror_land import HelperFixtures


class CredentialHelperKey(HelperFixtures):
    def setUp(self):
        super().setUp()
        # Some sandboxes own `/` by another user, which the helper's parent-directory walk refuses: report this user.
        real = os.stat

        def stat(path, *a, **kw):
            st = real(path, *a, **kw)
            if st.st_uid in (0, os.getuid()):
                return st
            return os.stat_result((st.st_mode, st.st_ino, st.st_dev, st.st_nlink, os.getuid(), st.st_gid, st.st_size,
                                   int(st.st_atime), int(st.st_mtime), int(st.st_ctime)))
        p = mock.patch.object(mirror.os, "stat", stat)
        p.start()
        self.addCleanup(p.stop)

    def mirrors(self, command, helpers=()):
        if not hasattr(self, "remote"):
            self.remote, _ = self.remote_with_main()
        remote = self.remote
        env = LandEnv(led=None, owner={}, judged=set, ci_status=lambda sha: "green", trusted_helpers=tuple(helpers))
        return land._mirrors(env, [Destination("a", remote, self.cred(command))])

    @support.posix_only("file modes")
    def test_a_listed_absolute_helper_is_allowed(self):
        gh = self.gh()
        for listed in (gh, f"{gh} auth git-credential"):
            with self.subTest(listed):
                self.assertEqual(len(self.mirrors(f"{gh} auth git-credential", [listed])), 1)

    @support.posix_only("file modes")
    def test_a_non_default_helper_with_arguments_is_allowed_by_path_or_full_string(self):
        foo = self.gh(name="foo")
        command = f"{foo} sub arg"
        for listed in (foo, command):
            with self.subTest(listed):
                self.assertEqual(len(self.mirrors(command, [listed])), 1)

    @support.posix_only("file modes")
    def test_a_full_string_listing_allows_only_its_arguments(self):
        foo = self.gh(name="foo")
        with self.assertRaises(LandRefused) as cm:
            self.mirrors(f"{foo} other", [f"{foo} sub arg"])
        self.assertIn("credential_helpers", str(cm.exception))

    @support.posix_only("file modes")
    def test_a_helper_in_a_system_directory_is_allowed(self):
        gh = self.gh()
        with mock.patch.object(mirror, "SYSTEM_DIRS", (*mirror.SYSTEM_DIRS, os.path.dirname(gh))):
            self.assertEqual(len(self.mirrors(f"{gh} auth git-credential")), 1)

    @support.posix_only("file modes")
    def test_an_unlisted_absolute_helper_is_refused_naming_the_key(self):
        gh = self.gh()
        other = self.gh(name="other")
        with self.assertRaises(LandRefused) as cm:
            self.mirrors(f"{gh} auth git-credential")
        self.assertIn("credential_helpers", str(cm.exception))
        with self.assertRaises(LandRefused) as cm:
            self.mirrors(f"{gh} auth git-credential", [other])
        self.assertIn("credential_helpers", str(cm.exception))

    def test_a_relative_helper_is_refused_naming_the_key(self):
        for pin in ("helper:gh auth git-credential", "helper:./helper"):
            with self.subTest(pin), self.assertRaises(LandRefused) as cm:
                credential_from_pin(pin, config_dir=self.home)
            self.assertIn("credential_helpers", str(cm.exception))
            self.assertIn("absolute", str(cm.exception))

    @support.posix_only("file modes")
    def test_the_config_list_flows_into_the_landing_environment(self):
        gh = self.gh()
        cfg = {"drive": {"credential_helpers": [gh, 7, "", "gh auth git-credential"]}}
        self.assertEqual(driver._credential_helpers(cfg), (gh, "gh auth git-credential"))
        self.assertEqual(driver._credential_helpers({"drive": {}}), ())
        self.assertEqual(driver._credential_helpers({"drive": {"credential_helpers": gh}}), ())
        self.assertEqual(driver._credential_helpers(None), ())
        self.assertEqual(len(self.mirrors(f"{gh} auth git-credential", driver._credential_helpers(cfg))), 1)


if __name__ == "__main__":
    unittest.main()
