"""Best-effort detection of shell commands that write files, for the verifier gate (best
effort; the hard guarantee is OpenShell). Conservative: a few false positives on
unusual read-only commands are acceptable, completeness is not claimed."""
from __future__ import annotations

import os
import re
import shutil
import subprocess

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
    ("tar extraction", re.compile(r"\btar\b[^|;&\n]*\s-?[a-zA-Z]*x[a-zA-Z]*\b")),
    ("sort -o", re.compile(r"\bsort\b[^|;&\n]*\s(?:-[a-zA-Z]*o\b|--output)")),
    ("pip install", re.compile(r"\bpip3?\s+install\b|-m\s+pip\s+install\b")),
    ("a download to a file", re.compile(r"\bcurl\b[^|;&\n]*\s-[a-zA-Z]*[oO]\b|\bwget\b")),
)
_ON_RAW = (
    ("awk output to a file", re.compile(r"\bawk\b[^|;&\n]*['\"][^'\"]*>[^'\"]*['\"]")),
    ("sed w (write to a file)", re.compile(r"\bsed\b[^|;&\n]*['\"][^'\"]*(?:^|[\s;{}0-9$/])w\s*[\w./-]+")),
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


def heredoc_bodies(command: str) -> list[str]:
    """The bodies of the here-documents of `command` (what drop_heredoc_bodies takes out), for a caller
    that must look at what a shell reading its stdin would run."""
    lines, view = command.split("\n"), _code_view(command).split("\n")
    if len(lines) != len(view):
        return []
    out, pending, k, cur = [], [], 0, None
    while k < len(lines):
        line = lines[k]
        if pending:
            strip, word, _quoted = pending[0]
            if (line.lstrip("\t") if strip else line) == word:
                pending.pop(0)
                out.append("\n".join(cur or []))
                cur = [] if pending else None
            else:
                cur.append(line)
            k += 1
            continue
        for m in _HEREDOC_OP.finditer(view[k]):
            w = _HEREDOC_WORD.match(line, m.end())
            if w:
                pending.append((m.group(1) == "-", next(g for g in w.groups() if g is not None), True))
        if pending:
            cur = []
        k += 1
    return out


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


# ---- drive jobs (spec 3.4, 4.0): what no launch may run, and what a read-only launch may not write -----
# A guardrail, not a boundary (spec 4.0, [F24]): the boundary is the Bash sandbox and the launches' lack of landing
# credentials. It stops recognisable misuse, and the driver's own checks (`swarm land`, the mirror audit) decide
# outcomes. The refusals are a push to the job's target branch (any spelling that names it, or that names none and
# so pushes whatever is checked out), any force or mirror push, a headless Claude or Codex session, a merge through
# gh, and scheduling (at, batch, crontab). Read-only launches (judges, reviewers) also refuse writes and SQL writes,
# and only judges run `swarm verdict`. Words are split like a shell does (quotes resolved, not blanked).
#
# ONE RULE for code (the coordinator's ruling, round 4): code that can run commands must be a LITERAL ARGUMENT,
# and it is scanned: `sh -c '<literal>'` (and every other shell's -c) is parsed and walked recursively like the
# command line itself; interpreter code (`python3 -c`, `perl -e`, `node -e`, `ruby -e`, `php -r`, `lua -e`,
# `expect -c`, `osascript -e`, an awk or sed program, `vim -c`, `git -c core.pager=...`, GIT_SSH_COMMAND=...) is
# scanned as text (_inline_refusal). Code that arrives any other way cannot be checked and is refused: stdin, a
# pipe, a here-string, a heredoc, `<(...)`, a variable or `$(...)` (as the program, as -c code or under eval), a
# glob or brace expansion as the program, and a script FILE, unless that file is tracked unmodified at HEAD in its
# repository (or is a file the launch cannot write, such as /usr/bin/...). The same file rule covers `./script.sh`,
# `bash script.sh`, `python3 x.py`, `source f`, `awk -f f`, `make -f f`. A program reached through a path that
# resolves to a shell or interpreter under another name (`ln -s /bin/sh x; ./x`) is refused; a bare name is
# resolved through PATH and its real name decides (rbash, a symlink to bash, is bash).

_MAX_NEST = 5
STDIN_SHELL = "<stdin-shell>"   # code that cannot be checked; args, when given, is [why]
TOO_DEEP = "<too-deep>"   # the program iter_commands yields for a command nested beyond _MAX_NEST
CODE = "<code>"           # interpreter code given as a literal argument: args == [the code]
SCRIPT = "<script-file>"  # a script file a shell or interpreter runs: args == [the path as written]
_POSIX_SHELLS = frozenset(("sh", "bash", "rbash", "zsh", "dash", "ksh", "ksh93", "mksh", "lksh", "oksh", "loksh",
                           "pdksh", "posh", "yash", "ash", "hush", "fish", "csh", "tcsh", "osh", "ysh"))
_OTHER_SHELLS = frozenset(("elvish", "nu", "xonsh", "pwsh", "powershell", "rc", "es", "ion", "murex"))
_SHELLS = _POSIX_SHELLS | _OTHER_SHELLS
_SEPARATORS = ";&|()\n"
_KEYWORDS = frozenset(("{", "}", "!", "if", "then", "do", "else", "elif", "while", "until", "time", "command",
                       "builtin", "exec", "nohup", "setsid", "nice", "ionice", "chrt", "stdbuf", "xargs", "env",
                       "sudo", "doas", "timeout", "watch", "unbuffer", "caffeinate", "taskset", "coproc", "busybox",
                       "toybox", "chroot", "unshare", "nsenter", "dbus-run-session", "strace", "pkexec", "cgexec"))
_SHELL_IF_EMPTY = frozenset(("chroot", "unshare", "nsenter"))   # with no command they start an interactive shell
_REDIRECT = re.compile(r"\d*(?:<<<|<<-?|>>|>&|<&|&>|>|<)")
_ASSIGN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")


class _Word(str):
    """A shell word, quotes resolved. `dyn`: the shell expands something in it (a $ parameter, `$(...)` or
    backticks, `$'...'`, a glob or a brace outside quotes), so what it really is cannot be read from the text."""
    dyn = False


def _w(text: str, dyn: bool) -> str:
    word = _Word(text)
    word.dyn = dyn
    return word


def _dyn(word) -> bool:
    return bool(getattr(word, "dyn", False))


def _read_substitution(text: str, i: int) -> tuple[str, int]:
    """(inner text, index after it) of the `$(` at text[i:i+2], nested parentheses and quotes counted."""
    depth, j, n = 0, i + 2, len(text)
    while j < n:
        c = text[j]
        if c == "\\":
            j += 2
            continue
        if c == "'":
            j = text.find("'", j + 1)
            j = n if j < 0 else j + 1
            continue
        if c == '"':
            j += 1
            while j < n and text[j] != '"':
                j += 2 if text[j] == "\\" else 1
            j += 1
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            if depth == 0:
                return text[i + 2:j], j + 1
            depth -= 1
        j += 1
    return text[i + 2:], n


def _read_backticks(text: str, i: int) -> tuple[str, int]:
    j, n = i + 1, len(text)
    while j < n and text[j] != "`":
        j += 2 if text[j] == "\\" else 1
    return text[i + 1:j], min(j + 1, n)


_PARAM_START = re.compile(r"[A-Za-z_{0-9@*#?!$-]")


def split_commands(text: str, _depth: int = 0, _keep_redirections: bool = False) -> list[list[str]]:
    """The words of each simple command of a shell text, quotes resolved. Command separators are ; & | ( )
    and newlines outside quotes; the commands inside `$(...)` and backticks are listed as commands of their
    own (and leave a placeholder word where they stood). Redirections are dropped. Each word is a _Word whose
    `dyn` says whether the shell expands it. Never raises."""
    if _depth > _MAX_NEST or not isinstance(text, str):
        return []
    text = drop_heredoc_bodies(text)
    out: list[list[str]] = []
    words: list[str] = []
    cur: list[str] = []
    has = False            # a word is being built (an empty "" still counts)
    dyn = glob = brace = False
    i, n = 0, len(text)

    def end_word():
        nonlocal cur, has, dyn, glob, brace
        if has:
            word = "".join(cur)
            words.append(_w(word, dyn or (glob and word not in ("[", "[[")) or (brace and word != "{")))
        cur, has, dyn, glob, brace = [], False, False, False, False

    def end_command():
        nonlocal words
        end_word()
        if words:
            out.append(words)
        words = []

    while i < n:
        c = text[i]
        if c == "\\" and i + 1 < n:
            if text[i + 1] != "\n":
                cur.append(text[i + 1])
                has = True
            i += 2
        elif c == "'":
            j = text.find("'", i + 1)
            j = n if j < 0 else j
            cur.append(text[i + 1:j])
            has = True
            i = j + 1
        elif c == '"':
            has = True
            i += 1
            while i < n and text[i] != '"':
                if text[i] == "\\" and i + 1 < n:
                    cur.append(text[i + 1] if text[i + 1] in '"\\$`' else text[i:i + 2])
                    i += 2
                elif text.startswith("$(", i):
                    inner, i = _read_substitution(text, i)
                    out.extend(split_commands(inner, _depth + 1))
                    cur.append("$SUB")
                    dyn = True
                elif text[i] == "`":
                    inner, i = _read_backticks(text, i)
                    out.extend(split_commands(inner, _depth + 1))
                    cur.append("$SUB")
                    dyn = True
                else:
                    if text[i] == "$" and i + 1 < n and _PARAM_START.match(text[i + 1]):
                        dyn = True
                    cur.append(text[i])
                    i += 1
            i += 1
        elif text.startswith("$(", i):
            inner, i = _read_substitution(text, i)
            out.extend(split_commands(inner, _depth + 1))
            cur.append("$SUB")
            has = dyn = True
        elif c == "`":
            inner, i = _read_backticks(text, i)
            out.extend(split_commands(inner, _depth + 1))
            cur.append("$SUB")
            has = dyn = True
        elif c == "#" and not has:
            while i < n and text[i] != "\n":
                i += 1
        elif c == "&" and has and cur and cur[-1][-1:] in ("<", ">"):
            cur.append(c)      # 2>&1, >&2: part of the redirection word
            i += 1
        elif c in _SEPARATORS:
            end_command()
            i += 1
        elif c in " \t\r":
            end_word()
            i += 1
        else:
            if c == "$" and i + 1 < n and (_PARAM_START.match(text[i + 1]) or text[i + 1] in "'\""):
                dyn = True     # $NAME, ${...}, $1, $@, $'...' (ANSI-C: escapes decoded), $"..." (locale)
            elif c in "*?[":
                glob = True
            elif c == "{":
                brace = True
            cur.append(c)
            has = True
            i += 1
    end_command()
    return [w if _keep_redirections else _drop_redirections(w) for w in out if w]


def _drop_redirections(words: list[str]) -> list[str]:
    kept, skip = [], False
    for w in words:
        if skip:
            skip = False
            continue
        m = _REDIRECT.match(w)
        if m and (m.group(0) == w or w.startswith(m.group(0))) and not w.startswith("--"):
            skip = m.group(0) == w     # `> file`: the file is the next word; `>file` carries it
            continue
        kept.append(w)
    return kept


_VALUE_FLAGS = {
    "env": {"-u", "--unset", "-C", "--chdir", "-S", "--split-string"},
    "sudo": {"-u", "-g", "-h", "-p", "-C", "-r", "-t", "-D", "-R", "-U", "--user", "--group", "--chdir", "--host",
             "--prompt", "--role", "--type", "--other-user", "--close-from"},
    "doas": {"-u", "-C"},
    "timeout": {"-s", "--signal", "-k", "--kill-after"},
    "nice": {"-n", "--adjustment"},
    "ionice": {"-c", "-n", "-p", "-P", "-u"},
    "chrt": set(),
    "stdbuf": {"-i", "-o", "-e"},
    "xargs": {"-I", "-i", "-n", "-P", "-d", "-E", "-e", "-L", "-l", "-s", "-a", "--max-args", "--max-procs",
              "--delimiter", "--arg-file", "--max-lines", "--max-chars"},
    "watch": {"-n", "--interval", "-d"},
    "taskset": {"-c"},
    "exec": {"-a"},
    "chroot": {"--userspec", "--groups"},
    "unshare": {"-S", "--setuid", "-G", "--setgid", "-R", "--root", "-w", "--wd", "--map-user", "--map-group",
                "--propagation", "--setgroups"},
    "nsenter": {"-t", "--target", "-S", "--setuid", "-G", "--setgid", "--user-parent"},
    "dbus-run-session": {"--config-file", "--dbus-daemon"},
    "strace": {"-o", "-e", "-p", "-s", "-u", "-E", "-a", "-b", "-I", "-O", "-P", "-S", "-X"},
    "pkexec": {"--user"},
    "cgexec": {"-g"},
}


def _unwrap(words: list[str]) -> tuple[list[str], set[str], list[tuple[str, str]]]:
    """(the command after assignments and wrappers like env, sudo, timeout, nohup, xargs, chroot, busybox; the
    wrappers seen; the NAME=value assignments made for it). A wrapper's own options are skipped, and the ones that
    take a value skip it. Tags in the wrappers: `split:TEXT` (env -S), `shell-if-empty` (a wrapper that runs an
    interactive shell when given no command), `chdir` (the command runs in another directory), `xargs-I`."""
    seen: set[str] = set()
    assigns: list[tuple[str, str]] = []
    words = list(words)
    while words:
        w = words[0]
        if (_ASSIGN.match(w) or ("env" in seen and "=" in w and not w.startswith(("=", "-")))) and not w.startswith("="):
            name, _eq, value = w.partition("=")
            assigns.append((name, _w(value, _dyn(w))))
            words = words[1:]
            continue
        base = _prog(w)
        if base not in _KEYWORDS:
            break
        seen.add(base)
        if base in _SHELL_IF_EMPTY:
            seen.add("shell-if-empty")
        words = words[1:]
        values = _VALUE_FLAGS.get(base, set())
        while words and words[0].startswith("-") and words[0] != "-":
            flag = words[0]
            words = words[1:]
            name = flag.split("=", 1)[0]
            if base == "env" and (flag.startswith("-S") and len(flag) > 2 or flag.startswith("--split-string=")):
                seen.add("split:" + (flag[2:] if flag.startswith("-S") else flag.split("=", 1)[1]))
            if (base == "env" and name in ("-C", "--chdir") or base == "sudo" and name in ("-D", "--chdir")
                    or base == "env" and flag.startswith("-C") and len(flag) > 2):
                seen.add("chdir")
            if base in ("sudo", "doas") and (flag in ("--shell", "--login") or not flag.startswith("--") and
                                             set(flag[1:]) & set("si")):
                seen.add("shell-if-empty")
            if base == "xargs" and (name in ("-I", "-i", "--replace") or flag.startswith(("-I", "-i"))):
                seen.add("xargs-I")
            if flag in values and words:
                if base == "env" and flag in ("-S", "--split-string"):
                    seen.add("split:" + words[0])
                words = words[1:]
        if base == "timeout" and words and re.fullmatch(r"\d+(?:\.\d+)?[smhd]?", words[0]):
            words = words[1:]
        if base == "chrt" and words and words[0].isdigit():
            words = words[1:]
        if base == "taskset" and words and re.fullmatch(r"(?:0x)?[0-9a-fA-F,-]+", words[0]):
            words = words[1:]
        if base == "chroot" and words:      # NEWROOT
            words = words[1:]
        if base == "env":
            continue
    return words, seen, assigns


# ---- what a program is: its name, or the real name a path or a PATH lookup resolves to -----------------------

def _norm(word: str) -> str:
    """The program name of a word: its last path component, without a Windows .exe."""
    name = re.split(r"[/\\]", str(word))[-1]
    return name[:-4] if name.lower().endswith(".exe") else name


_FAMILY = re.compile(r"(?P<python>(?:python|pypy)[0-9.]*)|(?P<perl>perl[0-9.]*)|(?P<ruby>j?ruby[0-9.]*)|"
                     r"(?P<php>php[0-9.]*(?:-cli)?)|(?P<lua>lua(?:jit)?[0-9.]*)|(?P<tcl>(?:tclsh|wish)[0-9.]*|jimsh)|"
                     r"(?P<node>node(?:js)?)|(?P<expect>expect)|(?P<osascript>osascript)|(?P<awk>[gmn]?awk)|"
                     r"(?P<sed>g?sed)|(?P<vim>g?vim?|nvim|ex|view|rvim)|(?P<make>g?make)|(?P<deno>deno)|(?P<bun>bun)")


def _family(name: str) -> str | None:
    """`sh` for a POSIX-like shell, `shx` for another shell, the interpreter's family, or None."""
    if name in _POSIX_SHELLS:
        return "sh"
    if name in _OTHER_SHELLS:
        return "shx"
    m = _FAMILY.fullmatch(name)
    return m.lastgroup if m else None


_NATIVE_MAGIC = (b"\x7fELF", b"MZ", b"\xfe\xed\xfa\xce", b"\xfe\xed\xfa\xcf", b"\xce\xfa\xed\xfe", b"\xcf\xfa\xed\xfe",
                 b"\xca\xfe\xba\xbe")


def _is_native(path: str) -> bool:
    """Whether `path` is a compiled program (ELF, Mach-O, PE), not a script a shell or interpreter reads."""
    try:
        with open(path, "rb") as f:
            head = f.read(4)
    except OSError:
        return False
    return any(head.startswith(m) for m in _NATIVE_MAGIC)


def _effective_prog(word: str) -> str:
    """The program name `word` runs as: its own name when that names a shell or interpreter; else, for a bare name
    found on PATH or an absolute path, the real name of the compiled program it resolves to when THAT is a shell
    or interpreter (rbash -> bash, a symlink `mysh` -> dash). A relative path is resolved by the caller, which
    knows the directory (launch_command_refusal)."""
    base = _norm(word)
    if _family(base) is not None:
        return base
    try:
        path = os.path.expanduser(str(word))
        if "/" not in path and "\\" not in path:
            path = shutil.which(path) or ""
        elif not os.path.isabs(path):
            return base
        real = os.path.realpath(path) if path else ""
        if real and _family(_norm(real)) is not None and _is_native(real):
            return _norm(real)
    except Exception:
        pass
    return base


def _is_path(word: str) -> bool:
    return "/" in word or (os.sep == "\\" and "\\" in word)


# ---- the code a command runs through its arguments ---------------------------------------------------------

def _bad(why: str):
    return ("bad", why)


def _code(word, what="code"):
    if word is None:
        return _bad(f"{what} is missing")
    return ("code", str(word)) if not _dyn(word) else _bad(f"{what} given by a variable or $(...) cannot be checked")


def _text(word, what="the -c command"):
    if word is None:
        return _bad(f"{what} is missing")
    return ("t", str(word)) if not _dyn(word) else _bad(f"{what} given by a variable or $(...) cannot be checked")


def _file(word, what="a script"):
    if word is None:
        return _bad(f"{what} is missing")
    if _dyn(word):
        return _bad(f"{what} named by a variable or $(...) cannot be checked")
    if word in ("-", "/dev/stdin") or str(word).startswith(("/dev/fd/", "/proc/self/fd/", "/dev/stdin")):
        return _bad(f"{what} read from stdin cannot be checked")
    return ("file", word)


_READS = "reads its code from stdin (a pipe, here-string or heredoc)"
_STDIN = f"it {_READS}, which cannot be checked"


def _shell_items(family: str, args: list[str]):
    """What a shell runs: the -c literal (parsed and walked as shell text; scanned as code for a non-POSIX shell), a
    script file (the file rule), --rcfile/--init-file files; anything else reads stdin and cannot be checked."""
    out, has_c, stdin, i, n = [], False, False, 0, len(args)
    as_text = _text if family == "sh" else _code
    while i < n:
        a = args[i]
        if a in ("--", "-"):
            i += 1
            break
        if a.startswith("--"):
            name, eq, val = a.partition("=")
            value = _w(val, _dyn(a)) if eq else (args[i + 1] if i + 1 < n else None)
            step = 1 if eq or name not in ("--rcfile", "--init-file", "--command", "--init-command") else 2
            if name in ("--rcfile", "--init-file"):
                out.append(_file(value, "a shell rc file"))
            elif name == "--command":           # fish
                return out + [as_text(value)]
            elif name == "--init-command":      # fish
                out.append(as_text(value))
            i += step
            continue
        if a[:1] in ("-", "+") and len(a) > 1:
            takes = 0
            for ch in a[1:]:
                if ch == "c":
                    has_c = True
                elif ch in "oO":
                    takes += 1
                elif ch == "s":
                    stdin = True
                elif ch == "i":
                    return out + [_bad("an interactive shell reads its rc files and stdin, which cannot be checked")]
            i += 1 + takes
            continue
        break
    rest = args[i:]
    if stdin:
        return out + [_bad("a shell with -s " + _STDIN)]
    if has_c:
        return out + [as_text(rest[0] if rest else None)]
    if rest:
        return out + [_file(rest[0])]
    return out + [_bad("a shell with no -c " + _STDIN)]


def _pwsh_items(prog: str, args: list[str]):
    """PowerShell: -Command (all the rest) is scanned, -EncodedCommand cannot be, -File is the file rule."""
    i, n = 0, len(args)
    while i < n:
        a = args[i]
        low = a.lower()
        if not low.startswith(("-", "/")):
            if prog == "pwsh":
                return [_file(a)]
            rest = args[i:]
            return [_bad("a command given by a variable cannot be checked")] if any(map(_dyn, rest)) else \
                [("code", " ".join(rest))]
        name = low.lstrip("-/").split(":", 1)[0]
        if name in ("e", "ec") or (len(name) >= 3 and "encodedcommand".startswith(name)):
            return [_bad("an encoded PowerShell command cannot be checked")]
        if name == "c" or (len(name) >= 2 and "command".startswith(name)):
            rest = args[i + 1:]
            if not rest or rest[0] == "-":
                return [_bad("PowerShell " + _STDIN)]
            return [_bad("a command given by a variable cannot be checked")] if any(map(_dyn, rest)) else \
                [("code", " ".join(rest))]
        if name == "f" or (len(name) >= 2 and "file".startswith(name)):
            return [_file(args[i + 1] if i + 1 < n else None)]
        if name in ("v", "version", "h", "help", "?"):
            return []
        takes = name in ("ex", "ep", "executionpolicy", "wd", "workingdirectory", "o", "of", "outputformat", "if",
                         "inputformat", "w", "windowstyle", "configurationname", "settingsfile", "custompipename")
        i += 2 if takes else 1
    return [_bad("PowerShell with no -Command: " + _STDIN)]


# family: code letters, code long options, file letters, file long options, value letters (take an argument),
# value long options, stop letters/long options (a module or a mode that runs no code from the command line),
# attach letters (consume the rest of their bundle), bad letters (interactive), info arguments (print and exit),
# positional ("file": a script path, "code": the program text, "none": not looked at), code_ends (options end
# after the code).
_INTERPRETERS = {
    "python": dict(code="c", vals="WX", stop="m", bad="i", info={"-V", "-VV", "--version", "-h", "--help", "-?"},
                   pos="file", code_ends=True),
    "perl": dict(code="eE", attach="IMmxiCdD", digits="l0", info={"-v", "-V", "--version", "-h", "--help"},
                 pos="file"),
    "ruby": dict(code="e", vals="IrCE", attach="xFTWK", digits="0", info={"-v", "--version", "-h", "--help"},
                 vlong={"--encoding", "--external-encoding", "--internal-encoding", "--enable", "--disable", "--dump"},
                 pos="file"),
    "node": dict(code="ep", clong={"--eval", "--print"}, vals="rC",
                 vlong={"--require", "--import", "--loader", "--experimental-loader", "--input-type", "--conditions",
                        "--env-file", "--title"},
                 slong={"--test"}, info={"-v", "--version", "-h", "--help", "--v8-options"}, pos="file",
                 code_ends=True),
    "php": dict(code="rBRE", files="fF", vals="dczt", stop="lsS", bad="a",
                info={"-v", "--version", "-h", "--help", "-i", "-m", "--ini"}, pos="file"),
    "lua": dict(code="e", vals="l", bad="i", info={"-v"}, pos="file"),
    "tcl": dict(vlong={"-encoding"}, pos="file"),
    "expect": dict(code="c", files="f", vals="D", bad="i", info={"-v"}, pos="file"),
    "osascript": dict(code="e", vals="ls", pos="file"),
    "awk": dict(code="e", clong={"--source"}, files="fi", flong={"--file", "--include"}, vals="Fvl",
                vlong={"--field-separator", "--assign", "--load"}, info={"--version", "-V", "--help"}, pos="code"),
    "sed": dict(code="e", clong={"--expression"}, files="f", flong={"--file"}, vals="l", vlong={"--line-length"},
                attach="i", info={"--version", "--help"}, pos="code"),
    "bun": dict(code="e", clong={"--eval", "--print"}, pos="none"),
}


def _interp_items(family: str, args: list[str]):
    """What an interpreter runs from its command line: its literal code (scanned), its script file (the file
    rule), or nothing it can be checked by (stdin)."""
    spec = _INTERPRETERS[family]
    code, files, vals = spec.get("code", ""), spec.get("files", ""), spec.get("vals", "")
    out, given, info, i, n = [], False, False, 0, len(args)
    while i < n:
        a = args[i]
        if a in spec.get("info", ()):
            info = True
            i += 1
            continue
        if a == "--":
            i += 1
            break
        if a.startswith("--") or (a.startswith("-") and a.split("=", 1)[0] in spec.get("vlong", ())):
            name, eq, val = a.partition("=")
            value = _w(val, _dyn(a)) if eq else (args[i + 1] if i + 1 < n else None)
            if name in spec.get("clong", ()):
                out.append(_code(value))
                given = True
                if spec.get("code_ends"):
                    return out
            elif name in spec.get("flong", ()):
                out.append(_file(value))
                given = True
            elif name in spec.get("slong", ()):
                return out
            elif name not in spec.get("vlong", ()):
                i += 1
                continue
            i += 1 if eq else 2
            continue
        if a.startswith("-") and len(a) > 1:
            letters, j, step, ended = a[1:], 0, 1, False
            while j < len(letters):
                ch, rest = letters[j], letters[j + 1:]
                if ch in code or ch in files or ch in vals:
                    value = _w(rest, _dyn(a)) if rest else (args[i + 1] if i + 1 < n else None)
                    step = 1 if rest else 2
                    if ch in code:
                        out.append(_code(value))
                        given = True
                        ended = bool(spec.get("code_ends"))
                    elif ch in files:
                        out.append(_file(value))
                        given = True
                    break
                if ch in spec.get("stop", ""):
                    return out
                if ch in spec.get("bad", ""):
                    return out + [_bad(f"an interactive {family} reads stdin, which cannot be checked")]
                if ch in spec.get("attach", ""):
                    break
                j += 1
                if ch in spec.get("digits", ""):
                    while j < len(letters) and letters[j] in "0123456789abcdefABCDEFx":
                        j += 1
            i += step
            if ended:
                return out
            continue
        break
    rest = args[i:]
    if given or spec["pos"] == "none":
        return out
    if rest:
        return out + [_code(rest[0], f"the {family} program") if spec["pos"] == "code" else _file(rest[0])]
    if info:
        return out
    return out + [_bad(f"{family} with no code argument: {_STDIN}")]


def _vim_items(prog: str, args: list[str]):
    out, ex, cmds, i, n = [], prog == "ex", False, 0, len(args)
    while i < n:
        a = args[i]
        nxt = args[i + 1] if i + 1 < n else None
        if a in ("-c", "--cmd"):
            out.append(_code(nxt))
            cmds = True
            i += 2
            continue
        if a.startswith("+") and len(a) > 1:
            out.append(_code(_w(a[1:], _dyn(a))))
            cmds = True
        elif a in ("-e", "-E", "-es", "-Es"):
            ex = True
        elif a in ("-u", "-U") and nxt is not None:
            if nxt not in ("NONE", "NORC", "DEFAULTS"):
                out.append(_file(nxt, "a vim rc file"))
            i += 2
            continue
        elif a == "-S" or (a == "-s" and not ex and nxt is not None and not nxt.startswith("-")):
            out.append(_file(nxt or "Session.vim", "a vim script"))
            i += 2
            continue
        i += 1
    if ex and not cmds:
        out.append(_bad("vim in ex mode with no -c " + _STDIN))
    return out


def _make_items(args: list[str]):
    out, i, n = [], 0, len(args)
    while i < n:
        a = args[i]
        name, eq, val = a.partition("=")
        if name in ("--file", "--makefile") or a == "-f" or (a.startswith("-f") and len(a) > 2):
            value = _w(val, _dyn(a)) if eq else (_w(a[2:], _dyn(a)) if a.startswith("-f") and len(a) > 2
                                                  else (args[i + 1] if i + 1 < n else None))
            out.append(_file(value, "a makefile"))
            i += 1 if eq or (a.startswith("-f") and len(a) > 2) else 2
            continue
        if name == "--eval" or a == "-E":
            out.append(_code(_w(val, _dyn(a)) if eq else (args[i + 1] if i + 1 < n else None)))
            i += 1 if eq else 2
            continue
        i += 1
    return out


def _script_arg(prog: str, args: list[str]) -> str | None:
    """The command string `script -c STR`, `script --command=STR`, `su -c STR` or `runuser -c STR` runs, or None."""
    for k, a in enumerate(args):
        if a.startswith("--command="):
            return _w(a[len("--command="):], _dyn(a))
        if a in ("--command", "-c") and k + 1 < len(args):
            return args[k + 1]
        if re.fullmatch(r"-[A-Za-z]*c[A-Za-z]*", a) and k + 1 < len(args):
            return args[k + 1]
    return None


_FIND_EXEC = ("-exec", "-execdir", "-ok", "-okdir")
_SYSTEMD_VALUE = {"-p", "-u", "-E", "-t", "--unit", "--property", "--setenv", "--description", "--slice", "--uid", "--gid",
                  "--working-directory", "-d", "--service-type", "--on-active", "--on-calendar"}
_SSH_VALUE = {"-p", "-i", "-l", "-o", "-F", "-J", "-L", "-R", "-D", "-b", "-c", "-e", "-m", "-S", "-w", "-E", "-B",
              "-O", "-Q", "-W", "-I", "-P"}
_SSH_CODE_OPTIONS = ("proxycommand", "localcommand", "knownhostscommand", "remotecommand")
_TMUX_RUN = ("new", "new-session", "new-window", "neww", "split-window", "splitw", "run-shell", "run", "respawn-pane",
             "respawnp", "respawn-window", "respawnw", "pipe-pane", "pipep", "display-popup", "popup", "if-shell", "if")
_TMUX_SEND = ("send-keys", "send", "paste-buffer", "pasteb", "load-buffer", "loadb", "set-buffer", "setb",
              "command-prompt", "confirm-before", "confirm")
_TMUX_VALUES = {"display-popup": "bcdehsStTwxy", "popup": "bcdehsStTwxy", "run-shell": "cdt", "run": "cdt"}
_GIT_RUNNER_OPTS = {"--upload-pack", "-u", "--receive-pack", "--exec"}
_GIT_FILTERS = ("--tree-filter", "--index-filter", "--msg-filter", "--env-filter", "--commit-filter",
                "--parent-filter", "--tag-name-filter")


def _ssh_option(opt: str):
    parts = re.split(r"[=\s]+", str(opt).strip(), maxsplit=1)
    key, value = parts[0].lower(), (parts[1] if len(parts) > 1 else "")
    if key in _SSH_CODE_OPTIONS and value.strip().lower() not in ("", "none"):
        return [_text(_w(value.strip(), _dyn(opt)), f"ssh {key}")]
    if key == "include" and value.strip():
        return [_file(_w(value.strip(), _dyn(opt)), "an ssh config file")]
    return []


def _ssh_items(prog: str, args: list[str]):
    out, k, n, quiet = [], 0, len(args), False
    while k < n and args[k].startswith("-"):
        a = args[k]
        if a.startswith("-o"):
            opt = a[2:] if len(a) > 2 else (args[k + 1] if k + 1 < n else "")
            out.extend(_ssh_option(_w(opt, _dyn(a) if len(a) > 2 else _dyn(opt))))
            k += 1 if len(a) > 2 else 2
            continue
        if a.startswith("-F"):
            cfg = _w(a[2:], _dyn(a)) if len(a) > 2 else (args[k + 1] if k + 1 < n else None)
            if cfg != "/dev/null":
                out.append(_file(cfg, "an ssh config file"))
            k += 1 if len(a) > 2 else 2
            continue
        if prog in ("scp", "sftp") and a == "-S" and k + 1 < n:
            out.append(("w", [args[k + 1]]))
            k += 2
            continue
        if prog == "sftp" and a == "-b" and k + 1 < n:
            out.append(_file(args[k + 1], "an sftp batch file"))
            k += 2
            continue
        quiet = quiet or (not a.startswith("--") and bool(set(a[1:]) & set("NWOGQV")))
        k += 2 if a in _SSH_VALUE else 1
    if prog != "ssh":
        return out
    rest = args[k + 1:]
    if rest:
        if any(map(_dyn, rest)):
            return out + [_bad("a remote command given by a variable cannot be checked")]
        return out + [("t", " ".join(rest))]
    if k < n and not quiet:
        return out + [_bad(f"ssh with no remote command runs a shell that {_READS}")]
    return out


def _tmux_items(args: list[str]):
    out, i, n = [], 0, len(args)
    while i < n and args[i].startswith("-"):
        a = args[i]
        if a == "-f" and i + 1 < n:
            out.append(_file(args[i + 1], "a tmux config file"))
        elif a == "-c" and i + 1 < n:
            out.append(_text(args[i + 1]))
        i += 2 if a in ("-L", "-S", "-f", "-c", "-T") else 1
    segments, seg = [], []
    for a in args[i:]:
        if a == ";":
            segments.append(seg)
            seg = []
        else:
            seg.append(a)
    segments.append(seg)
    for seg in segments:
        if not seg:
            continue
        sub, rest = seg[0], seg[1:]
        if sub in _TMUX_SEND:
            return out + [_bad(f"tmux {sub} types or pastes text into a pane: it cannot be checked")]
        if sub in ("source-file", "source"):
            out.extend(_file(a, "a tmux config file") for a in rest if not a.startswith("-"))
            continue
        if sub not in _TMUX_RUN:
            continue
        values, k = _TMUX_VALUES.get(sub, "cefFlnstxy"), 0
        while k < len(rest) and rest[k].startswith("-") and rest[k] != "-":
            k += 2 if rest[k][-1] in values and len(rest[k]) == 2 else 1
        pos = rest[k:]
        if sub in ("if-shell", "if"):
            out.extend(_text(p) for p in pos)
        elif pos:
            out.append(_bad("a tmux command given by a variable cannot be checked") if any(map(_dyn, pos))
                       else ("t", " ".join(pos)))
    return out


def _screen_items(args: list[str]):
    out, k, n = [], 0, len(args)
    while k < n and args[k].startswith("-"):
        a = args[k]
        if a in ("-ls", "-list", "-v", "-version", "-wipe", "-help", "--version", "--help"):
            return []
        if a == "-X":
            return [_bad("screen -X sends commands into a session: they cannot be checked")]
        if a == "-s":
            return [_bad("screen -s sets the shell of its windows: it cannot be checked")]
        last = a[-1]
        if a == "-c" or (len(a) > 2 and last == "c"):
            out.append(_file(args[k + 1] if k + 1 < n else None, "a screen config file"))
            k += 2
            continue
        k += 2 if (last in "SptTeh" or a in ("-Logfile",)) else 1
    if k >= n:
        return out + [_bad("screen with no command runs an interactive shell")]
    return out + [("w", args[k:])]


_ENV_SHELL = frozenset(("GIT_EDITOR", "GIT_SEQUENCE_EDITOR", "GIT_PAGER", "PAGER", "MANPAGER", "EDITOR", "VISUAL",
                        "GIT_EXTERNAL_DIFF", "GIT_ASKPASS", "SSH_ASKPASS", "GIT_PROXY_COMMAND", "PROMPT_COMMAND", "PS4",
                        "LESSOPEN", "LESSCLOSE", "BROWSER"))
_ENV_SSH = frozenset(("GIT_SSH_COMMAND", "RSYNC_RSH"))
_ENV_PROGRAM = frozenset(("GIT_SSH",))
_ENV_REFUSED = re.compile(r"BASH_ENV|ENV|ZDOTDIR|LD_PRELOAD|LD_AUDIT|DYLD_INSERT_LIBRARIES|GIT_EXEC_PATH|"
                          r"GIT_TEMPLATE_DIR|PERL5OPT|RUBYOPT|PYTHONSTARTUP|SHELLOPTS|BASHOPTS|BASH_FUNC_.*")
# where git and gh read their configuration (a launch-written .gitconfig with a push refspec, an alias, a hook path)
_ENV_CONFIG = re.compile(r"HOME|XDG_CONFIG_HOME|GIT_DIR|GIT_WORK_TREE|GIT_COMMON_DIR|GIT_INDEX_FILE|GH_CONFIG_DIR")
_NODE_CODE = re.compile(r"(?:^|\s)(?:-r|--require|--import|--loader|--experimental-loader|-e|--eval|-p|--print)\b")
_SSH_TAIL = " swarm-host true"     # git and rsync append the host and command to an ssh command


def _assignment_items(name: str, value):
    """What an assignment of `name` hands a program as code: the commands of GIT_EDITOR, GIT_PAGER, PAGER, EDITOR,
    GIT_SSH_COMMAND, GIT_EXTERNAL_DIFF, ... (git, ssh and less run them through a shell); the program of GIT_SSH;
    and a refusal for what loads code no command line shows (BASH_ENV, LD_PRELOAD, BASH_FUNC_*, ...)."""
    if _ENV_REFUSED.fullmatch(name) or (name == "NODE_OPTIONS" and _NODE_CODE.search(str(value))):
        return [_bad(f"setting {name} loads code that cannot be checked")]
    if _ENV_CONFIG.fullmatch(name):
        return [_bad(f"setting {name} moves where git and gh read their configuration")]
    if name == "PATH":
        why = _path_value_refusal(value)
        return [_bad(why)] if why else []
    if name in _ENV_SHELL or name in _ENV_SSH:
        if not str(value).strip():
            return []
        if _dyn(value):
            return [_bad(f"{name} given by a variable or $(...) cannot be checked")]
        return [("t", str(value).lstrip("!") + (_SSH_TAIL if name in _ENV_SSH else ""))]
    if name in _ENV_PROGRAM and str(value).strip():
        return [("w", [value])]
    return []


_GIT_CMD_KEY = re.compile(r"(?i)(?:pager|editor|command|cmd|textconv|driver|helper|program|askpass|proxy|hook|"
                          r"hookspath|templatedir)$|(?:^|\.)(?:external|clean|smudge|process|smtpserver|uploadpack|"
                          r"receivepack|browser|fsmonitor)$|^pager\.")


def _git_globals(args: list[str]):
    """The (key, value, how) of git's global `-c key=value` (how "c") and `--config-env key=VAR` (how "env")."""
    out, i = [], 0
    while i < len(args) and args[i].startswith("-"):
        opt = args[i]
        name = opt.split("=", 1)[0]
        if name == "-c" and i + 1 < len(args):
            key, _eq, value = args[i + 1].partition("=")
            out.append((key, _w(value, _dyn(args[i + 1])), "c"))
        elif name == "--config-env":
            val = opt.split("=", 1)[1] if "=" in opt else (args[i + 1] if i + 1 < len(args) else "")
            out.append((val.split("=", 1)[0], "", "env"))
        i += 2 if (name in _GIT_VALUE_OPTS and "=" not in opt) else 1
    return out


def _git_items(args: list[str]):
    out = []
    for key, value, how in _git_globals(args):
        if _GIT_CMD_KEY.search(key):
            if how == "env":
                out.append(_bad(f"git --config-env {key} (a command from the environment) cannot be checked"))
            elif str(value).strip():
                out.append(_text(_w(str(value).lstrip("!") + (_SSH_TAIL if key.lower().endswith("sshcommand") else ""),
                                    _dyn(value)), f"git -c {key}"))
    sub, rest, _bad_why, _cdir = _git_args(args)
    if sub == "rebase":
        for k, a in enumerate(rest):
            if a in ("-x", "--exec") and k + 1 < len(rest):
                out.append(_text(rest[k + 1], "git rebase --exec"))
            elif a.startswith("--exec="):
                out.append(_text(_w(a[len("--exec="):], _dyn(a)), "git rebase --exec"))
            elif a.startswith("-x") and len(a) > 2 and not a.startswith("--"):
                out.append(_text(_w(a[2:], _dyn(a)), "git rebase --exec"))
    elif sub == "submodule" and "foreach" in rest:
        tail = rest[rest.index("foreach") + 1:]
        tail = [a for a in tail if not a.startswith("--")]
        out.append(_bad("git submodule foreach given a variable cannot be checked") if any(map(_dyn, tail))
                   else ("t", " ".join(tail)))
    elif sub == "bisect" and rest[:1] == ["run"]:
        out.append(("w", rest[1:]))
    elif sub == "difftool":
        for k, a in enumerate(rest):
            if a in ("-x", "--extcmd") and k + 1 < len(rest):
                out.append(_text(rest[k + 1], "git difftool --extcmd"))
            elif a.startswith("--extcmd="):
                out.append(_text(_w(a[len("--extcmd="):], _dyn(a)), "git difftool --extcmd"))
    elif sub == "filter-branch":
        for k, a in enumerate(rest):
            name, eq, val = a.partition("=")
            if name in _GIT_FILTERS:
                out.append(_text(_w(val, _dyn(a)) if eq else (rest[k + 1] if k + 1 < len(rest) else None), name))
    if sub in ("fetch", "clone", "pull", "ls-remote", "archive", "push", "send-pack", "remote"):
        for k, a in enumerate(rest):
            name, eq, val = a.partition("=")
            if name in _GIT_RUNNER_OPTS and not (name == "-u" and sub not in ("clone", "fetch", "pull", "ls-remote")):
                out.append(_text(_w(val, _dyn(a)) if eq else (rest[k + 1] if k + 1 < len(rest) else None), name))
            if str(a).startswith("ext::"):
                out.append(_bad("a git ext:: remote runs a command"))
    return out


def _parallel_items(args: list[str]):
    k = 0
    while k < len(args) and args[k].startswith("-"):
        k += 2 if (k + 1 < len(args) and re.fullmatch(r"\d+%?|\S*\{\}\S*", args[k + 1]) and "=" not in args[k]) else 1
    cmd, extra, sep = [], [], False
    for w in args[k:]:
        if w.startswith(":::"):
            sep = True
            continue
        (extra if sep else cmd).append(w)
    if not cmd:
        return [_bad("parallel with no command runs its input as commands")]
    return [("xt", " ".join(cmd + extra))]


def _cmd_items(args: list[str]):
    """cmd.exe (spec 3.11 [T21], the scanned-literal rule on Windows): what follows /C, /K or /R (all the rest,
    also glued as `/cdir`) is the command. Cmd's outer quotes are dropped as cmd does, then it is walked as
    shell text (cmd's & && || | separate commands as in sh) AND scanned as code. A %VAR% or !VAR! (expanded by
    cmd before it runs) or a variable cannot be checked; with no /C it reads stdin, which cannot be checked."""
    for i, a in enumerate(args):
        low = str(a).lower()
        if low[:2] in ("/c", "/k", "/r"):
            rest = ([a[2:]] if len(a) > 2 else []) + list(args[i + 1:])
            if not rest:
                return [_bad("cmd " + _STDIN)]
            if any(map(_dyn, rest)):
                return [_bad("a command given by a variable cannot be checked")]
            text = " ".join(str(w) for w in rest).strip()
            if len(text) >= 2 and text[0] == text[-1] == '"':
                text = text[1:-1]
            if re.search(r"%[^%\s]+%|![^!\s]+!", text):
                return [_bad("a cmd variable (%VAR% or !VAR!) is expanded before it runs and cannot be checked")]
            return [("t", text), ("code", text)]
        if low.startswith("/"):
            continue                             # /d /q /s /a /u /e:on /f:off /v:on /t:fg
        return [_bad("cmd with no /C " + _STDIN)]
    return [_bad("cmd with no /C: " + _STDIN)]


def _nested(prog: str, args: list[str]):
    """The commands a command runs through its arguments: ("t", text) shell text to walk; ("w", words) a command;
    ("xt", text) shell text whose commands get appended arguments (parallel); ("code", text) interpreter code to
    scan; ("file", path) a script file; ("assign", (name, value)); ("bad", why) what cannot be checked."""
    if prog.lower() == "cmd":
        return _cmd_items(args)
    family = _family(prog)
    if family == "sh":
        return _shell_items(family, args)
    if family == "shx":
        return _pwsh_items(prog, args) if prog in ("pwsh", "powershell") else _shell_items(family, args)
    if family in _INTERPRETERS:
        return _interp_items(family, args)
    if family == "vim":
        return _vim_items(prog, args)
    if family == "make":
        return _make_items(args)
    if family == "deno":
        return [_code(args[1] if len(args) > 1 else None)] if args[:1] == ["eval"] else []
    if prog in ("script", "su", "runuser"):
        inner = _script_arg(prog, args)
        if inner is not None:
            return [_text(inner)]
        if prog == "runuser" and "--" in args:
            return [("w", args[args.index("--") + 1:])]
        if prog == "script" and any(a in ("-V", "--version", "-h", "--help") for a in args):
            return []
        return [_bad(f"{prog} with no -c runs a shell that {_READS}")]
    if prog == "sg":
        pos = [a for a in args if a not in ("-", "-c")]
        return [_text(pos[1])] if len(pos) > 1 else [_bad("sg with no command runs an interactive shell")]
    if prog in ("ed", "red"):
        return [] if any(a in ("--version", "--help", "-V", "-h") for a in args) else \
            [_bad(f"{prog} {_READS}: its commands cannot be checked")]
    if prog == "newgrp":
        return [_bad("newgrp runs an interactive shell")]
    if prog == "eval":
        if any(map(_dyn, args)):
            return [_bad("eval of a variable or $(...) cannot be checked")]
        return [("t", " ".join(args))]
    if prog == "trap":
        rest = args[1:] if args[:1] == ["--"] else args
        if rest and not rest[0].startswith("-"):
            return [_text(rest[0], "a trap command")]
        return []
    if prog == "alias":
        return [_text(_w(a.split("=", 1)[1], _dyn(a)), "an alias") for a in args if "=" in a]
    if prog in ("export", "declare", "typeset", "local", "readonly"):
        return [("assign", (a.split("=", 1)[0], _w(a.split("=", 1)[1], _dyn(a)))) for a in args
                if not a.startswith("-") and _ASSIGN.match(a)]
    if prog in ("source", "."):
        return [_file(args[0] if args else None, "a sourced file")]
    if prog == "find":
        out, k = [], 0
        while k < len(args):
            if args[k] in _FIND_EXEC:
                j = k + 1
                while j < len(args) and args[j] not in (";", "+"):
                    j += 1
                out.append(("w", args[k + 1:j]))
                k = j
            k += 1
        return out
    if prog == "git":
        return _git_items(args)
    if prog == "parallel":
        return _parallel_items(args)
    if prog == "flock":
        words = [a for a in args if not a.startswith("-")]
        return [("w", words[1:])]
    if prog == "systemd-run":
        k = 0
        while k < len(args) and args[k].startswith("-"):
            k += 2 if args[k] in _SYSTEMD_VALUE else 1
        return [("w", args[k:])]
    if prog == "tmux":
        return _tmux_items(args)
    if prog == "screen":
        return _screen_items(args)
    if prog in ("ssh", "scp", "sftp"):
        return _ssh_items(prog, args)
    if prog == "rsync":
        out = []
        for k, a in enumerate(args):
            name, eq, val = a.partition("=")
            if name in ("-e", "--rsh"):
                value = _w(val, _dyn(a)) if eq else (args[k + 1] if k + 1 < len(args) else None)
                out.append(_text(_w(str(value) + _SSH_TAIL, _dyn(value)) if value is not None else None, "rsync -e"))
        return out
    if prog == "swarm":
        k = 0
        while k < len(args) and args[k].startswith("-"):
            k += 1 if "=" in args[k] else 2
        if args[k:k + 1] == ["bg"] and "--" in args:
            tail = args[args.index("--") + 1:]
            return [_bad("swarm bg given a variable cannot be checked")] if any(map(_dyn, tail)) else \
                [("t", " ".join(tail))] if tail else []
    return []


def _runs_code(prog: str, args: list[str], seen: set[str]) -> bool:
    """Whether a program under xargs or parallel would run code from their input: any shell, interpreter, eval or
    source; sed and awk only when the input can land in their program (a replacement string)."""
    family = _family(prog)
    if prog in ("eval", "source", ".", "trap", "alias"):
        return True
    if family is None or family in ("make",):
        return False
    if family in ("sed", "awk"):
        return "xargs-I" in seen or any("{}" in a for a in args)
    return True


def _walk(lists, depth: int):
    if depth > _MAX_NEST:      # too deep to look at: callers refuse what they cannot check
        yield TOO_DEEP, [], set()
        return
    for words in lists:
        words, seen, assigns = _unwrap(words)
        for name, value in assigns:
            yield from _items(_assignment_items(name, value), depth)
        for tag in sorted(seen):
            if tag.startswith("split:"):
                yield from iter_commands(tag[6:], depth + 1)
        if not words:
            if "shell-if-empty" in seen:
                yield STDIN_SHELL, ["a wrapper with no command runs an interactive shell"], seen
            continue
        if "watch" in seen:       # watch runs its words as one command line
            yield from iter_commands(" ".join(words), depth + 1)
            continue
        w0 = words[0]
        if _dyn(w0) or "{}" in w0:
            yield STDIN_SHELL, [f"the program {str(w0)!r} is a variable, $(...), glob or brace expansion"], seen
            continue
        prog, args = _effective_prog(w0), words[1:]
        if "xargs" in seen and _runs_code(prog, args, seen):
            yield STDIN_SHELL, [f"{prog} under xargs or parallel runs code from their input"], seen
            continue
        yield from _items(_nested(prog, args), depth)
        if prog in ("cd", "pushd", "popd") and depth == 0:
            seen = seen | {"depth0"}
        yield prog, args, seen | {"argv0:" + w0}


def _items(items, depth: int):
    for kind, payload in items:
        if kind == "bad":
            yield STDIN_SHELL, [payload] if payload else [], set()
        elif kind == "t":
            yield from iter_commands(payload, depth + 1)
        elif kind == "xt":
            if depth + 1 > _MAX_NEST:
                yield TOO_DEEP, [], set()
            else:
                yield from _walk([["xargs", "-I", "{}", *w] for w in split_commands(payload)], depth + 1)
        elif kind == "code":
            yield CODE, [payload], set()
        elif kind == "file":
            yield SCRIPT, [payload], set()
        elif kind == "assign":
            yield from _items(_assignment_items(*payload), depth)
        elif payload:
            yield from _walk([payload], depth + 1)


def iter_commands(command: str, _depth: int = 0):
    """Yield (prog, args, wrappers) for every command a shell text runs, looking inside everything that runs
    another command (_nested), `$(...)` and backticks. Code that cannot be checked is yielded as STDIN_SHELL
    (args: [why]), interpreter code as CODE (args: [code]) and a script file as SCRIPT (args: [path]). A path
    program carries `argv0:<word>` in its wrappers."""
    if command:
        yield from _walk(split_commands(command), _depth)


def _nested_texts(command: str, _depth: int = 0) -> list[str]:
    """`command` and every shell text a command in it runs, for a caller that scans text."""
    out = [command]
    if _depth > _MAX_NEST or not command:
        return out
    for words in split_commands(command):
        words, seen, assigns = _unwrap(words)
        texts = [t[6:] for t in sorted(seen) if t.startswith("split:")]
        items = [i for name, value in assigns for i in _assignment_items(name, value)]
        if words and "watch" in seen:
            texts.append(" ".join(words))
        elif words:
            items += _nested(_prog(words[0]), words[1:])
        for kind, payload in items:
            if kind in ("t", "xt"):
                texts.append(payload)
            elif kind == "w" and payload:
                texts.append(" ".join(payload))
        for t in texts:
            out.extend(_nested_texts(t, _depth + 1))
    return out


_nested_scripts = _nested_texts


_GIT_VALUE_OPTS = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path", "--config-env",
                   "--super-prefix", "--attr-source", "--list-cmds"}
