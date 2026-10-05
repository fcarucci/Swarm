"""Deadline checkpoints on adversarial inputs, driven by an injected monotonic clock.
Runtime and scheduler speed do not determine whether these tests pass."""
from __future__ import annotations

import base64
import datetime as dt
import json
import lzma
import random
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from support import ManualClock, MemoryHarness  # noqa: F401  (sets sys.path)

from swarm import hooks, provenance  # noqa: E402
from swarm import transcripts as T  # noqa: E402
from swarm.provenance import MemoryWrite  # noqa: E402

MARGIN = 1.5          # configured budget margin (not measured test runtime)
HOOK_TIMEOUT = 10.0   # the hosts' hook timeout


def _b64(rng: random.Random, n: int) -> str:
    return base64.b64encode(rng.randbytes(n * 3 // 4 + 3)).decode()[:n]


def _tool_line(content: str) -> str:
    return json.dumps({"type": "user", "message": {"content": [{"type": "tool_result", "content": content}]}}) + "\n"


def adversarial(shape: str, total: int = 24_000_000) -> str:
    """The adversarial shapes: ~2000 JSONL lines of ~12 KB each, every one expensive."""
    rng = random.Random(1)
    if shape == "key-runs":        # every string packed with 2-line 70-wide runs
        one = _tool_line("\n".join(_b64(rng, 70) + "\n" + _b64(rng, 70) + "\nok" for _ in range(80)))
    elif shape == "key-hint":      # a key-like name before a long base64 value on every line
        one = _tool_line("api_key " + _b64(rng, 11500))
    else:                          # "end-lines": END lines with no body
        one = _tool_line("\n".join("-----END RSA PRIVATE KEY-----" for _ in range(400)))
    return one * (total // len(one) + 1)


class ClockTest(unittest.TestCase):
    def setUp(self):
        self.clock = ManualClock(step=0.05)
        patch = mock.patch("time.monotonic", self.clock)
        patch.start()
        self.addCleanup(patch.stop)


class RedactDeadlineTest(ClockTest):
    def test_redact_stops_near_its_deadline(self):
        for shape in ("key-runs", "key-hint", "end-lines"):
            text = adversarial(shape)
            with self.subTest(shape=shape):
                t0 = time.monotonic()
                with self.assertRaises(T.OutOfTime):
                    T.redact(text, t0 + 0.5)
                self.assertGreater(self.clock.calls, 1)

    def test_one_huge_line_is_checked_too(self):
        rng = random.Random(2)
        text = _tool_line("\n".join("api_key " + _b64(rng, 200) for _ in range(60000)))   # one 12 MB line
        t0 = time.monotonic()
        with self.assertRaises(T.OutOfTime):
            T.redact(text, t0 + 0.2)
        self.assertGreater(self.clock.calls, 1)


class WalkerBudgetTest(ClockTest):
    """BEGIN-looking lines can't make the key walker quadratic."""
    HDR = "X: -----BEGIN RSA PRIVATE KEY-----"

    def test_begin_header_lines_finish_or_stop_near_the_deadline(self):
        s = "\n".join(self.HDR for _ in range(300 * 1024 // len(self.HDR)))
        for form, text in (("raw", s), ("jsonl", _tool_line(s))):
            with self.subTest(form=form):
                t0 = time.monotonic()
                with self.assertRaises(T.OutOfTime):
                    T.redact(text, t0 + 2.0)
                self.assertGreater(self.clock.calls, 1)

    def test_key_lines_across_json_strings_stop_near_the_deadline(self):
        # R-redact 2: one huge line of quoted key lines, and of 1-line text blocks
        rng = random.Random(3)
        keys = [_b64(rng, 64) for _ in range(50)]
        for form, text in (("array", json.dumps({"l": keys * 4000}) + "\n"),
                           ("blocks", json.dumps({"c": [{"type": "text", "text": k} for k in keys * 3000]}) + "\n")):
            with self.subTest(form=form):
                t0 = time.monotonic()
                with self.assertRaises(T.OutOfTime):
                    T.redact(text, t0 + 0.5)
                self.assertGreater(self.clock.calls, 1)

    def test_anchor_on_64_kb_of_them_keeps_its_budget(self):
        self.clock.step = 0
        out = "\n".join(self.HDR for _ in range(64 * 1024 // len(self.HDR)))
        t0 = time.monotonic()
        anchor, count = provenance._anchor([], "claude", "c1", dt.datetime.now(dt.timezone.utc), out,
                           t0 + hooks.PROVENANCE_BUDGET_SECONDS)
        self.assertGreater(self.clock.calls, 1)
        self.assertEqual(json.loads(anchor)["type"], provenance.EXCERPT_TYPE)


class CaptureBudgetTest(ClockTest):
    def setUp(self):
        super().setUp()
        self.h = MemoryHarness("redact-budget")
        self.h.reset()
        self.b = self.h.board()
        self.addCleanup(self.b.close)
        self.cfg = self.h.cfg
        self.cfg["transcripts"] = dict(T.DEFAULTS, enabled=True)
        self.dir = Path(tempfile.mkdtemp(prefix="swarm-budget-"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.addCleanup(shutil.rmtree, self.dir, True)

    def test_capture_respects_the_hook_budget(self):
        self.assertLess(hooks.TRANSCRIPT_BUDGET_SECONDS + MARGIN, HOOK_TIMEOUT)
        self.b.open_job("j", None, None, None, "me")
        self.b.allocate_name("k1", "j")
        for shape in ("key-runs", "key-hint", "end-lines"):
            p = self.dir / f"agent-{shape}.jsonl"
            p.write_text(adversarial(shape))
            with self.subTest(shape=shape):
                t0 = time.monotonic()
                with self.assertRaises(T.OutOfTime):
                    T.capture_subagent(self.b, self.cfg, "j", "k1", p, final=True,
                                       deadline=t0 + hooks.TRANSCRIPT_BUDGET_SECONDS)
                self.assertGreater(self.clock.calls, 1)


class ExcerptBudgetTest(ClockTest):
    AT = dt.datetime(2026, 9, 28, 12, 0, tzinfo=dt.timezone.utc)
    WRITES = (MemoryWrite("note-tool", "notes", ("tool-batch-x",)),)

    def setUp(self):
        super().setUp()
        self.dir = Path(tempfile.mkdtemp(prefix="swarm-budget-"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.addCleanup(shutil.rmtree, self.dir, True)

    def transcript(self) -> Path:
        """~8 MB (the tail read) of 400 KB user turns packed with 70-wide pairs, then the call."""
        rng = random.Random(3)
        big = "\n".join(_b64(rng, 70) + "\n" + _b64(rng, 70) + "\nok" for _ in range(2700))
        out = [json.dumps({"type": "user", "timestamp": f"2026-09-28T11:{i:02d}:00Z",
                           "message": {"role": "user", "content": big}}) + "\n" for i in range(20)]
        out.append(json.dumps({"type": "assistant", "timestamp": "2026-09-28T11:59:30Z", "message": {
            "role": "assistant", "content": [{"type": "tool_use", "id": "toolu_mem", "name": "Bash",
                                              "input": {"command": "note-tool save --batch items"}}]}}) + "\n")
        p = self.dir / "t.jsonl"
        p.write_text("".join(out))
        return p

    def test_excerpt_degrades_to_a_smaller_one_within_budget(self):
        p = self.transcript()
        t0 = time.monotonic()
        ex = provenance.make_excerpt(p, "claude", "toolu_mem", self.WRITES, "retained 1 item", self.AT,
                                     t0 + hooks.PROVENANCE_BUDGET_SECONDS, provenance.settings({}))
        self.assertGreater(self.clock.calls, 1)
        text = lzma.decompress(ex.body).decode()
        last = json.loads(text.splitlines()[-1])
        self.assertEqual(last["type"], provenance.EXCERPT_TYPE)             # the ref keeps its anchor
        self.assertEqual(last["memories"][0]["document_id"], "tool-batch-x")

    def test_a_small_excerpt_is_not_degraded(self):
        self.clock.step = 0
        lines = [json.dumps({"type": "user", "timestamp": f"2026-09-28T11:{i:02d}:00Z",
                             "message": {"role": "user", "content": f"question {i}"}}) + "\n" for i in range(30)]
        p = self.dir / "small.jsonl"
        p.write_text("".join(lines))
        ex = provenance.make_excerpt(p, "claude", None, self.WRITES, "", self.AT,
                                     time.monotonic() + hooks.PROVENANCE_BUDGET_SECONDS, provenance.settings({}))
        self.assertIn("question 29", lzma.decompress(ex.body).decode())


if __name__ == "__main__":
    unittest.main()
