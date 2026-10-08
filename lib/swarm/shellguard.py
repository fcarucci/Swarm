"""Best-effort detection of shell commands that write files, for the verifier gate (best
effort; the hard guarantee is OpenShell). Conservative: a few false positives on
unusual read-only commands are acceptable, completeness is not claimed."""
from __future__ import annotations

import re

_QUOTED = re.compile(r"'[^']*'|\"(?:[^\"\\]|\\.)*\"")
_AT_CMD = r"(?:^|[;&|(\n`]|\$\(|\b(?:then|do|else|xargs|sudo|env|exec|nohup|time|command)\s|-exec(?:dir)?\s)\s*"
_WRITERS = r"(?:rm|rmdir|mv|cp|install|mkdir|touch|chmod|chown|chgrp|ln|truncate|tee|shred|unlink|rsync|patch|apply_patch)"
_ON_STRIPPED = (
    ("output redirection to a file", re.compile(r"(?<![<>&])(?:\d?>>?|&>>?)\s*(?!&|/dev/(?:null|stdout|stderr|tty)\b)[^\s&|;<>]")),
    ("in-place edit (sed -i)", re.compile(r"\bsed\b[^|;&\n]*\s(?:-[a-zA-Z]*i\b|--in-place)")),
    # -i, with or without a backup extension (-i, -ibak, -i.bak, -pi, -pie), after perl's
    # argument-less switches only: in -Mstrict or -Ilib the letters belong to -M's/-I's argument
    ("in-place edit (perl -i)", re.compile(r"\bperl\b[^|;&\n]*\s-[aclnpsTtUuWwX0-9]*i")),
    ("a file-changing command", re.compile(_AT_CMD + _WRITERS + r"\b")),
    ("find -delete", re.compile(r"\bfind\b[^|;&\n]*\s-delete\b")),
    ("dd of=", re.compile(r"\bdd\b[^|;&\n]*\bof=")),
    ("a git command that writes", re.compile(r"\bgit\s+(?:-C\s+\S+\s+)?(?:commit|push|reset|checkout|switch|merge|rebase|"
                                            r"apply|am|stash|restore|clean|rm|mv|tag|cherry-pick|revert|pull|init|clone)\b")),
    ("a download to a file", re.compile(r"\bcurl\b[^|;&\n]*\s-[a-zA-Z]*[oO]\b|\bwget\b")),
)
_ON_RAW = (
    ("an inline script that writes", re.compile(
        r"\b(?:python3?|node|ruby|perl)\b[^|;&\n]*\s-[ce]\b.*(?:open\([^)]*,\s*['\"][wax]|write_text|write_bytes|"
        r"writeFile|appendFile|os\.remove|os\.unlink|shutil\.|\.rename\()", re.S)),
)


# A here-document's body is data, and is dropped from the examined text, ONLY in one strict shape and
# otherwise never (fail closed: a judge whose report is refused retries, a bypass is a hole): the whole
# command starts with a `swarm verdict ... --details - <<'WORD'` line (the delimiter one fully quoted
# word, so bash expands nothing in the body), every word of that line is a plain word or a quoted
# string with no `$`, backtick or backslash inside, and the body ends at the exact terminator line.
# Anything uncertain before the body (`$((`, `$'`, a `#`, parentheses, ;&|<>, an unbalanced quote) makes
# the line not match, so nothing is hidden. What follows the terminator is still examined.
_WORD = r"(?:[\w./:=@%+,~-]|'[^'\n]*'|\"[^\"\\$`\n]*\")+"
_SEP = r"(?:[ \t]|\\\n)+"
_VERDICT_HEREDOC = re.compile(
    rf"[ \t]*(?:[\w./~-]*/)?swarm{_SEP}verdict(?:{_SEP}{_WORD})*{_SEP}--details{_SEP}-{_SEP}"
    r"<<(?:'(\w+)'|\"(\w+)\")[ \t]*\n")


def _heredoc_spans(command: str) -> list[tuple[int, int, int]]:
    """[(operator start, body start, body end incl. the terminator line)] of the one hideable
    `swarm verdict --details - <<'WORD'` heredoc at the start of `command`, else []."""
    m = _VERDICT_HEREDOC.match(command)
    if not m:
        return []
    word = m.group(1) or m.group(2)
    body = pos = m.end()
    while True:
        nl = command.find("\n", pos)
        if (command[pos:] if nl < 0 else command[pos:nl]) == word:
            return [(command.rfind("<<", 0, body), body, len(command) if nl < 0 else nl)]
        if nl < 0:
            return []
        pos = nl + 1


def mask_heredocs(command: str) -> str:
    """`command` with the `<<` operator and the body of each `swarm verdict --details -` heredoc
    blanked to spaces (newlines kept), the same length so word offsets still point into the original:
    what a word-splitter that refuses `<<` can take apart."""
    out = list(command)
    for start, body, end in _heredoc_spans(command):
        for k in list(range(start, start + 2)) + list(range(body, end)):
            if out[k] != "\n":
                out[k] = " "
    return "".join(out)


def writes_files(command: str) -> str | None:
    if not command:
        return None
    for _, body, end in reversed(_heredoc_spans(command)):
        command = command[:body] + command[end:]
    stripped = _QUOTED.sub("''", command)
    for why, rx in _ON_STRIPPED:
        if rx.search(stripped):
            return why
    for why, rx in _ON_RAW:
        if rx.search(command):
            return why
    return None
