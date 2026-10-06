"""The engineering-team CLI plugin (skills/engineering-team/swarm_plugin.py): `swarm team`,
`activate --team`, the status line, and team.toml ($SWARM_TEAM_CONFIG or next to config.toml)."""
from __future__ import annotations

import os
from unittest import mock

from support import posix_only
from test_hooks_cli import Env  # noqa: E402  (sets sys.path)


class TeamEnv(Env):
    plugins_disabled = ()

    def setUp(self):
        super().setUp()
        p = mock.patch.dict(os.environ)
        p.start()
        self.addCleanup(p.stop)
        os.environ.pop("SWARM_TEAM_CONFIG", None)
        self.team_toml = self.tmp / "team.toml"   # next to config.toml: the default place

    def team(self, *args):
        return self.cli("team", "--job", "J", *args)


class TeamInstallTests(TeamEnv):
    @posix_only("POSIX umask and primary groups; Windows uses ACLs")
    def test_shipped_team_plugin_installed_with_umask_002_loads(self):
        from swarm import paths
        source = paths.PLUGIN_ROOT / "skills" / "engineering-team" / "swarm_plugin.py"
        body = source.read_text()
        root = self.tmp / "installed-plugin"
        old_umask = os.umask(0o002)
        try:
            skill = root / "skills" / "engineering-team"
            skill.mkdir(parents=True)
            installed = skill / "swarm_plugin.py"
            installed.write_text(body)
        finally:
            os.umask(old_umask)
        self.assertEqual(installed.stat().st_mode & 0o777, 0o664)
        with mock.patch.object(paths, "PLUGIN_ROOT", root):
            self.assertIn("engineering-team\tloaded", self.cli("plugins")[1])
            rc, out, err = self.cli("activate", "--job", "J", "--team", "build_engineer")
            self.assertEqual(rc, 0, err)
            self.assertIn("optional build_engineer", out)
            rc, out, err = self.team("--show")
            self.assertEqual(rc, 0, err)
            self.assertIn("optional   build_engineer", out)


class TeamCommandTests(TeamEnv):
    def setUp(self):
        super().setUp()
        self.assertEqual(self.cli("activate", "--job", "J")[0], 0)

    def test_the_default_is_product_manager_on_and_build_engineer_off(self):
        rc, out, _ = self.team("--show")
        self.assertEqual(rc, 0)
        self.assertIn("mandatory  engineering_lead, qa, engineer, judge", out)
        self.assertIn("optional   product_manager  (from default)", out)
        self.assertIn("absent     build_engineer: engineering_lead owns", out)
        self.assertNotIn("absent     product_manager", out)

    def test_add_and_remove_are_kept_with_the_job_and_shown_in_status(self):
        rc, out, _ = self.team("--add", "build_engineer", "--remove", "product_manager")
        self.assertEqual(rc, 0)
        self.assertIn("optional   build_engineer  (from this job)", out)
        self.assertIn("absent     product_manager: engineering_lead writes the change criteria", out)
        self.assertIn("optional   build_engineer", self.team()[1])   # no flags: show
        status = self.cli("status", "--job", "J", "--no-color")[1]
        self.assertIn("team       engineering_lead, qa, engineer, judge, build_engineer  (this job)", status)
        self.team("--add", "product_manager")
        self.assertIn("optional   build_engineer, product_manager", self.team("--show")[1])

    def test_a_mandatory_role_cannot_be_removed(self):
        for role in ("engineering_lead", "qa", "engineer", "judge"):
            rc, out, err = self.team("--remove", role)
            self.assertEqual((rc, out), (2, ""), role)
            self.assertIn("is mandatory and cannot be removed", err)
        self.assertIn("(from default)", self.team("--show")[1])   # nothing was changed

    def test_an_unknown_role_or_job_is_refused(self):
        rc, _, err = self.team("--add", "wizard")
        self.assertEqual(rc, 2)
        self.assertIn("unknown role 'wizard'", err)
        rc, _, err = self.cli("team", "--job", "nope", "--show")
        self.assertEqual(rc, 1)
        self.assertIn("no job 'nope'", err)

    def test_the_composition_survives_a_close_and_reactivation(self):
        self.team("--add", "build_engineer")
        self.cli("deactivate", "--job", "J")
        self.cli("activate", "--job", "J")
        self.assertIn("build_engineer", self.team("--show")[1])


