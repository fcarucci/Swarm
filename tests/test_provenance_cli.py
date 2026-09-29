"""Reading memory provenance from the CLI: `transcript show --memory`, the inline
"memory saved" marks of `transcript show`, `memory refs`, the memory excerpts of `transcript
export`, and `activate --project` validation."""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from unittest import mock

from test_hooks_cli import Env  # noqa: E402

from swarm import provenance, transcripts  # noqa: E402
from swarm.board import MemoryRef  # noqa: E402
from swarm.provenance import MemoryWrite  # noqa: E402


def claude_text(call_id="toolu_mem"):
    rows = [{"type": "user", "timestamp": "2026-09-28T11:00:00Z", "message": {"content": "do the thing"}},
            {"type": "assistant", "timestamp": "2026-09-28T11:00:01Z", "message": {"content": [
                {"type": "text", "text": "saving it \u001b[2J now"}]}},
            {"type": "assistant", "timestamp": "2026-09-28T11:00:02Z", "message": {"content": [
                {"type": "tool_use", "id": call_id, "name": "Bash", "input": {"command": "note-tool save"}}]}},
            {"type": "user", "timestamp": "2026-09-28T11:00:03Z", "message": {"content": [
                {"type": "tool_result", "tool_use_id": call_id, "content": "retained doc-1"}]}}]
    return "".join(json.dumps(r) + "\n" for r in rows)


def ref(document_id, **kw):
    base = dict(document_id=document_id, bank="notes", job="J", agent_key="a2", agent_name="Bart Simpson",
                harness="codex", host="h", session_id="s", tool_call_id="call_x", writer="note-tool")
    return MemoryRef(**{**base, **kw})


