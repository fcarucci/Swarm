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


# ---- CI waits (swarm-orphans A) ---------------------------------------------------------------
# A shell command that waits on or polls GitHub/Gitea CI is steered to `swarm ci wait` (one shared,
# budget-safe poller per box; a CI event ends it with no API call). Best effort, like the write
# check above: text in quotes is data, except what `$(...)` or a backtick runs inside double quotes.

_PREFIXES = {"env", "sudo", "nohup", "time", "command", "exec", "do", "then", "else", "elif", "if", "!", "nice",
             "stdbuf", "setsid", "xargs", "while", "until"}
_LOOPS = {"while", "until", "for", "select"}
_CI_PATH = re.compile(r"(?:^|/)(?:actions/|runs\b|check-runs\b|check-suites\b|statuses\b|status\b)")


def _code_view(command: str) -> str:
    """`command` with its quoted data blanked (same length): single-quoted text, escapes and the
    plain text of double quotes become spaces; `$(...)` and backticks inside double quotes stay
    (they run). A quote that never closes leaves the rest as code (fail towards seeing it)."""
    out, i, n = [], 0, len(command)
    stack = ["code"]          # code | dq | sub (a $( inside dq: code until its ) ) | bt (backtick in dq)
    depth = []                # paren depth of each open "sub"
    while i < n:
        c, mode = command[i], stack[-1]
        if mode in ("code", "sub", "bt"):
            if c == "'":
                end = command.find("'", i + 1)
                end = n - 1 if end < 0 else end
                out.append(re.sub(r"[^\n]", " ", command[i:end + 1])); i = end + 1; continue
            if c == "\\":
                out.append("  "[:n - i]); i += 2; continue
            if c == '"':
                stack.append("dq"); out.append(" "); i += 1; continue
            if mode == "bt" and c == "`":
                stack.pop(); out.append(" "); i += 1; continue
            if mode == "sub":
                if c == "(":
                    depth[-1] += 1
                elif c == ")":
                    if depth[-1] == 0:
                        stack.pop(); depth.pop(); out.append(")"); i += 1; continue
                    depth[-1] -= 1
            out.append(c); i += 1; continue
        # inside double quotes
        if c == '"':
            stack.pop(); out.append(" "); i += 1
        elif c == "\\":
            out.append("  "[:n - i]); i += 2
        elif command.startswith("$(", i):
            stack.append("sub"); depth.append(0); out.append("$("); i += 2
        elif c == "`":
            stack.append("bt"); out.append("`"); i += 1
        else:
            out.append("\n" if c == "\n" else " "); i += 1
    return "".join(out)[:n]


_HEREDOC_OP = re.compile(r"<<(?!<)(-?)")
_HEREDOC_WORD = re.compile(r"[ \t]*(?:'([^'\n]*)'|\"([^\"\n]*)\"|\\?([A-Za-z0-9_.-]+))")


def drop_heredoc_bodies(command: str) -> str:
    """`command` with the bodies of its here-documents taken out: their lines are data to the
    command reading them. With a quoted delimiter (<<'EOF', <<"EOF", <<\\EOF) bash expands nothing,
    so the body goes; with an unquoted one bash runs its `$(...)` and backticks, so those stay (the
    rest of the line is blanked, as inside double quotes). Operators inside quotes are not operators."""
    lines, view = command.split("\n"), _code_view(command).split("\n")
    if len(lines) != len(view):   # never: the view keeps every newline
        return command
    out, pending, k = [], [], 0
    while k < len(lines):
        line = lines[k]
        if pending:
            strip, word, quoted = pending[0]
            if (line.lstrip("\t") if strip else line) == word:
                pending.pop(0)
            elif not quoted:   # what bash expands in it: its command substitutions
                out.append(_code_view('"' + line.replace('"', " ") + '"'))
            k += 1
            continue
        out.append(line)
        for m in _HEREDOC_OP.finditer(view[k]):
            w = _HEREDOC_WORD.match(line, m.end())
            if w:
                quoted = w.group(3) is None or "\\" in w.group(0)
                pending.append((m.group(1) == "-", next(g for g in w.groups() if g is not None), quoted))
        k += 1
    return "\n".join(out)


