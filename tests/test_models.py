from __future__ import annotations

import json
import os
import unittest
from unittest import mock

from test_hooks_cli import Env  # noqa: E402
from support import base_config  # noqa: E402

from swarm import hooks as swarm_hooks, models  # noqa: E402


def cfg_with(mode="default", claude=None, codex=None):
    cfg = base_config()
    cfg["models"] = {"mode": mode}
    if claude is not None:
        cfg["models"]["claude"] = claude
    if codex is not None:
        cfg["models"]["codex"] = codex
    return cfg


CLAUDE = {"worker": "opus", "verifier": "sonnet", "judge": "opus", "helper": "haiku"}


class ModelRuleTests(unittest.TestCase):
    def test_role_detection(self):
        self.assertEqual(models.role_of("[swarm role: verifier]\nx", False), "verifier")
        self.assertEqual(models.role_of("[swarm role: judge]", False), "judge")
        self.assertEqual(models.role_of("[swarm role: judge]", True), "helper")   # spawned by a member
        self.assertEqual(models.role_of("do things", False), "worker")

    def test_modes(self):
        self.assertEqual(models.choose(cfg_with(claude=CLAUDE), "claude", "verifier", None), "sonnet")
        self.assertIsNone(models.choose(cfg_with(claude=CLAUDE), "claude", "verifier", "opus"))       # default: caller's pick stays
        self.assertEqual(models.choose(cfg_with("enforce", claude=CLAUDE), "claude", "verifier", "opus"), "sonnet")
        self.assertIsNone(models.choose(cfg_with("off", claude=CLAUDE), "claude", "worker", None))
        self.assertIsNone(models.choose(cfg_with(claude=CLAUDE), "codex", "worker", None))            # no host section: off
        self.assertEqual(models.choose(cfg_with(claude={"worker": "opus"}), "claude", "judge", None), "opus")  # falls back to worker
        self.assertEqual(models.choose(cfg_with(claude={"worker": "my-custom-model"}), "claude", "worker", None), "my-custom-model")

    def test_hint(self):
        self.assertIn("verifier=sonnet", models.spawn_hint(cfg_with(claude=CLAUDE), "claude"))
        self.assertIsNone(models.spawn_hint(cfg_with("off", claude=CLAUDE), "claude"))