_PUSH_VALUE_OPTS = {"-o", "--push-option", "--receive-pack", "--exec", "--repo", "--recurse-submodules",
                    "--signed"}
_PUSH_FORCE = ("--force", "--force-with-lease", "--force-if-includes")
_PUSH_REFUSED = {"--mirror": "a mirror push", "--all": "a push of every branch", "--branches": "a push of every branch",
                 "--delete": "a branch deletion", "--prune": "a pruning push"}
_GIT_WRITES = frozenset(("commit", "push", "reset", "checkout", "switch", "merge", "rebase", "apply", "am", "stash",
                         "restore", "clean", "rm", "mv", "tag", "cherry-pick", "revert", "pull", "init", "clone",
                         "add", "update-ref", "worktree", "gc", "prune", "filter-branch", "send-pack",
                         "fast-import", "replace", "notes", "submodule"))
_GIT_CONFIG_RISK = re.compile(r"(?i)^(?:alias\.|remote\.[^.]+\.push(?:url)?$|url\..*\.pushinsteadof$|push\.|"
                              r"core\.(?:sshcommand|hookspath|fsmonitor)$|protocol\.|include\.|includeif\.)")


def _git_args(args: list[str]):
    """(subcommand, its arguments, a reason to refuse, the -C directory) of a git argv: git's global options
    are skipped, the ones that take a value skip it, and `-c alias.*=` (or another setting that can hide a
    push) is refused."""
    i, cdir = 0, None
    while i < len(args) and args[i].startswith("-"):
        opt = args[i]
        name = opt.split("=", 1)[0]
        if name == "--config-env":
            val = opt.split("=", 1)[1] if "=" in opt else (args[i + 1] if i + 1 < len(args) else "")
            if _GIT_CONFIG_RISK.match(val.split("=", 1)[0]):
                return None, [], f"git --config-env {val.split('=', 1)[0]} (an alias or push setting) is refused", cdir
        if name == "-c" and i + 1 < len(args) and _GIT_CONFIG_RISK.match(args[i + 1].split("=", 1)[0]):
            return None, [], f"git -c {args[i + 1].split('=', 1)[0]} (an alias or push setting) is refused", cdir
        if name == "-C" and i + 1 < len(args):
            cdir = args[i + 1]
        if name in ("--git-dir", "--work-tree"):
            return None, [], f"git {name} (another repository and its configuration) is refused: use git -C <dir>", cdir
        i += 2 if (name in _GIT_VALUE_OPTS and "=" not in opt) else 1
    return (args[i] if i < len(args) else None), args[i + 1:], None, cdir


