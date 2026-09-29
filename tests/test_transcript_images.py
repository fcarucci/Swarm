"""Images in transcripts: taken out of the JSONL at capture (transcripts.extract_images), stored
once per sha256 on the board, put back for `show --format jsonl` and written as files by
`export`."""
from __future__ import annotations

import base64
import hashlib
import json
import lzma
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import os  # noqa: E402

from support import FileHarness, MemoryHarness, PostgresHarness, SqliteHarness, fake_image  # noqa: F401  (sets sys.path)

from swarm import transcripts as T  # noqa: E402
from test_transcript_cli import TranscriptEnv  # noqa: E402


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def user_image(data: bytes, mime: str = "image/png", order: str = "mime-first") -> str:
    """A Claude Code user entry with an image block, in one of the key orders seen on disk."""
    src = ({"type": "base64", "media_type": mime, "data": b64(data)} if order == "mime-first"
           else {"type": "base64", "data": b64(data), "media_type": mime})
    e = {"type": "user", "timestamp": "2026-09-26T10:00:00Z",
         "message": {"role": "user", "content": [{"type": "text", "text": "look"},
                                                  {"type": "image", "source": src}]}}
    return json.dumps(e, separators=(",", ":"))


def tool_result_image(data: bytes, mime: str = "image/jpeg") -> str:
    """A Read tool result carrying an image, in the content and in toolUseResult.file."""
    e = {"type": "user", "timestamp": "2026-09-26T10:01:00Z",
         "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": [
             {"type": "image", "source": {"type": "base64", "data": b64(data), "media_type": mime}}]}]},
         "toolUseResult": {"type": "image", "file": {"base64": b64(data), "type": mime}}}
    return json.dumps(e, separators=(",", ":"))


def codex_image(data: bytes, mime: str = "image/png") -> str:
    """A Codex rollout item with an input_image data URL."""
    e = {"timestamp": "2026-09-26T10:02:00Z", "type": "response_item",
         "payload": {"type": "message", "role": "user", "content": [
             {"type": "input_image", "image_url": f"data:{mime};base64,{b64(data)}"}]}}
    return json.dumps(e, separators=(",", ":"))


A, B = fake_image(1), fake_image(2, "jpeg")
SAMPLE = "\n".join([
    user_image(A),
    json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "password=hunter2secret"}]}},
               separators=(",", ":")),
    user_image(A, order="data-first"),
    tool_result_image(B),
]) + "\n"


REDACTED_SAMPLE = SAMPLE.replace("hunter2secret", "[REDACTED:password]")


class ExtractTest(unittest.TestCase):
    def test_two_identical_and_one_different_give_two_images(self):
        text, images = T.extract_images(SAMPLE)
        self.assertEqual(sorted(i.sha256 for i in images),
                         sorted({hashlib.sha256(A).hexdigest(), hashlib.sha256(B).hexdigest()}))
        by = {i.sha256: i for i in images}
        a = by[hashlib.sha256(A).hexdigest()]
        self.assertEqual((a.mime, a.size, a.data), ("image/png", len(A), A))
        self.assertEqual(by[hashlib.sha256(B).hexdigest()].mime, "image/jpeg")
        self.assertNotIn(b64(A)[:100], text)
        self.assertNotIn(b64(B)[:100], text)
        for line in text.splitlines():
            json.loads(line)   # still JSONL
        self.assertEqual(text.count('"type":"swarm-image"'), 4)   # 2x A, B in content and file

    def test_round_trip_is_byte_identical(self):
        for sample in (SAMPLE, codex_image(A) + "\n", user_image(A)):   # the last without a newline
            text, images = T.extract_images(sample)
            lookup = {i.sha256: i.data for i in images}
            self.assertEqual(T.restore_images(text, lookup.get), sample)

    def test_codex_data_url(self):
        text, [img] = T.extract_images(codex_image(A, "image/webp") + "\n")
        self.assertEqual(img.mime, "image/webp")
        self.assertIn('"data_url":true', text)
        self.assertNotIn("base64,", text)

    def test_small_and_non_image_base64_stay_inline(self):
        small = user_image(fake_image(3, n=100))
        blob = json.dumps({"x": b64(b"\x00\x01" * 2000)})
        text, images = T.extract_images(small + "\n" + blob + "\n")
        self.assertEqual((images, text), ([], small + "\n" + blob + "\n"))

    def test_missing_image_restores_as_a_marker(self):
        text, _ = T.extract_images(user_image(A))
        out = T.restore_images(text, lambda sha: None)
        json.loads(out)
        self.assertIn("swarm-image", out)