def _simple_commands(command: str) -> list[list[str]]:
    """The words of each simple command in `command` (see _code_view), split on ; & | ( ) { }
    `$(` backticks and newlines."""
    view = _code_view(command)
    parts = re.split(r"\$\(|[;&|(){}`\n]", view)
    return [p.split() for p in parts if p.split()]


def _strip_prefix(words: list[str]) -> tuple[list[str], bool]:
    """(the command words after assignments and wrappers like env/sudo/timeout/watch, whether
    the `watch` program wrapped it)."""
    watched = False
    while words:
        w = words[0]
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", w) or w in _PREFIXES:
            words = words[1:]
        elif w in ("timeout", "watch"):
            watched = watched or w == "watch"
            words = words[1:]
            while words and (words[0].startswith("-") or re.fullmatch(r"\d+(?:\.\d+)?[smhd]?", words[0])):
                takes = words[0] in ("-n", "--interval", "-s", "--signal", "-k", "--kill-after")
                words = words[2:] if takes else words[1:]
        else:
            break
    return words, watched


def _prog(word: str) -> str:
    return word.rsplit("/", 1)[-1]


def _gh_sub(words: list[str]) -> list[str]:
    """`gh` arguments without global flags before the subcommand (-R/--repo)."""
    args = words[1:]
    while args and args[0].startswith("-"):
        args = args[2:] if args[0] in ("-R", "--repo") else args[1:]
    return args


def _is_poll(words: list[str]) -> bool:
    prog = _prog(words[0])
    if prog == "gh":
        sub = _gh_sub(words)
        if sub[:2] in (["run", "view"], ["run", "list"]):
            return True
        if sub[:1] == ["api"]:
            return any(_CI_PATH.search(a.split("?", 1)[0]) for a in sub[1:] if not a.startswith("-"))
        return False
    if prog in ("curl", "wget", "http", "xh"):
        return any(re.search(r"/api/v\d+/repos/|api\.github\.com/repos/", a) and _CI_PATH.search(a.split("?", 1)[0])
                   for a in words[1:])
    if prog == "tea":
        return words[1:2] in (["actions"], ["action"], ["runs"])
    return False


def ci_wait(command: str) -> str | None:
    """What makes `command` a CI wait (a short phrase), else None: `gh run watch`, `gh pr checks
    --watch`, or a loop (while/until/for/select, or the `watch` program) around a CI poll (`gh run
    view|list`, `gh api` of runs/check-runs/statuses, curl/wget of a GitHub or Gitea runs or
    status URL, `tea actions`). A one-shot `gh run view <id> --json conclusion` is not one."""
    if not command or ("gh" not in command and "/repos/" not in command and "tea" not in command):
        return None
    depth = 0   # loops open around the current simple command: while/until/for/select ... done
    for words in _simple_commands(drop_heredoc_bodies(command)):
        if words[0] == "done":
            depth = max(0, depth - 1)
            continue
        if words[0] in _LOOPS:
            depth += 1
        loop = depth > 0
        words, watched = _strip_prefix(words)
        if not words:
            continue
        if _prog(words[0]) == "gh":
            sub = _gh_sub(words)
            if sub[:2] == ["run", "watch"]:
                return "`gh run watch`"
            if sub[:2] == ["pr", "checks"] and any(a == "--watch" or a.startswith("--watch=") for a in sub):
                return "`gh pr checks --watch`"
        if (loop or watched) and _is_poll(words):
            return f"a loop polling CI (`{' '.join(words[:3])[:60]} ...`)"
    return None


def ci_repo(command: str) -> str | None:
    """The OWNER/REPO a gh command names with -R/--repo, if any."""
    for words in _simple_commands(command or ""):
        words, _ = _strip_prefix(words)
        if words and _prog(words[0]) == "gh":
            for k, w in enumerate(words):
                if w in ("-R", "--repo") and k + 1 < len(words):
                    return words[k + 1]
                if w.startswith("--repo="):
                    return w[len("--repo="):]
    return None