def _push_refusal(rest: list[str], targets: set[str], branch_of, cdir=None) -> str | None:
    """Why this `git push` argv (after `push`) is refused, or None. `branch_of(directory)` names the branch
    checked out there (None: unknown, so a push that names no branch is refused)."""
    positional: list[str] = []
    repo_flag = False
    i = 0
    while i < len(rest):
        a = rest[i]
        i += 1
        if a == "--":
            positional.extend(rest[i:])
            break
        if a.startswith("--"):
            name = a.split("=", 1)[0]
            if len(name) >= 3 and name not in _PUSH_FORCE and name not in _PUSH_REFUSED:
                hit = next((f for f in (*_PUSH_FORCE, *_PUSH_REFUSED) if f.startswith(name)), None)
                if hit:          # git takes any unique prefix of a long option: --mirr, --force-w, --delet
                    name = hit
            if name in _PUSH_FORCE:
                return "a force push is refused"
            if name in _PUSH_REFUSED:
                return f"{_PUSH_REFUSED[name]} ({name}) is refused: name the one branch to push"
            if name == "--repo":
                repo_flag = True
            if name in _PUSH_VALUE_OPTS and "=" not in a and name != "--signed" and name != "--recurse-submodules":
                i += 1
            continue
        if a.startswith("-") and len(a) > 1:
            letters = a[1:]
            if "f" in letters:
                return "a force push is refused"
            if "d" in letters:
                return "a branch deletion (-d) is refused"
            if "o" in letters and letters.endswith("o"):
                i += 1
            continue
        positional.append(a)
    specs = positional if repo_flag else positional[1:]
    if not specs:                       # `git push [remote]`: whatever is checked out goes
        branch = branch_of(cdir) if branch_of else None
        if not branch:
            return "a push that names no branch is refused (name it: git push origin <branch>)"
        if branch.lower() in targets:
            return f"a push of the checked-out target branch {branch!r} is refused"
        return None
    for spec in specs:
        if spec.startswith("+"):
            return "a force push (+refspec) is refused"
        if any(ch in spec for ch in "*?["):
            return "a push of a glob refspec is refused: name the one branch to push"
        src, colon, dst = spec.partition(":")
        if colon and not src:
            return "a branch deletion (:branch) is refused"
        if not colon:
            dst = src
        if dst in ("HEAD", "@", ""):
            branch = branch_of(cdir) if branch_of else None
            if not branch:
                return "a push of HEAD with no known branch is refused (name it: git push origin HEAD:<branch>)"
            dst = branch
        if any(ch in dst for ch in "{$\\"):
            return "a push destination with brace, $ or backslash syntax is refused: name the branch"
        for prefix in ("refs/heads/", "heads/", "refs/"):      # git resolves HEAD:heads/main to refs/heads/main
            if dst.startswith(prefix):
                dst = dst[len(prefix):]
                break
        if dst.lower() in targets:
            return f"a push to the target branch {dst!r} is refused"
    return None


