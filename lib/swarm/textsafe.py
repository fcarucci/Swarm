"""Board text made safe for terminals and agent context (control sequences and lookalike text in it).

Posts, names, job names and transcript text come from agents (sandboxed or not) and, through the
shared database role, from another OS user. Printed raw, an ESC or C1 CSI byte is a terminal
command, a newline in a name starts a new `[swarm]` line in another agent's context, and a bidi
override reorders what the operator reads. So:

- term_safe(s): at render time, every such character becomes visible notation (\\xNN for C0,
  DEL and C1; \\u{NNNN} for U+2028/U+2029, bidi overrides/isolates and lone
  surrogates). Apply it to every
  board-derived field before adding our own colour codes.
- strip_controls(s): at write time (normalize_message), the same characters are dropped.
- has_controls(s): whether a value holds any of them, or an invisible format character
  (validation: names, job names).

Tab is kept by all three (it is whitespace, not a command); newlines only with keep_newlines."""
from __future__ import annotations

import re

# C0 (tab and, when kept, newline excluded below), DEL, C1, the line/paragraph separators
# U+2028/U+2029, bidi embeddings/overrides (U+202A-U+202E) and isolates (U+2066-U+2069), lone
# surrogates.
_ALL = "\x00-\x08\x0a-\x1f\x7f-\x9f\u2028\u2029\u202a-\u202e\u2066-\u2069\ud800-\udfff"
_ALL_BUT_NL = "\x00-\x08\x0b-\x1f\x7f-\x9f\u2028\u2029\u202a-\u202e\u2066-\u2069\ud800-\udfff"
_UNSAFE = re.compile(f"[{_ALL}]")
_UNSAFE_KEEP_NL = re.compile(f"[{_ALL_BUT_NL}]")
# Invisible format characters a name must not hide: zero-width space/joiners and LRM/RLM
# (U+200B-U+200F), the Arabic letter mark (U+061C), BOM/ZWNBSP (U+FEFF), tag characters
# (U+E0000-U+E007F). Rendered text keeps them (they don't command a terminal); names refuse them.
_FORMAT = re.compile("[\u200b-\u200f\u061c\ufeff\U000e0000-\U000e007f]")


def _pattern(keep_newlines: bool) -> re.Pattern:
    return _UNSAFE_KEEP_NL if keep_newlines else _UNSAFE


def _visible(m: re.Match) -> str:
    o = ord(m.group())
    return f"\\x{o:02x}" if o < 0x100 else f"\\u{{{o:04X}}}"


def _text(s) -> str:
    return "" if s is None else str(s)


def term_safe(s, *, keep_newlines: bool = False) -> str:
    """`s` (None → "", other values via str) with every terminal control made visible: C0 except
    tab (and newline if keep_newlines), DEL and C1 as \\xNN; U+2028/U+2029, bidi overrides and
    isolates and lone surrogates as \\u{NNNN}. The result holds no byte a terminal interprets as a command."""
    return _pattern(keep_newlines).sub(_visible, _text(s))


def strip_controls(s, *, keep_newlines: bool = False) -> str:
    """`s` with the characters term_safe would escape removed (for write-time normalisation)."""
    return _pattern(keep_newlines).sub("", _text(s))


def has_controls(s) -> bool:
    """Whether `s` holds a character term_safe would escape, a tab, or an invisible format
    character (zero-width, LRM/RLM/ALM, BOM, tag characters): a name or job name has no
    business holding any of them."""
    t = _text(s)
    return "\t" in t or _UNSAFE.search(t) is not None or _FORMAT.search(t) is not None
