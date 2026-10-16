"""Launch specs: exact argv per host, caps, model choice, and never a bypass flag."""
from __future__ import annotations

import os
import re
import unittest
import uuid
from unittest import mock
from pathlib import Path

from support import ROOT, base_config

from swarm.supervisor import launch


def cfg(**sup):
    c = base_config()
    c["supervise"] = {"enabled": True, **sup}
    return c


class ClaudeSpecTests(unittest.TestCase):
    def test_argv(self):
        s = launch.claude_spec(cfg(), prompt="BRIEF", workdir="/w", model="claude-sonnet-5", minutes=42,
                               session_id="3f0c1e9a-0000-4000-8000-000000000001")
        self.assertEqual(s.argv, ("claude", "-p", "--session-id", "3f0c1e9a-0000-4000-8000-000000000001",
                                  "--max-turns", "60", "--permission-mode", "auto", "--output-format", "json",
                                  "--setting-sources", "user", "--model", "claude-sonnet-5"))
        self.assertEqual((s.cwd, s.stdin, s.minutes, s.harness), ("/w", "BRIEF", 42, "claude"))

    def test_session_id_generated_as_uuid(self):
        s = launch.claude_spec(cfg(), prompt="p", workdir="/w", model=None, minutes=1)
        uuid.UUID(s.session_id)
        self.assertNotIn("--model", s.argv)

    def test_budget_and_plugin_dir(self):
        s = launch.claude_spec(cfg(max_budget_usd=2.5, claude_plugin_dir="/frozen"), prompt="p",
                               workdir="/w", model=None, minutes=1)
        self.assertIn(("--max-budget-usd", "2.5"), list(zip(s.argv, s.argv[1:])))
        self.assertIn(("--plugin-dir", "/frozen"), list(zip(s.argv, s.argv[1:])))
        self.assertIn(("--setting-sources", "user"), list(zip(s.argv, s.argv[1:])))


class SettingSourcesTests(unittest.TestCase):
    """A replacement never loads project or local settings (a
    sandboxed agent can plant .claude/settings.json in its work dir), with or without the e2e
    plugin dir."""

    def test_claude_setting_sources_user(self):
        for sup in ({}, {"claude_plugin_dir": "/frozen"}, {"max_budget_usd": 1}):
            with self.subTest(sup=sup):
                argv = launch.claude_spec(cfg(**sup), prompt="p", workdir="/w", model=None, minutes=1).argv
                pairs = list(zip(argv, argv[1:]))
                self.assertIn(("--setting-sources", "user"), pairs)
                self.assertEqual(argv.count("--setting-sources"), 1)
                self.assertFalse(any("project" in a or "local" in a for a in argv), argv)

    def test_check_safe_refuses_project_or_local_setting_sources(self):
        import os
        from unittest import mock
        bad = (["claude", "--setting-sources", "project"], ["claude", "--setting-sources", "user,project"],
               ["claude", "--setting-sources=local"], ["claude", "--Setting-Sources", "Project"],
               ["claude", "--setting-sources"])
        with mock.patch.dict(os.environ, {"SWARM_E2E": ""}):
            for argv in bad:
                with self.assertRaises(launch.UnsafeLaunch, msg=argv):
                    launch.check_safe(argv)
            launch.check_safe(["claude", "--setting-sources", "user"])
        with mock.patch.dict(os.environ, {"SWARM_E2E": "1"}):   # no exemption any more
            with self.assertRaises(launch.UnsafeLaunch):
                launch.check_safe(["claude", "--setting-sources", "project"])


