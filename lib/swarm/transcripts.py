"""The transcript archive: what swarm agents (and the orchestrator, for a job's run) said and did.

Off by default (`[transcripts] enabled = false`); disabled, every function here returns at once
without touching the board. When on:

* Subagents are captured at SubagentStop (final) from their Claude Code transcript,
  <main session transcript minus .jsonl>/subagents/agent-<agent_id>.jsonl, and every
  snapshot_minutes while still running (run_snapshots, from the SubagentStart/Stop sweep and the
  watch/tail sweeper; never from the per-tool-call hooks).
* The orchestrator: only the slice of its session transcript between the job's activation and
  its close (by entry timestamps), agent_key "orchestrator", at deactivate or auto-close (final)
  and in snapshots.
* Images come out first (extract_images): every base64 image in the JSONL (Claude image blocks,
  Read tool results, Codex data URLs) is replaced in place by a small
  {"type": "swarm-image", "sha256": ..., "mime": ..., "bytes": N} object and stored once per
  sha256 on the board (TranscriptRow.images), not recompressed. So redaction never sees image
  data, and restore_images puts the exact original bytes back. Text inside screenshots is not
  redacted.
* Every text is redacted (redact) before anything is stored, compressed with lzma, and stored
  one row per (job, agent_key) (Board.save_transcript), skipped when unchanged (sha256 of the
  redacted text). A compressed text over max_mb keeps its head and tail around one
  {"type": "swarm-truncated", ...} line.
* Rotation (rotate) after each write and in `swarm purge`: rows older than retention_days, then
  whole jobs, oldest first, while the total is over max_total_mb. Active jobs are never
  touched; if they alone exceed the limit a warning is logged.

Work in the hooks is bounded by a `deadline` (a time.monotonic() value): past it, capturing
raises OutOfTime between steps, and the hook logs it and moves on. A final capture that fails
that way (or any other way) stays pending and is retried (swarm.supervisor.lost: the sweeps, then
the supervisor pass with FINAL_RETRY_SECONDS); one that keeps failing ends as a capture-failed
row (capture_failed_row: the reason and the size, never any text).
"""
from __future__ import annotations

import base64
import datetime as _dt
import hashlib
import json
import lzma
import math
import os
import re
import stat
import time
from pathlib import Path

from swarm import enrolment, paths, safefs
from swarm.board import TranscriptImage, TranscriptRow, TranscriptSummary  # noqa: F401  (re-exported for the CLI)
from swarm.board import TRANSCRIPT_MAX_RAW  # what Board.transcript_body reads back at most

DEFAULTS = {"enabled": False, "retention_days": 30, "max_total_mb": 2048, "snapshot_minutes": 15,
            "max_mb": 50}
ORCHESTRATOR_KEY = "orchestrator"
ORCHESTRATOR_NAME = "orchestrator"
TRUNCATED_TYPE = "swarm-truncated"
LZMA_PRESET = 1          # ~30 MB/s on transcripts; the higher presets are 2-5x slower for ~15%
CHUNK = 1 << 20          # compress (and check the deadline) per MiB
MB = 1024 * 1024
STATE_DIR = "~/.local/state/swarm"   # only for removing the old machine-wide stamp
SNAPSHOT_STAMP = "transcripts-snapshot.stamp"   # the old machine-wide one: removed on sight
LOG_NAME = "hook-errors.log"   # in paths.host_dir(), shared with the hooks' own log
OVER_LIMIT_EVERY = 3600   # seconds between repeats of the same over-limit warning in the log


class OutOfTime(Exception):
    """The deadline passed before the capture finished (nothing was stored)."""


class TooLarge(Exception):
    """A transcript over what is ever read (RAW_READ_MAX; a session slice over
    TRANSCRIPT_MAX_RAW): not read on. A final capture marks it capture failed."""


def settings(cfg: dict) -> dict:
    """[transcripts] with the defaults filled in."""
    return {**DEFAULTS, **((cfg or {}).get("transcripts") or {})}


def enabled(cfg: dict) -> bool:
    return bool(((cfg or {}).get("transcripts") or {}).get("enabled"))


def _check(deadline: float | None) -> None:
    if deadline is not None and time.monotonic() > deadline:
        raise OutOfTime("transcript capture ran out of time")


# --------------------------------------------------------------------------- redaction

_NAMES = r"(?:token_secret|client_secret|secret_key|access_key|private_key|password|passwd|api_key|apikey|secret|token)"
# a key-like name: the keyword at the end of an identifier (PGPASSWORD, db_password), not inside
# one (max_tokens, secretary)
_NAME = rf"[A-Za-z0-9_.\-]*?{_NAMES}(?![A-Za-z0-9])"
_KEY_FULL = re.compile(rf"(?i)[A-Za-z0-9_.\-]*{_NAMES}")
_KEYISH_FULL = re.compile(r"(?i)[A-Za-z0-9_.\-]*(?:key|auth|credential|secret|token|passw(?:or)?d)[A-Za-z0-9_]*")

# PEM/OpenSSH private keys, and PGP private key armour (... PRIVATE KEY BLOCK-----)
_PEM = re.compile(r"-----BEGIN ([A-Z0-9 ]*?)PRIVATE KEY((?: BLOCK)?)-----.*?(?:-----END \1PRIVATE KEY\2-----|\Z)", re.S)
# Private keys over several lines, in raw text and in JSON-escaped text alike (_key_bodies): a
# "line" ends at a real newline or at an escaped one (\n, \\n, ... however many times the text was
# JSON-encoded). A line's key text (_KEY_TEXT) is the base64 at its end, after nothing or after
# any prefix ending in ":", "│", ">", "#", "-", "→", a tab (real or escaped), a space or a quote
# (Claude Read numbers, grep -r/-n/-A, nl -ba, bat/less -N, "> ", "# ", diff, YAML indentation,
# quoted or repr'd lines), and before nothing but quotes, commas and spaces (or, on the last
# line of a JSON string, before the string's end quote). The rules:
#  - a private BEGIN line (PEM, OpenSSH, PGP armour ... PRIVATE KEY BLOCK) with the key lines,
#    armour headers and blank lines after it, through its END line. A BEGIN whose lines aren't
#    recognised is never passed over: it goes from BEGIN to the matching END within the same
#    JSON string (never across an unescaped quote), or to the end of that string, as _PEM does;
#  - an orphan END line: the key lines right above it go with it (any number, so even a 2-line EC
#    key goes; an END with no key line above it is left, nothing to hide);
#  - a run of at least KEY_RUN_MIN consecutive lines whose key text is exactly 64 (PEM, RFC 7468;
#    PGP armour) or 70 (OpenSSH) base64 characters mixing upper case, lower case and digits,
#    plus a shorter key line just before (a cut first line) and after it (the last line, a PGP
#    =CRC). 4 lines (192 bytes) is less than any RSA/OpenSSH/PGP private key body, and most
#    base64 doesn't look like this: images are one JSON string (one line) or wrapped at 76
#    (base64(1), MIME); hex hashes lack mixed case; a run right under a public BEGIN line
#    (CERTIFICATE, PGP PUBLIC KEY BLOCK, PGP SIGNATURE) is kept.
# A span with no unescaped quote in it is replaced whole; otherwise only each line's key text
# (and the BEGIN/END marks), so the text stays valid JSONL either way.
# Accepted over-redaction: other base64 wrapped at exactly 64 (openssl base64, a certificate or
# public key shown without its BEGIN line) or 70, also as KEY_RUN_MIN or more JSON strings in a
# row on one line (see _JOINT). Not caught: a tiny key (EC, Ed25519 PEM: 1-3
# lines) with neither BEGIN nor END.
KEY_RUN_MIN = 4
_KEY_MARK = re.compile(r"-----(BEGIN|END) ([A-Z0-9 ]{0,40}?)-----")
_KEY_TEXT = re.compile(r"(?:^|(?<=[:│>#\t \"'\-→])|(?<=\\t)|(?<=\\u2192)|(?<=\\u2502))(\+?)"
                       r"([A-Za-z0-9+/]+={0,2}|=[A-Za-z0-9+/]{4})"
                       r"(?:[ \t,;'\"]|\\+[\"'])*$")
_KEY_LINE = re.compile(r"(?=[^\n]*[0-9+/=])(?:[A-Za-z0-9+/]+={0,2}|=[A-Za-z0-9+/]{4})")   # a digit, + / or =
_KEY_FULL_LINE = re.compile(r"(?=[^\n]{0,70}?[A-Z])(?=[^\n]{0,70}?[a-z])(?=[^\n]{0,70}?[0-9])"
                            r"[A-Za-z0-9+/]{64}(?:[A-Za-z0-9+/]{6})?")
_ARMOUR_HEADER = re.compile(r"[ \t]*(?:[A-Za-z][A-Za-z0-9\-]{0,40}: [^\"{}\\]*)?")
# a stretch of base64 as long as a full key line, plus a letter or five of what precedes it (the
# "n" of an escaped newline, the "t" or "u2192" of an escaped prefix, a diff "+")
_KEY_CANDIDATE = re.compile(r"(?<![A-Za-z0-9+/])[A-Za-z0-9+/]{64,76}(?![A-Za-z0-9+/])")
_CR_END = re.compile(r"(?:\r|\\+r)$")
# Key lines in different JSON strings of one line (R-redact 2): text blocks of a few lines each
# (an MCP tool result's content), an array or repr list of quoted lines. Between two such lines
# there is only a joint (_JOINT): JSON punctuation, blanks, escaped newlines, quotes (escaped or
# not), short quoted names or words ("type": "text"), line numbers (Read, cat -n); never a real
# newline, so a run never leaves its JSONL line. The joined-run rule (_Joined): KEY_RUN_MIN or
# more full-width key lines chained by joints that each hold a quote or an escaped newline, one
# of them at least a quote (a run with none is _key_runs' own), plus a cut line before and the
# short last line after (which may be mixed-case letters only); kept right under a public
# BEGIN. Each line's key text is redacted on its own, so the text stays valid JSONL.
_JOINT_TOKEN = (r"[ \t\r,:\[\]{}]|\\+[nrt]|\\*[\"']|\\*[\"'][A-Za-z_][A-Za-z_\-]{0,30}\\*[\"']"
                r"|[0-9]{1,7}(?:\t|\\+t|→|│|\\+u2192|\\+u2502)")
