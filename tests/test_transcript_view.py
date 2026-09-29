"""bin/transcript_view.py: Claude Code JSONL transcripts rendered as readable turns."""
from __future__ import annotations

import json
import re
import unittest
from types import SimpleNamespace

from support import ROOT  # noqa: F401  (sets sys.path)
import codex_fixtures as CF  # noqa: E402

from swarm import transcript_view as tv  # noqa: E402


def entry(kind: str, content, ts: str = "2026-09-26T12:00:00.000Z", **extra) -> str:
    role = "assistant" if kind == "assistant" else "user"
    return json.dumps({"type": kind, "timestamp": ts, "message": {"role": role, "content": content}, **extra})


SAMPLE = "\n".join([
    json.dumps({"type": "mode", "mode": "normal"}),
    entry("user", "Fix the bug in foo.py", ts="2026-09-26T12:00:01.000Z"),
    entry("assistant", [{"type": "thinking", "thinking": "secret musing"}]),
    entry("assistant", [{"type": "text", "text": "Looking at it."},
                        {"type": "tool_use", "id": "t1", "name": "Bash",
                         "input": {"command": "cat foo.py", "description": "Read foo"}}],
          ts="2026-09-26T12:00:02.000Z"),
    entry("user", [{"type": "tool_result", "tool_use_id": "t1",
                    "content": "\n".join(f"line {i}" for i in range(100))}],
          ts="2026-09-26T12:00:03.000Z"),
    entry("user", [{"type": "tool_result", "tool_use_id": "t1", "is_error": True,
                    "content": [{"type": "text", "text": "boom"}]}]),
    entry("user", "<local-command-caveat>meta</local-command-caveat>", isMeta=True),
    "this is not json {",
    entry("assistant", [{"type": "text", "text": "Done."}]),
])


class TurnsTests(unittest.TestCase):
    def setUp(self):
        self.turns = tv.turns(SAMPLE)

    def test_kinds_in_order_skipping_thinking_meta_and_bookkeeping(self):
        self.assertEqual([t.kind for t in self.turns],
                         ["user", "assistant", "tool call", "tool result", "tool result", "unparsed",
                          "assistant"])

    def test_user_and_assistant_text(self):
        self.assertEqual(self.turns[0].text, "Fix the bug in foo.py")
        self.assertEqual(self.turns[1].text, "Looking at it.")
        self.assertNotIn("secret musing", tv.render_text(self.turns))

    def test_tool_call_shows_name_and_input(self):
        call = self.turns[2]
        self.assertEqual(call.label, "Bash")
        self.assertIn("cat foo.py", call.text)

    def test_tool_result_is_labelled_with_its_tool_and_trimmed(self):
        res = self.turns[3]
        self.assertEqual(res.label, "Bash")
        lines = res.text.splitlines()
        self.assertEqual(lines[0], "line 0")
        self.assertEqual(lines[-1], "line 99")
        self.assertLess(len(lines), 40)
        self.assertTrue(any("lines trimmed" in l for l in lines))

    def test_error_result_and_list_content(self):
        res = self.turns[4]
        self.assertEqual(res.text, "boom")
        self.assertTrue(res.error)

    def test_bad_line_is_kept_visible(self):
        self.assertEqual(self.turns[5].text, "this is not json {")

    def test_render_has_headers_with_time(self):
        out = tv.render_text(self.turns)
        self.assertRegex(out, r"(?m)^── \d\d:\d\d:\d\d user$")
        self.assertRegex(out, r"(?m)^── \d\d:\d\d:\d\d tool call: Bash$")
        self.assertRegex(out, r"(?m)^── \d\d:\d\d:\d\d tool result: Bash \(error\)$")

    def test_long_single_line_is_trimmed_by_chars(self):
        t = tv.turns(entry("user", [{"type": "tool_result", "tool_use_id": "x", "content": "y" * 10000}]))
        self.assertLess(len(t[0].text), 3000)
        self.assertIn("chars trimmed", t[0].text)

    def test_truncation_marker_line_is_shown(self):
        marker = json.dumps({"type": "swarm-truncated", "omitted_bytes": 1234, "note": "middle cut"})
        t = tv.turns(marker)
        self.assertEqual(t[0].kind, "truncated")
        self.assertIn("1234", t[0].text)

    def test_memory_anchor_entry_is_a_memory_saved_turn(self):
        e = json.dumps({"type": "swarm-memory", "timestamp": "2026-09-28T12:00:00Z", "harness": "claude",
                        "tool_call_id": "t1", "memories": [{"writer": "note-tool", "bank": "notes",
                                                            "document_id": "d1"}], "output": "retained 1 item(s)"})
        [t] = tv.turns(e)
        self.assertEqual(t.kind, "memory saved")
        self.assertIn("d1 (bank notes, note-tool)", t.text)
        self.assertIn("retained 1 item(s)", t.text)

    def test_call_ids_on_tool_turns_both_hosts(self):
        items = tv.turns(entry("assistant", [{"type": "tool_use", "id": "t9", "name": "Bash", "input": {}}]))
        self.assertEqual(items[0].call_id, "t9")
        codex = tv.turns(CF.rollout("child").read_text())
        self.assertIn("call_synthetic_010", {t.call_id for t in codex})

    def test_mark_memories_after_the_call_and_its_result(self):
        items = tv.turns(entry("assistant", [{"type": "tool_use", "id": "t9", "name": "Bash", "input": {}}]) + "\n" +
                         entry("user", [{"type": "tool_result", "tool_use_id": "t9", "content": "ok"}]))
        ref = SimpleNamespace(document_id="d1", bank="notes", writer="note-tool", tool_call_id="t9", created_at=None)
        out = tv.mark_memories(items, [ref])
        self.assertEqual([t.kind for t in out], ["tool call", "tool result", "memory saved"])
        self.assertIn("swarm transcript show --memory d1", out[-1].text)
        miss = SimpleNamespace(document_id="d2", bank="notes", writer="note-tool", tool_call_id="zz", created_at=None)
        self.assertEqual(len(tv.mark_memories(items, [miss])), 2)   # unknown position: no marker


