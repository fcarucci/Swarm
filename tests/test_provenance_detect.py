"""Which shell calls wrote a memory, and which document ids they wrote (swarm.provenance.detect)."""
from __future__ import annotations

import unittest

import provenance_fixtures as PF
from support import ROOT  # noqa: F401  (sets sys.path)

from swarm import provenance as P  # noqa: E402
from swarm.provenance import MemoryWrite  # noqa: E402


W = P.configured_writers(PF.NOTE_CFG)


class HintTests(unittest.TestCase):
    def test_hint_matches_every_writer_and_nothing_common(self):
        for cmd in (PF.NOTE_MULTI_CMD, PF.NOTE_TOOL_CMD, PF.SWARM_REMEMBER_CMD):
            self.assertTrue(P.hint(cmd, W), cmd)
        for cmd in ("ls -la", "git status", "pytest -q", "echo retained x", "", None):
            self.assertFalse(P.hint(cmd, W), cmd)


class DetectTests(unittest.TestCase):
    def test_configured_writer_reads_every_document_line(self):
        d = P.detect(PF.NOTE_MULTI_CMD, PF.NOTE_MULTI_OUT, W)
        self.assertEqual(d.writes, (MemoryWrite("note-tool", "notes", ("tool-batch-1", "tool-batch-2")),))
        self.assertEqual(d.problems, ())

    def test_configured_writer_failure_is_nothing(self):
        self.assertEqual(P.detect(PF.NOTE_MULTI_CMD, "HTTP 422: bad\n", W), P.Detection((), ()))

    def test_unconfigured_writer_is_invisible(self):
        self.assertEqual(P.detect(PF.NOTE_TOOL_CMD, PF.NOTE_TOOL_OUT).writes, ())
        self.assertFalse(P.hint(PF.NOTE_TOOL_CMD))

    def test_note_tool(self):
        d = P.detect(PF.NOTE_TOOL_CMD, PF.NOTE_TOOL_OUT, W)
        self.assertEqual(d.writes, (MemoryWrite("note-tool", "notes", ("tool-note-1",)),))
        self.assertEqual(P.detect(PF.NOTE_TOOL_CMD, "save FAILED for x; nothing was stored\n", W).writes, ())

    def test_swarm_remember_with_project(self):
        d = P.detect(PF.SWARM_REMEMBER_CMD, PF.SWARM_REMEMBER_OUT)
        self.assertEqual(d.writes, (MemoryWrite("swarm-remember", "j", ("swarm-0123456789abcdef0123456789abcdef",),
                                                project="J"),))

    def test_swarm_remember_queued_without_project_leaves_the_bank_to_the_hook(self):
        out = 'queued (board not reachable from here: X); ... [memory swarm-spool-abc123 project ""]\n'
        self.assertEqual(P.detect(PF.SWARM_REMEMBER_CMD, out).writes,
                         (MemoryWrite("swarm-remember", "", ("swarm-spool-abc123",), project=""),))

    def test_bad_ids_and_too_many_are_refused(self):
        self.assertEqual(P.detect(PF.NOTE_TOOL_CMD, "saved ../../etc/passwd to notes\n", W).writes, ())
        many = "".join(f"saved doc-{i} to notes\n" for i in range(P.MAX_DOCS + 5))
        self.assertEqual(sum(len(w.document_ids) for w in P.detect(PF.NOTE_TOOL_CMD, many, W).writes), P.MAX_DOCS)

    def test_two_writers_in_one_command(self):
        cmd = PF.NOTE_TOOL_CMD + " && " + PF.SWARM_REMEMBER_CMD
        d = P.detect(cmd, PF.NOTE_TOOL_OUT + PF.SWARM_REMEMBER_OUT, W)
        self.assertEqual({w.writer for w in d.writes}, {"note-tool", "swarm-remember"})

    def test_only_the_last_mib_of_a_huge_command_is_scanned(self):
        pad = "x" * P.COMMAND_SCAN
        self.assertFalse(P.hint(PF.NOTE_TOOL_CMD + " " + pad, W))
        self.assertTrue(P.hint(pad + " ; " + PF.NOTE_TOOL_CMD, W))
        self.assertEqual(P.detect(PF.NOTE_TOOL_CMD + " " + pad, PF.NOTE_TOOL_OUT, W).writes, ())
        self.assertEqual(len(P.detect(pad + " ; " + PF.NOTE_TOOL_CMD, PF.NOTE_TOOL_OUT, W).writes), 1)

    def test_only_the_last_part_of_a_huge_output_is_scanned(self):
        out = "saved early-doc to notes\n" + "x" * (P.OUTPUT_SCAN + 10) + "\nsaved late-doc to notes\n"
        self.assertEqual(P.detect(PF.NOTE_TOOL_CMD, out, W).writes[0].document_ids, ("late-doc",))


class OutputTextTests(unittest.TestCase):
    def test_both_hosts(self):
        claude = PF.claude_post("x", "retained a\n")
        self.assertEqual(P.output_text(claude["tool_response"]), "retained a\n")
        self.assertEqual(P.output_text(PF.codex_post("x", "retained b\n")["tool_response"]), "retained b\n")
        self.assertEqual(P.output_text([{"type": "input_text", "text": "retained c"}]), "retained c")
        self.assertEqual(P.output_text({"stdout": "o", "stderr": "retained evil"}), "o")
        self.assertEqual(P.output_text(None), "")


if __name__ == "__main__":
    unittest.main()