_JOINT = re.compile(rf"(?:{_JOINT_TOKEN})*")
_NUMBER_TAIL = r"\t|\\+t|→|│|\\+u2192|\\+u2502"     # after a line number (cat -n, Read, bat)
_JOINT_DOWN = re.compile(rf"(?:{_JOINT_TOKEN})*?((?=[A-Za-z0-9+/]*[0-9+/=])[A-Za-z0-9+/]{{1,76}}={{0,2}}"
                         r"|(?=[a-z]*[A-Z])(?=[A-Z]*[a-z])[A-Za-z]{8,76})"
                         rf"(?![A-Za-z0-9+/=]|{_NUMBER_TAIL})")
_AFTER_NUMBER = re.compile(_NUMBER_TAIL)
_BOUNDARY = re.compile(r"[\"']|\\+[nr]")
_B64_TOKEN = re.compile(r"[A-Za-z0-9+/]+={0,2}")
_B64_MORE = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=")
_ESC_TAILS = ("n", "t", "r", "u2192", "u2502")   # what an escape leaves before a line's text
_JSON_NUMBER = re.compile(r"[0-9]+(?:[eE][0-9]+)?")
JOINT_MAX = 300          # a joint is at most this long
KEY_LINE_MAX = 1024      # only the last this many characters of a line are looked at
KEY_BLOCK_LINES = 400    # lines walked after one BEGIN at most
KEY_SPAN_MAX = 65536     # BEGIN to END (or the string's end) at most, when the lines aren't recognised
KEY_WALK_MAX = 200_000   # lines walked (classified) per call at most; past it BEGINs fall back to _PEM's rule
REDACT_CHECK_BYTES = 256 * 1024   # redact checks its deadline at least this often (characters)
_URL = re.compile(r"\b([A-Za-z][A-Za-z0-9+.\-]*://)([^\s/:@\"'\\]+):([^\s/@\"'\\]+)@")
_APITOKEN = re.compile(r"APIToken=[^\s\"'\\]+")
_BEARER = re.compile(r"(?i)\b(bearer)\s+([A-Za-z0-9._~+/=\-]{8,})")
# HTTP Basic credentials: base64 of user:password after "Basic" (see _basic)
_BASIC = re.compile(r"(?i)\b(basic)\s+([A-Za-z0-9+/]{4,}={0,2})(?![A-Za-z0-9+/=_\-])")
_AUTHORIZATION = re.compile(r"(?i)authori[sz]ation\W{0,4}$")
_SK = re.compile(r"(?<![A-Za-z0-9])sk-(ant-)?[A-Za-z0-9_\-]{16,}")
_JSON_KV = re.compile(rf'(?i)("{_NAME}")(\s*:\s*)"((?:[^"\\]|\\.)+)"')
_KV = re.compile(rf"(?i)(?<![A-Za-z0-9_])({_NAME})(\\?[\"']?[ \t]*(?:=[ \t]?|:[ \t]*))(\\?[\"']?)([^\s\"'`,;&|<>(){{}}\[\]\\]+)")
_ENTROPY = re.compile(r"(?i)(?<![A-Za-z0-9])([A-Za-z0-9_]*(?:key|token|secret|passw(?:or)?d|auth|credential|api)"
                      r"[A-Za-z0-9_]*)([\"'\s:=]{1,6})([A-Za-z0-9+/_\-]{24,}={0,2})(?![A-Za-z0-9+/_\-])")
# Bare provider tokens, by their documented prefixes, lengths and charsets: (pattern, kind).
# Matched anywhere, with no key-like name needed next to them.
_B = r"(?<![A-Za-z0-9_\-])"      # not inside a longer word
_E = r"(?![A-Za-z0-9_\-])"
_VENDOR = tuple((re.compile(p), kind) for p, kind in (
    (_B + r"gh[pousr]_[A-Za-z0-9]{36,255}" + _E, "github-token"),
    (_B + r"github_pat_[A-Za-z0-9_]{50,255}" + _E, "github-token"),
    (_B + r"glpat-[A-Za-z0-9_\-]{20,64}" + _E, "gitlab-token"),
    (_B + r"xox[baprs]-[A-Za-z0-9]{10,}(?:-[A-Za-z0-9]{6,})+" + _E, "slack-token"),
    (r"(?<![A-Za-z0-9])(?:AKIA|ASIA)[A-Z0-9]{16}(?![A-Za-z0-9])", "aws-access-key-id"),
    (_B + r"AIza[0-9A-Za-z_\-]{35}" + _E, "google-api-key"),
    (_B + r"hf_[A-Za-z0-9]{30,64}" + _E, "huggingface-token"),
    (_B + r"npm_[A-Za-z0-9]{36}" + _E, "npm-token"),
    (_B + r"eyJ[A-Za-z0-9_\-]{8,}\.eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{16,}" + _E, "jwt"),
))
_AWS_ID = re.compile(r"(?<![A-Za-z0-9])(?:AKIA|ASIA)[A-Z0-9]{16}(?![A-Za-z0-9])|\[REDACTED:aws-access-key-id\]")
# an AWS secret access key: 40 base64 characters, only redacted in a line that has a key id
_AWS_SECRET = re.compile(r"(?<![A-Za-z0-9+/])[A-Za-z0-9+/]{40}(?![A-Za-z0-9+/=])")
# cheap first look at a raw line: nothing here, nothing to redact
_HINT = re.compile(rf"sk-|APIToken|PRIVATE KEY|://[^\s/@]*:[^\s/@]*@|"
                   r"gh[pousr]_|github_pat_|glpat-|xox[baprs]-|AKIA|ASIA|AIza|hf_|npm_|eyJ|"
                   rf"(?i:bearer\s|basic\s|{_NAMES}(?![a-z0-9])|"
                   r"(?:key|auth|credential)(?![a-z0-9]))")
_PLACEHOLDERS = {"none", "null", "true", "false", "required", "optional", "string", "example",
                 "changeme", "xxxx", "your_token", "your_password", "<redacted>", "...", "…"}


def _kind(name: str) -> str:
    n = name.lower()
    if "passw" in n:
        return "password"
    if "api_key" in n or "apikey" in n:
        return "api-key"
    if "secret" in n:
        return "secret"
    if "key" in n:
        return "key"
    return "token"


def _entropy(s: str) -> float:
    counts: dict = {}
    for ch in s:
        counts[ch] = counts.get(ch, 0) + 1
    return -sum(c / len(s) * math.log2(c / len(s)) for c in counts.values())


def _high_entropy(s: str) -> bool:
    return (len(s) >= 24 and any(c.isdigit() for c in s) and any(c.isalpha() for c in s)
            and _entropy(s) >= 3.5)


# A whole value that is only a reference to an environment variable ($DB_PASSWORD,
# ${DB_PASSWORD}, %DB_PASSWORD%), not a secret. Upper-case names only, the environment
# convention: "$ecret1" or a crypt hash "$2b$12$..." is a value, not a reference.
_ENV_REF = re.compile(r"\$(?:[A-Z_][A-Z0-9_]*|\{[A-Z_][A-Z0-9_]*\})|%[A-Z_][A-Z0-9_]*%")


def _plain_value(v: str) -> bool:
    """Values that are not secrets: explicit placeholders and whole-value env references.
    Anything else under a secret name is redacted, however short or repetitive (PGPASSWORD=a,
    password: zz, a $-prefixed hash)."""
    return v.lower() in _PLACEHOLDERS or v.startswith("[REDACTED") or bool(_ENV_REF.fullmatch(v))


def _env_ref_at(s: str, pos: int) -> bool:
    """Whether s holds a whole env reference starting at pos (k=${X} in plain text, where the
    k=v value pattern stops at the brace)."""
    m = _ENV_REF.match(s, pos)
    return m is not None and not re.match(r"[A-Za-z0-9_$%{}]", s[m.end():m.end() + 1])


_CODE_REF = re.compile(r"(?:self|os|args|cfg|config|settings|env|environ|kwargs|params|opts|options)\b"
                       r"|[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_]")


def _code_ref(value: str, name: str) -> bool:
    """k=v where v is code, not a secret: `api_key=api_key`, `token=self.token`, `os.environ`."""
    return value.lower() == name.lower() or bool(_CODE_REF.match(value))


def _basic(m) -> str:
    """`Basic <base64>`: redacted after an Authorization header name, or anywhere if it decodes
    to user:password (so the word "basic" in prose stays)."""
    before = m.string[max(0, m.start() - 20):m.start()]
    secret = bool(_AUTHORIZATION.search(before))
    if not secret:
        try:
            secret = ":" in base64.b64decode(m.group(2), validate=True).decode("utf-8")
        except ValueError:   # binascii.Error and UnicodeDecodeError are ValueErrors
            secret = False
    return f"{m.group(1)} [REDACTED:basic-auth]" if secret else m.group(0)