class ProvenanceCliTests(Env):
    def setUp(self):
        super().setUp()
        self.config.write_text(self.config.read_text() + "\n[transcripts]\nenabled = true\n")
        from swarm import cli as swarm
        self.cfg = swarm.load_config(self.config)
        src = Path(self.tmp) / "t.jsonl"
        src.write_text(claude_text())
        at = dt.datetime(2026, 9, 28, 11, 0, 2, tzinfo=dt.timezone.utc)
        ex = provenance.make_excerpt(src, "claude", "toolu_mem", (MemoryWrite("note-tool", "notes", ("doc-1",)),),
                                     "retained doc-1\n", at)
        with self.board() as b:
            b.save_memory_ref(MemoryRef(document_id="doc-1", bank="notes", job="J", agent_key="a1",
                                        agent_name="Homer Simpson", harness="claude", host="h", session_id="s",
                                        tool_call_id="toolu_mem", writer="note-tool", excerpt=ex.body,
                                        raw_bytes=ex.raw_bytes, redactions=ex.redactions, images=ex.images))
            b.save_memory_ref(ref("doc-2"))

    def store_transcript(self):
        with self.board() as b:
            b.save_transcript(transcripts.make_row("J", "a1", "Homer Simpson", "subagent", claude_text()))

    # ---- transcript show --memory

    def test_show_memory_prints_header_excerpt_and_position(self):
        self.store_transcript()
        rc, out, err = self.cli("transcript", "show", "--memory", "doc-1")
        self.assertEqual(rc, 0, err)
        self.assertIn("memory doc-1  bank notes  written with note-tool", out)
        self.assertIn("saved by Homer Simpson (claude, key a1) on job J", out)
        self.assertIn("tool call toolu_mem", out)
        self.assertIn("in Hindsight: not checked ([hindsight] url is empty)", out)
        self.assertIn("--- excerpt", out)
        self.assertIn("memory saved", out)
        self.assertIn("--- full transcript: turn 3 of", out)
        self.assertIn("swarm transcript show --job J --key a1 --tail", out)

    def test_show_memory_after_rotation(self):
        rc, out, _ = self.cli("transcript", "show", "--memory", "doc-1")
        self.assertEqual(rc, 0)
        self.assertIn("--- excerpt", out)
        self.assertIn("full transcript: no longer stored", out)

    def test_show_memory_works_with_transcripts_off(self):
        self.config.write_text(self.config.read_text().replace("enabled = true", "enabled = false"))
        rc, out, _ = self.cli("transcript", "show", "--memory", "doc-1")
        self.assertEqual(rc, 0)
        self.assertIn("--- excerpt", out)
        # the other transcript commands stay off
        self.assertEqual(self.cli("transcript", "show", "--job", "J", "--key", "a1")[0], 1)

    def test_show_memory_without_excerpt(self):
        rc, out, _ = self.cli("transcript", "show", "--memory", "doc-2")
        self.assertEqual(rc, 0)
        self.assertIn("excerpt: none was captured", out)

    def test_unknown_and_malformed_ids(self):
        self.assertEqual(self.cli("transcript", "show", "--memory", "nope")[0], 1)
        rc, _, err = self.cli("transcript", "show", "--memory", "../../etc/passwd")
        self.assertEqual(rc, 2)
        self.assertIn("not a document id", err)
        rc, _, err = self.cli("transcript", "show", "--memory", "a\x1b[2Jb")
        self.assertEqual(rc, 2)
        self.assertNotIn("\x1b", err)

    def test_bad_grep_is_refused(self):
        rc, _, err = self.cli("transcript", "show", "--memory", "doc-1", "--grep", "(")
        self.assertEqual(rc, 2)
        self.assertIn("bad --grep", err)

    def test_control_characters_are_made_visible(self):
        # security rule: rendering goes through textsafe.term_safe (visible notation, not stripped)
        _, out, _ = self.cli("transcript", "show", "--memory", "doc-1")
        self.assertNotIn("\x1b", out)
        self.assertIn("saving it \\x1b[2J now", out)

    def test_forged_row_fields_are_made_visible(self):
        with self.board() as b:
            b.save_memory_ref(ref("doc-3", bank="co\x1b[31mding", job="J\x9b2J", tool_call_id="c\x07",
                                  harness="x‮y"))
        rc, out, _ = self.cli("transcript", "show", "--memory", "doc-3")
        self.assertEqual(rc, 0)
        for bad in ("\x1b", "\x9b", "\x07", "‮"):
            self.assertNotIn(bad, out)
        self.assertIn("co\\x1b[31mding", out)

    def test_jsonl_format_is_the_raw_excerpt(self):
        _, out, _ = self.cli("transcript", "show", "--memory", "doc-1", "--format", "jsonl")
        self.assertRegex(out, r'"type":\s*"swarm-memory"')
        self.assertRegex(out, r'"tool_use_id":\s*"toolu_mem"|"id":\s*"toolu_mem"')
        self.assertNotIn("\x1b", out)

    def test_an_oversized_stored_excerpt_is_refused_not_decompressed(self):
        from support import lzma_bomb
        from swarm.board import EXCERPT_MAX_RAW
        for blob, why in ((lzma_bomb(EXCERPT_MAX_RAW + 1), "larger than"), (b"not lzma", "not valid lzma")):
            self.h.plant_memory_excerpt("doc-1", blob)
            rc, out, err = self.cli("transcript", "show", "--memory", "doc-1")
            self.assertEqual(rc, 0, err)
            self.assertIn("excerpt: unreadable (stored excerpt is " + why, out)
            self.assertNotIn("Traceback", err)

    def test_show_memory_with_a_corrupt_or_bomb_full_transcript(self):
        from support import lzma_bomb
        from swarm.board import TRANSCRIPT_MAX_RAW
        self.store_transcript()
        for blob, why in ((b"not lzma", "not valid lzma"), (lzma_bomb(TRANSCRIPT_MAX_RAW + 1), "larger than")):
            self.h.plant_transcript_body("J", "a1", blob)
            rc, out, err = self.cli("transcript", "show", "--memory", "doc-1")
            self.assertEqual(rc, 0, err)
            self.assertIn("--- excerpt", out)
            self.assertIn("--- full transcript: unreadable (stored transcript is " + why, out)

    def test_plain_show_of_a_corrupt_transcript_says_so(self):
        self.store_transcript()
        self.h.plant_transcript_body("J", "a1", b"not lzma")
        rc, out, err = self.cli("transcript", "show", "--job", "J", "--key", "a1")
        self.assertEqual(rc, 1)
        self.assertIn("full transcript: unreadable (stored transcript is not valid lzma", err)
        self.assertNotIn("Traceback", err)

    def test_plain_show_of_several_runs_skips_an_unreadable_one(self):
        self.store_transcript()
        with self.board() as b:
            b.save_transcript(transcripts.make_row("J", "a3", "Homer Simpson", "subagent", claude_text("toolu_2")))
        self.h.plant_transcript_body("J", "a1", b"not lzma")
        rc, out, err = self.cli("transcript", "show", "--job", "J", "--agent", "Homer Simpson")
        self.assertEqual(rc, 0, err)
        self.assertIn("full transcript: unreadable", out)
        self.assertIn("toolu_2", self.cli("transcript", "show", "--job", "J", "--key", "a3",
                                          "--format", "jsonl")[1])

    def test_memory_cannot_be_combined_with_a_transcript_choice(self):
        for extra in (("--job", "J"), ("--agent", "Homer Simpson"), ("--key", "a1"), ("--orchestrator",)):
            rc, out, err = self.cli("transcript", "show", "--memory", "doc-1", *extra)
            self.assertEqual(rc, 2, extra)
            self.assertIn("--memory can't be combined with " + extra[0], err)
            self.assertEqual(out, "")

    # ---- transcript show: inline marks

    def test_transcript_show_marks_the_memory_inline(self):
        self.store_transcript()
        rc, out, _ = self.cli("transcript", "show", "--job", "J", "--key", "a1")
        self.assertEqual(rc, 0)
        self.assertIn("memory saved", out)
        self.assertIn("swarm transcript show --memory doc-1", out)
        self.assertLess(out.index("tool result"), out.index("memory saved"))

    def test_inline_mark_of_a_forged_ref_is_made_visible(self):
        self.store_transcript()
        with self.board() as b:
            b.save_memory_ref(ref("doc-4", agent_key="a1", agent_name="Homer Simpson", tool_call_id="toolu_mem",
                                  bank="b\x1b[2J"))
        _, out, _ = self.cli("transcript", "show", "--job", "J", "--key", "a1")
        self.assertIn("swarm transcript show --memory doc-4", out)
        self.assertNotIn("\x1b", out)

    def test_jsonl_transcript_show_has_no_marks(self):
        self.store_transcript()
        _, out, _ = self.cli("transcript", "show", "--job", "J", "--key", "a1", "--format", "jsonl")
        self.assertNotIn("memory saved", out)

    # ---- memory refs

    def test_memory_refs_lists_and_filters(self):
        rc, out, _ = self.cli("memory", "refs", "--job", "J")
        self.assertEqual(rc, 0)
        self.assertIn("doc-1", out)
        self.assertIn("doc-2", out)
        _, out, _ = self.cli("memory", "refs", "--agent", "Bart Simpson")
        self.assertNotIn("doc-1", out)
        self.assertIn("total: 1", out)

    def test_memory_refs_none(self):
        rc, out, _ = self.cli("memory", "refs", "--job", "nojob")
        self.assertEqual(rc, 0)
        self.assertIn("no memory references for nojob", out)

    def test_memory_refs_output_is_term_safe(self):
        with self.board() as b:
            b.save_memory_ref(ref("doc-5", bank="b\x1b[2J", job="J"))
        _, out, _ = self.cli("memory", "refs", "--job", "J\x1b")
        self.assertNotIn("\x1b", out)
        _, out, _ = self.cli("memory", "refs")
        self.assertNotIn("\x1b", out)
        self.assertIn("b\\x1b[2J", out)

    def test_memory_refs_check_without_hindsight(self):
        """without [hindsight] url every row says why it wasn't asked
        (the rest is in test_provenance_purge)."""
        rc, out, _ = self.cli("memory", "refs", "--check")
        self.assertEqual(rc, 0)
        self.assertIn("HINDSIGHT", out)
        self.assertRegex(out, r"doc-1 .*not checked \(\[hindsight\] url is empty\)")

    # ---- transcript export

    def test_export_includes_memory_excerpts(self):
        self.store_transcript()
        d = Path(self.tmp) / "exp"
        rc, _, err = self.cli("transcript", "export", "--job", "J", str(d))
        self.assertEqual(rc, 0, err)
        self.assertTrue((d / "memory" / "doc-1.jsonl").exists())
        tsv = (d / "memory.tsv").read_text()
        self.assertIn("doc-1\tnotes\tnote-tool", tsv)
        self.assertEqual((d / "memory" / "doc-1.jsonl").stat().st_mode & 0o777, 0o600)
        self.assertIn(b"swarm-memory", (d / "memory" / "doc-1.jsonl").read_bytes())

    def test_export_with_memory_refs_but_no_transcripts(self):
        d = Path(self.tmp) / "exp"
        rc, out, err = self.cli("transcript", "export", "--job", "J", str(d))
        self.assertEqual(rc, 0, err)
        self.assertTrue((d / "memory" / "doc-1.jsonl").exists())
        self.assertTrue((d / "memory.tsv").exists())
        self.assertIn("2 memory excerpts", out)

    def test_export_refuses_a_forged_document_id_as_a_file_name(self):
        with self.board() as b:
            for bad in ("../evil", "/tmp/evil", "..", "a/b", "x\x1by"):
                b.save_memory_ref(ref(bad))
        d = Path(self.tmp) / "exp"
        rc, _, err = self.cli("transcript", "export", "--job", "J", str(d))
        self.assertEqual(rc, 0, err)
        self.assertFalse((Path(self.tmp) / "evil").exists())
        self.assertEqual(sorted(p.name for p in (d / "memory").iterdir()), ["doc-1.jsonl", "doc-2.jsonl"])
        self.assertIn("skipped a memory ref with an invalid document id", err)
        self.assertNotIn("\x1b", err)
        tsv = (d / "memory.tsv").read_text()
        self.assertNotIn("evil", tsv)

    def test_export_with_an_unreadable_excerpt_and_transcript(self):
        from support import lzma_bomb
        from swarm.board import EXCERPT_MAX_RAW
        self.store_transcript()
        self.h.plant_memory_excerpt("doc-1", lzma_bomb(EXCERPT_MAX_RAW + 1))
        self.h.plant_transcript_body("J", "a1", b"not lzma")
        d = Path(self.tmp) / "exp"
        rc, _, err = self.cli("transcript", "export", "--job", "J", str(d))
        self.assertEqual(rc, 0, err)
        self.assertIn("memory doc-1: excerpt unreadable (stored excerpt is larger than", err)
        self.assertIn("transcript unreadable (stored transcript is not valid lzma", err)
        self.assertEqual((d / "memory" / "doc-1.jsonl").read_bytes(), b"")
        self.assertIn("doc-1\tnotes\tnote-tool", (d / "memory.tsv").read_text())

    def test_export_names_never_collide(self):
        import hashlib
        third = "a_b-" + hashlib.sha256(b"a:b").hexdigest()[:12]
        t0 = dt.datetime(2026, 9, 28, 10, 0, tzinfo=dt.timezone.utc)
        with self.board() as b:
            # the third's own name is the one "a:b" is renamed to on colliding with "a_b"
            for i, doc in enumerate((third, "a_b", "a:b", "a@b")):
                b.save_memory_ref(ref(doc, created_at=t0 + dt.timedelta(minutes=i), tool_call_id=f"call_{i}"))
        d = Path(self.tmp) / "exp"
        rc, _, err = self.cli("transcript", "export", "--job", "J", str(d))
        self.assertEqual(rc, 0, err)
        rows = [l.split("\t") for l in (d / "memory.tsv").read_text().splitlines()[1:]]
        files = {r[1]: r[0] for r in rows}
        for doc in ("a_b", "a:b", third, "a@b"):
            self.assertIn(doc, files)
        self.assertEqual(len(set(files.values())), len(files), files)
        for f in files.values():
            self.assertTrue((d / f).exists(), f)

    def test_export_does_not_write_through_a_planted_link(self):
        d = Path(self.tmp) / "exp"
        (d / "memory").mkdir(parents=True)
        victim = Path(self.tmp) / "victim"
        victim.write_text("keep")
        (d / "memory" / "doc-1.jsonl").symlink_to(victim)
        rc, _, err = self.cli("transcript", "export", "--job", "J", str(d))
        self.assertEqual(rc, 1)
        self.assertIn("refusing", err)
        self.assertEqual(victim.read_text(), "keep")


