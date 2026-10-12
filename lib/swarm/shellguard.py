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


# ---- drive jobs (spec 3.4, 4.0): what no seat may run, and what a read-only seat may not write -----
# A guardrail, not a boundary (spec 4.0, [F24]): it stops recognisable misuse by an agent that is not
# trying to hide it, and the driver's own checks (`swarm land`, the mirror audit) decide outcomes. The
# refusals are a push to the job's target branch (any spelling that names it, or that names none and so
# pushes whatever is checked out), any force or mirror push, a headless Claude or Codex session, a merge
# through gh. Read-only seats (judges, reviewers) also refuse writes and SQL writes, and only judges run
# `swarm verdict`. Words are split like a shell does (quotes resolved, not blanked), and what runs inside
# `sh -c`, `bash -lc`, `eval`, `script -c`, `$(...)` and backticks is looked at too.

_MAX_NEST = 5
STDIN_SHELL = "<stdin-shell>"   # a shell that reads its script from a pipe or file: unverifiable
TOO_DEEP = "<too-deep>"   # the program iter_commands yields for a command nested beyond _MAX_NEST
_SHELLS = frozenset(("sh", "bash", "zsh", "dash", "ksh", "ash", "fish", "csh", "tcsh", "busybox"))
_SEPARATORS = ";&|()\n"
_KEYWORDS = frozenset(("{", "}", "!", "if", "then", "do", "else", "elif", "while", "until", "time", "command",
                       "builtin", "exec", "nohup", "setsid", "nice", "ionice", "chrt", "stdbuf", "xargs", "env",
                       "sudo", "doas", "timeout", "watch", "unbuffer", "caffeinate", "taskset"))
_REDIRECT = re.compile(r"\d*(?:<<<|<<-?|>>|>&|<&|&>|>|<)")
_ASSIGN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")


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


def split_commands(text: str, _depth: int = 0) -> list[list[str]]:
    """The words of each simple command of a shell text, quotes resolved. Command separators are ; & | ( )
    and newlines outside quotes; the commands inside `$(...)` and backticks are listed as commands of their
    own (and leave a placeholder word where they stood). Redirections are dropped. Never raises."""
    if _depth > _MAX_NEST or not isinstance(text, str):
        return []
    text = drop_heredoc_bodies(text)
    out: list[list[str]] = []
    words: list[str] = []
    cur: list[str] = []
    has = False            # a word is being built (an empty "" still counts)
    i, n = 0, len(text)

    def end_word():
        nonlocal cur, has
        if has:
            words.append("".join(cur))
        cur, has = [], False

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
                elif text[i] == "`":
                    inner, i = _read_backticks(text, i)
                    out.extend(split_commands(inner, _depth + 1))
                    cur.append("$SUB")
                else:
                    cur.append(text[i])
                    i += 1
            i += 1
        elif text.startswith("$(", i):
            inner, i = _read_substitution(text, i)
            out.extend(split_commands(inner, _depth + 1))
            cur.append("$SUB")
            has = True
        elif c == "`":
            inner, i = _read_backticks(text, i)
            out.extend(split_commands(inner, _depth + 1))
            cur.append("$SUB")
            has = True
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
            cur.append(c)
            has = True
            i += 1
    end_command()
    return [_drop_redirections(w) for w in out if w]


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
    "sudo": {"-u", "-g", "-h", "-p", "-C", "-r", "-t", "-D", "-R", "-U"},
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
}


def _unwrap(words: list[str]) -> tuple[list[str], set[str]]:
    """(the command after assignments and wrappers like env, sudo, timeout, nohup, xargs, time; the wrappers
    seen). A wrapper's own options are skipped, and the ones that take a value skip it."""
    seen: set[str] = set()
    words = list(words)
    while words:
        w = words[0]
        if _ASSIGN.match(w) and not w.startswith("="):
            words = words[1:]
            continue
        base = _prog(w)
        if base not in _KEYWORDS:
            break
        seen.add(base)
        words = words[1:]
        values = _VALUE_FLAGS.get(base, set())
        while words and words[0].startswith("-") and words[0] != "-":
            flag = words[0]
            words = words[1:]
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
        if base == "env":
            continue
    return words, seen