class _Seg:
    """Lines of a text, where a line ends at a real or an escaped newline (see KEY_RUN_MIN). All
    positions are in the text; every look is bounded by KEY_LINE_MAX."""

    def __init__(self, s: str):
        self.s = s

    def start(self, x: int) -> int:
        """The start of the line holding position x (or x - KEY_LINE_MAX, a line too long to
        look at whole)."""
        s = self.s
        lo = max(0, x - KEY_LINE_MAX)
        i, j = s.rfind("\n", lo, x), s.rfind("\\n", lo, x)
        return max(i + 1, j + 2 if j >= 0 else 0, lo)

    def end(self, x: int) -> int:
        """The end of the line holding position x: where its newline (or the backslashes of an
        escaped one) begins; the text's end; or x + KEY_LINE_MAX."""
        s = self.s
        hi = min(len(s), x + KEY_LINE_MAX)
        i, j = s.find("\n", x, hi), s.find("\\n", x, hi)
        e = min(v for v in (i, j, hi) if v >= 0)
        if e == j:
            while e > x and s[e - 1] == "\\":
                e -= 1
        return e

    def next(self, e: int) -> int | None:
        """The start of the line after the one ending at e, or None."""
        s = self.s
        if e >= len(s):
            return None
        if s[e] == "\n":
            return e + 1
        k = e
        while k < len(s) and s[k] == "\\":
            k += 1
        return k + 1 if k > e and k < len(s) and s[k] == "n" else None

    def prev_end(self, a: int) -> int | None:
        """The end of the line before the one starting at a, or None."""
        s = self.s
        if a <= 0:
            return None
        if s[a - 1] == "\n":
            return a - 1
        if a >= 2 and s[a - 1] == "n" and s[a - 2] == "\\":
            e = a - 2
            while e > 0 and s[e - 1] == "\\":
                e -= 1
            return e
        return None

    def content(self, a: int, e: int) -> tuple[int, str]:
        """(where the looked-at text starts, the line's last KEY_LINE_MAX characters without a
        trailing CR, real or escaped)."""
        base = max(a, e - KEY_LINE_MAX)
        return base, _CR_END.sub("", self.s[base:e])

    def key(self, a: int, e: int, full: bool = False, at: int | None = None,
            head: bool = False) -> tuple[int, int] | None:
        """The span of the line's key text (see KEY_RUN_MIN), or None. full: a full-width line.
        One physical line may hold the end of one JSON string and the start of another (a
        Claude line's tool_result content and its toolUseResult copy): `at` picks the part
        between the unescaped quotes around that position; `head`, the part before the first
        unescaped quote (a line walked down into); otherwise the line's end, or failing that its
        head (the last line of a JSON string)."""
        base, c = self.content(a, e)
        if at is not None or head:
            lo, hi, i = 0, len(c), _unescaped_quote(c, 0, len(c))
            while i >= 0:
                if head or base + i >= at:
                    hi = i
                    break
                lo = i + 1
                i = _unescaped_quote(c, i + 1, len(c))
            m = _KEY_TEXT.search(_CR_END.sub("", c[:hi]), lo)
        else:
            m = _KEY_TEXT.search(c)
            if not m:               # the last line of a JSON string: key text, then its end quote
                q = _unescaped_quote(c, 0, len(c))
                m = _KEY_TEXT.search(c, 0, q) if q > 0 else None
        if not m:
            return None
        plus, text = m.group(1), m.group(2)
        for lead, t in ((plus, text), ("", plus + text)) if plus else (("", text),):
            if _KEY_FULL_LINE.fullmatch(t) or (not full and _KEY_LINE.fullmatch(t)):
                start = base + m.start(1) + len(lead)
                return start, start + len(t)
        return None

    def filler(self, a: int, e: int) -> bool:
        """An armour header or a blank line (inside a key block)."""
        _, c = self.content(a, e)
        return "-----" not in c and bool(_ARMOUR_HEADER.fullmatch(c))


class _Work:
    """The work budget of one _key_bodies call: a deadline check per line looked at, and at most
    KEY_WALK_MAX lines."""

    def __init__(self, deadline: float | None):
        self.deadline, self.n = deadline, 0

    def tick(self) -> bool:
        _check(self.deadline)
        self.n += 1
        return self.n <= KEY_WALK_MAX


def _progress(pos: int, mark: list, deadline: float | None) -> None:
    """_check(deadline) each time pos passes another REDACT_CHECK_BYTES."""
    if pos >= mark[0]:
        _check(deadline)
        mark[0] = pos + REDACT_CHECK_BYTES


def _unescaped_quote(s: str, a: int, b: int) -> int:
    """The first quote in s[a:b] not escaped by a backslash (a JSON string's end), or -1."""
    i = s.find('"', a, b)
    while i >= 0:
        k = i
        while k > 0 and s[k - 1] == "\\":
            k -= 1
        if (i - k) % 2 == 0:
            return i
        i = s.find('"', i + 1, b)
    return -1


def _new_string(s: str, a: int, b: int) -> bool:
    """Whether s[a:b] holds two unescaped quotes: one JSON string ends and another begins."""
    q = _unescaped_quote(s, a, b)
    return q >= 0 and _unescaped_quote(s, q + 1, b) >= 0


class _Edits:
    """Non-overlapping replacements, in order; `done`: nothing before it may change."""

    def __init__(self, s: str):
        self.s, self.out, self.done, self.count = s, [], 0, 0

    def span(self, a: int, b: int) -> None:
        self.out.append((a, b, "[REDACTED:private-key]"))
        self.done, self.count = b, self.count + 1

    def lines(self, parts: list) -> None:
        """One key over several lines: the whole stretch if it holds no unescaped quote, else each
        part (a line's key text, a BEGIN/END mark) on its own."""
        a, b = parts[0][0], parts[-1][1]
        if _unescaped_quote(self.s, a, b) < 0:
            self.span(a, b)
            return
        for i, (x, y) in enumerate(parts):
            self.out.append((x, y, "[REDACTED:private-key]" if i == 0 else ""))
        self.done, self.count = b, self.count + 1

    def apply(self) -> tuple[str, int]:
        if not self.out:
            return self.s, 0
        res, pos = [], 0
        for a, b, rep in self.out:
            res += [self.s[pos:a], rep]
            pos = b
        return "".join(res) + self.s[pos:], self.count


def _marked_keys(s: str, work: _Work, deadline: float | None) -> tuple[str, int]:
    """The BEGIN and orphan END rules (see KEY_RUN_MIN)."""
    import bisect
    if "-----" not in s or "PRIVATE KEY" not in s:
        return s, 0
    g, ed, mark = _Seg(s), _Edits(s), [0]
    marks = [m for m in _KEY_MARK.finditer(s) if "PRIVATE KEY" in m.group(2)]
    ends = [m.start() for m in marks if m.group(1) == "END"]
    cache = [0, 0, -1]                  # [searched from, to, the quote found there or -1]

    def string_end(p: int) -> int:
        """The next unescaped quote within KEY_SPAN_MAX of p, or -1."""
        lo, hi, q = cache
        if not (lo <= p <= hi and (q == -1 or q >= p)):
            hi = min(len(s), p + KEY_SPAN_MAX)
            cache[:] = [p, hi, _unescaped_quote(s, p, hi)]
            lo, q = p, cache[2]
        return q if q >= p and q - p <= KEY_SPAN_MAX else -1

    for m in marks:
        _progress(m.start(), mark, deadline)
        if m.start() < ed.done:
            continue                    # inside a span already taken (or walked)
        if m.group(1) == "BEGIN":
            q = string_end(m.end())
            e = g.end(m.end())
            if 0 <= q < e:
                e = q                   # the BEGIN line ends with its JSON string
            parts, end, a, n = [(m.start(), e)], None, g.next(e), 0
            while a is not None and n < KEY_BLOCK_LINES and (q < 0 or a < q) and work.tick():
                le = g.end(a)
                cut = 0 <= q < le       # the JSON string ends on this line: only its head counts
                if cut:
                    le = q
                end = _KEY_MARK.search(s, a, le)
                if end:
                    if end.group(1) != "END" or "PRIVATE KEY" not in end.group(2):
                        end = None
                    break
                k = g.key(a, le)
                if k:
                    parts.append(k)
                elif not g.filler(a, le):
                    break
                if cut:
                    break
                a, n = g.next(le), n + 1
            if end:                                      # a recognised block
                ed.lines(parts + [(end.start(), end.end())])
                continue
            # not recognised through its END: BEGIN to the matching END in the same JSON string,
            # or to that string's end (_PEM), never across an unescaped quote
            bound = min(q if q >= 0 else len(s), m.start() + KEY_SPAN_MAX)
            i = bisect.bisect_left(ends, m.end())
            if i < len(ends) and ends[i] < bound:
                nxt = _KEY_MARK.match(s, ends[i])
                ed.span(m.start(), nxt.end())
            elif q >= 0:                                 # no END: to the string's end (_PEM)
                ed.span(m.start(), q)
            elif len(parts) > 1:
                ed.lines(parts)
            else:
                ed.span(m.start(), e)
        else:                                            # an END with no BEGIN above it
            parts, a = [], g.start(m.start())
            while (pe := g.prev_end(a)) is not None and pe >= ed.done and work.tick():
                pa = max(g.start(pe), ed.done)
                k = g.key(pa, pe)
                if not k:
                    break
                parts.insert(0, k)
                if _new_string(s, pa, k[0]):    # one string ends on this line, another begins:
                    h = g.key(pa, pe, head=True)    # the earlier one's last line, if any, too
                    if h and h[1] <= k[0]:
                        parts.insert(0, h)
                    elif s[pa:_unescaped_quote(s, pa, k[0])].strip():
                        break                   # that line is no key text
                a = pa
            if parts:
                ed.lines(parts + [(m.start(), m.end())])
    return ed.apply()


def _public_above(g: _Seg, a: int, work: _Work) -> bool:
    """Whether a public BEGIN line (CERTIFICATE, PUBLIC KEY, PGP SIGNATURE...) sits right above
    the line at a, past at most a few armour headers, blank or key lines."""
    for _ in range(8):
        pe = g.prev_end(a)
        if pe is None or not work.tick():
            return False
        a = g.start(pe)
        _, c = g.content(a, pe)
        m = _KEY_MARK.search(c)
        if m:
            return m.group(1) == "BEGIN" and "PRIVATE" not in m.group(2)
        if not (g.key(a, pe) or g.filler(a, pe)):
            return False
    return False


def _key_runs(s: str, work: _Work, deadline: float | None) -> tuple[str, int]:
    """The run rule (see KEY_RUN_MIN)."""
    g, ed, mark = _Seg(s), _Edits(s), [0]
    chain: list = []

    def settle():
        if len(chain) < KEY_RUN_MIN:
            return
        lines = []                      # (start, end) of each candidate's line, in order
        for m in chain:
            x = m.end() - 1
            a, e = g.start(x), g.end(x)
            if not lines or lines[-1][:2] != (a, e):
                lines.append((a, e, x))
        run: list = []                  # (line start, line end, key span)
        for a, e, x in lines + [(None, None, None)]:
            k = g.key(a, e, full=True, at=x) if a is not None and x >= ed.done and work.tick() else None
            if k and k[0] < ed.done:
                k = None
            # a key text after a closing and an opening quote on its line is in another JSON
            # string: it can't continue a run from the string before (one quote: a quoted line)
            if k and (not run or (g.next(run[-1][1]) == a and not _new_string(s, a, k[0]))):
                run.append((a, e, k))
                continue
            if len(run) >= KEY_RUN_MIN and not _public_above(g, run[0][0], work):
                parts = [x[2] for x in run]
                pe = g.prev_end(run[0][0])
                if pe is not None and pe >= ed.done:
                    pa = g.start(pe)
                    up = g.key(pa, pe) if pa >= ed.done else None
                    if up:
                        parts.insert(0, up)              # a cut first line
                nx = g.next(run[-1][1])
                if nx is not None:
                    ne = g.end(nx)
                    down = g.key(nx, ne, head=True)
                    q1 = _unescaped_quote(s, nx, ne)
                    if not down and q1 >= 0 and not s[nx:q1].strip(" \t"):
                        down = g.key(nx, ne, at=q1 + 1)          # a quoted line: "KEY",
                    if down:
                        parts.append(down)               # the last line, a PGP =CRC
                ed.lines(parts)
            run = [(a, e, k)] if k else []

    joined = _Joined(s, deadline)       # the joined-run rule, fed the same candidates
    for m in _KEY_CANDIDATE.finditer(s):
        _progress(m.start(), mark, deadline)
        joined.add(m.start(), m.end())
        if chain:
            gap = s[chain[-1].end():m.start() + 1]      # + the "n" an escaped newline lends it
            if len(gap) > 300 or gap.count("\n") + gap.count("\\n") != 1:
                settle()
                chain = []
        chain.append(m)
    settle()
    joined.flush()
    joined.merge(ed)
    return ed.apply()


