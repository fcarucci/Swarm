"""textsafe: board-derived text never reaches a terminal or
an agent's context as control input."""
from __future__ import annotations

import re
import unittest

from support import ROOT  # noqa: F401

from swarm import textsafe  # noqa: E402

CONTROL_BYTES = re.compile("[\x00-\x08\x0a-\x1f\x7f-\x9f]")
BIDI = "\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069"


class TermSafeTests(unittest.TestCase):
    def test_escapes_become_visible(self):
        self.assertEqual(textsafe.term_safe("a\x1b[2Jb\x07"), "a\\x1b[2Jb\\x07")
        self.assertEqual(textsafe.term_safe("\x9b31m"), "\\x9b31m")
        self.assertEqual(textsafe.term_safe("x\x7fy\x00"), "x\\x7fy\\x00")

    def test_newlines_escaped_unless_kept(self):
        self.assertEqual(textsafe.term_safe("a\nb\rc"), "a\\x0ab\\x0dc")
        self.assertEqual(textsafe.term_safe("a\nb\rc", keep_newlines=True), "a\nb\\x0dc")

    def test_tab_kept(self):
        self.assertEqual(textsafe.term_safe("a\tb"), "a\tb")

    def test_bidi_overrides_and_isolates(self):
        self.assertEqual(textsafe.term_safe("evil\u202etxt.exe"), "evil\\u{202E}txt.exe")
        out = textsafe.term_safe(BIDI)
        self.assertFalse(any(c in out for c in BIDI), out)
        self.assertIn("\\u{2069}", out)

    def test_every_control_is_gone(self):
        s = "".join(chr(c) for c in range(0x00, 0xa0)) + BIDI + "\ud800"
        out = textsafe.term_safe(s)
        self.assertIsNone(CONTROL_BYTES.search(out), out)
        self.assertFalse(any(c in out for c in BIDI))
        out.encode("utf-8")   # no lone surrogate

    def test_ordinary_text_unchanged(self):
        s = "Homer Simpson → Marge: ça va? ✓ 日本語 [swarm] 100%"
        self.assertEqual(textsafe.term_safe(s), s)

    def test_none_and_non_strings(self):
        self.assertEqual(textsafe.term_safe(None), "")
        self.assertEqual(textsafe.term_safe(7), "7")

    def test_has_controls(self):
        self.assertFalse(textsafe.has_controls("plain name - ok"))
        for bad in ("a\nb", "\x1b", "\x9b", "\u202e", "\x7f", "a\tb"):
            self.assertTrue(textsafe.has_controls(bad), repr(bad))


class LineSeparatorAndFormatTests(unittest.TestCase):
    def test_line_separators_are_controls(self):
        self.assertEqual(textsafe.term_safe("a\u2028b\u2029c"), "a\\u{2028}b\\u{2029}c")
        self.assertEqual(textsafe.term_safe("a\u2028b", keep_newlines=True), "a\\u{2028}b")
        self.assertEqual(textsafe.strip_controls("a\u2028b\u2029c"), "abc")
        self.assertTrue(textsafe.has_controls("a\u2028b"))
        self.assertTrue(textsafe.has_controls("a\u2029b"))

    def test_has_controls_rejects_invisible_format_characters(self):
        for c in ("\u200b", "\u200c", "\u200d", "\u200e", "\u200f", "\u061c", "\ufeff",
                  "\U000e0000", "\U000e0041", "\U000e007f"):
            with self.subTest(c=hex(ord(c))):
                self.assertTrue(textsafe.has_controls(f"name{c}x"))
        self.assertFalse(textsafe.has_controls("\U000e0080 not a tag"))


class StripControlsTests(unittest.TestCase):
    def test_removes_what_term_safe_escapes(self):
        self.assertEqual(textsafe.strip_controls("a\x1b[2Jb\x07\u202ec\x9bd\x00"), "a[2Jbcd")

    def test_keeps_tab_and_optionally_newlines(self):
        self.assertEqual(textsafe.strip_controls("a\tb\nc"), "a\tbc")
        self.assertEqual(textsafe.strip_controls("a\tb\nc", keep_newlines=True), "a\tb\nc")

    def test_ordinary_text_unchanged(self):
        s = "ça va ✓ 日本語"
        self.assertEqual(textsafe.strip_controls(s), s)


if __name__ == "__main__":
    unittest.main()