def _script_arg(prog: str, args: list[str]) -> str | None:
    """The command string a shell-like program is told to run (`sh -c STR`, `bash -lc STR`, `script -qc STR`,
    `su -c STR`, `script --command=STR`), or None."""
    for k, a in enumerate(args):
        if a.startswith("--command="):
            return a[len("--command="):]
        if a in ("--command", "-c") and k + 1 < len(args):
            return args[k + 1]
        if (re.fullmatch(r"-[A-Za-z]*c[A-Za-z]*", a) and not a.startswith("--") and k + 1 < len(args)
                and prog in _SHELLS | {"script", "su"}):
            return args[k + 1]
    return None


_FIND_EXEC = ("-exec", "-execdir", "-ok", "-okdir")
_SYSTEMD_VALUE = {"-p", "-u", "-E", "-t", "--unit", "--property", "--setenv", "--description", "--slice", "--uid", "--gid",
                  "--working-directory", "-d", "--service-type", "--on-active", "--on-calendar"}
_SSH_VALUE = {"-p", "-i", "-l", "-o", "-F", "-J", "-L", "-R", "-D", "-b", "-c", "-e", "-m", "-S", "-w", "-E", "-B"}
_TMUX_RUN = ("new", "new-session", "new-window", "neww", "split-window", "splitw", "run-shell", "run", "send-keys")


def _shell_literal(prog: str, args: list[str]):
    """For a shell interpreter: ("t", the literal after -c) when it is run as `<shell> -c '<literal>'` and nothing
    else, else ("bad", None): a shell reading stdin, a script file (/dev/stdin, /dev/fd/N, a path), a here-string,
    a heredoc or `-c "$(...)"` cannot be checked, so none is allowed. None: not a shell."""
    if prog == "busybox":
        if not args or _prog(args[0]) not in _SHELLS - {"busybox"}:
            return None
        args = args[1:]
    elif prog not in _SHELLS:
        return None
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("-o", "+o"):
            i += 2
            continue
        if a.startswith("-") and not a.startswith("--") and "c" in a[1:]:
            script = args[i + 1] if i + 1 < len(args) else None
            return ("bad", None) if script is None or "$SUB" in script else ("t", script)
        if a.startswith(("-", "+")):
            i += 1
            continue
        return ("bad", None)          # a script file before any -c
    return ("bad", None)              # no -c: it reads stdin