def _subtree_push_refusal(rest: list[str], targets: set[str], branch_of, cdir=None) -> str | None:
    """`git subtree push -P <prefix> <repository> <refspec>`: the refspec is checked like a push's."""
    pos, k = [], 1
    while k < len(rest):
        a = rest[k]
        if a in ("-P", "--prefix", "-m", "--message", "--annotate", "-b", "--branch", "--onto"):
            k += 2
            continue
        if not a.startswith("-"):
            pos.append(a)
        k += 1
    if len(pos) < 2:
        return "a git subtree push that names no branch is refused"
    return _push_refusal([pos[0], pos[1]], targets, branch_of, cdir)


_GIT_ENV = re.compile(r"(?:export\s+)?GIT_CONFIG[A-Z_0-9]*=")
_GH_API_RISK = re.compile(r"(?:^|/)(?:git/(?:refs|commits|trees|tags)|merges|merge-upstream|pulls/\d+/merge)\b")
_GH_GRAPHQL_RISK = re.compile(r"mergePullRequest|updateRef|createRef|deleteRef|createCommitOnBranch|mergeBranch")
_CLAUDE_TOKEN = re.compile(r"(?:^|/)(?:claude|claude-code)(?:\.m?c?js)?$|@anthropic-ai/claude-code|"
                           r"claude(?:-code)?/(?:bin/)?(?:cli|index)\.m?c?js$")