class ModelHookTests(Env):
    def setUp(self):
        super().setUp()
        self.cfg["models"] = {"mode": "default", "claude": dict(CLAUDE)}

    def activate(self):
        """Job J bound to this session (sess-1), as a real activate from Claude or Codex makes it:
        the marker-only orchestrator path only looks at the session's bound jobs."""
        rc, _, err = self.cli("activate", "--job", "J", "--session", "sess-1")
        self.assertEqual(rc, 0, err)

    def orchestrator_spawn(self, prompt, **ti):
        return self.hook("turn", agent_id=None, tool_name="Agent",
                         tool_input={"prompt": prompt, "description": "d", **ti})

    def test_orchestrator_spawn_gets_role_model(self):
        self.activate()
        out = self.orchestrator_spawn("[swarm job: J]\n[swarm role: verifier]\ncheck")
        o = out["hookSpecificOutput"]
        self.assertEqual(o["updatedInput"]["model"], "sonnet")
        self.assertEqual(o["updatedInput"]["prompt"], "[swarm job: J]\n[swarm role: verifier]\ncheck")
        self.assertNotIn("permissionDecision", o)          # Claude: updatedInput alone

    def test_orchestrator_choice_kept_in_default_mode(self):
        self.activate()
        self.assertIsNone(self.orchestrator_spawn("[swarm job: J]\nwork", model="haiku"))

    def test_no_active_job_no_rewrite(self):
        self.assertIsNone(self.orchestrator_spawn("[swarm job: J]\nwork"))

    def test_codex_orchestrator_spawn_uses_allow_plus_full_arguments(self):
        from swarm import hosts
        self.cfg["models"]["codex"] = {"worker": "gpt-test"}
        self.activate()
        args = {"message": "[swarm job: J]\nwork", "agent_type": "default"}
        # the shape used once support is confirmed (the default is False)
        with mock.patch.object(type(hosts.get("codex")), "supports_spawn_model_rewrite", True):
            out = self.hook("turn", agent_id=None, host="codex", turn_id="t1", model="m",
                            tool_name="spawn_agent", tool_input=args)
        self.assertEqual(out["hookSpecificOutput"], {
            "hookEventName": "PreToolUse", "permissionDecision": "allow",
            "updatedInput": {**args, "model": "gpt-test"}})

    def test_codex_without_rewrite_support_emits_nothing(self):
        from swarm import hosts
        self.cfg["models"]["codex"] = {"worker": "gpt-test"}
        self.activate()
        with mock.patch.object(type(hosts.get("codex")), "supports_spawn_model_rewrite", False):
            out = self.hook("turn", agent_id=None, host="codex", turn_id="t1", model="m",
                            tool_name="spawn_agent", tool_input={"message": "[swarm job: J]\nwork"})
        self.assertIsNone(out)

    def test_deny_after_rewrite_drops_the_rewrite(self):
        swarm_hooks._OUTPUT.clear()
        swarm_hooks._set_input_rewrite({"permissionDecision": "allow", "updatedInput": {"message": "m"}})
        swarm_hooks._deny("no")
        o = swarm_hooks._OUTPUT["hookSpecificOutput"]
        self.assertEqual((o["permissionDecision"], "updatedInput" in o), ("deny", False))
        swarm_hooks._set_input_rewrite({"permissionDecision": "allow", "updatedInput": {"message": "m"}})
        self.assertEqual((o["permissionDecision"], "updatedInput" in o), ("deny", False))   # deny sticks
        swarm_hooks._OUTPUT.clear()

    def test_attach_prints_model_hint_when_codex_cannot_rewrite(self):
        from swarm import hosts
        self.config.write_text(self.config.read_text()
                               + '[models]\nmode = "default"\n[models.codex]\nworker = "gpt-test"\n')
        self.activate()
        with mock.patch.object(type(hosts.get("codex")), "supports_spawn_model_rewrite", False), \
                mock.patch.dict(os.environ, {"SWARM_HOST": "codex", "CODEX_THREAD_ID": "t", "CODEX_SESSION_ID": "cs"}):
            rc, out, err = self.cli("activate", "--job", "J", "--attach", "--session", "cs")
        self.assertEqual(rc, 0, err)
        self.assertIn("worker=gpt-test", out)
        self.assertNotIn("model hint: use spawn_agent", out)          # the placeholder is gone

    def test_codex_activate_prints_no_hint_when_the_hook_sets_models(self):
        self.config.write_text(self.config.read_text()
                               + '[models]\nmode = "default"\n[models.codex]\nworker = "gpt-test"\n')
        with mock.patch.dict(os.environ, {"SWARM_HOST": "codex", "CODEX_THREAD_ID": "cs", "CODEX_SESSION_ID": "cs"}):
            rc, out, err = self.cli("activate", "--job", "J")
        self.assertEqual(rc, 0, err)
        self.assertNotIn("gpt-test", out)
        self.assertNotIn("model hint", out)

    def test_codex_orchestrator_spawn_role_from_task_name(self):
        # the recorded shape: namespaced tool name, encrypted message, plaintext task_name
        self.cfg["models"]["codex"] = {"worker": "gpt-w", "verifier": "gpt-v"}
        self.activate()
        args = {"task_name": "verifier-1", "fork_turns": "none", "message": "gAAAAA-synthetic-ciphertext"}
        out = self.hook("turn", agent_id=None, host="codex", turn_id="t1", model="m",
                        tool_name="collaborationspawn_agent", tool_input=args)
        self.assertEqual(out["hookSpecificOutput"], {
            "hookEventName": "PreToolUse", "permissionDecision": "allow",
            "updatedInput": {**args, "model": "gpt-v"}})
        out = self.hook("turn", agent_id=None, host="codex", turn_id="t1", model="m",
                        tool_name="collaborationspawn_agent", tool_input={**args, "task_name": "fixer"})
        self.assertEqual(out["hookSpecificOutput"]["updatedInput"]["model"], "gpt-w")

    def test_orchestrator_spawn_into_an_unbound_job_gets_the_role_model(self):
        rc, _, err = self.cli("activate", "--job", "J")              # bound to no session (claimable)
        self.assertEqual(rc, 0, err)
        out = self.orchestrator_spawn("[swarm role: verifier]\ncheck")   # untagged: the only job
        self.assertEqual(out["hookSpecificOutput"]["updatedInput"]["model"], "sonnet")
        out = self.orchestrator_spawn("[swarm job: J]\nwork")
        self.assertEqual(out["hookSpecificOutput"]["updatedInput"]["model"], "opus")

    def test_orchestrator_spawn_with_several_jobs_needs_the_tag(self):
        self.activate()
        rc, _, err = self.cli("activate", "--job", "K", "--session", "sess-1")
        self.assertEqual(rc, 0, err)
        self.assertIsNone(self.orchestrator_spawn("work, no tag"))                     # which job? unknown
        out = self.orchestrator_spawn("[swarm job: K]\nwork")
        self.assertEqual(out["hookSpecificOutput"]["updatedInput"]["model"], "opus")


class RoleHintTests(unittest.TestCase):
    def test_role_of_uses_the_host_hint_when_untagged(self):
        self.assertEqual(models.role_of("gAAAAA", False, "verifier"), "verifier")
        self.assertEqual(models.role_of("[swarm role: judge]", False, "verifier"), "judge")   # the tag wins
        self.assertEqual(models.role_of("x", True, "verifier"), "helper")
        self.assertEqual(models.role_of("x", False, "nonsense"), "worker")