class CodexSpecTests(unittest.TestCase):
    def test_argv_workspace_write_stdin_prompt(self):
        s = launch.codex_spec(cfg(), prompt="BRIEF", workdir="/w", model="gpt-6-sol", minutes=60)
        self.assertEqual(s.argv, ("codex", "exec", "--json", "--sandbox", "workspace-write",
                                  "--skip-git-repo-check", "-m", "gpt-6-sol", "-"))
        self.assertIsNone(s.session_id)

    def test_configured_wrapper_string_and_argv(self):
        for value in ('"/opt/codex wrapper" --profile engineering',
                      ['/opt/codex wrapper', '--profile', 'engineering']):
            c = cfg()
            c['harness'] = {'codex': {'command': value}}
            with mock.patch.object(launch.shutil, 'which', return_value='/opt/codex wrapper') as which:
                spec = launch.codex_spec(c, prompt='p', workdir='/w', model=None, minutes=1,
                                         budget_usd=1, drive=True)
            self.assertEqual(spec.argv[:4], (os.path.abspath('/opt/codex wrapper'),
                                             '--profile', 'engineering', 'exec'))
            which.assert_called_once_with('/opt/codex wrapper')

    def test_path_then_plugin_bin_fallback(self):
        with mock.patch.object(launch.shutil, 'which', side_effect=[None, '/plugin/bin/codex']) as which:
            self.assertEqual(launch.codex_command(cfg(), resolve=True), [os.path.abspath('/plugin/bin/codex')])
        self.assertEqual(which.call_args_list[0].args, ('codex',))
        self.assertEqual(Path(which.call_args_list[1].args[0]), ROOT / 'bin' / 'codex')

    def test_missing_explicit_command_does_not_silently_substitute_path(self):
        c = cfg()
        c['harness'] = {'codex': {'command': '/missing/wrapper'}}
        with mock.patch.object(launch.shutil, 'which', return_value=None) as which:
            with self.assertRaisesRegex(ValueError, 'codex harness not available'):
                launch.codex_command(c, resolve=True)
        which.assert_called_once_with('/missing/wrapper')

    def test_token_limit(self):
        s = launch.codex_spec(cfg(codex_token_limit=200000), prompt="p", workdir="/w", model=None, minutes=1)
        self.assertEqual(s.argv[-7:], ("-c", "features.rollout_budget.enabled=true",
                                       "-c", "features.rollout_budget.limit_tokens=200000",
                                       "-c", "features.rollout_budget.reminder_at_remaining_tokens=[50000]",
                                       "-"))


class TokenLimitTests(unittest.TestCase):
    def test_limit_one_is_refused_and_small_limits_get_a_valid_reminder(self):
        from swarm.supervisor.settings import SettingsError
        with self.assertRaises(SettingsError):
            launch.codex_spec(cfg(codex_token_limit=1), prompt="p", workdir="/w", model=None, minutes=1)
        for n in (2, 3, 4, 7, 200000):
            argv = launch.codex_spec(cfg(codex_token_limit=n), prompt="p", workdir="/w", model=None,
                                     minutes=1).argv
            reminder = int(next(a for a in argv if "reminder_at_remaining_tokens" in a).split("=[")[1][:-1])
            self.assertTrue(0 < reminder < n, (n, reminder))
        self.assertNotIn("-c", launch.codex_spec(cfg(), prompt="p", workdir="/w", model=None, minutes=1).argv)


class SafetyTests(unittest.TestCase):
    def test_check_safe_refuses_every_bypass(self):
        for bad in launch.FORBIDDEN:
            with self.assertRaises(launch.UnsafeLaunch, msg=bad):
                launch.check_safe(["x", bad])
            with self.assertRaises(launch.UnsafeLaunch):
                launch.check_safe(["x", f"--permission-mode={bad}"])

    def test_no_bypass_string_outside_the_guard(self):
        pkg = ROOT / "lib/swarm/supervisor"
        for f in pkg.glob("*.py"):
            text = f.read_text()
            if f.name == "launch.py":
                text = re.sub(r"FORBIDDEN = \(.*?\)\n", "", text, flags=re.S)
            if f.name == "settings.py":
                text = text.replace('FORBIDDEN_PERMISSION_MODES = ("bypassPermissions",)', "")
            for bad in launch.FORBIDDEN:
                self.assertNotIn(bad, text, f"{bad} in {f.name}")