class TailGrepTests(unittest.TestCase):
    def test_tail_and_grep_on_turns(self):
        turns = tv.turns(SAMPLE)
        self.assertEqual([t.kind for t in tv.select(turns, tail=2)], ["unparsed", "assistant"])
        hits = tv.select(turns, grep="FOO\\.PY")
        self.assertEqual([t.kind for t in hits], ["user", "tool call"])
        self.assertEqual(tv.select(turns, grep="line 99", tail=5)[0].kind, "tool result")

    def test_grep_searches_the_untrimmed_text(self):
        turns = tv.turns(SAMPLE)
        self.assertEqual([t.kind for t in tv.select(turns, grep="line 50")], ["tool result"])

    def test_jsonl_lines(self):
        lines = SAMPLE.splitlines()
        self.assertEqual(tv.select_lines(SAMPLE, tail=1), [lines[-1]])
        self.assertEqual(tv.select_lines(SAMPLE, grep="not json"), ["this is not json {"])

    def test_empty(self):
        self.assertEqual(tv.turns(""), [])
        self.assertEqual(tv.render_text([]), "")



EVIL = "\x1b]0;pwn\x07\x1b[2J\x9b31m‮\x00"
FORBIDDEN = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f  ‪-‮⁦-⁩]")


class ControlsTests(unittest.TestCase):
    """A transcript is agent-written text; `transcript show` must not replay its terminal
    controls. Newlines stay (they are the turns' own line breaks)."""

    def assert_clean(self, out: str):
        self.assertIsNone(FORBIDDEN.search(out), repr(out))
        self.assertIn(r"\x1b]0;pwn\x07", out)
        self.assertIn(r"\u{202E}", out)

    def test_every_turn_kind_is_escaped(self):
        text = "\n".join([
            entry("user", "ask " + EVIL),
            entry("assistant", [{"type": "text", "text": "a\n" + EVIL},
                                {"type": "tool_use", "id": "t1", "name": "Bash" + EVIL, "input": {"c": EVIL}}]),
            entry("user", [{"type": "tool_result", "tool_use_id": "t1", "content": "out " + EVIL}]),
            json.dumps({"type": "swarm-truncated", "note": EVIL}),
            "not json " + EVIL,
        ])
        out = tv.render_text(tv.turns(text))
        self.assert_clean(out)
        self.assertIn("a\n", out)   # the text's own newline is kept

    def test_codex_turns_are_escaped(self):
        text = "\n".join(json.dumps(e) for e in [
            {"type": "session_meta", "payload": {}},
            {"type": "response_item", "payload": {"type": "message", "role": "assistant",
                                                  "content": [{"type": "output_text", "text": EVIL}]}},
            {"type": "response_item", "payload": {"type": "function_call", "name": "sh" + EVIL,
                                                  "call_id": "c", "arguments": json.dumps({"x": EVIL})}},
            {"type": "response_item", "payload": {"type": "function_call_output", "call_id": "c",
                                                  "output": EVIL}},
        ])
        self.assert_clean(tv.render_text(tv.turns(text)))

    def test_every_turn_kind_is_escaped_with_color_too(self):
        """Same as test_every_turn_kind_is_escaped, but with colour on: our own SGR codes are the
        only ESC bytes allowed through (asserted by the stripped-of-our-codes check below)."""
        text = "\n".join([
            entry("user", "ask " + EVIL),
            entry("assistant", [{"type": "text", "text": "a\n" + EVIL},
                                {"type": "tool_use", "id": "t1", "name": "Bash" + EVIL, "input": {"c": EVIL}}]),
            entry("user", [{"type": "tool_result", "tool_use_id": "t1", "content": "out " + EVIL}]),
            json.dumps({"type": "swarm-truncated", "note": EVIL}),
            "not json " + EVIL,
        ])
        out = tv.render_text(tv.turns(text), color=True)
        stripped = _OUR_SGR.sub("", out)
        self.assert_clean(stripped)
        self.assertIn("a\n", stripped)


