"""Host adapters and host detection."""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from support import ROOT  # noqa: F401  (sets sys.path)

from swarm import hosts  # noqa: E402


def _tree(pairs: dict[int, tuple[int, list[str]]]):
    """parent_of for a fake process tree: pid -> (ppid, argv)."""
    return lambda pid: pairs.get(pid)


class HostDetectionTests(unittest.TestCase):
    def test_flag_wins(self):
        self.assertEqual(hosts.detect_hook_host("codex", {}, {}), "codex")
        self.assertEqual(hosts.detect_hook_host("claude", {"turn_id": "t"}, {"PLUGIN_ROOT": "/p"}), "claude")

    def test_payload_with_turn_id_is_codex(self):
        self.assertEqual(hosts.detect_hook_host(None, {"turn_id": "t1", "model": "m"}, {}), "codex")

    def test_plugin_root_env_is_codex_but_claude_plugin_root_is_not(self):
        self.assertEqual(hosts.detect_hook_host(None, {}, {"PLUGIN_ROOT": "/x"}), "codex")
        self.assertEqual(hosts.detect_hook_host(None, {}, {"CLAUDE_PLUGIN_ROOT": "/x"}), "claude")

    def test_cli_env_only_one_host(self):
        self.assertEqual(hosts.detect_cli_host({"CODEX_THREAD_ID": "t"}), "codex")
        self.assertEqual(hosts.detect_cli_host({"CLAUDE_CODE_SESSION_ID": "s"}), "claude")
        self.assertIsNone(hosts.detect_cli_host({}))

    def test_cli_override(self):
        self.assertEqual(hosts.detect_cli_host({"SWARM_HOST": "codex", "CLAUDE_CODE_SESSION_ID": "s"}), "codex")

    def test_nested_codex_inside_claude_takes_nearest_ancestor(self):
        env = {"CLAUDE_CODE_SESSION_ID": "s", "CLAUDECODE": "1", "CODEX_THREAD_ID": "t"}
        tree = _tree({100: (90, ["/bin/sh"]), 90: (80, ["/usr/local/bin/codex", "exec"]),
                      80: (70, ["/home/u/.local/bin/claude"]), 70: (1, ["bash"])})
        self.assertEqual(hosts.detect_cli_host(env, parent_of=tree, pid=100), "codex")

    def test_nested_claude_inside_codex(self):
        env = {"CLAUDE_CODE_SESSION_ID": "s", "CODEX_SESSION_ID": "c"}
        tree = _tree({100: (90, ["bash"]), 90: (80, ["node", "/usr/lib/node_modules/@anthropic-ai/claude-code/cli.js"]),
                      80: (1, ["codex"])})
        self.assertEqual(hosts.detect_cli_host(env, parent_of=tree, pid=100), "claude")

    def test_nested_unknown_tree_is_none(self):
        env = {"CLAUDE_CODE_SESSION_ID": "s", "CODEX_SESSION_ID": "c"}
        self.assertIsNone(hosts.detect_cli_host(env, parent_of=_tree({}), pid=100))

    def test_unregistered_host_is_refused_even_when_cached(self):
        hosts.get("claude")                                   # cached now
        with unittest.mock.patch.dict(hosts._CLASSES, {}, clear=True):
            with self.assertRaises(KeyError):
                hosts.get("claude")
        self.assertEqual(hosts.get("claude").name, "claude")   # registry restored

    def test_cli_session_id_claude(self):
        self.assertEqual(hosts.cli_session_id({"CLAUDE_CODE_SESSION_ID": "abc"}), "abc")
        self.assertIsNone(hosts.cli_session_id({}))