class MoreSafetyTests(unittest.TestCase):
    def test_check_safe_ignores_case_and_catches_config_overrides(self):
        for argv in (["codex", "--YOLO"], ["codex", "-c", 'sandbox_mode="danger-full-access"'],
                     ["claude", "--permission-mode", "BypassPermissions"]):
            with self.assertRaises(launch.UnsafeLaunch, msg=argv):
                launch.check_safe(argv)
        launch.check_safe(["codex", "exec", "--sandbox", "workspace-write", "-"])

    def test_settings_refuse_the_bypass_mode_before_any_spec(self):
        from swarm.supervisor.settings import SettingsError
        with self.assertRaises(SettingsError):
            launch.claude_spec(cfg(claude_permission_mode="bypass" + "Permissions"), prompt="p",
                               workdir="/w", model=None, minutes=1)

    def test_spec_for_picks_the_host(self):
        c = launch.spec_for(cfg(), "codex", prompt="p", workdir="/w", model=None, minutes=1,
                            session_id="ignored")
        self.assertEqual((c.harness, c.argv[:2], c.session_id), ("codex", ("codex", "exec"), None))
        c = launch.spec_for(cfg(claude_bin="/opt/claude"), "claude", prompt="p", workdir="/w",
                            model=None, minutes=1)
        self.assertEqual((c.harness, c.argv[:2]), ("claude", ("/opt/claude", "-p")))
        self.assertNotIn("--plugin-dir", c.argv)   # claude_plugin_dir defaults to "" (e2e only)


class ModelTests(unittest.TestCase):
    def test_override_then_role_then_recorded(self):
        c = cfg(model_override="haiku-cheap")
        self.assertEqual(launch.replacement_model(c, "claude", "worker", "opus"), "haiku-cheap")
        c = cfg()
        c["models"] = {"mode": "default", "claude": {"worker": "sonnet", "verifier": "haiku"}}
        self.assertEqual(launch.replacement_model(c, "claude", "verifier", "opus"), "haiku")
        self.assertEqual(launch.replacement_model(c, "claude", None, "opus"), "sonnet")
        self.assertEqual(launch.replacement_model(cfg(), "claude", "worker", "opus"), "opus")
        self.assertIsNone(launch.replacement_model(cfg(), "codex", "worker", None))

    def test_replacement_preserves_custom_role_model_on_each_host(self):
        c = cfg()
        c["models"] = {"claude": {"worker": "haiku", "product_manager": "opus"},
                       "codex": {"worker": "worker-model", "product_manager": "product-model"}}
        self.assertEqual(launch.replacement_model(c, "claude", "product_manager", "opus"), "opus")
        self.assertEqual(launch.replacement_model(c, "codex", "product_manager", "product-model"), "product-model")


def _without(argv, flag):
    """argv with `flag` and the value after it removed (the session-id pair differs per launch)."""
    out, skip = [], False
    for a in argv:
        if skip:
            skip = False
            continue
        if a == flag:
            skip = True
            continue
        out.append(a)
    return out