_CODEX_TOKEN = re.compile(r"(?:^|/)codex(?:\.js)?$|@openai/codex")
_CODEX_VALUE_OPTS = {"-c", "--config", "-m", "--model", "-C", "--cd", "-s", "--sandbox", "-a", "--ask-for-approval",
                     "-p", "--profile", "-i", "--image", "--add-dir", "--local-provider", "--color",
                     "--output-last-message", "-o", "--output-schema", "--enable", "--disable"}
_CODEX_HEADLESS = ("exec", "e", "app-server", "proto", "p", "mcp-server", "cloud")
_LAUNCHERS = frozenset(("node", "nodejs", "npx", "bunx", "bun", "deno", "pnpm", "yarn", "npm", "pnpx", "tsx", "ts-node"))


def _claude_headless(args: list[str]) -> bool:
    for k, a in enumerate(args):
        if a == "--print" or a.startswith("--print="):
            return True
        if a.startswith("-") and not a.startswith("--") and len(a) > 1 and "p" in a[1:] and not a.startswith("-c="):
            return True
        if a == "mcp" and args[k + 1:k + 2] == ["serve"]:
            return True
    return False


def _codex_headless(args: list[str]) -> bool:
    i = 0
    while i < len(args):
        a = args[i]
        if a.startswith("-"):
            i += 2 if (a in _CODEX_VALUE_OPTS and "=" not in a) else 1
            continue
        return a in _CODEX_HEADLESS
    return False