def _nested(prog: str, args: list[str]):
    """The commands a command runs through its arguments: [("t", text)] to run as shell text, [("w", words)] as a
    command, [("bad", None)] for what cannot be checked. Covers shells (-c literal only), eval, script/su -c, find
    -exec, git rebase -x and submodule foreach, parallel, flock, systemd-run, tmux, ssh, source and `.`."""
    out = []
    shell = _shell_literal(prog, args)
    if shell is not None:
        return [shell]
    if prog in ("script", "su"):
        inner = _script_arg(prog, args)
        return [("t", inner)] if inner is not None else []
    if prog == "eval":
        return [("t", " ".join(args))]
    if prog in ("source", "."):
        return [("bad", None)] if not args or args[0].startswith(("/dev/", "-")) or "$SUB" in args[0] else []
    if prog == "find":
        k = 0
        while k < len(args):
            if args[k] in _FIND_EXEC:
                j = k + 1
                while j < len(args) and args[j] not in (";", "+"):
                    j += 1
                out.append(("w", args[k + 1:j]))
                k = j
            k += 1
    elif prog == "git":
        sub, rest, _bad, _cdir = _git_args(args)
        if sub == "rebase":
            for k, a in enumerate(rest):
                if a in ("-x", "--exec") and k + 1 < len(rest):
                    out.append(("t", rest[k + 1]))
                elif a.startswith("--exec="):
                    out.append(("t", a[len("--exec="):]))
                elif a.startswith("-x") and len(a) > 2 and not a.startswith("--"):
                    out.append(("t", a[2:]))
        elif sub == "submodule" and "foreach" in rest:
            tail = rest[rest.index("foreach") + 1:]
            tail = [a for a in tail if not a.startswith("--")]
            out.append(("t", " ".join(tail)))
    elif prog == "parallel":
        words = [a for a in args]
        k = 0
        while k < len(words) and words[k].startswith("-"):
            k += 2 if (k + 1 < len(words) and re.fullmatch(r"\d+%?|\S*\{\}\S*", words[k + 1]) and "=" not in words[k]) else 1
        cmd = []
        for w in words[k:]:
            if w.startswith(":::"):
                break
            cmd.append(w)
        out.append(("w", cmd))
    elif prog == "flock":
        words = [a for a in args if not a.startswith("-")]
        out.append(("w", words[1:]))
    elif prog == "systemd-run":
        k = 0
        while k < len(args) and args[k].startswith("-"):
            k += 2 if args[k] in _SYSTEMD_VALUE else 1
        out.append(("w", args[k:]))
    elif prog == "tmux":
        sub = next((a for a in args if a in _TMUX_RUN), None)
        if sub and args:
            out.append(("t", args[-1]))
    elif prog == "ssh":
        k = 0
        while k < len(args) and args[k].startswith("-"):
            k += 2 if args[k] in _SSH_VALUE else 1
        if k + 1 < len(args):
            out.append(("t", " ".join(args[k + 1:])))
    return out


def _walk(lists, depth: int):
    if depth > _MAX_NEST:      # too deep to look at: callers refuse what they cannot check
        yield TOO_DEEP, [], set()
        return
    for words in lists:
        words, seen = _unwrap(words)
        for tag in sorted(seen):
            if tag.startswith("split:"):
                yield from iter_commands(tag[6:], depth + 1)
        if not words:
            continue
        if "watch" in seen:       # watch runs its words as one command line
            yield from iter_commands(" ".join(words), depth + 1)
            continue
        prog, args = _prog(words[0]), words[1:]
        for kind, payload in _nested(prog, args):
            if kind == "bad":
                yield STDIN_SHELL, [], set()
            elif kind == "t":
                yield from iter_commands(payload, depth + 1)
            elif payload:
                yield from _walk([payload], depth + 1)
        yield prog, args, seen


def iter_commands(command: str, _depth: int = 0):
    """Yield (prog, args, wrappers) for every command a shell text runs, looking inside everything that runs
    another command (_nested), `$(...)` and backticks. A shell is only ever `<shell> -c '<literal>'`."""
    if command:
        yield from _walk(split_commands(command), _depth)


def _nested_texts(command: str, _depth: int = 0) -> list[str]:
    """`command` and every text a command in it runs, for a caller that scans text."""
    out = [command]
    if _depth > _MAX_NEST or not command:
        return out
    for words in split_commands(command):
        words, seen = _unwrap(words)
        texts = [t[6:] for t in sorted(seen) if t.startswith("split:")]
        if not words:
            for t in texts:
                out.extend(_nested_texts(t, _depth + 1))
            continue
        if "watch" in seen:
            texts.append(" ".join(words))
        else:
            for kind, payload in _nested(_prog(words[0]), words[1:]):
                if kind == "t":
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
                              r"core\.(?:sshcommand|hookspath|fsmonitor)$)")


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


_GIT_ENV = re.compile(r"(?:export\s+)?GIT_CONFIG[A-Z_0-9]*=")
_GH_API_RISK = re.compile(r"(?:^|/)(?:git/(?:refs|commits|trees|tags)|merges|merge-upstream|pulls/\d+/merge)\b")
_GH_GRAPHQL_RISK = re.compile(r"mergePullRequest|updateRef|createRef|deleteRef|createCommitOnBranch|mergeBranch")
_CLAUDE_TOKEN = re.compile(r"(?:^|/)(?:claude|claude-code)(?:\.m?c?js)?$|@anthropic-ai/claude-code|"
                           r"claude(?:-code)?/(?:bin/)?(?:cli|index)\.m?c?js$")
