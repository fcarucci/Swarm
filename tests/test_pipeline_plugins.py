"""Coding is a plugin recipe; generic artifacts and disabled plugins remain generic."""
from __future__ import annotations

import json
from unittest import mock

from test_team_plugin import TeamEnv


class PipelineRecipeTests(TeamEnv):
    def setUp(self):
        super().setUp()
        self.cli("activate", "--job", "J", "--goal", "deliver")
        self.sha = "a" * 40

    def recipe(self, artifact):
        from swarm import plugins
        from swarm.cli import load_config
        with self.board() as board:
            return plugins.pipeline_recipe(load_config(self.config), board, "J", artifact, self.config)

    def test_generic_report_and_disabled_adapter_have_no_recipe(self):
        self.assertEqual(self.recipe("reports/result.md"), {})
        self.config.write_text(self.config.read_text().replace('disabled = []', 'disabled = ["engineering-team"]'))
        self.assertEqual(self.recipe("feat/result@" + self.sha), {})

    def test_exact_sha_evidence_and_integrator_recipe(self):
        recipe = self.recipe("feat/result@" + self.sha)
        self.assertEqual(recipe["finalizer_role"], "integrator")
        self.assertIn(self.sha, recipe["evidence_command"])
        self.assertIn("every configured remote and push URL", recipe["finalize"])
        self.assertIn("delete feat/result", recipe["finalize"])
        self.assertIn("FINALIZE_BLOCKED <artifact>", recipe["finalize"])
        self.assertIn("moved branch", recipe["finalize"])
        self.assertTrue(recipe["enabled"])
        self.assertEqual(recipe["artifact_group"], "feat/result")

    def test_gitea_fetch_github_push_urls_select_github_repository(self):
        from swarm import plugins
        with self.board() as board:
            plugins.pipeline_recipe(self.cfg, board, "J", None, self.config)
        import swarm_plugin_engineering_team as adapter
        output = ('remote.origin.url git@git.local.carucci.studio:francesco/Swarm.git\n'
                  'remote.origin.pushurl git@git.local.carucci.studio:francesco/Swarm.git\n'
                  'remote.origin.pushurl git@github.com:fcarucci/Swarm.git\n')
        with mock.patch('subprocess.run', return_value=mock.Mock(returncode=0, stdout=output)):
            self.assertEqual(adapter.github_repository(str(self.tmp)), 'fcarucci/Swarm')

    def test_pending_red_absent_and_wrong_sha_never_pass_launch_gate(self):
        check = self.recipe("feat/result@" + self.sha)["evidence_check"]
        for runs in ([], [{"headSha": self.sha, "status": "queued", "conclusion": None}],
                     [{"headSha": self.sha, "status": "completed", "conclusion": "failure"}],
                     [{"headSha": "b" * 40, "status": "completed", "conclusion": "success"}]):
            with mock.patch("subprocess.run", return_value=mock.Mock(returncode=0, stdout=json.dumps(runs))):
                self.assertFalse(check(str(self.tmp)))
        green = [{"headSha": self.sha, "status": "completed", "conclusion": "success"}]
        with mock.patch("subprocess.run", return_value=mock.Mock(returncode=0, stdout=json.dumps(green))) as run:
            self.assertTrue(check(str(self.tmp)))
        self.assertIn(self.sha, run.call_args.args[0])
        self.assertEqual(run.call_args.kwargs["cwd"], str(self.tmp))

    def test_team_config_and_owner_repository_override(self):
        self.team_toml.write_text('[pipeline]\nintegrate = false\ndelete_branch = false\n'
                                  'merge_target = "release"\nevidence_command = "check {sha}"\n')
        recipe = self.recipe("feat/result@" + self.sha)
        self.assertFalse(recipe["enabled"])
        self.assertIn("into release", recipe["finalize"])
        self.assertEqual(recipe["evidence_command"], "check " + self.sha)
        self.assertNotIn("delete feat/result", recipe["finalize"])
        self.team_toml.write_text('[pipeline]\nmerge_target = "main"\n'
                                  '[repositories."' + str(self.tmp).replace('\\', '\\\\') + '"]\n'
                                  'repository = "owner/repo"\nmerge_target = "release"\n')
        with mock.patch("swarm.supervisor.orphans.local_record", return_value=mock.Mock(cwd=str(self.tmp), workdir=None)):
            recipe = self.recipe("feat/result@" + self.sha)
        self.assertIn("owner/repo", recipe["evidence_command"])
        self.assertIn("into release", recipe["finalize"])

    def test_invalid_coding_refs_stay_opaque_and_bad_config_holds(self):
        for branch in ("-bad", "a/../b", "a/.hidden", "a//b", "a.lock", "a."):
            self.assertEqual(self.recipe(branch + "@" + self.sha), {})
        self.team_toml.write_text('[pipeline]\nintegrate = "false"\n')
        with self.assertRaises(ValueError):
            self.recipe("feat/result@" + self.sha)

    def test_failing_recipe_holds_instead_of_falling_back_to_completion(self):
        from swarm import plugins
        registry = plugins.Registry()
        def broken(*args):
            raise RuntimeError("bad recipe")
        plugins.PluginAPI(registry, "example", self.tmp).add_pipeline_recipe(broken)
        with self.assertRaisesRegex(RuntimeError, "bad recipe"):
            registry.pipeline_recipe(None, "J", "report")