def _launch_refusal(prog: str, args: list[str]) -> str | None:
    if _CLAUDE_TOKEN.search(prog) and _claude_headless(args):
        return "a headless claude session (claude -p, claude mcp serve) is refused"
    if _CODEX_TOKEN.search(prog) and _codex_headless(args):
        return "a headless codex session (codex exec, app-server, proto) is refused"
    if prog in _LAUNCHERS:
        for k, a in enumerate(args):
            if _CLAUDE_TOKEN.search(a):
                return "a headless claude session (claude -p, claude mcp serve) is refused" if _claude_headless(args[k + 1:]) else None
            if _CODEX_TOKEN.search(a):
                return ("a headless codex session (codex exec, app-server, proto) is refused"
                        if _codex_headless(args[k + 1:]) else None)
    return None


def _inline_refusal(text: str, targets: set[str]) -> str | None:
    """Why an inline script (python -c, node -e, awk, a script on stdin) is refused: it runs a git push that names a
    target branch or forces, or starts a headless claude or codex. A best-effort scan of the text, not a parse
    (the close-time audit is the backstop for what an agent hides in code, spec 4.0)."""
    if re.search(r"\bgit\b", text) and re.search(r"\bpush\b", text):
        if re.search(r"--force|--mirror|--all\b|(?<![\w-])-f\b|['\"]\+", text) or any(
                re.search(r"(?<![\w/.-])(?:refs/heads/)?" + re.escape(t) + r"(?![\w/.-])", text, re.I) for t in targets):
            return "an inline script that pushes to a target branch or forces a push is refused"
    if re.search(r"\bclaude\b", text) and re.search(r"(?<![\w-])(?:-p|--print)(?![\w-])", text):
        return "a headless claude session (claude -p, claude mcp serve) is refused"
    if re.search(r"\bcodex\b", text) and re.search(r"(?<![\w-])(?:exec|app-server|proto)(?![\w-])", text):
        return "a headless codex session (codex exec) is refused"
    return None


# ---- the file rule: a script runs only from the repository at HEAD ---------------------------------------------

def _git_out(cwd: str, *args: str) -> str | None:
    """git's stdout in `cwd`, or None. No hook, filter, fsmonitor or pager runs (plumbing only, no index read)."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0")
    try:
        r = subprocess.run(["git", "-c", "core.fsmonitor=false", "-C", cwd, *args], capture_output=True,
                           stdin=subprocess.DEVNULL, env=env, timeout=10, check=False)
    except Exception:
        return None
    return r.stdout.decode("utf-8", "replace") if r.returncode == 0 else None


def tracked_unmodified(path: str) -> bool:
    """Whether `path` (resolved) is a regular file tracked at HEAD in its repository with exactly the content at
    HEAD (`git diff --quiet HEAD -- <path>`, by blob hash, with no clean filter)."""
    try:
        real = os.path.realpath(path)
        if not os.path.isfile(real):
            return False
        top = _git_out(os.path.dirname(real) or ".", "rev-parse", "--show-toplevel")
        if not top or not top.strip():
            return False
        top = os.path.realpath(top.strip())
        rel = os.path.relpath(real, top).replace(os.sep, "/")
        if rel.startswith("../") or rel == "..":
            return False
        tree = _git_out(top, "ls-tree", "-z", "HEAD", "--", rel)
        if not tree:
            return False
        meta, _tab, name = tree.rstrip("\0").partition("\t")
        mode, kind, sha = (meta.split() + ["", "", ""])[:3]
        if name != rel or kind != "blob" or mode not in ("100644", "100755"):
            return False
        cur = _git_out(top, "hash-object", "--no-filters", "--", real)
        return bool(cur) and cur.strip() == sha
    except Exception:
        return False


def _real_home() -> str | None:
    try:
        import pwd
        return pwd.getpwuid(os.getuid()).pw_dir
    except Exception:
        return None


def _swarm_launchers() -> set[str]:
    """The swarm command given to agents (the plugin's bin/swarm) and the user launcher (in the account's real home
    too): trusted as programs."""
    try:
        from swarm import paths
        found = [str(p) for p in (paths.agent_bin(), paths.launcher_path())]
        home = _real_home()
        if home:
            found.append(os.path.join(home, ".local", "bin", "swarm"))
        return {os.path.realpath(p) for p in found} | {os.path.realpath(os.path.splitext(p)[0]) for p in found}
    except Exception:
        return set()


# programs a virtualenv's bin may not hold (or shadow on PATH): what the guard checks by name
_GUARDED_NAMES = _SHELLS | {"git", "gh", "ssh", "scp", "sftp", "rsync", "claude", "codex", "sudo", "doas", "env",
                            "xargs", "parallel", "at", "batch", "crontab", "swarm", "npx", "busybox", "toybox"}


def _venv_bin_ok(d: str) -> bool:
    """Whether directory `d` is a virtualenv's bin (Scripts on Windows): <venv>/pyvenv.cfg exists, and no file in it
    is named like a program the guard checks (git, gh, ssh, a shell, claude, codex, ...): a venv that shadows one is a
    launch-made trap, not a virtualenv."""
    try:
        d = os.path.abspath(d)
        if os.path.basename(d) not in ("bin", "Scripts") or not os.path.isfile(
                os.path.join(os.path.dirname(d), "pyvenv.cfg")):
            return False
        return not any(_norm(n) in _GUARDED_NAMES for n in os.listdir(d))
    except Exception:
        return False


def _venv_entry(path: str) -> bool:
    """Whether `path` is a virtualenv entry point: a file in a virtualenv's bin (_venv_bin_ok), such as
    `source .venv/bin/activate` or `.venv/bin/pytest`. Running the venv's code is what `python -m` does too."""
    try:
        return _venv_bin_ok(os.path.dirname(os.path.abspath(path)))
    except Exception:
        return False


def _path_value_refusal(value) -> str | None:
    """Why a PATH value is refused: every entry must be `$PATH`, a virtualenv's bin, or an absolute directory the launch
    cannot write (a launch-written ./bin/git must not shadow git)."""
    for entry in str(value).split(":"):
        if entry in ("$PATH", "${PATH}", ""):
            continue
        if "$" in entry or "`" in entry:
            return "a PATH entry given by a variable or $(...) cannot be checked"
        d = os.path.normpath(os.path.expanduser(entry))
        if not os.path.isabs(d):
            return f"the PATH entry {entry!r} is relative: a program there could be one the launch wrote"
        if _venv_bin_ok(d):
            continue
        if not os.path.isdir(d) or os.access(d, os.W_OK):
            return f"the PATH entry {entry!r} is a directory the launch can write: a program there could be one it wrote"
    return None


def _bare_program_refusal(word: str) -> str | None:
    """A program named by a bare word runs what PATH finds: a compiled program, a script the launch cannot write, a
    virtualenv entry point, the swarm launcher, or a script tracked unmodified at HEAD. Any other script in a
    directory the launch can write (a launch-written script on PATH) is refused."""
    try:
        found = shutil.which(str(word))
    except Exception:
        return None
    if not found:
        return None        # a builtin, a function, or not found (the shell says so)
    real = os.path.realpath(found)
    if _is_native(real) or real in _swarm_launchers() or _trusted_script(found):
        return None
    return f"{str(word)!r} runs the script {found!r}: the launch can write it and it is not tracked unmodified at HEAD"


def _trusted_script(path: str) -> bool:
    """A script may run when the launch cannot write it (nor its directory), or it is tracked unmodified at HEAD."""
    try:
        real = os.path.realpath(path)
        if not os.path.isfile(real):
            return False
        if not (os.access(real, os.W_OK) or os.access(os.path.dirname(real) or ".", os.W_OK)):
            return True
        return _venv_entry(path) or tracked_unmodified(real)
    except Exception:
        return False


_SCRIPT_RULE = ("in a drive launch a script runs only when it is tracked unmodified at HEAD in its repository "
                "(commit it, or give the code as a literal `-c` argument)")


def _locations(word, cands, wrappers) -> list[str] | None:
    """The paths a (literal) file word may name: itself when absolute, else under each directory the command may be
    in. None when that cannot be known."""
    path = os.path.expanduser(str(word))
    if os.path.isabs(path):
        return [path]
    if cands is None or "chdir" in wrappers:
        return None
    return [os.path.normpath(os.path.join(c, path)) for c in sorted(cands)]


def _script_refusal(word, cands, wrappers=frozenset()) -> str | None:
    paths = _locations(word, cands, wrappers)
    if paths is None:
        return f"the file {str(word)!r} cannot be located (the directory is not known): {_SCRIPT_RULE}"
    for p in paths:
        if not _trusted_script(p):
            return f"the script {str(word)!r} is not tracked unmodified at HEAD: {_SCRIPT_RULE}"
    return None


def _program_path_refusal(word, cands, wrappers=frozenset()) -> str | None:
    """A program named by a path: a compiled program runs (but a shell or interpreter only under its own name); a
    script (anything else: a shell runs a text file it cannot exec) follows the file rule; the swarm launchers run."""
    paths = _locations(word, cands, wrappers)
    if paths is None:
        return f"the program {str(word)!r} cannot be located (the directory is not known): run it by its full path"
    launchers = None
    for p in paths:
        real = os.path.realpath(p)
        launchers = _swarm_launchers() if launchers is None else launchers
        if real in launchers:
            continue
        if not os.path.isfile(real):
            return f"{str(word)!r} is not an existing file: {_SCRIPT_RULE}"
        if _is_native(real):
            if _family(_norm(real)) is not None and _family(_norm(real)) != _family(_norm(word)):
                return f"{str(word)!r} is the {_norm(real)} interpreter under another name: call it by its own name"
            continue
        if not _trusted_script(p):
            return f"the script {str(word)!r} is not tracked unmodified at HEAD: {_SCRIPT_RULE}"
    return None


def _sequential(command: str) -> bool:
    """Whether every command of `command` runs in the one shell, in order: no subshell, pipeline, background job,
    `$(...)` or backticks (then a `cd` lasts for what follows it)."""
    view = _code_view(drop_heredoc_bodies(command))
    return not re.search(r"[()`]|(?<![|&])[|&](?![|&])", view.replace(">&", "  ").replace("<&", "  "))


def _cd(cands, prog: str, args: list[str], command: str, wrappers=frozenset()):
    """The directories a command may be in after `cd`/`pushd`/`popd`, None when unknown. A top-level cd in a
    sequential command moves it; any other keeps the earlier directories as candidates too (a subshell's cd does
    not last)."""
    if cands is None or prog == "popd" or "CDPATH" in command:
        return None
    pos = [a for a in args if not (a.startswith("-") and a != "-")]
    if not pos:
        if prog == "pushd":
            return None
        target = os.path.expanduser("~")
    else:
        if _dyn(pos[0]) or pos[0] == "-" or pos[0].startswith("+"):
            return None
        target = os.path.expanduser(str(pos[0]))
    new = {os.path.normpath(os.path.join(c, target)) for c in cands}
    if not ("depth0" in wrappers and _sequential(command)):
        new |= set(cands)
    return new if len(new) <= 16 else None


