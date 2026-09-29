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


def writes_files(command: str) -> str | None:
    if not command:
        return None
    stripped = _QUOTED.sub("''", command)
    for why, rx in _ON_STRIPPED:
        if rx.search(stripped):
            return why
    for why, rx in _ON_RAW:
        if rx.search(command):
            return why
    return None