def _has_quote(s: str, a: int, b: int) -> bool:
    return s.find('"', a, b) >= 0 or s.find("'", a, b) >= 0


def _full_key_at(s: str, a: int, b: int) -> tuple[int, int] | None:
    """The full-width key text in the _KEY_CANDIDATE s[a:b]: all of it, or all but the letters an
    escape before it lends it (the "n" of \\n, the "u2192" of \\u2192)."""
    for k in (0, 1, 5):
        if (b - a - k in (64, 70) and (s[a - 1:a] == "\\") == (k > 0) and (k == 0 or s[a:a + k] in _ESC_TAILS)
                and _KEY_FULL_LINE.fullmatch(s, a + k, b)):
            return a + k, b
    return None


def _line_text(s: str, x: int, y: int) -> bool:
    """Whether s[x:y] is a line's text: it starts a JSON string or a line (after an escaped
    newline, blanks, or a line number's tab or bar), and is no bare JSON number."""
    if _JSON_NUMBER.fullmatch(s, x, y):
        return False
    j = x
    while j > 0 and s[j - 1] == " ":
        j -= 1
    c = s[j - 1:j]
    return (j == 0 or c in ('"', "'", "\n", "\t", "→", "│") or (j >= 2 and s[j - 2] == "\\" and c in "nrt")
            or s.endswith(("\\u2192", "\\u2502"), 0, j))


def _joint_down(s: str, x: int) -> tuple[int, int] | None:
    """The key text of the line after the one ending at x, past a joint (see _JOINT), or None."""
    hi = min(len(s), x + JOINT_MAX + 80)
    m = _JOINT_DOWN.match(s, x, hi)
    if (not m or (m.end() == hi < len(s) and s[hi] in _B64_MORE) or not _BOUNDARY.search(s, x, m.start(1))
            or not _line_text(s, *m.span(1))):
        return None
    return m.span(1)


def _joint_up(s: str, a: int, work: _Work) -> tuple[int, int] | None:
    """The key text of the line before the one starting at a, past a joint (a cut first line;
    see _JOINT), or None."""
    lo = max(0, a - JOINT_MAX)
    for t in reversed(list(_B64_TOKEN.finditer(s, lo, a))[-16:]):
        if not work.tick():
            return None
        x, y = t.span()
        if not _JOINT.fullmatch(s, y, a):
            return None
        if x == lo and lo > 0 and s[lo - 1] in _B64_MORE:
            return None                 # a token cut by the window
        if s[x - 1:x] == "\\":         # the letters of an escape (\n, \u2192), and a line after it
            x += next((len(e) for e in _ESC_TAILS if s.startswith(e, x)), 0)
        if (x < y and y - x <= 76 and _KEY_LINE.fullmatch(s, x, y) and _BOUNDARY.search(s, y, a)
                and not _AFTER_NUMBER.match(s, y) and _line_text(s, x, y)):
            return x, y
        # else a quoted name or word ("text") the joint went past, or the escape alone: go on
        # (the next token's joint has to get past it)
    return None


class _Joined:
    """The joined-run rule (see _JOINT): fed _key_runs' candidates in order, it chains full-width
    key texts by joints and keeps the runs that qualify (found); merge adds them to _key_runs'
    own edits where those don't cover them already. Its own work budget: a deadline check per
    candidate (and per token looked at for a cut first line), at most KEY_WALK_MAX in all."""

    def __init__(self, s: str, deadline: float | None):
        self.s, self.work = s, _Work(deadline)
        self.chain: list = []           # key text spans
        self.quoted = False             # a joint in the chain holds a quote
        self.found: list = []           # the parts of each run to redact
        self.live = True

    def add(self, a: int, b: int) -> None:
        """The next _KEY_CANDIDATE, s[a:b]."""
        if not self.live:
            return
        if not self.work.tick():
            self.live, self.chain = False, []
            return
        s = self.s
        n = b - a
        if n == 64 or n == 70:          # (not when its first letter is an escape's: \n, \t)
            k = (a, b) if s[a - 1:a] != "\\" and _KEY_FULL_LINE.fullmatch(s, a, b) else None
        elif (n == 65 or n == 71) and s[a] == "n" and s[a - 1] == "\\":     # after an escaped newline
            k = (a + 1, b) if _KEY_FULL_LINE.fullmatch(s, a + 1, b) else None
        else:
            k = _full_key_at(s, a, b)
        if k and self.chain:
            x = self.chain[-1][1]
            if k[0] - x <= JOINT_MAX:
                gap = s[x:k[0]]
                quote = '"' in gap or "'" in gap
                if (quote or "\\n" in gap or "\\r" in gap) and _JOINT.fullmatch(gap):
                    self.chain.append(k)
                    self.quoted = self.quoted or quote
                    return
        self.flush()
        if k:
            self.chain = [k]

    def flush(self) -> None:
        chain, quoted = self.chain, self.quoted
        self.chain, self.quoted = [], False
        if len(chain) < KEY_RUN_MIN:
            return
        s = self.s
        first, last = chain[0][0], chain[-1][1]
        up = _joint_up(s, first, self.work)
        top = up[0] if up else first
        mk = None
        for mk in _KEY_MARK.finditer(s, max(0, top - JOINT_MAX - 100), top):
            pass
        if mk and mk.group(1) == "BEGIN" and "PRIVATE" not in mk.group(2):
            return                      # a certificate, public key, PGP public block or signature
        down = _joint_down(s, last)
        if up:
            quoted = quoted or _has_quote(s, up[1], first)
        if down:
            quoted = quoted or _has_quote(s, last, down[0])
        if quoted:
            self.found.append(([up] if up else []) + chain + ([down] if down else []))

    def merge(self, ed: _Edits) -> None:
        """Add the found runs' key texts to ed, each on its own (the first as the placeholder),
        leaving out any that ed changes already."""
        import bisect
        if not self.found:
            return
        taken = [(a, b) for a, b, _ in ed.out]
        starts = [a for a, _ in taken]
        new, last = [], -1
        for parts in self.found:
            keep = []
            for a, b in parts:
                i = bisect.bisect_right(starts, a) - 1
                if a < last or (i >= 0 and taken[i][1] > a) or (i + 1 < len(taken) and taken[i + 1][0] < b):
                    continue
                keep.append((a, b))
                last = b
            if keep:
                new += [(a, b, "[REDACTED:private-key]" if n == 0 else "") for n, (a, b) in enumerate(keep)]
                ed.count += 1
        ed.out = sorted(ed.out + new)


def _key_bodies(s: str, deadline: float | None = None) -> tuple[str, int]:
    """Redact private keys over several lines (see KEY_RUN_MIN), raw or JSON-escaped: the text
    stays valid JSONL (see _Edits.lines). Bounded by the deadline per line looked at and by
    KEY_WALK_MAX lines in all."""
    work = _Work(deadline)
    s, n = _marked_keys(s, work, deadline)
    s, k = _key_runs(s, work, deadline)
    return s, n + k


def _redact_text(s: str, deadline: float | None = None) -> tuple[str, int]:
    """Redact one string (a decoded JSON string value, or a raw non-JSON line). A string over
    REDACT_CHECK_BYTES checks the deadline between passes."""
    n = 0
    big = len(s) > REDACT_CHECK_BYTES

    def sub(pattern, repl, text):
        nonlocal n
        if big:
            _check(deadline)
        text, k = pattern.subn(repl, text)
        n += k
        return text

    def counted(fn):
        def wrapper(m):
            nonlocal n
            out = fn(m)
            if out != m.group(0):
                n += 1
            return out
        return wrapper

    s = sub(_PEM, "[REDACTED:private-key]", s)
    s = sub(_URL, lambda m: f"{m.group(1)}{m.group(2)}:[REDACTED:url-password]@", s)
    s = sub(_APITOKEN, "APIToken=[REDACTED:api-token]", s)
    if big:
        _check(deadline)
    s = _BEARER.sub(counted(lambda m: m.group(0) if not re.search(r"[0-9._~+/=\-]", m.group(2))
                            else f"{m.group(1)} [REDACTED:bearer]"), s)
    if big:
        _check(deadline)
    s = _BASIC.sub(counted(_basic), s)
    s = sub(_SK, lambda m: "[REDACTED:anthropic-key]" if m.group(1) else "[REDACTED:api-key]", s)
    for pattern, kind in _VENDOR:
        s = sub(pattern, f"[REDACTED:{kind}]", s)
    if _AWS_ID.search(s):
        s = sub(_AWS_SECRET, "[REDACTED:aws-secret-key]", s)
    if big:
        _check(deadline)
    s = _JSON_KV.sub(counted(lambda m: m.group(0) if _plain_value(m.group(3))
                             else f'{m.group(1)}{m.group(2)}"[REDACTED:{_kind(m.group(1))}]"'), s)

    def kv(m):
        v = m.group(4)
        nxt = m.string[m.end():m.end() + 1]
        if (_plain_value(v) or nxt == "(" or _code_ref(v, m.group(1))
                or (v == "$" and nxt == "{" and _env_ref_at(m.string, m.start(4)))):
            return m.group(0)
        return f"{m.group(1)}{m.group(2)}{m.group(3)}[REDACTED:{_kind(m.group(1))}]"
    if big:
        _check(deadline)
    s = _KV.sub(counted(kv), s)
    if big:
        _check(deadline)
    s = _ENTROPY.sub(counted(lambda m: m.group(0) if not _high_entropy(m.group(3))
                             else f"{m.group(1)}{m.group(2)}[REDACTED:high-entropy]"), s)
    return s, n