class ClaudeHostTests(unittest.TestCase):
    def setUp(self):
        self.h = hosts.get("claude")
        self.tmp = Path(tempfile.mkdtemp(prefix="swarm-host-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.main = self.tmp / "sess.jsonl"
        self.main.write_text("")
        sub = self.tmp / "sess" / "subagents"
        sub.mkdir(parents=True)
        (sub / "agent-a1.jsonl").write_text(
            json.dumps({"type": "user", "message": {"content": "[swarm job: J]\ndo it"}}) + "\n" +
            json.dumps({"type": "assistant", "message": {"model": "claude-opus-5-5", "content": []}}) + "\n")
        (sub / "agent-a1.meta.json").write_text(json.dumps({"spawnDepth": 1}))
        self.payload = {"transcript_path": str(self.main)}

    def test_tools(self):
        self.assertEqual(self.h.spawn_tools, ("Agent", "Task"))
        self.assertEqual(self.h.verifier_denied, ("Edit", "Write", "MultiEdit", "NotebookEdit", "Agent", "Task"))
        self.assertTrue(self.h.stop_is_final)

    def test_spawn_call(self):
        c = self.h.spawn_call({"prompt": "p", "description": "d", "model": "haiku"})
        self.assertEqual((c.prompt, c.description, c.model), ("p", "d", "haiku"))
        self.assertEqual(self.h.spawn_call({}).prompt, "")

    def test_spawn_prompt_depth_model_transcript(self):
        self.assertEqual(self.h.spawn_prompt(self.payload, "a1"), "[swarm job: J]\ndo it")
        self.assertIsNone(self.h.spawn_prompt(self.payload, "missing"))
        self.assertEqual(self.h.spawn_depth(self.payload, "a1"), 1)
        self.assertEqual(self.h.agent_model(self.payload, "a1"), "claude-opus-5-5")
        self.assertEqual(self.h.subagent_transcript(self.payload, "a1"),
                         self.tmp / "sess" / "subagents" / "agent-a1.jsonl")

    def test_rewrite_spawn_model_replaces_whole_input(self):
        self.assertEqual(self.h.rewrite_spawn_model({"prompt": "p"}, "sonnet"), {"prompt": "p", "model": "sonnet"})
        self.assertEqual(self.h.input_rewrite_output({"prompt": "p"}), {"updatedInput": {"prompt": "p"}})


class ClaudeSessionLookupTests(unittest.TestCase):
    """A board-supplied session id never becomes a filesystem pattern."""
    UUID = "0b5c2a8e-1f3d-4c6b-9a7e-2d4f6a8c0e1b"

    def setUp(self):
        self.h = hosts.get("claude")
        self.tmp = Path(tempfile.mkdtemp(prefix="swarm-host-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.projects = self.tmp / "projects"
        self.main = self.projects / "-proj" / f"{self.UUID}.jsonl"
        self.main.parent.mkdir(parents=True)
        self.main.write_text("{}\n")
        # files a glob would match for a metacharacter "session id"
        (self.projects / "-proj" / "other-session.jsonl").write_text("{}\n")
        (self.projects / "-proj" / "[x].jsonl").write_text("{}\n")
        p = unittest.mock.patch.dict("os.environ", {"CLAUDE_CONFIG_DIR": str(self.tmp)})
        p.start()
        self.addCleanup(p.stop)

    def test_claude_session_id_must_be_uuid(self):
        bad = ["*", "?", "[x]", "..", "../x", "other-session", "*-session", self.UUID + "*",
               self.UUID[:-1] + "?", "", None, self.UUID + "\n"]
        with unittest.mock.patch("pathlib.Path.glob", side_effect=AssertionError("glob")), \
                unittest.mock.patch("os.scandir", side_effect=AssertionError("filesystem touched")), \
                unittest.mock.patch("pathlib.Path.iterdir", side_effect=AssertionError("filesystem touched")):
            for sid in bad:
                self.assertIsNone(self.h.find_session_transcript(sid), sid)

    def test_a_uuid_is_found_by_exact_name_without_glob(self):
        with unittest.mock.patch("pathlib.Path.glob", side_effect=AssertionError("glob")):
            self.assertEqual(self.h.find_session_transcript(self.UUID), self.main)
            self.assertEqual(self.h.find_session_transcript(self.UUID.upper()), None)   # names are exact
            self.assertIsNone(self.h.find_session_transcript("1b5c2a8e-1f3d-4c6b-9a7e-2d4f6a8c0e1b"))

    def test_a_symlinked_project_dir_or_session_file_is_not_followed(self):
        outside = self.tmp / "outside"
        outside.mkdir()
        other = "2b5c2a8e-1f3d-4c6b-9a7e-2d4f6a8c0e1b"
        (outside / f"{other}.jsonl").write_text("{}\n")
        (self.projects / "-link").symlink_to(outside)
        self.assertIsNone(self.h.find_session_transcript(other))
        third = "3b5c2a8e-1f3d-4c6b-9a7e-2d4f6a8c0e1b"
        (self.projects / "-proj" / f"{third}.jsonl").symlink_to(outside / f"{other}.jsonl")
        self.assertIsNone(self.h.find_session_transcript(third))

    def test_the_session_id_pattern_is_shared_with_codex(self):
        from swarm.hosts import base, codex
        self.assertIs(codex.THREAD_ID, base.SESSION_ID)
        self.assertTrue(base.SESSION_ID.fullmatch(self.UUID))
        self.assertIsNone(base.SESSION_ID.fullmatch("x" + self.UUID))