class RowTest(unittest.TestCase):
    def test_redaction_never_sees_image_data(self):
        seen = []
        real = T.redact

        def spy(text, deadline=None):
            seen.append(text)
            return real(text, deadline)
        with mock.patch.object(T, "redact", spy):
            row = T.make_row("j", "k", "n", "subagent", SAMPLE)
        self.assertTrue(seen)
        self.assertFalse(any(b64(A)[:64] in t or b64(B)[:64] in t for t in seen))
        self.assertNotIn(b"hunter2secret", lzma.decompress(row.body))
        self.assertEqual(len(row.images), 2)
        self.assertEqual(row.raw_bytes, len(lzma.decompress(row.body)))   # text only

    def test_the_text_cap_ignores_images(self):
        big = fake_image(9, n=200_000)
        text = user_image(big) + "\n" + "\n".join(json.dumps({"i": i}) for i in range(20)) + "\n"
        row = T.make_row("j", "k", "n", "subagent", text, max_bytes=50_000)
        self.assertNotIn(T.TRUNCATED_TYPE, lzma.decompress(row.body).decode())   # text is small
        self.assertEqual([i.size for i in row.images], [len(big)])

    def test_images_cut_with_the_text_are_not_kept(self):
        import random
        rnd = random.Random(1)
        noise = [json.dumps({"i": i, "n": "".join(rnd.choice("0123456789abcdef") for _ in range(400))})
                 for i in range(400)]
        text = "\n".join(noise[:200] + [user_image(A)] + noise[200:]) + "\n"   # image mid-way
        row = T.make_row("j", "k", "n", "subagent", text, max_bytes=20_000)
        self.assertIn(T.TRUNCATED_TYPE, lzma.decompress(row.body).decode())
        self.assertEqual(row.images, ())