def _redact_obj(obj, count: list, deadline: float | None = None, mark: list | None = None):
    """Redact every string in a parsed JSON value (and values under secret-named keys). mark:
    [characters looked at so far, the next deadline check] (_progress)."""
    mark = mark if mark is not None else [0, 0]
    if isinstance(obj, str):
        mark[0] += len(obj)
        if mark[0] >= mark[1]:
            _check(deadline)
            mark[1] = mark[0] + REDACT_CHECK_BYTES
        if not _HINT.search(obj):
            return obj
        out, k = _redact_text(obj, deadline)
        count[0] += k
        return out
    if isinstance(obj, list):
        return [_redact_obj(v, count, deadline, mark) for v in obj]
    if isinstance(obj, dict):
        out = {}
        for key, v in obj.items():
            if isinstance(v, str) and v and not _plain_value(v) and isinstance(key, str):
                if _KEY_FULL.fullmatch(key) or (_KEYISH_FULL.fullmatch(key) and _high_entropy(v)):
                    out[key] = f"[REDACTED:{_kind(key)}]"
                    count[0] += 1
                    continue
            out[key] = _redact_obj(v, count, deadline, mark)
        return out
    return obj


def redact(text: str, deadline: float | None = None) -> tuple[str, int]:
    """Replace secrets with [REDACTED:<kind>]; returns (text, how many were replaced).

    Works line by line so JSONL stays valid: a JSON line is parsed, every string in it redacted
    (a secret-named key's whole value, then the patterns), and re-serialised only if something
    changed; other lines are redacted as plain text. Lines without a hint of a secret keep their
    exact bytes. Covers sk-/sk-ant- keys, Bearer tokens, HTTP Basic credentials, APIToken=,
    the values (however short) of password/passwd/secret/token/api_key/apikey/token_secret-style
    keys such as PGPASSWORD (JSON, k=v, k: v), URL
    credentials, PEM/OpenSSH private keys and PGP private key armour (with or without their
    BEGIN line, behind line prefixes, raw or JSON-escaped: see KEY_RUN_MIN), and high-entropy
    strings next to key-like names. The deadline is checked every REDACT_CHECK_BYTES characters
    (and between the passes over one huge string): past it, OutOfTime."""
    _check(deadline)
    text, total = _key_bodies(text, deadline)   # first, on the raw text: escaped keys too
    out, seen, mark = [], 0, [0, 0]
    for line in text.splitlines(keepends=True):
        seen += len(line)
        if seen >= mark[1]:                  # by size, not line count: one line can be huge
            _check(deadline)
            mark[1] = seen + REDACT_CHECK_BYTES
        if not _HINT.search(line):
            out.append(line)
            continue
        body = line.rstrip("\r\n")
        end = line[len(body):]
        try:
            obj = json.loads(body)
        except ValueError:
            obj = None
        if isinstance(obj, (dict, list)):
            count = [0]
            new = _redact_obj(obj, count, deadline, [0, REDACT_CHECK_BYTES])
            if count[0]:
                total += count[0]
                body = json.dumps(new, ensure_ascii=False, separators=(",", ":"))
        else:
            body, k = _redact_text(body, deadline)
            total += k
        out.append(body + end)
    return "".join(out), total


# --------------------------------------------------------------------------- images

IMAGE_TYPE = "swarm-image"
IMAGE_MIN_B64 = 512      # shorter base64 strings (under ~380 bytes) stay inline
# A JSON string value (not a key, not inside another string) holding only base64, or a
# data:<type>/<subtype>;base64, URL of ANY media type (Codex's view_image writes
# data:application/octet-stream;base64,...: the bytes decide whether it's an image, not the URL's
# own type -- see extract_images.repl).
_B64_VALUE = re.compile(r'(?<!\\)"(data:([A-Za-z0-9.+\-]+/[A-Za-z0-9.+\-]+);base64,)?([A-Za-z0-9+/]{%d,}={0,2})"(?!\s*:)'
                        % IMAGE_MIN_B64)
_PLACEHOLDER = re.compile(r'\{"type":"swarm-image","sha256":"([0-9a-f]{64})","mime":"([^"\\]*)",'
                          r'"bytes":(\d+)(,"data_url":true)?\}')
_MAGIC = ((b"\x89PNG\r\n\x1a\n", "image/png"), (b"\xff\xd8\xff", "image/jpeg"),
          (b"GIF87a", "image/gif"), (b"GIF89a", "image/gif"))
IMAGE_EXT = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/webp": "webp"}


def _sniff(head: bytes) -> str | None:
    for magic, mime in _MAGIC:
        if head.startswith(magic):
            return mime
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    return None


def extract_images(text: str, deadline: float | None = None) -> tuple[str, list[TranscriptImage]]:
    """Take the images out of JSONL text: (text with each one replaced by a swarm-image object,
    the distinct images with their data). An image is a JSON string value of at least
    IMAGE_MIN_B64 base64 characters that decodes to PNG/JPEG/GIF/WebP bytes, or any
    data:image/<type>;base64, URL; whatever the block around it looks like (Claude
    source.data, a Read result's file.base64, Codex image_url). Only strings whose base64
    re-encodes to exactly the same characters are taken, so restore_images is byte-exact."""
    found: dict[str, TranscriptImage] = {}
    if len(text) < IMAGE_MIN_B64:
        return text, []

    def repl(m: re.Match) -> str:
        _check(deadline)
        prefix, url_mime, data64 = m.group(1), m.group(2), m.group(3)
        if len(data64) % 4:
            return m.group(0)
        # A data: URL of an image/* type is trusted; any other type (Codex's octet-stream) is
        # taken only if the bytes themselves sniff as an image.
        mime = url_mime if url_mime and url_mime.startswith("image/") else _sniff(base64.b64decode(data64[:24]))
        if mime is None:
            return m.group(0)
        try:
            raw = base64.b64decode(data64, validate=True)
        except ValueError:
            return m.group(0)
        if base64.b64encode(raw).decode("ascii") != data64:
            return m.group(0)
        sha = hashlib.sha256(raw).hexdigest()
        found.setdefault(sha, TranscriptImage(sha, mime, len(raw), raw))
        shown = url_mime if prefix else mime   # the placeholder keeps the URL's own type (byte-exact restore)
        return (f'{{"type":"{IMAGE_TYPE}","sha256":"{sha}","mime":{json.dumps(shown)},"bytes":{len(raw)}'
                + (',"data_url":true}' if prefix else "}"))

    return _B64_VALUE.sub(repl, text), list(found.values())


def image_refs(text: str) -> set[str]:
    """The sha256 of every image placeholder in the text."""
    return {m.group(1) for m in _PLACEHOLDER.finditer(text)}


def restore_images(text: str, lookup) -> str:
    """The text with each swarm-image placeholder replaced by the original JSON string (base64,
    or the data URL); lookup(sha256) -> the image bytes, or None to leave that placeholder."""
    def repl(m: re.Match) -> str:
        data = lookup(m.group(1))
        if data is None:
            return m.group(0)
        b = base64.b64encode(data).decode("ascii")
        return f'"data:{m.group(2)};base64,{b}"' if m.group(4) else f'"{b}"'
    return _PLACEHOLDER.sub(repl, text) if IMAGE_TYPE in text else text


from swarm.transcript_view import human_size  # noqa: E402,F401  (one definition, stdlib-only module)


# --------------------------------------------------------------------------- rows

def _compress(data: bytes, deadline: float | None) -> bytes:
    comp = lzma.LZMACompressor(preset=LZMA_PRESET)
    parts = []
    for i in range(0, len(data), CHUNK):
        _check(deadline)
        parts.append(comp.compress(data[i:i + CHUNK]))
    parts.append(comp.flush())
    return b"".join(parts)


def _head_tail(lines: list[str], budget: int) -> str:
    """The first and last lines within `budget` bytes (half each), with a marker line between."""
    head, tail, used = [], [], 0
    i, j = 0, len(lines) - 1
    half = budget // 2
    while i <= j and used + len(lines[i].encode()) <= half:
        used += len(lines[i].encode())
        head.append(lines[i])
        i += 1
    used = 0
    while j >= i and used + len(lines[j].encode()) <= half:
        used += len(lines[j].encode())
        tail.append(lines[j])
        j -= 1
    omitted = lines[i:j + 1]
    marker = json.dumps({"type": TRUNCATED_TYPE, "omitted_lines": len(omitted),
                         "omitted_bytes": sum(len(x.encode()) for x in omitted)}) + "\n"
    return "".join(head) + marker + "".join(reversed(tail))


def _fit(text: str, max_bytes: int, deadline: float | None) -> tuple[bytes, bytes]:
    """(uncompressed, compressed) of the text, cut to head+tail if it compresses over max_bytes
    or is over TRANSCRIPT_MAX_RAW uncompressed (Board.transcript_body refuses a larger body)."""
    data = text.encode("utf-8")
    raw_cap = TRANSCRIPT_MAX_RAW

    def fits(data: bytes, body: bytes | None) -> bool:
        return len(data) <= raw_cap and (not max_bytes or body is None or len(body) <= max_bytes)

    body = _compress(data, deadline) if len(data) <= raw_cap else None
    if body is not None and fits(data, body):
        return data, body
    lines = text.splitlines(keepends=True)
    budget = int(raw_cap * 0.9)
    if body is not None and max_bytes:
        budget = min(budget, int(len(data) * max_bytes / len(body) * 0.9))
    for _ in range(8):
        data = _head_tail(lines, budget).encode("utf-8")
        body = _compress(data, deadline)
        if fits(data, body):
            return data, body
        budget = int(budget * 0.7)
    data = _head_tail(lines, 0).encode("utf-8")
    return data, _compress(data, deadline)


def _prepared(text: str, deadline: float | None) -> tuple[str, int, str, list]:
    """(redacted text with image placeholders, redactions, its sha256, the images)."""
    text, images = extract_images(text, deadline)   # first: redaction never sees image data
    red, n = redact(text, deadline)
    return red, n, hashlib.sha256(red.encode("utf-8")).hexdigest(), images


def prepare_text(text: str, deadline: float | None = None) -> tuple[str, int, list]:
    """Images out, then redaction: (text with image placeholders, redactions, images)."""
    red, n, _sha, images = _prepared(text, deadline)
    return red, n, images


def compress(data: bytes, deadline: float | None = None) -> bytes:
    return _compress(data, deadline)