class LaunchContractTests(unittest.TestCase):
    def test_codex_budget_tokens(self):
        c = {"drive": {"codex_usd_per_mtok": 10}}
        spec = launch.spec_for(c, "codex", prompt="p", workdir="/w", model="m", minutes=5, budget_usd=2.5)
        self.assertIn("features.rollout_budget.limit_tokens=250000", " ".join(spec.argv))

    def test_codex_budget_overrides_the_fixed_token_limit(self):
        c = {"drive": {"codex_usd_per_mtok": 10}}
        spec = launch.spec_for(cfg(codex_token_limit=200000), "codex", prompt="p", workdir="/w", model=None,
                               minutes=5, budget_usd=0.3)
        self.assertIn("features.rollout_budget.limit_tokens=30000", spec.argv)
        self.assertNotIn("features.rollout_budget.limit_tokens=200000", spec.argv)

    def test_codex_budget_below_the_smallest_rollout_budget_is_refused(self):
        with self.assertRaises(ValueError):
            launch.spec_for({"drive": {"codex_usd_per_mtok": 10}}, "codex", prompt="p", workdir="/w",
                            model=None, minutes=5, budget_usd=1e-5)   # floor(1e-5 / 10 * 1e6) == 1
        spec = launch.spec_for({"drive": {"codex_usd_per_mtok": 10}}, "codex", prompt="p", workdir="/w",
                               model=None, minutes=5, budget_usd=2e-5)
        self.assertIn("features.rollout_budget.limit_tokens=2", spec.argv)

    def test_codex_budget_must_be_positive(self):
        with self.assertRaises(ValueError):
            launch.spec_for({"drive": {"codex_usd_per_mtok": 10}}, "codex", prompt="p", workdir="/w",
                            model=None, minutes=5, budget_usd=0)

    def test_resume_uses_same_contract(self):
        a = launch.spec_for({}, "claude", prompt="p", workdir="/w", model="opus", minutes=5, budget_usd=1)
        b = launch.spec_for({}, "claude", prompt="p", workdir="/w", model="opus", minutes=5, budget_usd=1,
                            resume="sid")
        self.assertEqual(b.session_id, "sid")
        self.assertEqual(b.argv[b.argv.index("--resume") + 1], "sid")
        self.assertNotIn("--session-id", b.argv)
        self.assertEqual(_without(b.argv, "--resume"), _without(_without(a.argv, "--session-id"), "--resume"))
        self.assertEqual(_without(b.argv, "--resume")[0:1], ["claude"])
        self.assertIn(("--max-budget-usd", "1"), list(zip(b.argv, b.argv[1:])))
        self.assertIn(("--model", "opus"), list(zip(b.argv, b.argv[1:])))

    def test_resume_still_passes_check_safe(self):
        with self.assertRaises(launch.UnsafeLaunch):
            launch.spec_for({}, "claude", prompt="p", workdir="/w", model=None, minutes=5,
                            resume="bypass" + "Permissions")
        with self.assertRaises(launch.UnsafeLaunch):
            launch.spec_for({}, "codex", prompt="p", workdir="/w", model=None, minutes=5,
                            resume="--dangerously-bypass-approvals-and-sandbox")

    def test_codex_resume_is_exec_resume(self):
        spec = launch.spec_for({}, "codex", prompt="p", workdir="/w", model="m", minutes=5, resume="thr-1")
        self.assertEqual(spec.argv[:3], ("codex", "exec", "resume"))
        self.assertEqual(spec.argv[-2:], ("thr-1", "-"))
        self.assertNotIn("--sandbox", spec.argv)
        self.assertIn('sandbox_mode="workspace-write"', spec.argv)
        self.assertIsNone(spec.session_id)

    def test_codex_resume_read_only_is_read_only(self):
        spec = launch.spec_for({}, "codex", prompt="p", workdir="/w", model=None, minutes=5, resume="thr-1",
                               read_only=True)
        self.assertIn('sandbox_mode="read-only"', spec.argv)
        self.assertNotIn('sandbox_mode="workspace-write"', spec.argv)

    def test_judge_read_only(self):
        spec = launch.spec_for({}, "codex", prompt="p", workdir="/w", model="m", minutes=5, read_only=True)
        self.assertIn("read-only", spec.argv)
        self.assertEqual(spec.argv[spec.argv.index("--sandbox") + 1], "read-only")

    def test_claude_read_only_disallows_edits(self):
        spec = launch.spec_for({}, "claude", prompt="p", workdir="/w", model=None, minutes=5, read_only=True)
        self.assertEqual(spec.argv[spec.argv.index("--disallowedTools") + 1], "Edit,Write,NotebookEdit,MultiEdit,mcp__*")
        writable = launch.spec_for({}, "claude", prompt="p", workdir="/w", model=None, minutes=5)
        self.assertNotIn("--disallowedTools", writable.argv)

    def test_claude_budget_must_be_positive(self):
        for bad in (0, -1, float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                launch.spec_for({}, "claude", prompt="p", workdir="/w", model=None, minutes=5, budget_usd=bad)

    def test_drive_launch_without_a_budget_is_refused(self):
        for harness in ("claude", "codex"):
            with self.assertRaises(ValueError):
                launch.spec_for({"drive": {"codex_usd_per_mtok": 10}}, harness, prompt="p", workdir="/w",
                                model=None, minutes=5, drive=True)
            with self.assertRaises(ValueError):
                launch.spec_for({}, harness, prompt="p", workdir="/w", model=None, minutes=5, drive=True,
                                resume="sid")
        # a drive launch with a budget is fine; a non-drive launch with none keeps the config path
        self.assertIn("--max-budget-usd", launch.spec_for({}, "claude", prompt="p", workdir="/w", model=None,
                                                         minutes=5, drive=True, budget_usd=1).argv)
        self.assertNotIn("--max-budget-usd", launch.spec_for({}, "claude", prompt="p", workdir="/w", model=None,
                                                            minutes=5).argv)

    def test_budget_given_replaces_the_configured_claude_budget(self):
        spec = launch.spec_for(cfg(max_budget_usd=9), "claude", prompt="p", workdir="/w", model=None,
                               minutes=5, budget_usd=1.25)
        self.assertEqual(spec.argv[spec.argv.index("--max-budget-usd") + 1], "1.25")
        self.assertNotIn("9", spec.argv)
