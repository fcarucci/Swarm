"""CodexHost against the recorded Codex 0.157.1 session (MultiAgentV2: the spawn message is
encrypted, tool names carry the "collaboration" namespace; see ANSWERS.md and the Codex routing
."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from support import ROOT  # noqa: F401
import codex_fixtures as F  # noqa: E402

from swarm import hosts  # noqa: E402

SHELL_TOOLS = ("Bash", "shell", "exec_command", "local_shell", "unified_exec")


def _spawns(event: str = "PreToolUse") -> list[dict]:
    return [p for p in F.payloads(event) if str(p.get("tool_name", "")).endswith("spawn_agent")]


class AnswersParserTests(unittest.TestCase):
    """The ANSWERS.md parser against the real (long) lines, evidence and all."""

    def test_q1_real_line(self):
        self.assertEqual(F.answer("Q1"), {"spawn message in child rollout at SubagentStart": "no"})

    def test_q3_and_q4_fields(self):
        self.assertEqual(F.answer("Q3"), {"CODEX_SESSION_ID == hook session_id": "yes",
                                          "CODEX_THREAD_ID == hook agent_id": "yes",
                                          "root CODEX_THREAD_ID == hook session_id": "yes"})
        self.assertEqual(F.answer("Q4"), {"PreToolUse updatedInput honoured for spawn_agent": "yes",
                                          "spawn_agent accepts model": "yes"})


class FixtureBuilderTests(unittest.TestCase):
    def test_builder_context_prefixes_match_the_adapter(self):
        from make_fixtures import CONTEXT
        from swarm.hosts.codex import CONTEXT_PREFIXES
        self.assertEqual(CONTEXT, CONTEXT_PREFIXES)

    def test_no_captured_hex_identifiers(self):
        # every 32+ hex-digit run in the fixtures is a synthetic one (the capture's ids are all replaced)
        from make_fixtures import SYNTH_HEX, hex_leaks
        self.assertEqual(hex_leaks(F.FIX), [])
        self.assertIn(SYNTH_HEX, (F.FIX / "ANSWERS.md").read_text())          # the P line's connector id
        import tempfile
        d = Path(tempfile.mkdtemp(prefix="swarm-hex-"))
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        (d / "x.md").write_text(f"ok {SYNTH_HEX} bad plugin_connector_1p_{'95d3' * 8}\n")
        self.assertEqual(hex_leaks(d), [("x.md", "95d3" * 8)])          # the rebuild would stop on it

    def test_fixture_image_is_the_synthetic_png(self):
        from make_fixtures import SYNTH_B64
        self.assertIn("base64," + SYNTH_B64, F.rollout("root").read_text())


class CodexHostTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="swarm-codex-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = F.staged(self.tmp)
        p = mock.patch.dict(os.environ, {"CODEX_HOME": str(self.home)}); p.start(); self.addCleanup(p.stop)
        self.h = hosts.get("codex")

    def test_tools_and_stop(self):
        self.assertEqual(self.h.spawn_tools, ("spawn_agent",))
        self.assertEqual(self.h.verifier_denied, ("apply_patch", "spawn_agent"))
        self.assertFalse(self.h.stop_is_final)
        self.assertFalse(self.h.completes_on("stop"))
        self.assertFalse(self.h.completes_on("session-stop"))
        self.assertTrue(self.h.completes_on("session-end"))

    def test_namespaced_tool_names_match_by_suffix(self):
        spawn = _spawns()[0]
        self.assertEqual(spawn["tool_name"], "collaborationspawn_agent")      # as recorded
        self.assertTrue(self.h.is_spawn(spawn))
        self.assertTrue(self.h.denies_verifier(spawn))
        self.assertTrue(self.h.denies_verifier({"tool_name": "apply_patch"}))
        for name in ("collaborationwait_agent", "collaborationfollowup_task", "Bash", "view_image"):
            self.assertFalse(self.h.is_spawn({"tool_name": name}), name)
            self.assertFalse(self.h.denies_verifier({"tool_name": name}), name)
        self.assertFalse(hosts.get("claude").is_spawn(spawn))                # Claude matches exactly

    def test_every_recorded_payload_is_detected_as_codex(self):
        for ev in ("SubagentStart", "PreToolUse", "PostToolUse", "SubagentStop"):
            for p in F.payloads(ev):
                self.assertEqual(hosts.detect_hook_host(None, p, {}), "codex", ev)

    def test_spawn_call_from_recorded_spawn_agent(self):
        spawns = _spawns()
        self.assertGreaterEqual(len(spawns), 2)                       # root->child, child->grandchild
        call = self.h.spawn_call(spawns[0]["tool_input"])
        self.assertEqual(call.description, "fixture")                 # the plaintext task_name
        self.assertTrue(call.prompt.startswith("gAAAAA"))             # V2: the message is ciphertext
        self.assertIsNone(call.model)
        self.assertEqual(self.h.spawn_call(spawns[1]["tool_input"]).description, "grandchild_fixture")
        v1 = self.h.spawn_call({"message": "[swarm job: J]\nhi", "agent_type": "worker", "model": "m"})
        self.assertEqual((v1.prompt, v1.description, v1.model), ("[swarm job: J]\nhi", "worker", "m"))
        self.assertEqual(self.h.spawn_call({"items": [{"type": "text", "text": "a"}]}).prompt, "a")
        self.assertEqual(self.h.spawn_call("junk").description, "a subagent")

    def test_spawn_prompt_after_first_tool_call_is_readable_but_encrypted(self):
        child_turn = next(p for p in F.payloads("PreToolUse") if p.get("agent_id") == F.ids()["child"]["thread"])
        prompt = self.h.spawn_prompt(F.localize(child_turn, self.home), child_turn["agent_id"])
        self.assertEqual(prompt, "")        # the task message arrived, encrypted: readable, no tags

    def test_spawn_prompt_plaintext_task_message(self):
        # a plaintext NEW_TASK payload (multi-agent V1 shape) is the prompt, header and context dropped
        roll = self.tmp / "rollout-v1.jsonl"
        lines = [{"type": "session_meta", "payload": {"id": "x", "source": "exec"}},
                 {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [
                     {"type": "input_text", "text": "<environment_context> synthetic context"}]}},
                 {"type": "response_item", "payload": {"type": "agent_message", "author": "/root",
                  "recipient": "/root/w", "content": [{"type": "input_text", "text":
                      "Message Type: NEW_TASK\nTask name: /root/w\nSender: /root\nPayload:\n[swarm job: J]\ndo it"}]}}]
        roll.write_text("".join(json.dumps(line) + "\n" for line in lines))
        self.assertEqual(self.h.spawn_prompt({"transcript_path": str(roll)}, "w"), "[swarm job: J]\ndo it")
        user = self.tmp / "rollout-user.jsonl"
        user.write_text(json.dumps(lines[0]) + "\n" + json.dumps({"type": "response_item", "payload": {
            "type": "message", "role": "user", "content": [{"type": "input_text", "text": "[swarm job: K]"}]}}) + "\n")
        self.assertEqual(self.h.spawn_prompt({"transcript_path": str(user)}, "u"), "[swarm job: K]")

    def _with_user_messages(self, role: str, *texts_at: tuple[int, str]) -> Path:
        """A copy of a recorded rollout with user messages inserted at (ordinal, text)."""
        lines = [json.loads(l) for l in F.rollout(role).read_text().splitlines()]
        for ordinal, text in texts_at:
            lines.append({"type": "response_item", "ordinal": ordinal, "payload": {
                "type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}})
        lines.sort(key=lambda e: e.get("ordinal", 0))
        path = self.tmp / f"rollout-{role}-edited.jsonl"
        path.write_text("".join(json.dumps(line) + "\n" for line in lines))
        return path

    def test_forked_rollout_skips_only_the_inherited_history(self):
        # the recorded grandchild was forked (fork_turns "all"): its own header has forked_from_id
        # and subagent_history_start_ordinal, and the parent's session_meta follows it
        header = json.loads(F.rollout("grandchild").read_text().splitlines()[0])["payload"]
        self.assertTrue(header["forked_from_id"])
        start = header["subagent_history_start_ordinal"]
        path = self._with_user_messages("grandchild", (start - 1, "[swarm role: judge]\nthe parent's"),
                                        (start + 100, "[swarm job: K]\nthe child's own"))
        got = self.h.spawn_prompt({"transcript_path": str(path)}, "g")
        self.assertIn("the child's own", got)
        self.assertNotIn("judge", got)
        self.assertEqual(self.h.spawn_prompt({"transcript_path": str(F.rollout("grandchild"))}, "g"), "")

    def test_unforked_rollout_keeps_every_user_message(self):
        header = json.loads(F.rollout("child").read_text().splitlines()[0])["payload"]
        self.assertNotIn("forked_from_id", header)
        path = self._with_user_messages("child", (1, "[swarm job: J]\nearly"), (40, "later"))
        got = self.h.spawn_prompt({"transcript_path": str(path)}, "c")
        self.assertIn("early", got)
        self.assertIn("later", got)

    def test_spawn_prompt_at_subagent_start_matches_answer_q1(self):
        start = F.payloads("SubagentStart")[0]
        at_hook = F.transcript_at_hook("SubagentStart", 1)
        payload = {**start, "transcript_path": str(at_hook)}
        got = self.h.spawn_prompt(payload, start["agent_id"])
        first = json.loads(at_hook.read_text().splitlines()[0])
        own = (first.get("payload") or {}).get("id") == start["agent_id"]     # the child's own rollout?
        self.assertTrue(own)
        if F.answer("Q1")["spawn message in child rollout at SubagentStart"] == "yes":
            self.assertIn("[swarm job: fixture]", got)
        else:
            self.assertIsNone(got)                                          # not there yet: defer

    def test_spawn_prompt_unreadable_rollout_is_none(self):
        self.assertIsNone(self.h.spawn_prompt({"transcript_path": str(self.tmp / "missing.jsonl")}, "nope"))

    def test_depths_and_agent_paths(self):
        ids = F.ids()
        for role, depth, path in (("child", 1, "/root/fixture"), ("grandchild", 2, "/root/fixture/grandchild_fixture")):
            turn = next(p for p in F.payloads("PreToolUse") if p.get("agent_id") == ids[role]["thread"])
            local = F.localize(turn, self.home)
            self.assertEqual(self.h.spawn_depth(local, turn["agent_id"]), depth, role)
            self.assertEqual(self.h.agent_path(local, turn["agent_id"]), path, role)
        # the grandchild's rollout carries two session_meta lines (its own first, then the forked
        # parent's): the depth is its own
        metas = [l for l in F.rollout("grandchild").read_text().splitlines() if '"session_meta"' in l]
        self.assertEqual(len(metas), 2)
        root = {"transcript_path": str(next(self.home.glob(f"sessions/*/*/*/*{ids['root']['thread']}.jsonl")))}
        self.assertEqual(self.h.spawn_depth(root, ids["root"]["thread"]), 0)
        self.assertIsNone(self.h.agent_path(root, ids["root"]["thread"]))

    def test_depth_found_by_thread_id_without_transcript_path(self):
        child = F.ids()["child"]["thread"]
        self.assertEqual(self.h.spawn_depth({}, child), 1)

    def test_input_rewrite_output_shape(self):
        self.assertEqual(self.h.input_rewrite_output({"message": "m", "model": "x"}),
                         {"permissionDecision": "allow", "updatedInput": {"message": "m", "model": "x"}})

    def test_spawn_model_rewrite_per_q4(self):
        q4 = F.answer("Q4")
        want = q4["PreToolUse updatedInput honoured for spawn_agent"] == "yes" and q4["spawn_agent accepts model"] == "yes"
        self.assertEqual(self.h.supports_spawn_model_rewrite, want)
        ti = _spawns()[0]["tool_input"]
        self.assertEqual(self.h.rewrite_spawn_model(ti, "gpt-x"), {**ti, "model": "gpt-x"})   # ciphertext kept

    def test_shell_command_shape_from_fixture(self):
        bash = next(p for p in F.payloads("PreToolUse") if p.get("tool_name") in SHELL_TOOLS)
        cmd = bash["tool_input"].get("command", bash["tool_input"].get("cmd"))
        self.assertEqual(self.h.shell_command(bash), " ".join(cmd) if isinstance(cmd, list) else cmd)
        self.assertEqual(self.h.shell_command({"tool_name": "Bash", "tool_input": {"command": ["bash", "-lc", "rm x"]}}),
                         "bash -lc rm x")
        self.assertIsNone(self.h.shell_command({"tool_name": "apply_patch", "tool_input": {"command": "x"}}))

    def test_model_from_payload(self):
        p = F.payloads("PreToolUse")[0]
        self.assertEqual(self.h.agent_model(p, p.get("agent_id") or ""), p["model"])

    def test_transcript_locators(self):
        ids = F.ids()
        self.assertTrue(str(self.h.find_agent_transcript(None, ids["child"]["thread"])).endswith(f"{ids['child']['thread']}.jsonl"))
        root = self.h.find_session_transcript(ids["root"]["session"])
        self.assertIsNotNone(root)
        self.assertIsNone(self.h.find_session_transcript("no-such-id"))
        stop = next(p for p in F.payloads("SubagentStop") if p.get("agent_id") == ids["child"]["thread"])
        self.assertEqual(self.h.subagent_transcript(stop, stop["agent_id"]), Path(stop["agent_transcript_path"]))
        self.assertTrue(str(self.h.subagent_transcript({}, ids["child"]["thread"])).endswith(".jsonl"))

    def test_cli_session_id_matches_hook_session_per_q3(self):
        ids = F.ids()
        env = {"CODEX_SESSION_ID": ids["root"]["session"], "CODEX_THREAD_ID": ids["root"]["thread"]}
        hook_sid = F.payloads("PreToolUse")[0]["session_id"]
        fields = F.answer("Q3")
        if fields["CODEX_SESSION_ID == hook session_id"] == "yes" or fields["root CODEX_THREAD_ID == hook session_id"] == "yes":
            self.assertEqual(self.h.cli_session_id(env), hook_sid)
        else:                               # no id visible to the shell matches: no binding
            self.assertIsNone(self.h.cli_session_id(env))
        self.assertEqual(hosts.cli_session_id(env), hook_sid)          # detect_cli_host -> codex

    def test_zst_rollout_is_read_and_found_in_archive(self):
        import zstandard                                              # a requirement: never skipped
        from swarm.hosts.codex import find_rollout, read_rollout
        src = F.rollout("child")
        arch = self.home / "archived_sessions" / "2026" / "09" / "27"; arch.mkdir(parents=True)
        z = arch / "rollout-2026-09-27T00-00-00-00000000-0000-4000-8000-00000000abcd.jsonl.zst"
        z.write_bytes(zstandard.ZstdCompressor().compress(src.read_bytes()))
        self.assertEqual(find_rollout("00000000-0000-4000-8000-00000000abcd"), z)
        self.assertEqual(read_rollout(z), src.read_text())

    def test_corrupt_plain_rollouts_raise_with_a_reason(self):
        from swarm.hosts.codex import RolloutUnreadable, read_rollout
        meta = json.dumps({"type": "session_meta", "payload": {"id": "x", "source": "exec"}}) + "\n"
        cases = {"empty": b"", "blank": b"\n\n", "not utf-8": meta.encode() + b'{"a": "\xff\xfe"}\n',
                 "bad line": meta.encode() + b"{not json\n" + meta.encode(),
                 "not an object": meta.encode() + b"[1, 2]\n",
                 "no session_meta": b'{"type": "response_item", "payload": {}}\n'}
        for why, data in cases.items():
            p = self.tmp / f"rollout-{why.replace(' ', '-')}.jsonl"; p.write_bytes(data)
            with self.assertRaises(RolloutUnreadable, msg=why) as ctx:
                read_rollout(p)
            self.assertIn(str(p), str(ctx.exception), why)
        with self.assertRaises(RolloutUnreadable):
            read_rollout(self.tmp / "missing.jsonl")

    def test_rollout_being_written_drops_only_its_unfinished_last_line(self):
        from swarm.hosts.codex import read_rollout
        whole = F.rollout("child").read_text()
        p = self.tmp / "rollout-live.jsonl"; p.write_text(whole + '{"type": "response_it')
        self.assertEqual(read_rollout(p), whole)
        self.assertEqual(read_rollout(F.rollout("child")), whole)

    def test_unterminated_last_line(self):
        from swarm.hosts.codex import RolloutUnreadable, read_rollout
        whole = F.rollout("child").read_bytes()
        last = json.dumps({"type": "response_item", "payload": {"type": "message", "role": "assistant",
                                                                "content": [{"type": "output_text", "text": "é"}]}}, ensure_ascii=False)
        cases = {
            "complete record kept": (whole + last.encode(), whole.decode() + last),
            "truncated JSON dropped": (whole + last.encode()[:40], whole.decode()),
            "cut mid-multibyte dropped": (whole + last.encode()[:last.encode().index("é".encode()) + 1], whole.decode()),
        }
        for why, (data, want) in cases.items():
            p = self.tmp / f"rollout-{why.split()[0]}.jsonl"; p.write_bytes(data)
            self.assertEqual(read_rollout(p), want, why)
        only = self.tmp / "rollout-only.jsonl"
        only.write_bytes(whole.split(b"\n")[0])                       # the header alone, still being written
        self.assertEqual(json.loads(read_rollout(only))["type"], "session_meta")
        bad = self.tmp / "rollout-bad.jsonl"
        bad.write_bytes(whole + b"{not json\n" + last.encode()[:40])    # a corrupt complete line still raises
        with self.assertRaises(RolloutUnreadable):
            read_rollout(bad)
        badutf = self.tmp / "rollout-badutf.jsonl"
        badutf.write_bytes(whole + b'{"a": "\xff"}\n')
        with self.assertRaises(RolloutUnreadable):
            read_rollout(badutf)

    def test_corrupt_zst_content_raises(self):
        import zstandard
        from swarm.hosts.codex import RolloutUnreadable, read_rollout
        z = self.tmp / "r.jsonl.zst"; z.write_bytes(zstandard.ZstdCompressor().compress(b"{not json\n"))
        with self.assertRaises(RolloutUnreadable):
            read_rollout(z)

    def test_find_rollout_takes_only_uuid_shaped_ids(self):
        from swarm.hosts.codex import find_rollout
        child = F.ids()["child"]["thread"]
        self.assertIsNotNone(find_rollout(child))
        for bad in ("*", "[0-9]*", "*" + child[1:], "00000000-0000-4000-8000-00000000000?", "../x", "", child + "*"):
            self.assertIsNone(find_rollout(bad), bad)
        self.assertIsNone(self.h.find_session_transcript("*"))
        self.assertIsNone(self.h.find_agent_transcript(None, "[0-9]*"))

    def test_corrupt_zst_raises_never_empty(self):
        from swarm.hosts.codex import RolloutUnreadable, read_rollout
        z = self.tmp / "bad.jsonl.zst"; z.write_bytes(b"not zstd at all")
        with self.assertRaises(RolloutUnreadable):
            read_rollout(z)

    def test_no_decompressor_raises_with_reason(self):
        import builtins, zstandard
        from swarm.hosts import codex as cx
        z = self.tmp / "r.jsonl.zst"; z.write_bytes(zstandard.ZstdCompressor().compress(b"{}\n"))
        real_import = builtins.__import__
        def no_zstd(name, *a, **k):
            if name == "zstandard":
                raise ImportError(name)
            return real_import(name, *a, **k)
        with mock.patch("builtins.__import__", no_zstd), mock.patch.object(cx.shutil, "which", return_value=None):
            with self.assertRaises(cx.RolloutUnreadable) as ctx:
                cx.read_rollout(z)
        self.assertIn("zstandard", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