def _row(job, agent_key, agent_name, role, red, n, sha, images, final, host, session_id, max_bytes,
         captured_at, deadline, harness=None) -> TranscriptRow:
    data, body = _fit(red, max_bytes, deadline)   # the cap is on the text: images are apart
    if images and len(data) != len(red.encode("utf-8")):
        kept = image_refs(data.decode("utf-8"))    # cut to head+tail: only the images still shown
        images = [i for i in images if i.sha256 in kept]
    return TranscriptRow(job=job, agent_key=agent_key, agent_name=agent_name, role=role, host=host,
                         session_id=session_id, final=bool(final), raw_bytes=len(data), redactions=n,
                         sha256=sha, body=body, captured_at=captured_at, images=tuple(images),
                         harness=harness)


def make_row(job: str, agent_key: str, agent_name: str, role: str, text: str, final: bool = True,
             host: str | None = None, session_id: str | None = None, max_bytes: int = 50 * MB,
             captured_at: _dt.datetime | None = None, deadline: float | None = None,
             harness: str | None = None) -> TranscriptRow:
    """A TranscriptRow from raw JSONL text: redacted, compressed, cut to head+tail when it
    compresses over max_bytes. raw_bytes is the size of what is stored, uncompressed; sha256 is
    of the whole redacted text (before any cut)."""
    red, n, sha, images = _prepared(text, deadline)
    return _row(job, agent_key, agent_name, role, red, n, sha, images, final, host, session_id,
                max_bytes, captured_at, deadline, harness)


def capture_failed_row(job: str, agent_key: str, agent_name: str, reason: str,
                       host: str | None = None, session_id: str | None = None,
                       harness: str | None = None, role: str = "subagent") -> TranscriptRow:
    """The bodiless marker Board.mark_capture_failed stores when an agent whose final capture kept
    failing has no snapshot to keep: `reason` (from this code, never from the
    transcript; it names the transcript file's size) and no text at all, redacted or not:
    raw_bytes 0, an empty body that Board.transcript_body never returns (board.base.bodiless).
    Final, so the agent is no longer pending; any later capture replaces it."""
    reason = " ".join(str(reason or "").split())[:200] or "capture failed"
    sha = hashlib.sha256(f"swarm-capture-failed\0{job}\0{agent_key}\0{reason}".encode("utf-8", "replace")).hexdigest()
    return TranscriptRow(job=job, agent_key=agent_key, agent_name=agent_name, role=role, host=host,
                         session_id=session_id, final=True, raw_bytes=0, redactions=0, sha256=sha,
                         body=lzma.compress(b"", preset=LZMA_PRESET), harness=harness, failed=reason)


# --------------------------------------------------------------------------- reading transcripts

def _parse_ts(value) -> _dt.datetime | None:
    if not isinstance(value, str):
        return None
    try:
        ts = _dt.datetime.fromisoformat(value)
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=_dt.timezone.utc)


def _line_ts(line: bytes) -> _dt.datetime | None:
    try:
        obj = json.loads(line)
    except ValueError:
        return None
    return _parse_ts(obj.get("timestamp")) if isinstance(obj, dict) else None


def _first_ts_from(fh, offset: int) -> tuple[int, _dt.datetime | None]:
    """(start of the first whole line at or after offset that has a timestamp, its timestamp);
    (end of file, None) if none within a few lines."""
    fh.seek(offset)
    if offset:
        fh.readline()
    for _ in range(50):
        pos = fh.tell()
        line = fh.readline()
        if not line:
            break
        ts = _line_ts(line)
        if ts is not None:
            return pos, ts
    return fh.tell(), None


def _codex_slice(path, start: _dt.datetime, end: _dt.datetime | None,
                 deadline: float | None) -> str:
    """read_slice for a Codex rollout (plain or .zst): read_rollout validates and decompresses
    it whole (rollouts are one session's worth, not the multi-gigabyte logs read_slice's
    bisection is for), then the same timestamp-window filter, linearly (no bisection: nothing to
    seek in decompressed text). Unreadable (RolloutUnreadable): logged, "" like a missing file."""
    from swarm.hosts.codex import RolloutTooLarge, RolloutUnreadable, read_rollout
    try:
        text = read_rollout(Path(path), RAW_READ_MAX, deadline)
    except RolloutTooLarge as exc:
        raise TooLarge(f"session rollout {human_size(exc.size)}+") from exc
    except RolloutUnreadable as exc:
        log(f"transcripts: {exc}")
        return ""
    out, inside = [], False
    for i, line in enumerate(text.splitlines(keepends=True)):
        if i % 2000 == 0:
            _check(deadline)
        ts = _line_ts(line.encode("utf-8"))
        if ts is None:
            if inside:
                out.append(line)
            continue
        if ts < start:
            continue
        if end is not None and ts > end:
            break
        inside = True
        out.append(line)
    sliced = "".join(out)
    return sliced if not sliced or sliced.endswith("\n") else sliced + "\n"


def read_slice(path, start: _dt.datetime, end: _dt.datetime | None,
               deadline: float | None = None, harness: str | None = None) -> str:
    """The lines of a session transcript whose timestamps fall within [start, end] (end None:
    up to the end of the file). Lines without a timestamp are kept when they sit inside the
    window. Timestamps grow through a session file, so the start is found by bisecting byte
    offsets; only the window is read. Missing file: ""."""
    if harness == "codex":
        return _codex_slice(path, start, end, deadline)
    try:
        fh = open(path, "rb")
    except OSError:
        return ""
    with fh:
        size = os.fstat(fh.fileno()).st_size
        lo, hi = 0, size
        while hi - lo > 65536:
            _check(deadline)
            mid = (lo + hi) // 2
            pos, ts = _first_ts_from(fh, mid)
            if ts is not None and ts < start:
                lo = pos
            else:
                hi = mid
        fh.seek(lo)
        if lo:
            # lo is a line start (from _first_ts_from) or 0
            pass
        out, inside, kept = [], False, 0
        while True:   # line by line, never one longer than the cap, the deadline on each
            _check(deadline)
            line = fh.readline(TRANSCRIPT_MAX_RAW + 1)
            if not line:
                break
            if len(line) > TRANSCRIPT_MAX_RAW and not line.endswith(b"\n"):
                raise TooLarge(f"a session line over {human_size(TRANSCRIPT_MAX_RAW)}")
            ts = _line_ts(line)
            if ts is None:
                if inside:
                    out.append(line)
                    kept += len(line)
                    if kept > TRANSCRIPT_MAX_RAW:
                        raise TooLarge(f"session slice over {human_size(TRANSCRIPT_MAX_RAW)}")
                continue
            if ts < start:
                continue
            if end is not None and ts > end:
                break
            inside = True
            out.append(line)
            kept += len(line)
            if kept > TRANSCRIPT_MAX_RAW:
                raise TooLarge(f"session slice over {human_size(TRANSCRIPT_MAX_RAW)}")
    text = b"".join(out).decode("utf-8", errors="replace")
    return text if not text or text.endswith("\n") else text + "\n"


def _read(path, harness: str | None = None, deadline: float | None = None) -> str | None:
    """The transcript's text, or None (never stored: `capture_subagent` treats that as nothing to
    write). Claude: read and decoded as before. Codex (plain or .zst): `read_rollout`, which
    validates and decompresses; an unreadable rollout (RolloutUnreadable) is logged and returns
    None here too -- capturing nothing, never an empty transcript."""
    if harness == "codex":
        from swarm.hosts.codex import RolloutTooLarge, RolloutUnreadable, read_rollout
        try:
            return read_rollout(Path(path), RAW_READ_MAX, deadline)
        except RolloutTooLarge as exc:
            raise TooLarge(f"rollout {human_size(exc.size)}+ (decompressed or on disk)") from exc
        except RolloutUnreadable as exc:
            log(f"transcripts: {exc}")
            return None
    try:
        return Path(path).read_bytes().decode("utf-8", errors="replace")
    except OSError:
        return None


def _mtime(path) -> _dt.datetime | None:
    """The file's mtime; None if missing or not a regular file (a FIFO would block the read)."""
    try:
        st = os.stat(path)
    except (OSError, ValueError):
        return None
    if not stat.S_ISREG(st.st_mode):
        return None
    return _dt.datetime.fromtimestamp(st.st_mtime, _dt.timezone.utc)


def projects_dir() -> Path:
    from swarm.hosts.claude import projects_dir as p
    return p()


def find_session_transcript(session_id: str | None, hint=None) -> Path | None:
    """The main transcript of a Claude session: `hint` if it is that file, else
    <projects_dir>/*/<session_id>.jsonl."""
    from swarm import hosts
    return hosts.get("claude").find_session_transcript(session_id, hint)


def subagent_path(main_transcript, agent_id: str) -> Path | None:
    """<main transcript minus .jsonl>/subagents/agent-<agent_id>.jsonl."""
    from swarm.hosts.claude import subagent_path as p
    return p(main_transcript, agent_id)


def session_transcript(session_id: str | None, hint=None) -> tuple[Path | None, str | None]:
    """The orchestrating session's transcript and its host: Claude's projects dir, then Codex's
    sessions dir (session ids are UUIDs on both, so there's no clash)."""
    from swarm import hosts
    for name in ("claude", "codex"):
        p = hosts.get(name).find_session_transcript(session_id, hint)
        if p is not None:
            return p, name
    return None, None


def agent_transcript(main: Path | None, agent_key: str, harness: str | None) -> Path | None:
    """Where `agent_key`'s transcript is, according to its own host's adapter (defaulting to
    Claude for an agent whose harness was never recorded)."""
    from swarm import hosts
    try:
        return hosts.get(harness or "claude").find_agent_transcript(main, agent_key)
    except KeyError:
        return None


# --------------------------------------------------------------------------- capture

def _host() -> str:
    return os.uname().nodename


def _existing(board, job: str, agent_key: str):
    rows = board.transcripts(job=job, agent_key=agent_key)
    return rows[0] if rows else None


def _store(board, cfg: dict, job, agent_key, agent_name, role, text, final, session_id,
           old, deadline, harness=None) -> bool:
    red, n, sha, images = _prepared(text, deadline)
    if old is not None and old.sha256 == sha:   # unchanged content: only record that it was checked
        board.refresh_transcript(job, agent_key, final)
        return False
    s = settings(cfg)
    row = _row(job, agent_key, agent_name, role, red, n, sha, images, final, _host(), session_id,
               int(float(s["max_mb"]) * MB), None, deadline, harness)
    _check(deadline)
    saved = board.save_transcript(row)
    if saved:
        rotate(board, cfg)
    return saved