_CODEX_TOKEN = re.compile(r"(?:^|/)codex(?:\.js)?$|@openai/codex")
_CODEX_VALUE_OPTS = {"-c", "--config", "-m", "--model", "-C", "--cd", "-s", "--sandbox", "-a", "--ask-for-approval",
                     "-p", "--profile", "-i", "--image", "--add-dir", "--local-provider", "--color",
                     "--output-last-message", "-o", "--output-schema", "--enable", "--disable"}
_LAUNCHERS = frozenset(("node", "nodejs", "npx", "bunx", "bun", "deno", "pnpm", "yarn", "npm", "pnpx", "tsx", "ts-node"))


def _claude_headless(args: list[str]) -> bool:
    for a in args:
        if a == "--print" or a.startswith("--print="):
            return True
        if a.startswith("-") and not a.startswith("--") and len(a) > 1 and "p" in a[1:] and not a.startswith("-c="):
            return True
    return False


def _codex_headless(args: list[str]) -> bool:
    i = 0
    while i < len(args):
        a = args[i]
        if a.startswith("-"):
            i += 2 if (a in _CODEX_VALUE_OPTS and "=" not in a) else 1
            continue
        return a in ("exec", "e")
    return False


def _launch_refusal(prog: str, args: list[str]) -> str | None:
    if _CLAUDE_TOKEN.search(prog) and _claude_headless(args):
        return "a headless claude session (claude -p) is refused"
    if _CODEX_TOKEN.search(prog) and _codex_headless(args):
        return "a headless codex session (codex exec) is refused"
    if prog in _LAUNCHERS:
        for k, a in enumerate(args):
            if _CLAUDE_TOKEN.search(a):
                return "a headless claude session (claude -p) is refused" if _claude_headless(args[k + 1:]) else None
            if _CODEX_TOKEN.search(a):
                return "a headless codex session (codex exec) is refused" if _codex_headless(args[k + 1:]) else None
    return None


_INTERP = re.compile(r"(?:python[0-9.]*|node(?:js)?|ruby|perl|php|deno|bun)")
_INTERP_CODE_FLAGS = ("-c", "-e", "--eval", "-pe", "-ne", "-E")


def _inline_refusal(text: str, targets: set[str]) -> str | None:
    """Why an inline script (python -c, node -e, a script on stdin) is refused: it runs a git push that names a
    target branch or forces, or starts a headless claude or codex. A best-effort scan of the text, not a parse
    (the close-time audit is the backstop for what an agent hides in code, spec 4.0)."""
    if re.search(r"\bgit\b", text) and re.search(r"\bpush\b", text):
        if re.search(r"--force|--mirror|--all\b|(?<![\w-])-f\b|['\"]\+", text) or any(
                re.search(r"(?<![\w/.-])(?:refs/heads/)?" + re.escape(t) + r"(?![\w/.-])", text, re.I) for t in targets):
            return "an inline script that pushes to a target branch or forces a push is refused"
    if re.search(r"\bclaude\b", text) and re.search(r"(?<![\w-])(?:-p|--print)(?![\w-])", text):
        return "a headless claude session (claude -p) is refused"
    if re.search(r"\bcodex\b", text) and re.search(r"(?<![\w-])exec(?![\w-])", text):
        return "a headless codex session (codex exec) is refused"
    return None