class ActivateTeamTests(TeamEnv):
    def test_activate_team_sets_the_composition_and_says_so(self):
        rc, out, _ = self.cli("activate", "--job", "J", "--team", "product_manager,build_engineer")
        self.assertEqual(rc, 0)
        self.assertIn("optional product_manager, build_engineer (from this job)", out)
        self.assertIn("optional   product_manager, build_engineer  (from this job)", self.team("--show")[1])

    def test_an_empty_team_means_no_optional_roles(self):
        self.cli("activate", "--job", "J", "--team", "")
        self.assertIn("optional   none", self.team("--show")[1])

    def test_a_bad_team_is_refused_before_the_job_is_activated(self):
        rc, _, err = self.cli("activate", "--job", "J", "--team", "product_manager,wizard")
        self.assertEqual(rc, 2)
        self.assertIn("--team: unknown role in --team 'wizard'", err)
        with self.board() as b:
            self.assertIsNone(b.job_status("J"))

    def test_mandatory_names_in_team_are_accepted_as_already_present(self):
        rc, out, _ = self.cli("activate", "--job", "J", "--team", "qa,build_engineer")
        self.assertEqual(rc, 0)
        self.assertIn("optional build_engineer (from this job)", out)

    def test_without_team_the_job_follows_team_toml_and_later_changes_to_it(self):
        self.team_toml.write_text('optional_roles = ["build_engineer"]\n')
        self.cli("activate", "--job", "J")
        self.assertIn("optional   build_engineer  (from " + str(self.team_toml), self.team("--show")[1])
        self.team_toml.write_text('optional_roles = []\n')
        self.assertIn("optional   none", self.team("--show")[1])


class TeamTomlTests(TeamEnv):
    def setUp(self):
        super().setUp()
        self.cli("activate", "--job", "J")

    def test_env_var_overrides_the_default_file(self):
        self.team_toml.write_text('optional_roles = ["reviewer"]\n')
        other = self.tmp / "other.toml"
        other.write_text('optional_roles = ["build_engineer", "verifier"]\n')
        with mock.patch.dict(os.environ, {"SWARM_TEAM_CONFIG": str(other)}):
            out = self.team("--show")[1]
        self.assertIn("optional   build_engineer, verifier  (from " + str(other), out)

    def test_a_missing_file_means_the_defaults(self):
        self.assertFalse(self.team_toml.exists())
        self.assertIn("optional   product_manager  (from default)", self.team("--show")[1])

    def test_a_broken_file_is_a_clear_error_and_core_is_unaffected(self):
        self.team_toml.write_text("optional_roles = [\n")
        rc, _, err = self.team("--show")
        self.assertEqual(rc, 2)
        self.assertIn("team.toml", err)
        rc, out, _ = self.cli("status", "--job", "J", "--no-color")   # the status line is the plugin's: it must not break status
        self.assertEqual(rc, 0)
        self.assertIn("job        J", out)
        self.team_toml.write_text('optional_roles = ["wizard"]\n')
        self.assertEqual(self.team("--show")[0], 2)
        self.team_toml.write_text('optional_roles = "qa"\n')
        self.assertEqual(self.team("--show")[0], 2)

    def test_the_example_file_parses_to_the_default(self):
        from pathlib import Path
        example = Path(__file__).resolve().parent.parent / "team.example.toml"
        with mock.patch.dict(os.environ, {"SWARM_TEAM_CONFIG": str(example)}):
            out = self.team("--show")[1]
        self.assertIn("optional   product_manager  (from " + str(example), out)

    def test_a_team_section_is_read_too(self):
        self.team_toml.write_text('[team]\noptional_roles = ["build_engineer"]\n')
        self.assertIn("optional   build_engineer", self.team("--show")[1])

    def test_config_toml_has_no_team_section_and_core_ignores_one(self):
        self.config.write_text(self.config.read_text() + '[team]\noptional_roles = ["build_engineer"]\n')
        self.assertIn("optional   product_manager  (from default)", self.team("--show")[1])


class CoreWithoutThePluginTests(Env):
    def test_team_does_not_exist_in_core(self):
        with self.assertRaises(SystemExit):
            with mock.patch("sys.stderr"):
                self.cli("team", "--job", "J", "--show")
        with self.assertRaises(SystemExit):
            with mock.patch("sys.stderr"):
                self.cli("activate", "--job", "J", "--team", "qa")
        self.assertEqual(self.cli("activate", "--job", "J")[0], 0)
        self.assertNotIn("team ", self.cli("status", "--job", "J", "--no-color")[1])