# Our own colour codes (render_text's _sgr), so ColorTests can strip them before re-checking the
# no-raw-ESC-from-content invariant that ControlsTests establishes without colour.
_OUR_SGR = re.compile(r"\033\[[0-9;]*m")


class ColorTests(unittest.TestCase):
    """`swarm transcript show`'s colour: forced (color=True) gives the expected codes per turn
    kind; without it (the default, as piped output uses), rendering is unchanged from before
    colour existed."""

    def test_piped_output_is_byte_identical_to_uncolored(self):
        items = tv.turns(SAMPLE)
        self.assertEqual(tv.render_text(items), tv.render_text(items, color=False))
        self.assertNotIn("\033[", tv.render_text(items, color=False))

    def test_user_header_is_bold_blue(self):
        out = tv.render_text(tv.turns(entry("user", "hi")), color=True)
        self.assertIn("\033[1;34muser\033[0m", out)

    def test_assistant_header_is_bold_green(self):
        out = tv.render_text(tv.turns(entry("assistant", [{"type": "text", "text": "hi"}])), color=True)
        self.assertIn("\033[1;32massistant\033[0m", out)

    def test_tool_call_header_is_magenta_with_bold_name(self):
        items = tv.turns(entry("assistant", [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}]))
        out = tv.render_text(items, color=True)
        self.assertIn("\033[35mtool call\033[0m", out)
        self.assertIn("\033[1;35mBash\033[0m", out)

    def test_tool_result_is_cyan_and_red_on_error(self):
        ok = tv.turns(entry("user", [{"type": "tool_result", "tool_use_id": "t1", "content": "fine"}]))
        err = tv.turns(entry("user", [{"type": "tool_result", "tool_use_id": "t1", "is_error": True,
                                       "content": "boom"}]))
        self.assertIn("\033[36mtool result\033[0m", tv.render_text(ok, color=True))
        out_err = tv.render_text(err, color=True)
        self.assertIn("\033[31mtool result\033[0m", out_err)
        self.assertIn("\033[31m (error)\033[0m", out_err)

    def test_memory_saved_header_is_yellow(self):
        e = json.dumps({"type": "swarm-memory", "timestamp": "2026-09-28T12:00:00Z", "harness": "claude",
                        "memories": [{"writer": "note-tool", "bank": "notes", "document_id": "d1"}],
                        "output": "retained 1"})
        out = tv.render_text(tv.turns(e), color=True)
        self.assertIn("\033[33mmemory saved\033[0m", out)

    def test_clock_is_dim(self):
        out = tv.render_text(tv.turns(entry("user", "hi", ts="2026-09-26T12:00:01.000Z")), color=True)
        self.assertRegex(out, r"\033\[2m\d\d:\d\d:\d\d\033\[0m")

    def test_redaction_marker_is_highlighted_yellow(self):
        out = tv.render_text(tv.turns(entry("user", [{"type": "tool_result", "tool_use_id": "t1",
                                                       "content": "key [REDACTED:api-key] here"}])),
                             color=True)
        self.assertIn("\033[33m[REDACTED:api-key]\033[0m", out)

    def test_image_placeholder_is_dim(self):
        out = tv.render_text(tv.turns(entry("user", [{"type": "image", "source": {}}])), color=True)
        self.assertIn("\033[2m[image]\033[0m", out)

    def test_tool_call_json_keys_are_dimmed_without_reformatting(self):
        items = tv.turns(entry("assistant", [{"type": "tool_use", "id": "t1", "name": "Bash",
                                              "input": {"command": "ls"}}]))
        out = tv.render_text(items, color=True)
        self.assertIn('\033[2m"command"\033[0m', out)
        plain = _OUR_SGR.sub("", out)
        self.assertIn('{"command": "ls"}', plain)   # unreformatted, still valid JSON once stripped


if __name__ == "__main__":
    unittest.main()