# ---- git and tool configuration a launch may not write (rr31, rr32, rr35) ---------------------------------------
# A push refspec in .git/config, a hook in .git/hooks, an alias or hooksPath in a .gitconfig, a ProxyCommand in
# ~/.ssh/config: each changes what a later, innocent-looking command does. Write, Edit, MultiEdit, NotebookEdit,
# apply_patch and the shell's writers (a redirection, tee, cp, mv, ...) may not touch them in a drive launch.

_GIT_DIR_PROTECTED = re.compile(r"(?:config|config\.worktree|hooks|info|commondir|gitdir|remotes|branches)(?:/.*)?")
_PROTECTED_TEXT = re.compile(r"(?:^|[/\s'\"=])(?:\.git/(?:config|hooks|info|commondir)|\.gitmodules|\.gitconfig|"
                             r"\.ssh/|\.config/(?:git|gh)/)")
_FILE_WRITERS = frozenset(("tee", "cp", "mv", "install", "ln", "touch", "truncate", "dd", "rsync", "patch", "sed",
                           "perl", "chmod", "rm", "shred", "unlink", "mkdir", "tar", "unzip", "curl", "wget"))


def _git_dir_of(directory: str) -> list[str]:
    """The git dir (a .git directory, or where a .git file's gitdir: points) and its commondir, for the checkout
    that contains `directory`."""
    out, d = [], os.path.abspath(directory)
    while True:
        dot = os.path.join(d, ".git")
        if os.path.isdir(dot):
            out.append(dot)
            break
        if os.path.isfile(dot):
            try:
                with open(dot, encoding="utf-8", errors="replace") as f:
                    first = f.readline().strip()
            except OSError:
                break
            if first.startswith("gitdir:"):
                gd = first[len("gitdir:"):].strip()
                gd = gd if os.path.isabs(gd) else os.path.join(d, gd)
                out.append(gd)
                try:
                    with open(os.path.join(gd, "commondir"), encoding="utf-8") as f:
                        common = f.readline().strip()
                    out.append(common if os.path.isabs(common) else os.path.join(gd, common))
                except OSError:
                    pass
            break
        up = os.path.dirname(d)
        if up == d:
            break
        d = up
    return [os.path.realpath(x) for x in out]


def _is_git_dir(d: str) -> bool:
    return os.path.basename(d) == ".git" or (os.path.isfile(os.path.join(d, "HEAD")) and
                                             (os.path.isdir(os.path.join(d, "objects")) or
                                              os.path.isfile(os.path.join(d, "commondir"))))


def protected_path_reason(path, cwd=None) -> str | None:
    """Why a launch may not write `path` (resolved against `cwd`), or None: a git dir's config, hooks or info (the
    checkout's, a worktree's common dir, any directory that is a git dir), .gitmodules, a .gitconfig or
    ~/.config/git, ~/.config/gh, ~/.ssh."""
    if not isinstance(path, str) or not path or _dyn(path):
        return None
    try:
        p = os.path.expanduser(path)
        if not os.path.isabs(p):
            p = os.path.join(cwd or os.getcwd(), p)
        p = os.path.normpath(p)
        real = os.path.realpath(p)
        why = f"a drive launch may not write {path!r}: git, gh and ssh configuration changes what later commands do"
        for q in {p, real}:
            parts = q.replace(os.sep, "/").split("/")
            if parts[-1] in (".gitmodules", ".gitconfig"):
                return why
            for k, part in enumerate(parts[:-1]):
                rest = "/".join(parts[k + 1:])
                if part == ".ssh" or (part == ".config" and parts[k + 1] in ("git", "gh")):
                    return why
                if (part == ".git" or _is_git_dir("/".join(parts[:k + 1]) or "/")) and _GIT_DIR_PROTECTED.fullmatch(rest):
                    return why
        for gd in _git_dir_of(cwd or os.getcwd()) + _git_dir_of(os.path.dirname(real)):
            rel = os.path.relpath(real, gd).replace(os.sep, "/")
            if not rel.startswith("..") and _GIT_DIR_PROTECTED.fullmatch(rel):
                return why
    except Exception:
        return None
    return None


def _protected_name(name) -> bool:
    """A variable a drive launch may not set by any means: where git and gh read configuration, PATH, every GIT_*, and
    the variables that load or name code (BASH_ENV, LD_PRELOAD, GIT_EDITOR, PAGER, ...)."""
    n = re.sub(r"\[.*$", "", str(name))
    return bool(n) and (n == "PATH" or n.startswith("GIT_") or bool(_ENV_CONFIG.fullmatch(n)) or
                        bool(_ENV_REFUSED.fullmatch(n)) or n in _ENV_SHELL or n in _ENV_SSH or n in _ENV_PROGRAM)


_SETS_VALUE_OPTS = {"read": set("dinNptu"), "mapfile": set("dnOsuCc"), "readarray": set("dnOsuCc")}
_PARAM_ASSIGN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*):?[=]")