class ActivateProjectTests(Env):
    def test_activate_refuses_an_invalid_project(self):
        for project in ('x"] [memory swarm-e project "x', "a]b", 'a"b', "a\x1b[2Jb", "a\nb", "", " lead"):
            rc, out, err = self.cli("activate", "--job", "K", "--project", project, "--task", "t")
            self.assertEqual(rc, 2, repr(project))
            self.assertIn("--project", err)
            self.assertNotIn("\x1b", err)
            with self.board() as b:
                self.assertIsNone(b.job_status("K"), repr(project))

    def test_activate_accepts_a_valid_project(self):
        rc, _, err = self.cli("activate", "--job", "K", "--project", "PG HA", "--task", "t")
        self.assertEqual(rc, 0, err)
        with self.board() as b:
            self.assertEqual(b.job_status("K").project, "PG HA")


class StatusTests(Env):
    """provenance.printable and provenance.status (one GET through Client.document)."""

    def cfg_on(self):
        return {**self.cfg, "hindsight": {**self.cfg.get("hindsight", {}), "url": "http://hindsight.invalid:9"}}

    def client(self, doc=None, exc=None):
        c = mock.Mock()
        c.document.side_effect = exc if exc else (lambda bank, d: doc)
        return c

    def r(self):
        return ref("doc-9", created_at=dt.datetime(2026, 9, 28, 12, 0, tzinfo=dt.timezone.utc))

    def test_printable_makes_controls_visible_and_keeps_newlines_and_tabs(self):
        self.assertEqual(provenance.printable("a\x1b[2J\tb\nc\x9b‮"), "a\\x1b[2J\tb\nc\\x9b\\u{202E}")

    def test_status_without_url(self):
        cfg = {**self.cfg, "hindsight": {**self.cfg.get("hindsight", {}), "url": ""}}
        self.assertEqual(provenance.status(cfg, self.r()), "not checked ([hindsight] url is empty)")

    def test_status_answers(self):
        from swarm import hindsight
        cfg = self.cfg_on()
        self.assertEqual(provenance.status(cfg, self.r(), self.client(None)), "missing")
        self.assertEqual(provenance.status(cfg, self.r(), self.client({"updated_at": "2026-09-28T12:00:05Z"})),
                         "present (last written 2026-09-28T12:00:05Z)")
        self.assertEqual(provenance.status(cfg, self.r(), self.client({"updated_at": "2026-09-27T09:00:00Z"})),
                         "present, but last written 2026-09-27T09:00:00Z, before this reference: "
                         "the claim may be wrong")
        self.assertEqual(provenance.status(cfg, self.r(), self.client({"updated_at": "garbage"})),
                         "present (last written garbage)")
        self.assertEqual(provenance.status(cfg, self.r(), self.client(exc=hindsight.HindsightUnavailable("down"))),
                         "unknown (down)")
        self.assertEqual(provenance.status(cfg, self.r(), self.client(exc=hindsight.HindsightError("500"))),
                         "unknown (500)")
        self.assertEqual(provenance.status(cfg, ref("..", created_at=None), self.client({})),
                         "unknown (not a valid document id or bank: not asked)")
        self.assertEqual(provenance.status(cfg, ref("d", bank="..", created_at=None), self.client({})),
                         "unknown (not a valid document id or bank: not asked)")