def drive_command_refusal(command: str, targets=(), *, branch_of=None) -> str | None:
    """Why a drive seat may not run `command` (its reason, for the agent), or None. `targets` are the
    branch names a push may not update (the recipe's target, e.g. "main"; compared without case), or a
    function giving them (called only when a push or an inline script needs them).
    `branch_of(directory)` names the branch checked out in `directory` (the -C value, None for where the
    command runs), for a push that names none (None when it cannot be told: such a push is refused)."""
    if not command:
        return None
    lazy, done = targets, []

    def get_targets():       # computed at the first push or inline script (`targets` may be a callable)
        if not done:
            names = lazy() if callable(lazy) else lazy
            done.append({t.removeprefix("refs/heads/").lower() for t in names if isinstance(t, str) and t})
        return done[0]
    for text in _nested_scripts(command):
        for words in split_commands(text):
            if any(_GIT_ENV.match(w) for w in words):
                return "git configuration through GIT_CONFIG_* environment variables is refused"
    interp_stdin = False
    for prog, args, wrappers in iter_commands(command):
        if prog == TOO_DEEP:
            return "a command nested too deeply to check (sh -c in sh -c ...) is refused"
        if prog == STDIN_SHELL:
            return ("a shell is allowed only as `sh -c '<literal>'` in a drive job (no stdin, script file, here-string, "
                    "heredoc or $(...) script: they cannot be checked)")
        if _INTERP.fullmatch(prog):
            code = [args[k + 1] for k, a in enumerate(args[:-1]) if a in _INTERP_CODE_FLAGS]
            interp_stdin = interp_stdin or not code
            for text in code:
                why = _inline_refusal(text, get_targets())
                if why:
                    return why
        if prog in ("git", "git-push", "git-send-pack"):
            sub, rest, why, cdir = _git_args(args) if prog == "git" else (prog[4:], args, None, None)
            if why:
                return why
            if sub in ("push", "send-pack"):
                if "xargs" in wrappers:
                    return "a push through xargs is refused: name the branch on the command line"
                why = _push_refusal(rest, get_targets(), branch_of, cdir)
                if why:
                    return why
            elif sub == "config" and any(_GIT_CONFIG_RISK.match(a) for a in rest if not a.startswith("-")):
                return "changing a git alias or push setting is refused"
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
    if interp_stdin and "<<" in command:      # `python3 - <<'EOF' ... EOF`
        for body in heredoc_bodies(command):
            why = _inline_refusal(body, get_targets())
            if why:
                return why
    return None


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
                           r"shutil\.|subprocess|\.rename\(|child_process")


def readonly_write_reason(command: str) -> str | None:
    """Why `command` writes (for a read-only seat), or None: the writer patterns of writes_files over the
    command and over every string a wrapper in it runs (sh -c, eval, ...), git's own writing subcommands
    whatever options come before them (`git -c k=v commit`), and sqlite3 with a writing statement."""
    if not command:
        return None
    for text in _nested_scripts(command):
        why = writes_files(text)
        if why:
            return why
    interp_stdin = False
    for prog, args, _ in iter_commands(command):
        if _INTERP.fullmatch(prog) and not any(a in _INTERP_CODE_FLAGS for a in args):
            interp_stdin = True
        if prog == TOO_DEEP:
            return "a command nested too deeply to check is refused"
        if prog == STDIN_SHELL:
            return "a shell is allowed only as `sh -c '<literal>'`: anything else cannot be checked"
        if prog == "git":
            sub, _rest, bad, _cdir = _git_args(args)
            if bad or _git_writes(sub, _rest):
                return "a git command that writes" if not bad else bad
        elif prog in ("sqlite3", "sqlite", "litecli") and "-readonly" not in args:
            if _SQL_WRITE.search(" ".join(args)) or "<" in command:
                return "a sqlite3 command that may write (use -readonly, or only SELECT)"
    if interp_stdin and "<<" in command:      # `python3 - <<'EOF' ... EOF`
        for body in heredoc_bodies(command):
            if _INLINE_WRITE.search(body):
                return "an inline script that writes or runs commands"
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
    """Whether a read-only seat may call this MCP tool: an allowlist, not a denylist. The tool's own name
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