def _builtin_sets(prog: str, args: list[str]) -> str | None:
    """The protected variable a builtin sets without a NAME= word (printf -v, read, mapfile, declare -n, let, for,
    select, getopts, wait -p, unset), or None."""
    targets: list[str] = []
    if prog == "printf":
        for k, a in enumerate(args):
            if a == "-v" and k + 1 < len(args):
                targets.append(args[k + 1])
            elif a.startswith("-v") and len(a) > 2:
                targets.append(a[2:])
    elif prog in _SETS_VALUE_OPTS:
        values, k = _SETS_VALUE_OPTS[prog], 0
        while k < len(args):
            a = args[k]
            if a.startswith("-") and len(a) > 1 and a != "--":
                if a[1] == "a" and prog == "read":
                    targets.append(a[2:] or (args[k + 1] if k + 1 < len(args) else ""))
                    k += 1 if len(a) > 2 else 2
                    continue
                k += 2 if (len(a) == 2 and a[1] in values) else 1
                continue
            targets.append(a)
            k += 1
    elif prog in ("declare", "typeset", "local", "export", "readonly", "unset"):
        nameref = any(a.startswith("-") and "n" in a[1:] for a in args)
        for a in args:
            if not a.startswith("-"):
                name, eq, value = a.partition("=")
                if not eq:
                    targets.append(name)         # NAME=value is an assignment, checked like any other
                if nameref and eq:
                    targets.append(value)        # declare -n R=HOME: R is HOME
    elif prog == "let":
        targets += [m.group(1) for a in args for m in [re.match(r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*[-+*/%^|&<>]*=", a)] if m]
    elif prog in ("for", "select") and args:
        targets.append(args[0])
    elif prog == "getopts" and len(args) >= 2:
        targets.append(args[1])
    elif prog == "wait":
        targets += [args[k + 1] for k, a in enumerate(args[:-1]) if a == "-p"]
    return next((t for t in targets if _protected_name(t)), None)


def _configured_push_refusal(rest: list[str], cdir, cwd) -> str | None:
    """A push that names no refspec pushes what the configuration says: refused when that is a configured push
    refspec (remote.<name>.push) or a push.default other than simple/current/nothing (upstream, matching)."""
    pos, k = [], 0
    while k < len(rest):
        a = rest[k]
        if a == "--":
            pos.extend(rest[k + 1:])
            break
        if not a.startswith("-"):
            pos.append(a)
        elif a in _PUSH_VALUE_OPTS and a not in ("--signed", "--recurse-submodules"):
            k += 1
        k += 1
    if len(pos) > 1 or not isinstance(cwd, str) or not cwd:
        return None
    where = os.path.expanduser(cdir if cdir and os.path.isabs(os.path.expanduser(cdir)) else os.path.join(cwd, cdir or ""))
    common = (_git_out(where, "rev-parse", "--git-common-dir") or "").strip()
    if common:
        common = common if os.path.isabs(common) else os.path.join(where, common)
        for legacy in ("remotes", "branches"):        # git still reads .git/remotes/<name> and .git/branches/<name>
            d = os.path.join(common, legacy)
            names = [n for n in (os.listdir(d) if os.path.isdir(d) else []) if not n.startswith(".")]
            if names and (not pos or pos[0] in names):
                return (f"a push that names no refspec to a remote defined in .git/{legacy}/ is refused: name the "
                        "remote and the branch (git push origin <branch>)")
    out = _git_out(where, "config", "--get-regexp", r"^(remote\..*\.push|push\.default)$")
    for line in (out or "").splitlines():
        key, _sep, value = line.partition(" ")
        if key.endswith(".push") and key.startswith("remote."):
            return f"a push that names no refspec follows {key} = {value!r}: name the branch (git push origin <branch>)"
        if key == "push.default" and value.strip().lower() not in ("simple", "current", "nothing"):
            return f"a push that names no refspec follows push.default = {value!r}: name the branch"
    return None


_PATCH_FILE = re.compile(r"^\*\*\* (?:Add|Update|Delete) File: (.+)$|^\*\*\* Move to: (.+)$", re.M)


def protected_write_refusal(tool_input, cwd=None) -> str | None:
    """Why a file-writing tool call (Write, Edit, MultiEdit, NotebookEdit; Codex apply_patch) may not go ahead in a
    drive launch: it writes git, gh or ssh configuration (protected_path_reason)."""
    if not isinstance(tool_input, dict):
        return None
    paths = [tool_input.get(k) for k in ("file_path", "notebook_path", "path")]
    for value in tool_input.values():
        if isinstance(value, str):
            paths += [m.group(1) or m.group(2) for m in _PATCH_FILE.finditer(value)]
    for path in paths:
        why = protected_path_reason(path.strip(), cwd) if isinstance(path, str) else None
        if why:
            return why
    return None


def _redirect_targets(words: list[str]) -> list[str]:
    """The files a simple command's output redirections (>, >>, >|, &>, 2>, <>) write."""
    out = []
    for k, w in enumerate(words):
        m = re.match(r"\d*(?:>>|>\||&>>|&>|<>|>)(?!&)(.*)$", str(w))
        if m:
            target = m.group(1) or (words[k + 1] if k + 1 < len(words) else "")
            if target:
                out.append(target)
    return out


_UNCHECKED = ("A shell is allowed only as `sh -c '<literal>'` in a drive job: code that runs commands must be a "
              "literal argument (`sh -c '...'`, `python3 -c '...'`) or a script tracked unmodified at HEAD; stdin, a "
              "pipe, a here-string, a heredoc, <(...), a variable and $(...) cannot be checked")


def launch_command_refusal(command: str, targets=(), *, branch_of=None, cwd=None) -> str | None:
    """Why a launch may not run `command` (its reason, for the agent), or None. `targets` are the
    branch names a push may not update (the recipe's target, e.g. "main"; compared without case), or a
    function giving them (called only when a push or an inline script needs them).
    `branch_of(directory)` names the branch checked out in `directory` (the -C value, None for where the
    command runs), for a push that names none (None when it cannot be told: such a push is refused).
    `cwd` is the directory the command runs in (for a script named by a relative path; None: such a script is
    refused)."""
    if not command:
        return None
    lazy, done = targets, []

    def get_targets():       # computed at the first push or inline script (`targets` may be a callable)
        if not done:
            names = lazy() if callable(lazy) else lazy
            done.append({t.removeprefix("refs/heads/").lower() for t in names if isinstance(t, str) and t})
        return done[0]
    m = next((m for m in _PARAM_ASSIGN.finditer(command) if _protected_name(m.group(1))), None)
    if m:
        return f"setting {m.group(1)} through ${{{m.group(1)}:=...}} is refused in a drive launch"
    for text in _nested_scripts(command):
        for words in split_commands(text):
            if any(_GIT_ENV.match(w) for w in words):
                return "git configuration through GIT_CONFIG_* environment variables is refused"
        for words in split_commands(text, _keep_redirections=True):
            for target in _redirect_targets(words):
                why = protected_path_reason(target, cwd)
                if why:
                    return why
    cands = {os.path.normpath(os.path.abspath(cwd))} if isinstance(cwd, str) and cwd else None
    script_why = None        # a script refusal is given when nothing more specific (a push, a launch) is found
    for prog, args, wrappers in iter_commands(command):
        if prog == TOO_DEEP:
            return "a command nested too deeply to check (sh -c in sh -c ...) is refused"
        if prog == STDIN_SHELL:
            return f"{args[0]}: refused. {_UNCHECKED}" if args else f"refused. {_UNCHECKED}"
        if prog == CODE:
            why = _inline_refusal(args[0], get_targets())
            if why:
                return why
            if _PROTECTED_TEXT.search(args[0]):
                return "inline code that names git, gh or ssh configuration (.git/config, .git/hooks, ...) is refused"
            continue
        if prog == SCRIPT:
            script_why = script_why or _script_refusal(args[0], cands, wrappers)
            continue
        argv0 = next((w[6:] for w in wrappers if w.startswith("argv0:")), None)
        if argv0 is not None and _is_path(argv0):
            script_why = script_why or _program_path_refusal(argv0, cands, wrappers)
        elif argv0 is not None:
            script_why = script_why or _bare_program_refusal(argv0)
        if prog in _FILE_WRITERS:
            for a in args:
                why = protected_path_reason(a, cwd)
                if why:
                    return why
        name = _builtin_sets(prog, args)
        if name:
            return (f"setting {name} is refused in a drive launch (it changes where git, gh or the shell find their "
                    "configuration or programs)")
        if prog in ("cd", "pushd", "popd"):
            cands = _cd(cands, prog, args, command, wrappers)
            continue
        if prog in ("at", "batch") or (prog == "crontab" and "-l" not in args):
            return f"scheduling a command ({prog}) is refused in a drive launch"
        if prog in ("git", "git-push", "git-send-pack"):
            sub, rest, why, cdir = _git_args(args) if prog == "git" else (prog[4:], args, None, None)
            if why:
                return why
            if sub in ("push", "send-pack"):
                if "xargs" in wrappers:
                    return "a push through xargs is refused: name the branch on the command line"
                why = _push_refusal(rest, get_targets(), branch_of, cdir) or _configured_push_refusal(rest, cdir, cwd)
                if why:
                    return why
            elif sub == "subtree" and rest[:1] == ["push"]:
                why = _subtree_push_refusal(rest, get_targets(), branch_of, cdir)
                if why:
                    return why
            elif sub == "config" and any(_GIT_CONFIG_RISK.match(a) or _GIT_CMD_KEY.search(a.split("=", 1)[0])
                                         for a in rest if not a.startswith("-")) and not _git_config_read(rest):
                return "changing a git alias, push setting or command setting is refused"
            elif sub == "remote" and any(a == "--push" or a.startswith("--push=") for a in rest):
                return "changing a remote's push url is refused"
        elif prog == "gh":
            gargs = []
            skip = False
            for a in args:
                if skip:
                    skip = False
                elif a in ("-R", "--repo", "--hostname"):
                    skip = True
                elif not a.startswith("-"):
                    gargs.append(a)
            if gargs[:2] == ["api", "graphql"] and (
                    any(a == "--input" or a.startswith("--input=") or _dyn(a) for a in args)
                    or any(re.match(r"[^=]*=@", str(b)) for a, b in zip(args, args[1:]) if a in ("-F", "--field"))
                    or any(re.match(r"-F[^=]*=@|--field=[^=]*=@", str(a)) for a in args)):
                return ("a gh api graphql query from a file, stdin or a variable is refused: give it literally "
                        "(-f query='...'), so it can be checked")
            if gargs[:2] == ["pr", "merge"]:
                return "`gh pr merge` is refused: landing goes through `swarm land`"
            if gargs[:1] == ["api"] and (any(_GH_GRAPHQL_RISK.search(a) for a in args)
                                         or (any("/contents/" in a for a in gargs[1:])
                                             and any(a in ("-X", "--method", "-f", "-F", "--field", "--raw-field",
                                                           "--input") or a.startswith(("-X", "--method=", "-f", "-F"))
                                                     for a in args))):
                return "a gh api call that commits or merges is refused: landing goes through `swarm land`"
            if gargs[:1] == ["api"] and any(_GH_API_RISK.search(a.split("?", 1)[0]) for a in gargs[1:]):
                return "a gh api call that moves a ref or merges is refused: landing goes through `swarm land`"
        why = _launch_refusal(prog, args)
        if why:
            return why
    return script_why


def _git_config_read(rest: list[str]) -> bool:
    longs = {a.split("=", 1)[0] for a in rest if a.startswith("--")}
    return bool(longs & {"--get", "--get-all", "--get-regexp", "--list", "--get-urlmatch", "--show-origin",
                         "--show-scope", "--name-only", "--unset", "--unset-all"}) or "-l" in rest


_SQL_WRITE = re.compile(r"(?i)\b(?:insert|update|delete|replace|create|drop|alter|vacuum|attach|detach|reindex|"
                        r"truncate)\b|\bpragma\s+[a-z_.]+\s*=|\.(?:import|write|save|backup|restore|output|once|"
                        r"dump|shell|system|read)\b")


_BRANCH_WRITE = set("fdDmMcCu")


def _git_writes(sub, rest) -> bool:
    """Whether this git subcommand changes the repository: the always-writing subcommands, and the ones that read
    or write by their arguments (branch, config, symbolic-ref, remote, reflog, hash-object)."""
    if sub in _GIT_WRITES:
        return True
    flags = [a for a in rest if a.startswith("-")]
    pos = [a for a in rest if not a.startswith("-")]
    letters = {c for a in flags if not a.startswith("--") for c in a[1:]}
    longs = {a.split("=", 1)[0] for a in flags if a.startswith("--")}
    if sub == "branch":
        listing = bool(longs & {"--list", "--contains", "--merged", "--no-merged", "--points-at", "--show-current"}) \
            or "l" in letters
        return bool(letters & _BRANCH_WRITE) or bool(longs & {"--force", "--delete", "--move", "--copy",
                                                              "--set-upstream-to", "--unset-upstream", "--edit-description"}) \
            or (bool(pos) and not listing)
    if sub == "config":
        return not (longs & {"--get", "--get-all", "--get-regexp", "--list", "--get-urlmatch", "--show-origin",
                             "--show-scope", "--name-only"} or "l" in letters)
    if sub == "symbolic-ref":
        return len(pos) >= 2 or "d" in letters or "--delete" in longs
    if sub == "remote":
        return bool(pos) and pos[0] in ("add", "set-url", "remove", "rm", "rename", "prune", "set-head", "set-branches", "update")
    if sub == "reflog":
        return bool(pos) and pos[0] in ("expire", "delete")
    if sub == "hash-object":
        return "w" in letters
    return False


_INLINE_WRITE = re.compile(r"open\([^)]*,\s*['\"][wax]|write_text|write_bytes|writeFile|appendFile|os\.(?:remove|unlink|rename|system)|"
                           r"shutil\.|subprocess|\.rename\(|child_process|\bsystem\s*\(|\bpopen\b|\bspawn")


def readonly_write_reason(command: str) -> str | None:
    """Why `command` writes (for a read-only launch), or None: the writer patterns of writes_files over the
    command and over every string a wrapper in it runs (sh -c, eval, ...), git's own writing subcommands
    whatever options come before them (`git -c k=v commit`), interpreter code that writes or runs commands, code
    that cannot be checked, and sqlite3 with a writing statement."""
    if not command:
        return None
    for text in _nested_scripts(command):
        why = writes_files(text)
        if why:
            return why
    for prog, args, _ in iter_commands(command):
        if prog == TOO_DEEP:
            return "a command nested too deeply to check is refused"
        if prog == STDIN_SHELL:
            return "a shell is allowed only as `sh -c '<literal>'`: anything else cannot be checked"
        if prog == CODE:
            if _INLINE_WRITE.search(args[0]):
                return "an inline script that writes or runs commands"
            continue
        if prog == "git":
            sub, _rest, bad, _cdir = _git_args(args)
            if bad or _git_writes(sub, _rest):
                return "a git command that writes" if not bad else bad
        elif prog in ("sqlite3", "sqlite", "litecli") and "-readonly" not in args:
            if _SQL_WRITE.search(" ".join(args)) or "<" in command:
                return "a sqlite3 command that may write (use -readonly, or only SELECT)"
    return None


def swarm_verdict_call(command: str) -> bool:
    """Whether `command` runs `swarm verdict` to record a verdict (anything but `swarm verdict show`)."""
    value_opts = {"--job", "--as", "--artifact", "--details", "--scope", "--open", "--progress", "--candidate",
                  "--req", "--reason", "--next"}
    for prog, args, _ in iter_commands(command or ""):
        if prog != "swarm":
            continue
        i = 0
        while i < len(args) and args[i].startswith("-"):    # swarm's own options (--config PATH, --host H)
            i += 2 if args[i] in ("--config", "--host") else 1
        if i >= len(args) or args[i] != "verdict":
            continue
        rest, j = args[i + 1:], 0
        while j < len(rest):
            a = rest[j]
            if a.startswith("--"):
                j += 2 if (a in value_opts and "=" not in a) else 1
                continue
            return a != "show"
        return True    # `swarm verdict` with no verdict word: the CLI refuses it, but never let it through
    return False


_READ_VERBS = frozenset(("get", "list", "search", "read", "find", "count", "describe", "fetch", "view",
                         "show", "lookup", "browse", "snapshot", "history", "status", "info", "stats",
                         "recent", "tool", "index", "health"))
_WRITE_VERBS = frozenset(("create", "update", "delete", "write", "post", "send", "set", "add", "remove", "push",
                          "merge", "commit", "edit", "put", "patch", "reply", "comment", "move", "rename", "upload",
                          "modify", "revoke", "approve", "trigger", "close", "execute", "exec", "run", "call",
                          "reboot", "restart", "block", "unblock", "toggle", "save", "fire", "share", "trash",
                          "copy", "change", "schedule", "click", "evaluate", "respond", "authorize", "unauthorize",
                          "adopt", "forget", "provision", "power", "cycle", "reorder", "restore", "backup",
                          "cleanup", "clear", "adjust", "batch", "apply", "install", "uninstall", "enable",
                          "disable", "drag", "drop", "type", "press", "fill", "select", "hover", "navigate",
                          "invite", "publish", "cancel", "assign", "transfer", "grant", "deny", "rotate",
                          "archive", "unarchive", "subscribe", "unsubscribe", "force", "sync", "start", "stop",
                          "kill", "launch", "open", "submit", "register", "unregister", "import", "export",
                          "download", "locate", "wipe", "reset", "rebuild", "refresh", "retry", "resolve", "mark", "replace",
                          "purge", "insert", "upsert", "overwrite", "truncate", "flush", "empty", "query", "sql"))


def mcp_read_allowed(tool_name) -> bool:
    """Whether a read-only launch may call this MCP tool: an allowlist, not a denylist. The tool's own name
    (after `mcp__<server>__`) is split on `_`; it must contain a read verb (get, list, search, read, find,
    count, describe, query, fetch, view, ...) and no write verb. Anything else is refused: a tool whose name
    says neither is not known to be read-only."""
    if not isinstance(tool_name, str) or not tool_name.startswith("mcp__"):
        return True
    tool = re.sub(r"(?<=[a-z])(?=[A-Z])", "_", tool_name.rsplit("__", 1)[-1]).lower()
    tokens = [t for t in re.split(r"[_\-\s]+", tool) if t]
    return any(t in _READ_VERBS for t in tokens) and not any(t in _WRITE_VERBS for t in tokens)


_PS_WRITE = re.compile(r"(?i)\b(?:Set|Add|Clear|Out|New|Remove|Copy|Move|Rename|Export|Install|Uninstall)-"
                       r"(?:Content|Item|Items|File|ItemProperty|Acl|Package|Module|Service|Process|Variable)\b"
                       r"|\bOut-File\b|\bSet-Location\b.*\bgit\b|\bgit\s+(?:commit|push|add|apply|stash|reset)\b"
                       r"|\[(?:System\.)?IO\.File\]::(?:Write|Create|Delete|Move|Copy|Append)|(?<![<>\w])>>?(?!=)")


def powershell_writes(command: str) -> str | None:
    """Whether a PowerShell command writes (redirection, Set-Content, Out-File, Remove-Item, git
    commit or push, .NET file writes), a best-effort match like writes_files; None when it reads."""
    if not command:
        return None
    m = _PS_WRITE.search(command)
    return f"a PowerShell write ({m.group(0).strip()!r})" if m else None
