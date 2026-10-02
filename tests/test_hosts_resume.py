"""hosts.resume: re-creating a paused agent from the transcript stored on the board."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

from support import MemoryHarness  # noqa: F401  (sets sys.path)

from swarm import hosts, transcripts as T  # noqa: E402
from swarm.hosts import resume as R  # noqa: E402
from swarm.hosts.base import ResumeUnsupported  # noqa: E402
from swarm.supervisor.launch import UnsafeLaunch, check_safe  # noqa: E402

SECRET = "sk-ant-api03-AbCdEfGhIjKlMnOpQrStUv_wx-yz0123"
SID = "11111111-2222-3333-4444-555555555555"


def claude_lines(sidechain: bool = False, dangling_tool: bool = False) -> str:
    base = {"sessionId": SID, "cwd": "/old/box/work", "isSidechain": sidechain, "userType": "external"}
    if sidechain:
        base["agentId"] = "abc123"
    rows = [
        {**base, "type": "user", "uuid": "u1", "parentUuid": None,
         "message": {"role": "user", "content": f"fix it, key {SECRET}"}},
        {**base, "type": "assistant", "uuid": "a1", "parentUuid": "u1",
         "message": {"role": "assistant", "content": [{"type": "text", "text": "on it"}]}},
    ]
    if dangling_tool:
        rows.append({**base, "type": "assistant", "uuid": "a2", "parentUuid": "a1",
                     "message": {"role": "assistant", "content": [
                         {"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {"command": "ls"}}]}})
    return "\n".join(json.dumps(r) for r in rows) + "\n"


CODEX = "\n".join(json.dumps(r) for r in [
    {"timestamp": "2026-10-01T10:00:00Z", "type": "session_meta", "ordinal": 0,
     "payload": {"id": SID, "session_id": SID, "cwd": "/old", "source": {"subagent": {"depth": 1}},
                 "forked_from_id": "zzz"}},
    {"timestamp": "2026-10-01T10:00:01Z", "type": "response_item", "ordinal": 1,
     "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "do it"}]}},
    {"timestamp": "2026-10-01T10:00:02Z", "type": "session_meta", "ordinal": 2, "payload": {"id": "parent"}},
]) + "\n"


class ResumeBase(unittest.TestCase):
    def setUp(self):
        self.h = MemoryHarness("hosts-resume")
        self.h.reset()
        self.b = self.h.board()
        self.addCleanup(self.b.close)
        self.tmp = Path(tempfile.mkdtemp(prefix="swarm-res-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.work = self.tmp / "work"
        self.work.mkdir()
        self.root = self.tmp / "claude-home"

    def store(self, text, key="k1", name="Homer", role="subagent", harness="claude"):
        self.b.save_transcript(T.make_row("job1", key, name, role, text, harness=harness))

    def manifest(self, **kw):
        m = {"job": "job1", "agent_key": "k1", "agent_name": "Homer", "role": "worker", "harness": "claude",
             "model": "sonnet", "cursor": 41, "last_tool": "Bash", "task": "fix the build",
             "paused_at": "2026-10-02T06:00:00Z", "reason": "moving box", "cwd": None}
        m.update(kw)
        return m

    def restore(self, host="claude", **kw):
        kw.setdefault("workdir", str(self.work))
        kw.setdefault("host_label", "box-b")
        return R.restore(self.b, self.manifest(**kw.pop("m", {})), host, self.root, cfg={}, **kw)


class ClaudeResumeTest(ResumeBase):
    def test_restored_from_the_board_not_from_disk(self):
        self.store(claude_lines())
        # nothing about this agent exists on local disk: only the DB has it
        self.assertFalse(list(self.tmp.rglob("*.jsonl")))
        r = self.restore()
        self.assertEqual((r.mode, r.harness), ("resume", "claude"))
        self.assertTrue(hosts.base.valid_session_id(r.session_id))
        from swarm.hosts.claude import project_dir_name
        want = self.root / "projects" / project_dir_name(str(self.work.resolve())) / f"{r.session_id}.jsonl"
        self.assertEqual(r.path, want)
        self.assertTrue(want.is_file())
        self.assertEqual(oct(want.stat().st_mode & 0o777), "0o600")
        self.assertEqual(r.argv[:4], ("claude", "-p", "--resume", r.session_id))
        self.assertIn("box-b", r.stdin)
        self.assertIn("paused", r.stdin)

    def test_session_is_rewritten_for_this_box(self):
        self.store(claude_lines(sidechain=True))
        r = self.restore()
        rows = [json.loads(x) for x in r.path.read_text().splitlines()]
        for e in rows:
            self.assertEqual(e["sessionId"], r.session_id)
            self.assertEqual(e["cwd"], str(self.work.resolve()))
            self.assertFalse(e["isSidechain"])
            self.assertNotIn("agentId", e)

    def test_entries_get_a_timestamp(self):
        # Claude Code refuses ("No conversation found") a session whose entries have none
        self.store(claude_lines())
        for line in self.restore().path.read_text().splitlines():
            self.assertTrue(json.loads(line)["timestamp"])

    def test_redacted_secret_never_comes_back(self):
        self.store(claude_lines())
        r = self.restore()
        self.assertNotIn("AbCdEfGhIjKlMnOp", r.path.read_text())
        self.assertIn("[REDACTED", r.path.read_text())
        self.assertNotIn("AbCdEfGhIjKlMnOp", r.stdin)

    def test_dangling_tool_call_is_closed(self):
        self.store(claude_lines(dangling_tool=True))
        r = self.restore()
        last = json.loads(r.path.read_text().splitlines()[-1])
        self.assertEqual(last["type"], "user")
        block = last["message"]["content"][0]
        self.assertEqual((block["type"], block["tool_use_id"], block["is_error"]), ("tool_result", "toolu_1", True))
        self.assertEqual(last["parentUuid"], "a2")

    def test_truncated_copy_resumes_with_a_gap_noted(self):
        body = claude_lines().splitlines()
        text = "\n".join([body[0], json.dumps({"type": "swarm-truncated", "bytes": 5}),
                          json.dumps({**json.loads(body[1]), "parentUuid": "gone"})]) + "\n"
        self.store(text)
        r = self.restore()
        self.assertTrue(r.truncated)
        self.assertIn("missing", r.stdin)
        rows = [json.loads(x) for x in r.path.read_text().splitlines()]
        self.assertTrue(all(e["type"] != "swarm-truncated" for e in rows))
        self.assertIsNone(rows[-1]["parentUuid"])

    def test_never_overwrites(self):
        self.store(claude_lines())
        r = self.restore()
        with self.assertRaises(FileExistsError):
            hosts.get("claude").write_session("{}", session_id=r.session_id, cwd=str(self.work.resolve()),
                                               root=self.root)

    def test_images_come_back(self):
        png = b"\x89PNG\r\n\x1a\n" + b"x" * 40
        import base64
        b64 = base64.b64encode(png).decode()
        e = {"type": "user", "uuid": "u1", "parentUuid": None, "sessionId": SID,
             "message": {"role": "user", "content": [{"type": "image", "source": {"type": "base64",
                         "media_type": "image/png", "data": b64}}]}}
        self.store(json.dumps(e) + "\n")
        r = self.restore()
        self.assertIn(b64, r.path.read_text())

    def test_model_comes_from_config_or_manifest(self):
        self.store(claude_lines())
        r = self.restore()
        self.assertEqual(r.argv[r.argv.index("--model") + 1], "sonnet")

    def test_bad_manifest_model_is_ignored(self):
        self.store(claude_lines())
        r = self.restore(m={"model": "--dangerously-skip-permissions"})
        self.assertNotIn("--dangerously-skip-permissions", r.argv)

    def test_argv_passes_check_safe(self):
        self.store(claude_lines())
        check_safe(self.restore().argv)
        with self.assertRaises(UnsafeLaunch):
            check_safe(("claude", "--dangerously-skip-permissions"))

    def test_write_false_writes_nothing(self):
        self.store(claude_lines())
        r = self.restore(write=False)
        self.assertIsNone(r.path)
        self.assertFalse(list(self.tmp.rglob("*.jsonl")))


class FallbackTest(ResumeBase):
    def test_no_stored_transcript_is_unsupported(self):
        with self.assertRaises(ResumeUnsupported):
            self.restore()

    def test_bodiless_capture_failed_row_is_unsupported(self):
        row = T.capture_failed_row("job1", "k1", "Homer", "too big")
        self.b.mark_capture_failed("job1", "k1", "too big", row)
        with self.assertRaises(ResumeUnsupported):
            self.restore()

    def test_cross_harness_gets_a_briefing(self):
        self.store(claude_lines())
        r = self.restore("codex")
        self.assertEqual((r.mode, r.harness, r.session_id, r.path), ("briefing", "codex", None, None))
        self.assertIn("exec", r.argv)
        self.assertIn("on it", r.stdin)            # the recap
        self.assertIn("claude", " ".join(r.notes))
        self.assertFalse(list(self.tmp.rglob("*.jsonl")))

    def test_codex_defaults_to_briefing(self):
        self.store(CODEX, harness="codex")
        r = self.restore("codex", m={"harness": "codex"})
        self.assertEqual(r.mode, "briefing")
        self.assertIn("do it", r.stdin)
        self.assertIn("experimental", " ".join(r.notes))

    def test_unusable_transcript_falls_back(self):
        self.store('{"type":"user","message":{"role":"user","content":"hi"}}\n', harness="codex")
        r = self.restore("codex", m={"harness": "codex"}, native="codex")
        self.assertEqual(r.mode, "briefing")

    def test_unknown_host(self):
        self.store(claude_lines())
        with self.assertRaises(ResumeUnsupported):
            self.restore("nope")

    def test_missing_workdir_refused(self):
        self.store(claude_lines())
        with self.assertRaises(ResumeUnsupported):
            self.restore(workdir=str(self.tmp / "absent"))

    def test_note_cleans_board_text(self):
        n = R.resume_note(self.manifest(agent_name="Ho\x1b[31mmer", task=f"use {SECRET}"), "b\x07ox")
        self.assertNotIn("\x1b", n)
        self.assertNotIn("\x07", n)
        self.assertNotIn("AbCdEfGhIjKlMnOp", n)


class CodexResumeTest(ResumeBase):
    def test_native_rollout_written_when_asked(self):
        self.store(CODEX, harness="codex")
        home = self.tmp / "codex-home"
        r = R.restore(self.b, self.manifest(harness="codex"), "codex", home, cfg={}, native="codex",
                      workdir=str(self.work), host_label="box-b")
        self.assertEqual(r.mode, "resume")
        self.assertEqual(r.path.parent.parent.parent.parent, home / "sessions")
        self.assertTrue(r.path.name.startswith("rollout-") and r.path.name.endswith(f"-{r.session_id}.jsonl"))
        rows = [json.loads(x) for x in r.path.read_text().splitlines()]
        self.assertEqual(len(rows), 2)                       # the parent's repeated meta is dropped
        pl = rows[0]["payload"]
        self.assertEqual((pl["id"], pl["session_id"], pl["cwd"], pl["source"]),
                         (r.session_id, r.session_id, str(self.work.resolve()), "exec"))
        self.assertNotIn("forked_from_id", pl)
        self.assertEqual(r.argv[:3], ("codex", "exec", "resume"))
        self.assertEqual(r.argv[-2:], (r.session_id, "-"))
        check_safe(r.argv)


class StartTest(ResumeBase):
    def test_start_runs_checked_argv_with_prompt_on_stdin(self):
        self.store(claude_lines())
        r = self.restore()
        proc = mock.MagicMock()
        popen = mock.MagicMock(return_value=proc)
        R.start(r, popen=popen)
        args, kw = popen.call_args
        self.assertEqual(args[0], list(r.argv))
        self.assertEqual(kw["cwd"], r.cwd)
        proc.stdin.write.assert_called_once_with(r.stdin)

    def test_start_refuses_a_bypass(self):
        bad = R.Restored("claude", "resume", ("claude", "--dangerously-skip-permissions"), "/", "x", None, None)
        with self.assertRaises(UnsafeLaunch):
            R.start(bad, popen=mock.MagicMock())


if __name__ == "__main__":
    unittest.main()