def capture_subagent(board, cfg: dict, job: str, agent_key: str, path, final: bool,
                     agent_name: str | None = None, session_id: str | None = None,
                     deadline: float | None = None, harness: str | None = None,
                     use_mtime: bool = True) -> bool:
    """Store (or refresh) the transcript of one subagent from its transcript file. Returns
    whether it wrote. No-op (False) when disabled, the file is missing, or nothing changed.
    With use_mtime (the periodic snapshots), a file not modified since the stored capture is
    not even read (unless this capture would make it final, or the row is a capture-failed
    marker); without it (the owner's stop and finalize captures) the file is always read, and
    unchanged content refreshes the row.
    `harness` ("claude", "codex", or None to mean Claude) says which host's transcript format
    `path` is in (for reading) and is stored on the row (for the HOST column)."""
    if not enabled(cfg) or not path:
        return False
    mtime = _mtime(path)
    if mtime is None:
        return False
    old = _existing(board, job, agent_key)
    if use_mtime and old is not None and not old.failed and mtime < old.captured_at and (old.final or not final):
        return False
    _check(deadline)   # past it, not even read (capture_job goes on to the next agent at once)
    try:
        size = os.stat(path).st_size
    except OSError:
        return False
    try:
        if size > RAW_READ_MAX:   # never read, never cut unredacted: a final gets a capture-failed mark
            raise TooLarge(f"transcript file {human_size(size)}")
        text = _read(path, harness, deadline)
    except TooLarge as exc:
        if not final:
            return False
        from swarm.supervisor import lost
        why = f"too large to capture: {exc}, over the {human_size(RAW_READ_MAX)} read limit"
        log(f"transcripts: {agent_key} (job {job}) {why}")
        return lost.mark_failed(board, job, agent_key, agent_name, session_id, harness or "claude", why) != "kept"
    if not text:
        return False
    if agent_name is None:
        state = board.sync_state(agent_key)
        agent_name = state.name if state else (old.agent_name if old else agent_key)
    return _store(board, cfg, job, agent_key, agent_name, "subagent", text, final, session_id,
                  old, deadline, harness)


def _board_key(cfg: dict | None) -> str | None:
    if not cfg:
        return None
    try:
        from swarm.board.autoinit import store_key
        return store_key(cfg)
    except Exception:
        return None


def enrolled(agent, cfg: dict | None) -> "enrolment.Record | None":
    """The local enrolment record of a board agent row: this host user enrolled that agent_key on
    this board for the row's job (enrolment.py, written by the unsandboxed hook). None for
    anything else. The row's host and os_user are display data only: agents and the other OS
    user can write any value there."""
    board_key = _board_key(cfg)
    key, job = getattr(agent, "agent_key", None), getattr(agent, "job", None)
    if board_key is None or not isinstance(key, str) or not isinstance(job, str):
        return None
    try:
        rec = enrolment.find(board_key, key)
    except (OSError, ValueError):
        return None
    return rec if rec is not None and rec.job == job else None


def owns(agent, cfg: dict | None = None) -> bool:
    """Whether this host user enrolled the agent here (so may read and store its transcript):
    a local enrolment record for this board (cfg) and the row's agent_key and job. Without a
    cfg nothing is owned."""
    return enrolled(agent, cfg) is not None


def activation(cfg: dict | None, job: str) -> "enrolment.Record | None":
    """The local record that this host user's session activated `job` on this board; its
    session, harness and time (the snapshot window start) are the only ones capture trusts
    for the orchestrator."""
    board_key = _board_key(cfg)
    if board_key is None or not isinstance(job, str):
        return None
    try:
        return enrolment.find_job(board_key, job)
    except (OSError, ValueError):
        return None


def _record_time(rec) -> _dt.datetime:
    return _dt.datetime.fromtimestamp(rec.created_at, _dt.timezone.utc)


def _orchestrator(cfg: dict, job: str, hints=None, hint=None):
    """(session transcript, harness, activation record) of the job's orchestrator, from the local
    activation record only (the board's session_id and activated_at are not used). `hints`:
    {session_id: transcript path} from the calling hook, or `hint` a path; either is used only
    if the host adapter confirms it is that session's transcript."""
    rec = activation(cfg, job)
    if rec is None or not rec.session_id:
        return None, None, rec
    from swarm import hosts
    try:
        path = hosts.get(rec.harness).find_session_transcript(rec.session_id,
                                                              hint or (hints or {}).get(rec.session_id))
    except KeyError:
        path = None
    return path, (rec.harness if path is not None else None), rec


def _enrolled_transcript(rec, mains: dict, fallback_main: Path | None) -> Path | None:
    """Where an enrolled agent's transcript is, from its record: a Codex rollout by its own key,
    a Claude subagent file under its (recorded) parent session's transcript."""
    if rec.harness == "claude":
        if rec.session_id not in mains:
            from swarm import hosts
            mains[rec.session_id] = hosts.get("claude").find_session_transcript(rec.session_id) \
                if rec.session_id else None
        main = mains[rec.session_id] or fallback_main
        return agent_transcript(main, rec.agent_key, "claude")
    return agent_transcript(None, rec.agent_key, rec.harness)


# A subagent transcript file larger than this is never read: reading and
# redacting it would cost memory and time out anyway, and only TRANSCRIPT_MAX_RAW (128 MiB) of
# text can be stored. Twice that, because base64 images (taken out before redaction and stored
# apart) can make a file much larger than its text. A final capture of such a file gets a
# capture-failed mark at once (reason: too large), never an unredacted cut.
RAW_READ_MAX = 2 * TRANSCRIPT_MAX_RAW
FINALIZE_PER_SWEEP = 20   # final captures per sweep at most (each is bounded by the deadline too)
# Pending finals (swarm.supervisor.lost): a failed final capture is retried, by the
# supervisor pass once it failed at a real budget, each retry with FINAL_RETRY_SECONDS; after
# FINAL_RETRY_MAX retries that fail with that full budget the agent gets a capture-failed row
# (capture_failed_row). 3: a transcript that three 60 s tries can't redact won't make it on a
# fourth, and an agent's archive is settled within ~3 supervisor passes.
FINAL_RETRY_SECONDS = 60.0
FINAL_RETRY_MAX = 3
SLOW_AFTER_SECONDS = 2.0   # a failure with at least this budget leaves the retry to the pass
SWEEP_SECONDS = 60.0       # sweep_jobs' budget when its caller gives none (one-shot CLI commands)


def codex_source(board, cfg: dict):
    """(swarm.supervisor.lost.Source of this host user's pending Codex finals, finish()): each
    ended Codex agent enrolled here (`owns`) whose stored row is missing or not final yet (its
    stop capture failed, it was completed by another user's sweep, or its job was closed by
    someone else). A rollout not found is logged once and listed (_lost_rollouts_file), then
    left out while it stays pending; finish() writes that list."""
    import getpass
    from types import SimpleNamespace
    from swarm import hosts
    from swarm.hosts import codex
    from swarm.supervisor import lost as _lost
    since = board.now() - _dt.timedelta(days=float(settings(cfg)["retention_days"]))
    pending = board.pending_final_transcripts(_host(), getpass.getuser(), "codex", since)
    # the board's host/os_user only narrow the query; the local enrolment record decides
    pending = [(job, key) for job, key in pending
               if getattr(enrolled(SimpleNamespace(job=job, agent_key=key), cfg), "harness", None) == "codex"]
    if not pending:
        return _lost.Source("codex", [], None), lambda: None
    lost_file = _lost_rollouts_file(cfg)
    lost = _read_lost(lost_file) & {f"{job}\t{key}" for job, key in pending}   # forget the resolved
    before = set(lost)
    todo = [(job, key) for job, key in pending if f"{job}\t{key}" not in lost]

    def locate(job, key):
        path = hosts.get("codex").find_agent_transcript(None, key)
        if path is None:   # logged once, then skipped, so lost rollouts can't starve newer agents
            log(f"transcripts: rollout of {key} (job {job}) not found under {codex.codex_home()}; not finalized")
            lost.add(f"{job}\t{key}")
        return None, None, path

    def finish():
        if lost != before:
            _write_lost(lost_file, lost)
    return _lost.Source("codex", todo, locate), finish


def finalize_owned(board, cfg: dict, deadline: float | None = None, retry: bool = False) -> int:
    """Owner side: store the final transcript of each pending Codex agent (codex_source).
    Non-owners never touch transcript rows. Least recently tried first
    (swarm.supervisor.lost._finalize, whose supervisor-pass retries take the ones that keep
    failing: `retry`), so agents whose capture keeps failing can't starve the others. One query
    when there is nothing to do."""
    if not enabled(cfg):
        return 0
    from swarm.supervisor import lost as _lost
    source, finish = codex_source(board, cfg)
    try:
        return _lost._finalize(board, cfg, [source], deadline, retry)
    finally:
        finish()


def _lost_rollouts_file(cfg: dict) -> Path:
    """This board's list of owned Codex agents whose rollout wasn't found (one "job<TAB>key" a
    line). A listed agent is not retried while it stays pending (its row missing or non-final
    within retention_days); it drops off the list once it leaves the pending list. Kept simple
    on purpose: a rollout that reappears later is not finalized by the sweeps, and the stored
    row (if any) stays non-final."""
    from swarm.board.autoinit import store_key
    return paths.host_dir() / f"transcripts-lost-rollouts-{store_key(cfg)}.txt"


def _read_lost(path: Path) -> set[str]:
    """The list, read without following a link (safefs); anything unsafe or missing: empty."""
    try:
        with safefs.dir_fd(Path(path).parent, create=False, strict_mode=0o700) as d:
            text = safefs.read_text(d, Path(path).name)
    except (OSError, ValueError):
        return set()
    return {line for line in (text or "").splitlines() if line}


def _write_lost(path: Path, lost: set[str]) -> None:
    try:   # own random temp file, atomic rename, 0600; a planted link is replaced, not followed
        with safefs.dir_fd(Path(path).parent, strict_mode=0o700) as d:
            safefs.write_atomic(d, Path(path).name, "".join(f"{line}\n" for line in sorted(lost)))
    except (OSError, ValueError) as exc:
        log(f"transcripts: can't record lost rollouts in {path}: {getattr(exc, 'strerror', None) or exc}")