class CaptureTest(unittest.TestCase):
    def setUp(self):
        self.h = MemoryHarness("transcript-images")
        self.h.reset()
        self.b = self.h.board()
        self.addCleanup(self.b.close)
        self.cfg = self.h.cfg
        self.cfg["transcripts"] = dict(T.DEFAULTS, enabled=True)
        self.dir = Path(tempfile.mkdtemp(prefix="swarm-img-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.dir, ignore_errors=True))

    def test_capture_stores_two_images_with_references(self):
        p = self.dir / "agent-k1.jsonl"
        p.write_text(SAMPLE)
        self.b.open_job("j", None, None, None, "me")
        self.b.allocate_name("k1", "j")
        self.assertTrue(T.capture_subagent(self.b, self.cfg, "j", "k1", p, final=True))
        self.assertEqual(len(self.b.transcript_images()), 2)
        [s] = self.b.transcripts()
        self.assertEqual(s.image_bytes, len(A) + len(B))
        body = self.b.transcript_body("j", "k1").decode()
        self.assertEqual(T.restore_images(body, lambda sha: self.b.transcript_image(sha).data),
                         REDACTED_SAMPLE)


class CliTest(TranscriptEnv):
    def setUp(self):
        super().setUp()
        self.row = self.seed("J1", "k1", "Homer Simpson", SAMPLE)

    def test_show_text_marks_images(self):
        rc, out, _ = self.cli("transcript", "show", "--job", "J1", "--agent", "Homer Simpson")
        self.assertEqual(rc, 0)
        sha = hashlib.sha256(A).hexdigest()
        self.assertIn(f"[image image/png {T.human_size(len(A))} sha256:{sha[:12]}]", out)
        self.assertNotIn(b64(A)[:40], out)

    def test_show_jsonl_restores_the_original_blocks(self):
        rc, out, _ = self.cli("transcript", "show", "--job", "J1", "--agent", "Homer Simpson",
                              "--format", "jsonl")
        self.assertEqual(rc, 0)
        self.assertEqual(out, REDACTED_SAMPLE)

    def test_export_writes_image_files_and_lists_them(self):
        target = self.tmp / "out"
        rc, _, _ = self.cli("transcript", "export", "--job", "J1", str(target))
        self.assertEqual(rc, 0)
        sa, sb = hashlib.sha256(A).hexdigest(), hashlib.sha256(B).hexdigest()
        self.assertEqual((target / "images" / f"{sa}.png").read_bytes(), A)
        self.assertEqual((target / "images" / f"{sb}.jpg").read_bytes(), B)
        header, line = (target / "index.tsv").read_text().splitlines()
        cells = dict(zip(header.split("\t"), line.split("\t")))
        self.assertEqual(sorted(cells["images"].split(",")), sorted([f"images/{sa}.png", f"images/{sb}.jpg"]))
        # the exported JSONL keeps the markers (the files are next to it)
        self.assertIn("swarm-image", (target / cells["file"]).read_text())

    def test_list_and_status_show_images(self):
        rc, out, _ = self.cli("transcript", "list")
        self.assertEqual(rc, 0)
        self.assertIn(f"2 images ({T.human_size(len(A) + len(B))})", out)
        rc, out, _ = self.cli("status")
        self.assertIn(f"2 images ({T.human_size(len(A) + len(B))})", out)


class SaveChecksImagesContract:
    """The board recomputes each image's sha256 on save, so a row can
    never store an image under a key that isn't the digest of its bytes (a key names a file in
    the file backend and in `transcript export`)."""

    def setUp(self):
        self.h = self.harness_factory()
        self.addCleanup(self.h.close)
        self.h.reset()
        self.b = self.h.board()
        self.addCleanup(self.b.close)

    def row(self, *images):
        from swarm.board import TranscriptRow
        return TranscriptRow("j", "k1", "Lisa", "subagent", "h", None, True, 3, 0, "0" * 64,
                             lzma.compress(b"abc"), images=tuple(images))

    def test_save_rejects_mismatched_sha(self):
        from swarm.board import TranscriptImage
        data = fake_image(1)
        good = hashlib.sha256(data).hexdigest()
        for sha in ("../../../.bashrc", "a" * 63, good.upper(), "0" * 64, good + "/x", "g" * 64):
            with self.subTest(sha=sha), self.assertRaises(ValueError):
                self.b.save_transcript(self.row(TranscriptImage(sha, "image/png", len(data), data)))
        self.assertEqual(self.b.transcripts(), [])
        self.assertEqual(self.b.transcript_images(), [])
        self.assertTrue(self.b.save_transcript(self.row(TranscriptImage(good, "image/png", len(data), data))))
        self.assertEqual([i.sha256 for i in self.b.transcript_images()], [good])


class MemorySaveChecksImages(SaveChecksImagesContract, unittest.TestCase):
    harness_factory = staticmethod(lambda: MemoryHarness("save-checks-images"))


class SqliteSaveChecksImages(SaveChecksImagesContract, unittest.TestCase):
    harness_factory = SqliteHarness


class FileSaveChecksImages(SaveChecksImagesContract, unittest.TestCase):
    harness_factory = FileHarness


@unittest.skipUnless(os.environ.get("SWARM_TEST_CONFIG"),
                     "set SWARM_TEST_CONFIG to a throwaway Postgres board config to run")
class PostgresSaveChecksImages(SaveChecksImagesContract, unittest.TestCase):
    harness_factory = staticmethod(lambda: PostgresHarness(os.environ["SWARM_TEST_CONFIG"]))


if __name__ == "__main__":
    unittest.main()
