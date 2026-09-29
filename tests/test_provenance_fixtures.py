"""The hook payloads of memory-write tool calls, as each host really sends them."""
from __future__ import annotations

import json
import unittest

import codex_fixtures as CF  # noqa: E402
import provenance_fixtures as PF  # noqa: E402


class FixtureShapeTests(unittest.TestCase):
    def test_claude_bash_post_payload_shape(self):
        p = json.loads(PF.CLAUDE_FIX.read_text())
        self.assertEqual((p["hook_event_name"], p["tool_name"]), ("PostToolUse", "Bash"))
        self.assertTrue(p["tool_use_id"].startswith("toolu_"))
        self.assertTrue(p["agent_id"])
        self.assertIsInstance(p["tool_input"]["command"], str)
        self.assertIsInstance(p["tool_response"], (dict, str))
        if isinstance(p["tool_response"], dict):
            self.assertIsInstance(p["tool_response"]["stdout"], str)

    def test_claude_builder_replaces_only_command_output_and_ids(self):
        p = PF.claude_post("echo hi", "hi\n", agent_id="a9", transcript_path="/x/s.jsonl")
        base = json.loads(PF.CLAUDE_FIX.read_text())
        self.assertEqual(p["tool_input"]["command"], "echo hi")
        self.assertEqual(PF.output_of(p), "hi\n")
        self.assertEqual((p["agent_id"], p["transcript_path"]), ("a9", "/x/s.jsonl"))
        self.assertEqual(set(p), set(base))

    def test_codex_bash_post_payload_shape(self):
        p = PF.codex_post("echo hi", "hi\n")
        self.assertEqual(p["tool_name"], "Bash")
        self.assertIsInstance(p["tool_response"], str)
        self.assertTrue(p["tool_use_id"].startswith("call_"))
        self.assertTrue(p["agent_id"])

    def test_codex_bash_call_id_is_not_in_the_rollout(self):
        """code mode wraps the shell call in an `exec` call with its own id."""
        p = PF.codex_post("echo hi", "hi\n")
        self.assertNotIn(p["tool_use_id"], CF.rollout("child").read_text())


if __name__ == "__main__":
    unittest.main()