def capture_orchestrator(board, cfg: dict, job: str, session_path, start: _dt.datetime,
                         end: _dt.datetime | None, final: bool, session_id: str | None = None,
                         deadline: float | None = None, harness: str | None = None) -> bool:
    """Store the slice [start, end] of the orchestrating session's transcript as the job's
    "orchestrator" transcript. Returns whether it wrote. `harness`: as `capture_subagent`."""
    if not enabled(cfg) or not session_path:
        return False
    old = _existing(board, job, ORCHESTRATOR_KEY)
    mtime = _mtime(session_path)
    if mtime is None:
        return False
    if old is not None and not old.failed and mtime < old.captured_at and (old.final or not final):
        return False
    _check(deadline)
    try:
        text = read_slice(session_path, start, end, deadline, harness)
    except TooLarge as exc:   # never cut unredacted: a final gets a capture-failed mark
        if not final:
            return False
        from swarm.supervisor import lost
        why = f"too large to capture: {exc}"
        log(f"transcripts: orchestrator of {job} {why}")
        return lost.mark_failed(board, job, ORCHESTRATOR_KEY, ORCHESTRATOR_NAME,
                                session_id or Path(session_path).stem, harness or "claude", why,
                                role="orchestrator") != "kept"
    if not text:
        return False
    return _store(board, cfg, job, ORCHESTRATOR_KEY, ORCHESTRATOR_NAME, "orchestrator", text, final,
                  session_id or Path(session_path).stem, old, deadline, harness)


def capture_job(board, cfg: dict, job: str, final: bool, deadline: float | None = None,
                session_hint=None, agents: bool = True, warn=None) -> int:
    """Capture a job's orchestrator slice (when its session transcript is found) and
    (agents=True) its agents enrolled here (`owns`: a local enrolment record) -- agent
    capture does not depend on the root/orchestrator transcript being found: a Codex agent's
    rollout is looked up by its own agent_key (`agent_transcript(None, ...)` for Codex ignores
    `main` entirely), so a missing or not-yet-written root never blocks capturing an agent whose
    own rollout exists. For deactivate and auto-close (final=True) and snapshots. Returns how
    many were written.

    Each capture fails alone: running out of time or any other failure is reported
    through `warn` (default: the hook error log) and the rest go on, smallest transcript first,
    so one agent's huge or adversarial transcript can't cost the others their final capture
    within a shared deadline. A failed final capture of an agent is recorded as a pending final
    (swarm.supervisor.lost.note_pending), which the sweeps and the supervisor pass retry."""
    if not enabled(cfg):
        return 0
    js = board.job_status(job)
    if js is None:
        return 0
    say = warn or log
    main, main_host, rec = _orchestrator(cfg, job, hint=session_hint)
    n = 0
    if main is not None:   # only a job this host user activated, from its own activation on
        try:
            n += capture_orchestrator(board, cfg, job, main, _record_time(rec), js.finished_at, final,
                                      rec.session_id, deadline, main_host)
        except Exception as exc:
            say(f"transcripts: capture of {job} orchestrator failed: {type(exc).__name__}: {exc}")
    if agents:
        from swarm.supervisor import lost
        start = js.activated_at or js.created_at
        mains: dict = {}
        todo = []
        for a in board.agents(job):
            if a.ended_at is not None and a.ended_at < start:
                continue
            arec = enrolled(a, cfg)
            if arec is None:     # not enrolled here: only its owner writes its transcript
                continue
            path = _enrolled_transcript(arec, mains, main if main_host == "claude" else None)
            if path is None:
                continue
            try:
                size = os.stat(path).st_size
            except OSError:
                size = 0
            todo.append((size, a, arec, path))
        for _size, a, arec, path in sorted(todo, key=lambda t: t[0]):
            is_final = final or a.ended_at is not None
            began = time.monotonic()
            try:
                n += capture_subagent(board, cfg, job, a.agent_key, path, is_final, a.name,
                                      arec.session_id, deadline, harness=arec.harness)
            except Exception as exc:
                say(f"transcripts: capture of {a.agent_key} on {job} failed: {type(exc).__name__}: {exc}")
                if is_final:
                    lost.note_pending(cfg, job, a.agent_key, arec.harness, exc,
                                      None if deadline is None else deadline - began)
    return n


def capture_closed(board, cfg: dict, jobs, deadline: float | None = None, warn=None) -> int:
    """Final captures of jobs that were just closed (auto-close). Failures are reported through
    `warn` (default: the hook error log), never raised; each agent's fails alone (capture_job)."""
    if not enabled(cfg):
        return 0
    n = 0
    for job in jobs:
        try:
            n += capture_job(board, cfg, job, True, deadline, warn=warn)
        except Exception as exc:
            (warn or log)(f"transcripts: capture of {job} failed: {type(exc).__name__}: {exc}")
    return n


# --------------------------------------------------------------------------- snapshots

def snapshot_stamp(cfg: dict) -> Path:
    """This board's snapshot stamp: <host_dir>/transcripts-snapshot-<store>.stamp (per board, so
    a round on one board doesn't delay another's). Only ever opened through safefs."""
    from swarm.board.autoinit import store_key
    return paths.host_dir() / f"transcripts-snapshot-{store_key(cfg)}.stamp"


def _host_dir(create: bool = True):
    """A verified descriptor of the host-private dir (0700, this user's, no links on the way)."""
    return safefs.dir_fd(paths.host_dir(), create=create, strict_mode=0o700)


def snapshot_due(board, cfg: dict) -> bool:
    """Whether a snapshot round is due on this machine: enabled, snapshot_minutes > 0 and the
    last round (a stamp file's mtime) older than that. A stat, no board access."""
    if not enabled(cfg):
        return False
    minutes = float(settings(cfg)["snapshot_minutes"] or 0)
    if minutes <= 0:
        return False
    try:
        with _host_dir(create=False) as d:
            mtime = safefs.mtime(d, snapshot_stamp(cfg).name)
    except FileNotFoundError:
        return True
    except (OSError, ValueError):
        return False
    return mtime is None or time.time() - mtime >= minutes * 60


def snapshot(board, cfg: dict, deadline: float | None = None, hints=None) -> int:
    """run_snapshots if snapshot_due: what the Start/Stop hooks and watch/tail call."""
    return run_snapshots(board, cfg, deadline, hints) if snapshot_due(board, cfg) else 0


def run_snapshots(board, cfg: dict, deadline: float | None = None, hints=None) -> int:
    """One snapshot round: re-capture (final=False), for every active job, the orchestrator slice
    if the job was activated here (a local activation record; the slice starts at that record's
    time, from the recorded session) and the running agents enrolled here. Claims the round first (the
    stamp is touched under a non-blocking lock; a concurrent round returns 0), so a round that
    runs out of time is not retried before the next one is due. `hints`: {session_id: main
    transcript path} known to the caller. Returns how many transcripts were written."""
    if not enabled(cfg):
        return 0
    try:   # claim the round: a private regular file (no link, FIFO or hard link), locked
        with _host_dir() as d:
            fd = safefs.lock(d, snapshot_stamp(cfg).name, blocking=False)
    except (OSError, ValueError):   # held by a concurrent round, or unsafe
        return 0
    try:   # the machine-wide stamp from before stamps were per board (unlink never follows)
        (Path(STATE_DIR).expanduser() / SNAPSHOT_STAMP).unlink(missing_ok=True)
    except OSError:
        pass
    try:
        os.utime(fd)
        n = 0
        mains: dict = {}
        for js in board.jobs(False):
            # the orchestrator only of jobs activated here, sliced from the local activation
            main, main_host, rec = _orchestrator(cfg, js.job, hints)
            _check(deadline)
            if main is not None:
                n += capture_orchestrator(board, cfg, js.job, main, _record_time(rec),
                                          None, False, rec.session_id, deadline, main_host)
            # ended agents (this snapshot round or earlier) are finalize_owned's job, which also
            # covers a job closed mid-round; only running agents are snapshotted here. Agent
            # capture does not depend on the root/orchestrator transcript being found (see
            # capture_job): a missing root never blocks a Codex agent whose own rollout exists.
            for a in board.agents(js.job, include_departed=False):
                arec = enrolled(a, cfg)
                if arec is None:
                    continue
                path = _enrolled_transcript(arec, mains, main if main_host == "claude" else None)
                if path is None:
                    continue
                n += capture_subagent(board, cfg, js.job, a.agent_key, path, False, a.name,
                                      arec.session_id, deadline, harness=arec.harness)
        return n
    finally:
        os.close(fd)


# --------------------------------------------------------------------------- rotation

def log(message: str) -> None:
    """Append one line to the hook error log in the host-private dir (never raises). The message
    is escaped to a single printable line (it carries board and payload data)."""
    try:
        with _host_dir() as d:
            safefs.append(d, LOG_NAME, f"{time.strftime('%F %T')} {safefs.log_safe(message)}\n")
    except Exception:
        pass


def rotate(board, cfg: dict, warn=None) -> int:
    """Apply retention_days and max_total_mb (Board.rotate_transcripts, active jobs spared).
    If the active jobs alone hold more than max_total_mb, `warn` (default: the hook error
    log) gets one line. Returns how many rows were deleted."""
    if not enabled(cfg):
        return 0
    s = settings(cfg)
    max_bytes = int(float(s["max_total_mb"] or 0) * MB)
    active = [js.job for js in board.jobs(False)] + [js.job for js in board.jobs(True) if js.status == "paused"]
    deleted = board.rotate_transcripts(float(s["retention_days"] or 0), max_bytes, active)
    if max_bytes > 0:
        stored = board.transcript_totals().stored
        if stored > max_bytes:
            msg = (f"transcripts: {human_size(stored)} stored by active jobs alone, over max_total_mb = "
                   f"{s['max_total_mb']} ({human_size(max_bytes)}); nothing more to delete until they close")
            if warn is not None:
                warn(msg)
            elif _over_limit_news(cfg, active):
                log(msg)
    return deleted


def _over_limit_news(cfg: dict, active: list) -> bool:
    """Whether the over-limit warning is worth logging: not yet for this set of active jobs on
    this board, or last logged over OVER_LIMIT_EVERY ago (a stamp file holds the set)."""
    from swarm.board.autoinit import store_key
    name = f"transcripts-over-limit-{store_key(cfg)}"
    key = hashlib.sha256("\n".join(sorted(active)).encode()).hexdigest()
    try:
        with _host_dir() as d:
            mtime = safefs.mtime(d, name)
            if (safefs.read_text(d, name, 256) == key and mtime is not None
                    and time.time() - mtime < OVER_LIMIT_EVERY):
                return False
            safefs.write_atomic(d, name, key)   # never truncates a planted link's target
    except (OSError, ValueError):
        pass
    return True
