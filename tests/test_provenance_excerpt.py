"""The excerpt a memory reference keeps: the agent's own transcript, read safely, cut around the
call, redacted, images out, compressed."""
from __future__ import annotations

import base64
import datetime as dt
import json
import lzma
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from support import fake_image  # noqa: F401  (sets sys.path)
import codex_fixtures as CF  # noqa: E402

from swarm import hosts, provenance, transcript_view, transcripts  # noqa: E402
from swarm.provenance import MemoryWrite  # noqa: E402

AT = dt.datetime(2026, 9, 28, 12, 0, tzinfo=dt.timezone.utc)
WRITES = (MemoryWrite("note-tool", "notes", ("tool-batch-x",)),)
SECRET_KEY = "sk-ant-api03-" + "Q" * 40


def line(kind, content, ts):
    return json.dumps({"type": kind, "timestamp": ts, "message": {"role": kind, "content": content}}) + "\n"


def claude_transcript(before=40, call_id="toolu_mem", after=0, extra=""):
    out = []
    for i in range(before):
        ts = f"2026-09-28T11:{i % 60:02d}:00Z"
        out.append(line("user", f"question {i}", ts))
        out.append(line("assistant", [{"type": "text", "text": f"answer {i}"}], ts))
    out.append(extra)
    out.append(line("assistant", [{"type": "tool_use", "id": call_id, "name": "Bash",
                                   "input": {"command": "note-tool save --batch items"}}], "2026-09-28T11:59:30Z"))
    for i in range(after):
        out.append(line("assistant", [{"type": "text", "text": f"later {i}"}], "2026-09-28T11:59:40Z"))
    return "".join(out)


class ExcerptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="swarm-prov-", dir=os.environ.get("TMPDIR")))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.s = provenance.settings({})

    def write(self, text: str, name="t.jsonl") -> Path:
        p = self.tmp / name
        p.write_text(text)
        return p

    def text_of(self, ex) -> str:
        return lzma.decompress(ex.body).decode()

    def test_claude_excerpt_ends_at_the_call_with_about_20_turns(self):
        ex = provenance.make_excerpt(self.write(claude_transcript()), "claude", "toolu_mem", WRITES,
                                     "saved tool-batch-x to notes\n", AT, None, self.s)
        items = transcript_view.turns(self.text_of(ex))
        self.assertEqual(items[-1].kind, "memory saved")
        self.assertIn("tool-batch-x", items[-1].text)
        self.assertEqual(items[-2].kind, "tool call")
        self.assertTrue(20 <= len(items) - 1 <= 22, len(items))
        self.assertNotIn("question 20", self.text_of(ex))
        self.assertIn("answer 39", self.text_of(ex))
        self.assertEqual(ex.turns, len(items))

    def test_lines_already_written_after_the_call_are_kept_up_to_four(self):
        ex = provenance.make_excerpt(self.write(claude_transcript(after=10)), "claude", "toolu_mem", WRITES, "",
                                     AT, None, self.s)
        text = self.text_of(ex)
        self.assertIn("later 3", text)
        self.assertNotIn("later 4", text)

    def test_codex_anchor_by_time_in_code_mode(self):
        roll = self.write(CF.rollout("child").read_text(), "rollout.jsonl")
        at = dt.datetime(2026, 9, 27, 0, 32, 40, 300000, tzinfo=dt.timezone.utc)
        ex = provenance.make_excerpt(roll, "codex", "call_synthetic_009", WRITES, "retained x", at, None, self.s)
        text = self.text_of(ex)
        self.assertIn("call_synthetic_010", text)          # the exec call around the Bash call (R4)
        self.assertNotIn("call_synthetic_012", text)       # written after the hook time
        self.assertEqual(transcript_view.turns(text)[-1].kind, "memory saved")

    def test_secrets_redacted_in_transcript_and_output(self):
        extra = line("user", f"PGPASSWORD=hunter2hunter2 and {SECRET_KEY}", "2026-09-28T11:59:00Z")
        ex = provenance.make_excerpt(self.write(claude_transcript(extra=extra)), "claude", "toolu_mem", WRITES,
                                     "Authorization: Bearer abcdefgh12345678\n", AT, None, self.s)
        text = self.text_of(ex)
        for secret in ("hunter2hunter2", SECRET_KEY, "abcdefgh12345678"):
            self.assertNotIn(secret, text)
        self.assertGreaterEqual(ex.redactions, 3)

    def test_images_come_out_before_redaction_and_are_kept(self):
        png = fake_image(3)
        extra = line("user", [{"type": "tool_result", "tool_use_id": "t0", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                         "data": base64.b64encode(png).decode()}}]}], "2026-09-28T11:59:10Z")
        ex = provenance.make_excerpt(self.write(claude_transcript(extra=extra)), "claude", "toolu_mem", WRITES, "",
                                     AT, None, self.s)
        self.assertEqual([i.data for i in ex.images], [png])
        self.assertIn("swarm-image", self.text_of(ex))

    def test_image_budget_drops_images_but_keeps_placeholders(self):
        png = fake_image(4)
        extra = line("user", [{"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                                           "data": base64.b64encode(png).decode()}}], "2026-09-28T11:59:10Z")
        s = {**self.s, "excerpt_image_mb": 0}
        ex = provenance.make_excerpt(self.write(claude_transcript(extra=extra)), "claude", "toolu_mem", WRITES, "",
                                     AT, None, s)
        self.assertEqual(ex.images, ())
        self.assertIn("swarm-image", self.text_of(ex))

    def test_compressed_size_cap_keeps_the_anchor(self):
        noise = "".join(line("user", os.urandom(3000).hex(), "2026-09-28T11:58:00Z") for _ in range(30))
        s = {**self.s, "excerpt_max_kb": 4}
        ex = provenance.make_excerpt(self.write(claude_transcript(extra=noise)), "claude", "toolu_mem", WRITES, "",
                                     AT, None, s)
        self.assertLessEqual(len(ex.body), 4 * 1024 + 512)   # or only the anchor is left
        self.assertEqual(transcript_view.turns(self.text_of(ex))[-1].kind, "memory saved")

    def test_read_tail_refuses_symlink_and_hard_link(self):
        real = self.write("x\n", "real.jsonl")
        (self.tmp / "sym.jsonl").symlink_to(real)
        with self.assertRaises(provenance.UnsafeTranscript):
            provenance.read_tail(self.tmp / "sym.jsonl", 1024)
        os.link(real, self.tmp / "hard.jsonl")
        with self.assertRaises(provenance.UnsafeTranscript):
            provenance.read_tail(self.tmp / "hard.jsonl", 1024)

    def test_read_tail_reads_only_the_tail(self):
        big = self.tmp / "big.jsonl"
        with open(big, "w") as fh:
            for i in range(300_000):
                fh.write(json.dumps({"type": "user", "n": i, "pad": "x" * 80}) + "\n")
        t0 = time.monotonic()
        data = provenance.read_tail(big, 1 << 20)
        self.assertLess(time.monotonic() - t0, 1.0)
        self.assertLessEqual(len(data), 1 << 20)
        self.assertTrue(data.startswith(b'{"type"'))
        self.assertIn(b'"n": 299999', data)

    def test_out_of_time_raises(self):
        with self.assertRaises(transcripts.OutOfTime):
            provenance.make_excerpt(self.write(claude_transcript()), "claude", "toolu_mem", WRITES, "", AT,
                                    time.monotonic() - 1, self.s)

    def test_compressed_rollout_is_refused(self):
        with self.assertRaises(provenance.UnsafeTranscript):
            provenance.make_excerpt(self.write("x", "rollout-x.jsonl.zst"), "codex", None, WRITES, "", AT, None, self.s)


def fake_pem() -> tuple[str, list[str]]:
    """A PEM-shaped RSA key (random base64, not a key) and its body lines."""
    body = [base64.b64encode(os.urandom(48)).decode() for _ in range(25)]
    return "-----BEGIN RSA PRIVATE KEY-----\n" + "\n".join(body) + "\n-----END RSA PRIVATE KEY-----\n", body


class ExcerptFixTests(unittest.TestCase):
    """Redact before any cut, the raw-size cap, anchors never after
    the hook time, naive timestamps, and more planted files."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="swarm-prov-", dir=os.environ.get("TMPDIR")))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.s = provenance.settings({})

    def write(self, text: str, name="t.jsonl") -> Path:
        p = self.tmp / name
        p.write_text(text)
        return p

    def text_of(self, ex) -> str:
        return lzma.decompress(ex.body).decode()

    def test_a_key_cut_by_the_output_limit_is_still_redacted(self):
        pem, body = fake_pem()
        out = "saved tool-note-1 to notes\n" + pem + "".join(f"log line {i:04d} ok\n" for i in range(176))
        cut = len(out) - provenance.OUTPUT_CHARS                 # where a cut before redaction falls:
        self.assertTrue(out.index("BEGIN RSA") < cut < out.index("END RSA"))   # inside the key body
        ex = provenance.make_excerpt(self.write(claude_transcript()), "claude", "toolu_mem", WRITES, out,
                                     AT, None, self.s)
        text = self.text_of(ex)
        for b in body:
            for part in (b[:16], b[16:32], b[-16:]):
                self.assertNotIn(part, text)
        self.assertIn("log line 0175 ok", text)
        self.assertGreaterEqual(ex.redactions, 1)

    def test_output_is_cut_at_a_line_boundary(self):
        out = "".join(f"row {i:05d} " + "y" * 30 + "\n" for i in range(400))
        line_ = provenance.anchor_line(WRITES, "claude", "t", AT, out)
        kept = json.loads(line_)["output"]
        rows = [r for r in kept.splitlines() if r.startswith("row ")]
        self.assertTrue(all(len(r) == len(rows[-1]) for r in rows), "a partial first line was kept")
        self.assertLessEqual(len(kept), provenance.OUTPUT_CHARS + 100)

    def test_raw_size_is_capped_and_one_huge_line_becomes_a_marker(self):
        huge = line("user", "z" * (6 * 1024 * 1024), "2026-09-28T11:59:20Z")   # compresses to almost nothing
        ex = provenance.make_excerpt(self.write(claude_transcript(extra=huge)), "claude", "toolu_mem", WRITES,
                                     "retained x\n", AT, None, {**self.s, "tail_mb": 16})
        self.assertLessEqual(ex.raw_bytes, provenance.EXCERPT_MAX_RAW)
        text = self.text_of(ex)
        self.assertLessEqual(len(text.encode()), provenance.EXCERPT_MAX_RAW)
        kinds = [t.kind for t in transcript_view.turns(text)]
        self.assertEqual(kinds[-1], "memory saved")
        self.assertIn("truncated", kinds)
        self.assertIn("answer 39", text)                    # the context around it survives

    def test_raw_cap_matches_the_board(self):
        from swarm import board
        if not hasattr(board, "EXCERPT_MAX_RAW"):
            self.skipTest("board.EXCERPT_MAX_RAW is not there yet")
        self.assertEqual(provenance.EXCERPT_MAX_RAW, board.EXCERPT_MAX_RAW)

    def test_claude_anchor_without_the_call_never_takes_later_lines(self):
        later = "".join(line("assistant", [{"type": "text", "text": f"after the hook {i}"}], "2026-09-28T12:30:00Z")
                        for i in range(3))
        ex = provenance.make_excerpt(self.write(claude_transcript() + later), "claude", "toolu_missing", WRITES,
                                     "", AT, None, self.s)
        text = self.text_of(ex)
        self.assertNotIn("after the hook", text)
        self.assertIn("note-tool save", text)            # the last line at or before the hook time

    def test_naive_timestamps_are_utc(self):
        t = claude_transcript(after=2).replace("11:59:40Z", "12:10:00").replace("11:59:30Z", "11:59:30")
        ex = provenance.make_excerpt(self.write(t), "claude", "toolu_mem", WRITES, "", AT, None, self.s)
        self.assertNotIn("later 0", self.text_of(ex))
        items = transcript_view.turns(t)
        naive_at = AT.replace(tzinfo=None)
        self.assertEqual(items[transcript_view.anchor_index(items, None, naive_at)].kind, "tool call")

    def test_read_tail_refuses_a_symlinked_directory(self):
        real = self.tmp / "real"
        real.mkdir()
        (real / "t.jsonl").write_text("x\n")
        (self.tmp / "linked").symlink_to(real)
        with self.assertRaises(provenance.UnsafeTranscript):
            provenance.read_tail(self.tmp / "linked" / "t.jsonl", 1024)

    def test_read_tail_refuses_a_fifo_without_blocking(self):
        os.mkfifo(self.tmp / "fifo.jsonl")
        t0 = time.monotonic()
        with self.assertRaises(provenance.UnsafeTranscript):
            provenance.read_tail(self.tmp / "fifo.jsonl", 1024)
        self.assertLess(time.monotonic() - t0, 1.0)


class OwnTranscriptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="swarm-own-", dir=os.environ.get("TMPDIR")))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_claude_subagent_transcript_inside_the_projects_dir_only(self):
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.tmp / "cc")}):
            proj = self.tmp / "cc" / "projects" / "-work"
            sid = "00000000-0000-4000-9000-000000000001"
            (proj / sid / "subagents").mkdir(parents=True)
            (proj / sid / "subagents" / "agent-a1.jsonl").write_text("{}\n")
            host = hosts.get("claude")
            got = host.own_transcript({"agent_id": "a1", "session_id": sid, "transcript_path": str(proj / f"{sid}.jsonl")})
            self.assertEqual(got, (proj / sid / "subagents" / "agent-a1.jsonl").resolve())
            self.assertIsNone(host.own_transcript({"agent_id": "a1", "session_id": sid,
                                                   "transcript_path": str(self.tmp / "elsewhere" / f"{sid}.jsonl")}))

    def test_claude_needs_a_uuid_session_and_its_own_session_dir(self):
        s1, s2 = "00000000-0000-4000-9000-00000000000a", "00000000-0000-4000-9000-00000000000b"
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.tmp / "cc")}):
            proj = self.tmp / "cc" / "projects" / "-work"
            for sid in (s1, s2):
                (proj / sid / "subagents").mkdir(parents=True)
            (proj / s2 / "subagents" / "agent-a2.jsonl").write_text("{}\n")
            (proj / s1 / "subagents" / "agent-a1.jsonl").write_text("{}\n")
            host = hosts.get("claude")
            ok = {"agent_id": "a1", "session_id": s1, "transcript_path": str(proj / f"{s1}.jsonl")}
            self.assertIsNotNone(host.own_transcript(ok))
            self.assertIsNone(host.own_transcript({**ok, "session_id": "s1", "transcript_path": str(proj / "s1.jsonl")}))
            self.assertIsNone(host.own_transcript({**ok, "session_id": "../x"}))
            # agent-a1.jsonl planted as a symlink to another session's transcript
            (proj / s1 / "subagents" / "agent-a1.jsonl").unlink()
            (proj / s1 / "subagents" / "agent-a1.jsonl").symlink_to(proj / s2 / "subagents" / "agent-a2.jsonl")
            self.assertIsNone(host.own_transcript(ok))

    def test_codex_rollout_of_this_thread_only_and_no_glob(self):
        home = CF.staged(self.tmp)
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(home)}):
            ids = CF.ids()
            child = ids["child"]["thread"]
            roll = next(home.glob(f"sessions/*/*/*/rollout-*-{child}.jsonl"))
            host = hosts.get("codex")
            p = {"agent_id": child, "session_id": ids["root"]["session"], "transcript_path": str(roll)}
            self.assertEqual(host.own_transcript(p), roll.resolve())
            self.assertIsNone(host.own_transcript({**p, "agent_id": ids["grandchild"]["thread"]}))
            with mock.patch("swarm.hosts.codex.find_rollout", side_effect=AssertionError("globbed")):
                self.assertIsNone(host.own_transcript({"agent_id": child, "session_id": p["session_id"]}))


if __name__ == "__main__":
    unittest.main()
