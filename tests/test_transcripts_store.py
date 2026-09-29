"""bin/transcripts.py: redaction, rows, the capture functions and rotation (memory board)."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import lzma
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from support import MemoryHarness  # noqa: F401  (sets sys.path)

from swarm import transcripts as T  # noqa: E402

UTC = dt.timezone.utc


def entry(ts: str | None, text: str, kind: str = "assistant") -> str:
    e = {"type": kind, "message": {"role": kind, "content": [{"type": "text", "text": text}]}}
    if ts:
        e["timestamp"] = ts
    return json.dumps(e)


class RedactTest(unittest.TestCase):
    def check(self, text: str, secret: str, kind: str | None = None) -> str:
        out, n = T.redact(text)
        self.assertNotIn(secret, out)
        self.assertGreaterEqual(n, 1)
        if kind:
            self.assertIn(f"[REDACTED:{kind}]", out)
        return out

    def test_plain_text_is_untouched(self):
        text = 'Ran 12 tests, input_tokens 300, the token budget is fine; password rules: see docs\n'
        self.assertEqual(T.redact(text), (text, 0))

    def test_api_keys(self):
        self.check("key sk-ant-api03-AbCdEfGhIjKlMnOpQrStUv_wx-yz0123 here", "AbCdEfGhIjKlMnOp")
        self.check("OPENAI sk-proj0123456789abcdefABCDEF", "sk-proj0123456789abcdef")
        self.assertEqual(T.redact("a task-list and a disk-image")[1], 0)

    def test_bearer_and_api_token(self):
        out = self.check("curl -H 'Authorization: Bearer eyJhbGciOi.JIUzI1NiJ9.abcdefgh'", "eyJhbGciOi")
        self.assertIn("Bearer [REDACTED", out)
        out = self.check("Authorization: APIToken=user@realm!ci=1234abcd-5678-90ef-aaaa-bbbbccccdddd",
                         "1234abcd-5678")
        self.assertIn("APIToken=[REDACTED", out)

    def test_key_value_forms(self):
        self.check('{"password": "hunter2hunter2"}', "hunter2")
        self.check("PGPASSWORD=s3cr3t-Value psql", "s3cr3t-Value")
        self.check("api_key: abcd1234efgh", "abcd1234efgh")
        self.check("token_secret = 'Zm9vYmFyYmF6'", "Zm9vYmFyYmF6")
        self.check('"apikey":"q1w2e3r4t5"', "q1w2e3r4t5")

    def test_url_credentials(self):
        out = self.check("git clone https://alice:Pa55word@git.example.internal/x.git", "Pa55word")
        self.assertIn("git.example.internal/x.git", out)
        self.assertEqual(T.redact("see https://example.com/a@b")[1], 0)

    def test_pem_private_key(self):
        pem = "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAAA\nQUJD\n-----END OPENSSH PRIVATE KEY-----"
        out = self.check(f"here:\n{pem}\nafter", "b3BlbnNzaC1rZXktdjEAAAAA", "private-key")
        self.assertIn("after", out)

    def test_high_entropy_next_to_a_key_name(self):
        self.check("export HINDSIGHT_KEY Xy7Qp2Lm9Rt4Vw8Zb3Nc6Hj1Kd5Fg0 ok", "Xy7Qp2Lm9Rt4Vw8Zb3Nc6Hj1Kd5Fg0")
        # the same kind of string with no key-like name next to it is left alone (commit ids etc.)
        text = "commit 9f2c4e1a7b3d5f6e8a0c2e4f6a8b0c2d4e6f8a0b merged"
        self.assertEqual(T.redact(text), (text, 0))

    def test_jsonl_stays_valid_and_escaped_secrets_are_caught(self):
        lines = [
            json.dumps({"type": "user", "message": {"content": 'set "password": "abc123xyz" and "q"'}}),
            json.dumps({"type": "assistant", "toolUseResult": {"stdout": "Bearer abc123defghijklmnop\n\\ end"}}),
            json.dumps({"type": "x", "input": {"token": "tok_live_ABCDEFGH", "n": 3, "list": ["sk-ant-" + "a" * 30]}}),
            "not json at all password=zq9zq9zq",
        ]
        out, n = T.redact("\n".join(lines) + "\n")
        self.assertEqual(n, 5)
        parsed = out.splitlines()
        for line in parsed[:3]:
            json.loads(line)
        for secret in ("abc123xyz", "abc123defghijklmnop", "tok_live_ABCDEFGH", "a" * 30, "zq9zq9zq"):
            self.assertNotIn(secret, out)
        self.assertEqual(json.loads(parsed[2])["input"]["n"], 3)
        self.assertTrue(out.endswith("\n"))

    def test_unchanged_lines_keep_their_exact_bytes(self):
        line = '{"b":1,  "a":"x"}'
        self.assertEqual(T.redact(line + "\n"), (line + "\n", 0))


# Synthetic tokens in each vendor's documented shape (none is live).
VENDOR_TOKENS = [
    ("ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8", "github-token"),
    ("gho_" + "Z9y8X7w6V5u4T3s2R1q0P9o8N7m6L5k4J3i2", "github-token"),
    ("ghs_" + "0123456789abcdefghijABCDEFGHIJ012345", "github-token"),
    ("github_pat_" + "11ABCDEFG0123456789abc_" + "x" * 30 + "Y7" * 15, "github-token"),
    ("glpat-" + "Ab1Cd2Ef3Gh4Ij5Kl6Mn", "gitlab-token"),
    ("xoxb-" + "123456789012-1234567890123-AbCdEfGhIjKlMnOpQrStUvWx", "slack-token"),
    ("xoxp-" + "123456789012-123456789012-123456789012-0123456789abcdef0123456789abcdef", "slack-token"),
    ("AKIA" + "IOSFODNN7EXAMPLE", "aws-access-key-id"),
    ("ASIA" + "Q3EGRBSYWVV4ZX2H", "aws-access-key-id"),
    ("AIza" + "SyA1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q", "google-api-key"),
    ("hf_" + "AbCdEfGhIjKlMnOpQrStUvWxYz0123456789", "huggingface-token"),
    ("npm_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8", "npm-token"),
    ("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9" + ".eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4ifQ"
     + ".SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c", "jwt"),
]


class VendorTokenTest(unittest.TestCase):
    """Bare provider tokens, with no key-like name next to them, are redacted."""

    def test_redacts_vendor_tokens(self):
        for token, kind in VENDOR_TOKENS:
            with self.subTest(kind=kind, token=token[:12]):
                for text in (f"output: {token} done\n",
                             json.dumps({"type": "user", "toolUseResult": {"stdout": f"x {token} y"}}) + "\n",
                             json.dumps({"k": token}) + "\n"):
                    out, n = T.redact(text)
                    self.assertNotIn(token, out)
                    self.assertIn(f"[REDACTED:{kind}]", out)
                    self.assertGreaterEqual(n, 1)
                    if text.startswith("{"):
                        json.loads(out)

    def test_aws_secret_key_only_next_to_a_key_id(self):
        secret = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
        out, _ = T.redact(f"aws AKIAIOSFODNN7EXAMPLE {secret}\n")
        self.assertNotIn(secret, out)
        self.assertIn("[REDACTED:aws-secret-key]", out)
        text = f"base64 blob {secret} alone\n"
        self.assertEqual(T.redact(text), (text, 0))

    def test_look_alikes_are_not_redacted(self):
        for text in ("commit 9f2c4e1a7b3d5f6e8a0c2e4f6a8b0c2d4e6f8a0b merged\n",
                     "session 0b5c2a8e-1f3d-4c6b-9a7e-2d4f6a8c0e1b started\n",
                     "a sk-short word and a task-list\n",
                     "the ghp_ prefix, hf_ files, npm_config and AKIA alone\n",
                     "xoxb- is a slack prefix; glpat- a gitlab one; eyJ starts base64 JSON\n",
                     json.dumps({"path": "/src/hf_hub/npm_modules", "sha": "0" * 40}) + "\n"):
            with self.subTest(text=text):
                self.assertEqual(T.redact(text), (text, 0))


def _b64_lines(seed: int, nbytes: int, width: int) -> list[str]:
    """Deterministic key-body-like base64 (random bytes, not a real key) wrapped at width."""
    import base64
    import random
    data = base64.b64encode(random.Random(seed).randbytes(nbytes)).decode()
    return [data[i:i + width] for i in range(0, len(data), width)]


class RedactKeyBodyTest(unittest.TestCase):
    """R-redact: PGP private key armour, and PEM/OpenSSH key bodies with no BEGIN line."""

    def assert_gone(self, text: str, secrets: list[str]) -> str:
        out, n = T.redact(text)
        self.assertGreaterEqual(n, 1)
        self.assertIn("[REDACTED:private-key]", out)
        for secret in secrets:
            self.assertNotIn(secret, out)
        return out

    def variants(self, block: str) -> list[tuple[str, str]]:
        """(name, text): plain lines around the block, and the block JSON-escaped in a tool result."""
        return [("plain", f"before\n{block}\nafter\n"),
                ("json", json.dumps({"type": "user", "toolUseResult": {"stdout": f"cat k\n{block}\ndone"}}) + "\n")]

    def test_pgp_private_key_block(self):
        body = _b64_lines(1, 900, 64)
        block = "\n".join(["-----BEGIN PGP PRIVATE KEY BLOCK-----", "Comment: test key <a@b.example>", ""]
                          + body + ["=Ab3x", "-----END PGP PRIVATE KEY BLOCK-----"])
        for name, text in self.variants(block):
            with self.subTest(name):
                out = self.assert_gone(text, body[:-1] + ["=Ab3x"])
                self.assertIn("after" if name == "plain" else "done", out)
                if name == "json":
                    json.loads(out)

    def test_pgp_block_without_end(self):
        body = _b64_lines(2, 600, 64)
        block = "\n".join(["-----BEGIN PGP PRIVATE KEY BLOCK-----", ""] + body)
        for name, text in self.variants(block):
            with self.subTest(name):
                self.assert_gone(text, body[:-1])

    def test_orphan_end_line(self):
        # a tail capture that starts mid-key: body lines then END, no BEGIN; even a short EC key
        for kind, width, nbytes in (("RSA ", 64, 1190), ("EC ", 64, 121), ("OPENSSH ", 70, 399), ("", 64, 48)):
            body = _b64_lines(3, nbytes, width)
            body = body[1:] if len(body) > 1 else body
            block = "\n".join(body + [f"-----END {kind}PRIVATE KEY-----"])
            for name, text in self.variants(block):
                with self.subTest(kind=kind, variant=name):
                    out = self.assert_gone(text, [line for line in body if len(line) > 8])
                    self.assertIn("before" if name == "plain" else "cat k", out)
                    if name == "json":
                        json.loads(out)

    def test_run_of_key_lines_without_markers(self):
        # the middle of a key: no BEGIN, no END (a cut capture, a grep -A of the key file)
        for width, nbytes in ((64, 1190), (70, 399), (64, 2300)):
            body = _b64_lines(4, nbytes, width)
            block = "\n".join(body[1:-1])
            for name, text in self.variants(block):
                with self.subTest(width=width, nbytes=nbytes, variant=name):
                    out = self.assert_gone(text, body[1:-1])
                    self.assertIn("after" if name == "plain" else "done", out)   # words around stay
                    if name == "json":
                        json.loads(out)

    def test_threshold_short_runs_are_kept(self):
        # KEY_RUN_MIN - 1 full-width lines with no markers are not enough on their own
        body = _b64_lines(5, 48 * (T.KEY_RUN_MIN - 1), 64)
        self.assertEqual(len(body), T.KEY_RUN_MIN - 1)
        for name, text in self.variants("\n".join(body)):
            with self.subTest(name):
                self.assertEqual(T.redact(text), (text, 0))

    def test_ordinary_base64_is_not_redacted(self):
        import base64
        import random
        # (seed 7: some seeds hit the older high-entropy rule, "...Key" at a line end, unrelated to this)
        blob = base64.b64encode(random.Random(7).randbytes(3000)).decode()
        mime = "\n".join(blob[i:i + 76] for i in range(0, len(blob), 76))       # base64(1), MIME
        hashes = "\n".join(hashlib.sha256(str(i).encode()).hexdigest() for i in range(20))
        upper = hashes.upper()
        cert = "\n".join(["-----BEGIN CERTIFICATE-----"] + _b64_lines(7, 900, 64) + ["-----END CERTIFICATE-----"])
        pgp_pub = "\n".join(["-----BEGIN PGP PUBLIC KEY BLOCK-----", ""] + _b64_lines(8, 900, 64)
                            + ["=Zz9q", "-----END PGP PUBLIC KEY BLOCK-----"])
        prose = "\n".join(["x" * 64] * 8)
        for label, block in (("one-line image", blob), ("76-wide", mime), ("sha256 list", hashes),
                             ("SHA256 LIST", upper), ("certificate", cert), ("pgp public key", pgp_pub),
                             ("repeated letters", prose)):
            for name, text in self.variants(block):
                with self.subTest(label=label, variant=name):
                    self.assertEqual(T.redact(text), (text, 0))


def _read_result(numbered: str, path: str, start: int) -> str:
    """One Claude Code JSONL line for a Read tool result (the shape of a real transcript): the
    numbered text in message.content, the plain file text in toolUseResult.file.content."""
    plain = "\n".join(line.split("\t", 1)[1] for line in numbered.split("\n"))
    return json.dumps({"parentUuid": "p", "isSidechain": True, "type": "user",
                       "message": {"role": "user", "content": [
                           {"tool_use_id": "toolu_01", "type": "tool_result", "content": numbered}]},
                       "uuid": "u", "timestamp": "2026-09-28T10:00:00.000Z",
                       "toolUseResult": {"type": "text", "file": {
                           "filePath": path, "content": plain, "numLines": numbered.count("\n") + 1,
                           "startLine": start, "totalLines": 99}}}) + "\n"


def _key_file(kind: str) -> list[str]:
    """The lines of a key file: (PEM RSA, OpenSSH, PGP) with synthetic bodies."""
    if kind == "pem":
        return (["-----BEGIN RSA PRIVATE KEY-----"] + _b64_lines(11, 1190, 64)
                + ["-----END RSA PRIVATE KEY-----"])
    if kind == "openssh":
        return (["-----BEGIN OPENSSH PRIVATE KEY-----"] + _b64_lines(12, 1800, 70)
                + ["-----END OPENSSH PRIVATE KEY-----"])
    return (["-----BEGIN PGP PRIVATE KEY BLOCK-----", "Comment: k <a@b.example>", ""]
            + _b64_lines(13, 1500, 64) + ["=Qw3r", "-----END PGP PRIVATE KEY BLOCK-----"])


def _secret_lines(lines: list[str]) -> list[str]:
    return [x for x in lines if len(x) >= 40 and not x.startswith("-----")]


class RedactPrefixedKeyTest(unittest.TestCase):
    """Key bodies behind line prefixes (Claude Read numbers, grep -n, diff, YAML)."""

    def assert_no_leak(self, text: str, secrets: list[str], json_lines: bool = True) -> str:
        out, n = T.redact(text)
        self.assertGreaterEqual(n, 1)
        leaked = [x for x in secrets if x in out]
        self.assertEqual(leaked, [], f"{len(leaked)} of {len(secrets)} key lines leaked")
        if json_lines:
            for line in out.splitlines():
                json.loads(line)
        return out

    def test_read_output_with_offsets_past_begin(self):
        for kind in ("pem", "openssh", "pgp"):
            lines = _key_file(kind)
            for offset in (0, 3, 7):                       # Read's offset: BEGIN (and more) cut off
                for pad in (True, False):                   # "     5\t" and "5\t" both occur
                    shown = lines[offset:]
                    numbered = "\n".join((f"{offset + i + 1:6d}" if pad else str(offset + i + 1)) + "\t" + x
                                         for i, x in enumerate(shown))
                    with self.subTest(kind=kind, offset=offset, pad=pad):
                        self.assert_no_leak(_read_result(numbered, "/home/u/.ssh/k", offset + 1),
                                            _secret_lines(shown))

    def test_read_output_with_offsets_as_plain_text(self):
        lines = _key_file("pem")
        numbered = "\n".join(f"{i + 6:6d}\t{x}" for i, x in enumerate(lines[5:]))
        self.assert_no_leak("before\n" + numbered + "\nafter\n", _secret_lines(lines[5:]), json_lines=False)

    def test_grep_diff_and_yaml_prefixes(self):
        for kind in ("pem", "openssh", "pgp"):
            lines = _key_file(kind)
            shapes = {
                "grep -n": [f"{i + 1}:{x}" for i, x in enumerate(lines[2:])],
                "grep -n file": [f"keys/k.pem:{i + 3}:{x}" for i, x in enumerate(lines[2:])],
                "grep -A context": [f"keys/k.pem-{i + 3}-{x}" for i, x in enumerate(lines[2:])],
                "diff +": ["+" + x for x in lines[2:]],
                "diff -": ["-" + x for x in lines[2:]],
                "yaml": ["tls:", "  key: |"] + ["    " + x for x in lines[2:]],
                "yaml no markers": ["tls:", "  key: |"] + ["    " + x for x in lines[2:-2]],
                "prefixed with BEGIN": [f"{i + 1}:{x}" for i, x in enumerate(lines)],
            }
            for name, shown in shapes.items():
                block = "\n".join(shown)
                secrets = _secret_lines(lines[2:-2])
                with self.subTest(kind=kind, shape=name, variant="plain"):
                    self.assert_no_leak(f"$ cmd\n{block}\ndone\n", secrets, json_lines=False)
                with self.subTest(kind=kind, shape=name, variant="json"):
                    self.assert_no_leak(json.dumps({"type": "user", "toolUseResult": {"stdout": block}}) + "\n",
                                        secrets)

    def test_prefixed_public_blocks_and_prose_are_kept(self):
        cert = ["-----BEGIN CERTIFICATE-----"] + _b64_lines(14, 900, 64) + ["-----END CERTIFICATE-----"]
        for text in ("\n".join(f"{i + 1:6d}\t{x}" for i, x in enumerate(cert)) + "\n",
                     "\n".join(f"{i + 1}:{x}" for i, x in enumerate(cert)) + "\n",
                     "\n".join(f"{i + 1:6d}\tline {i} of an ordinary file" for i in range(50)) + "\n",
                     "\n".join("+" + "x" * 64 for _ in range(8)) + "\n"):
            with self.subTest(text=text[:30]):
                self.assertEqual(T.redact(text), (text, 0))


class RedactEscapedKeyTest(unittest.TestCase):
    """Key bodies in JSON-escaped text that isn't parseable JSON (a cut line), or is
    escaped twice (JSON text inside a tool result)."""

    def setUp(self):
        self.lines = _key_file("pem")
        self.secrets = _secret_lines(self.lines)

    def check(self, text: str) -> str:
        out, n = T.redact(text)
        self.assertGreaterEqual(n, 1)
        self.assertEqual([x for x in self.secrets if x in out], [])
        return out

    def test_truncated_jsonl_line(self):
        nob = "\n".join(self.lines[1:]) + "\n"
        esc = json.dumps(nob)[1:-1]
        for text in ('partial {"private_ke' + 'x": "' + esc[40:],       # a cut-off private_key name
                     '{"type":"user","content":"' + esc[:-200],          # cut before the END line
                     esc[100:] + '","is_error":false}}]}\n'):             # a tail that starts mid-string
            with self.subTest(text=text[:30]):
                self.check(text)

    def test_double_escaped_tool_result(self):
        sa = json.dumps({"type": "service_account", "private_key_id": "0" * 40,
                         "private_key": "\n".join(self.lines) + "\n", "client_email": "x@y.example"}, indent=2)
        for cut in (1700, 1300, len(sa)):                                 # tail -c 1700 sa.json
            inner = sa[-cut:]
            line = json.dumps({"type": "user", "message": {"content": [
                {"type": "tool_result", "content": f"$ tail -c {cut} sa.json\n{inner}"}]}}) + "\n"
            with self.subTest(cut=cut):
                out = self.check(line)
                json.loads(out)
                again = json.dumps({"type": "user", "toolUseResult": {"stdout": line}}) + "\n"   # three levels
                json.loads(self.check(again))

    def test_escaped_ordinary_base64_is_kept(self):
        import base64
        import random
        blob = base64.b64encode(random.Random(7).randbytes(3000)).decode()
        mime = "\n".join(blob[i:i + 76] for i in range(0, len(blob), 76))
        hashes = "\n".join(hashlib.sha256(str(i).encode()).hexdigest() for i in range(20))
        for block in (mime, hashes):
            text = json.dumps(json.dumps({"out": block}))[1:-40] + "\n"   # double-escaped and cut
            self.assertEqual(T.redact(text), (text, 0))


# The shapes (full.py, pref.py): how a key file's lines reach a transcript.
GENERIC_PREFIXES = {
    "grep -r": lambda i, x: f"secrets/a.pem:{x}",
    "tab": lambda i, x: "\t" + x,
    "nl -ba": lambda i, x: f"{i:6}  {x}",
    "bat": lambda i, x: f"{i:4} │ {x}",
    "md quote": lambda i, x: "> " + x,
    "comment": lambda i, x: "# " + x,
    "quoted": lambda i, x: f'"{x}",',
    "tab crlf": lambda i, x: "\t" + x + "\r",
    "grep -A context": lambda i, x: f"secrets/a.pem-{x}",
    "repr": lambda i, x: f"'{x}'",
}


def _three_ways(text: str) -> list[tuple[str, str]]:
    return [("raw", text),
            ("jsonl", json.dumps({"type": "user", "toolUseResult": {"stdout": text}}) + "\n"),
            ("tool_result", json.dumps({"type": "user", "message": {"role": "user", "content": [
                {"tool_use_id": "toolu_01", "type": "tool_result", "content": text}]}}) + "\n")]


class RedactGenericPrefixTest(unittest.TestCase):
    """A BEGIN line is never consumed without its key, and body
    lines behind any common prefix are recognised, with or without BEGIN/END."""

    def assert_gone(self, text: str, secrets: list[str], form: str):
        out, n = T.redact(text)
        self.assertGreaterEqual(n, 1)
        self.assertEqual([x for x in secrets if x in out], [], f"{form}: key lines leaked")
        if form != "raw":
            for line in out.splitlines():
                json.loads(line)

    def test_full_key_files_behind_each_prefix(self):
        for kind in ("pem", "openssh", "pgp"):
            lines = _key_file(kind)
            for name, f in GENERIC_PREFIXES.items():
                text = "\n".join(f(i, x) for i, x in enumerate(lines)) + "\n"
                for form, t in _three_ways(text):
                    with self.subTest(kind=kind, prefix=name, form=form):
                        self.assert_gone(t, _secret_lines(lines), form)

    def test_headerless_bodies_behind_each_prefix(self):
        for kind in ("pem", "openssh"):
            lines = _key_file(kind)
            for cut, shown in (("no BEGIN", lines[1:]), ("no markers", lines[1:-1])):
                for name, f in GENERIC_PREFIXES.items():
                    text = "cat out\n" + "\n".join(f(i, x) for i, x in enumerate(shown)) + "\n"
                    for form, t in _three_ways(text):
                        with self.subTest(kind=kind, cut=cut, prefix=name, form=form):
                            self.assert_gone(t, _secret_lines(shown), form)

    def test_unrecognised_block_falls_back_to_begin_through_end(self):
        # lines after BEGIN that no rule recognises: redacted from BEGIN to END, as _PEM did
        lines = _key_file("pem")
        odd = [lines[0]] + [f"~~{x}~~" for x in lines[1:-1]] + [lines[-1]]
        text = "\n".join(odd) + "\n"
        for form, t in _three_ways(text):
            with self.subTest(form=form):
                self.assert_gone(t, _secret_lines(lines), form)
        # no END: to the end of that JSON string, and never past it
        t = json.dumps({"a": "\n".join(odd[:-1]), "b": "keep me"}) + "\n"
        out, _ = T.redact(t)
        self.assertEqual([x for x in _secret_lines(lines) if x in out], [])
        self.assertEqual(json.loads(out)["b"], "keep me")

    def test_prefixed_public_blocks_76_wide_and_hex_are_kept(self):
        import base64
        import random
        cert = ["-----BEGIN CERTIFICATE-----"] + _b64_lines(14, 900, 64) + ["-----END CERTIFICATE-----"]
        blob = base64.b64encode(random.Random(7).randbytes(3000)).decode()
        wide = [blob[i:i + 76] for i in range(0, len(blob), 76)]
        hexes = [hashlib.sha256(str(i).encode()).hexdigest() for i in range(20)]
        for label, block in (("cert", cert), ("76-wide", wide), ("hex", hexes)):
            for name in ("bat", "nl -ba", "comment", "md quote", "grep -r", "quoted"):
                text = "\n".join(GENERIC_PREFIXES[name](i, x) for i, x in enumerate(block)) + "\n"
                for form, t in _three_ways(text):
                    with self.subTest(label=label, prefix=name, form=form):
                        self.assertEqual(T.redact(t), (t, 0))


class RedactTwoCopyLineTest(unittest.TestCase):
    """A real Claude line holds the tool_result content and a second copy in
    toolUseResult.stdout on one physical line; a key without END (text ending right at the
    closing quote) must lose every body line of both copies."""

    KEYS = {"rsa": ("RSA ", 1190, 64), "ec": ("EC ", 121, 64), "ed25519": ("OPENSSH ", 260, 70)}

    def line(self, text: str) -> str:
        pad = "x" * 3000
        return json.dumps({"parentUuid": "p", "type": "user", "message": {"role": "user", "content": [
            {"tool_use_id": "toolu_01", "type": "tool_result", "content": text}]}, "uuid": "u",
            "toolUseResult": {"stdout": text, "stderr": "", "interrupted": False, "note": pad},
            "more": pad}) + "\n"

    def test_every_shape_loses_every_body_line(self):
        for name, (kind, nbytes, width) in self.KEYS.items():
            body = _b64_lines(21, nbytes, width)
            full = [f"-----BEGIN {kind}PRIVATE KEY-----"] + body + [f"-----END {kind}PRIVATE KEY-----"]
            shapes = {"noEND": full[:-1], "noBEGIN": full[1:], "body only": body, "full": full}
            for shape, lines in shapes.items():
                if shape == "body only" and len(body) - 1 < T.KEY_RUN_MIN:
                    continue            # a 3-line EC body with no marker: the documented gap
                for read in (False, True):
                    shown = [f"{i + 1:6d}\t{x}" for i, x in enumerate(lines)] if read else lines
                    text = "\n".join(shown)           # no trailing newline: key text meets the quote
                    with self.subTest(key=name, shape=shape, read=read):
                        out, n = T.redact(self.line(text))
                        obj = json.loads(out)
                        self.assertEqual([x for x in body if len(x) >= 8 and x in out], [])
                        self.assertEqual(obj["more"], "x" * 3000)      # nothing past the strings
                        self.assertEqual(obj["toolUseResult"]["stderr"], "")


class RedactSplitStringsTest(unittest.TestCase):
    """A key without BEGIN split over several JSON strings on one line (an MCP
    tool_result returning 2-line text blocks, END in the last block) must lose every body line."""

    KEYS = {"rsa": ("RSA ", 1190, 64), "ec": ("EC ", 121, 64), "ed25519": ("OPENSSH ", 260, 70)}

    def line(self, blocks: list[str]) -> str:
        pad = "x" * 3000
        return json.dumps({"parentUuid": "p", "type": "user", "message": {"role": "user", "content": [
            {"tool_use_id": "toolu_01", "type": "tool_result",
             "content": [{"type": "text", "text": b} for b in blocks]}]}, "uuid": "u",
            "toolUseResult": {"stderr": "", "interrupted": False, "note": pad}, "more": pad}) + "\n"

    def test_no_begin_key_in_two_line_text_blocks(self):
        for name, (kind, nbytes, width) in self.KEYS.items():
            body = _b64_lines(22, nbytes, width)
            lines = body + [f"-----END {kind}PRIVATE KEY-----"]
            for read in (False, True):
                shown = [f"{i + 2:6d}\t{x}" for i, x in enumerate(lines)] if read else lines
                for tail in ("", "\n"):
                    blocks = ["\n".join(shown[i:i + 2]) + tail for i in range(0, len(shown), 2)]
                    with self.subTest(key=name, read=read, tail=repr(tail)):
                        out, n = T.redact(self.line(blocks))
                        obj = json.loads(out)
                        self.assertEqual([x for x in body if len(x) >= 8 and x in out], [])
                        self.assertEqual(obj["more"], "x" * 3000)      # nothing past the strings
                        self.assertEqual(obj["toolUseResult"]["note"], "x" * 3000)
                        self.assertEqual(obj["toolUseResult"]["stderr"], "")


def _leaked(body: list[str], out: str) -> list[str]:
    return [x for x in body if len(x) >= 8 and x in out]


class RedactMarkerlessSplitTest(unittest.TestCase):
    """R-redact 2: a key body with neither BEGIN nor END spread over several JSON strings
    on one line: text blocks of a few lines each, or an array of quoted lines."""

    KEYS = {"rsa": lambda: _b64_lines(36, 1190, 64), "ed25519": lambda: _b64_lines(37, 260, 70),
            "pgp": lambda: _b64_lines(38, 900, 64)}

    def blocks_line(self, blocks: list[str]) -> str:
        pad = "x" * 3000
        return json.dumps({"type": "user", "message": {"role": "user", "content": [
            {"type": "text", "text": b} for b in blocks]}, "toolUseResult": {"stderr": "", "note": pad},
            "more": pad}) + "\n"

    def check(self, text: str, body: list[str], jsonl: bool = True):
        out, n = T.redact(text)
        self.assertGreaterEqual(n, 1)
        self.assertEqual(_leaked(body, out), [])
        if jsonl:
            for line in out.splitlines():
                json.loads(line)
        return out

    def test_text_blocks_without_markers(self):
        for name, make in self.KEYS.items():
            body = make()
            for size in (1, 2, 3, 5):
                for tail in ("", "\n"):
                    blocks = ["\n".join(body[i:i + size]) + tail for i in range(0, len(body), size)]
                    with self.subTest(key=name, size=size, tail=repr(tail)):
                        obj = json.loads(self.check(self.blocks_line(blocks), body))
                        self.assertEqual(obj["more"], "x" * 3000)
                        self.assertEqual(obj["toolUseResult"]["note"], "x" * 3000)

    def test_arrays_of_quoted_lines(self):
        for name, make in self.KEYS.items():
            body = make()
            shapes = {"compact": json.dumps({"lines": body}, separators=(",", ":")) + "\n",
                      "spaced": json.dumps({"lines": body, "more": "keep"}) + "\n",
                      "in a tool result": json.dumps({"type": "user", "message": {"content": [
                          {"type": "tool_result", "content": json.dumps(body)}]}, "more": "keep"}) + "\n",
                      "twice escaped": json.dumps({"stdout": json.dumps({"lines": body})}) + "\n"}
            for shape, text in shapes.items():
                with self.subTest(key=name, shape=shape):
                    self.check(text, body)
            with self.subTest(key=name, shape="python repr"):
                self.check(f"print(lines)\n{body!r}\n", body, jsonl=False)

    def test_last_line_without_digits(self):
        # 1 in ~150 unpadded 24-character last lines has no digit, + / or =: it goes too
        body = _b64_lines(45, 1218, 64)
        body[-1] = "KtuumVXvjIyxRZAJDRohvoNF"
        shapes = {"1-line blocks": self.blocks_line(body),
                  "3-line blocks": self.blocks_line(["\n".join(body[i:i + 3]) for i in range(0, len(body), 3)]),
                  "array": json.dumps({"lines": body}) + "\n"}
        for shape, text in shapes.items():
            with self.subTest(shape):
                self.check(text, body)

    def test_split_public_and_ordinary_data_is_kept(self):
        import base64
        import random
        cert = ["-----BEGIN CERTIFICATE-----"] + _b64_lines(39, 900, 64) + ["-----END CERTIFICATE-----"]
        blob = base64.b64encode(random.Random(40).randbytes(1500)).decode()
        wide = [blob[i:i + 76] for i in range(0, len(blob), 76)]
        hexes = [hashlib.sha256(str(i).encode()).hexdigest() for i in range(20)]
        short = _b64_lines(42, 48 * (T.KEY_RUN_MIN - 1), 64)
        cases = {"cert array": json.dumps({"lines": cert}) + "\n",
                 "cert blocks": self.blocks_line(["\n".join(cert[i:i + 3]) for i in range(0, len(cert), 3)]),
                 "76-wide array": json.dumps({"lines": wide}) + "\n",
                 "hex array": json.dumps({"lines": hexes}) + "\n",
                 "threshold-1 array": json.dumps({"lines": short}) + "\n",
                 "threshold-1 blocks": self.blocks_line(short),
                 # one 64-wide string per JSONL entry: never chained across entries
                 "one per entry": "".join(json.dumps({"id": x}) + "\n" for x in _b64_lines(43, 480, 64))}
        for label, text in cases.items():
            with self.subTest(label):
                self.assertEqual(T.redact(text), (text, 0))


class DollarValueTest(unittest.TestCase):
    """Only whole-value environment references are exempt under a secret-named key."""

    def test_dollar_values(self):
        for text, secret in (('{"password":"$2b$12$R9h/cIPz0gi.URNNX3kh2OPST9/PgBkqquzi.Ss7KIUgO2t0jWMUW"}\n', "R9h/cIPz0gi"),
                             ('{"password":"$6$saltsalt$hashhashhash"}\n', "saltsalt"),
                             ('{"db_password": "$ecret-pass"}\n', "ecret-pass"),
                             ("password=$ecret1 psql\n", "ecret1"),
                             ("PGPASSWORD=$2b$10$abcdefghijk ./run\n", "abcdefghijk"),
                             ("api_key: %notavar here\n", "notavar"),
                             ('password="$x1y2z3"\n', "x1y2z3")):
            with self.subTest(text=text):
                out, n = T.redact(text)
                self.assertNotIn(secret, out)
                self.assertGreaterEqual(n, 1)
                self.assertIn("[REDACTED:", out)

    def test_environment_references_are_kept(self):
        for text in ('{"password":"$DB_PASSWORD"}\n', '{"password":"${DB_PASSWORD}"}\n',
                     '{"password":"%DB_PASSWORD%"}\n', "PGPASSWORD=$DB_PASSWORD psql\n",
                     "password=${DB_PASSWORD} psql\n", "token: %API_TOKEN%\n",
                     'export PGPASSWORD="$(cat ~/.pgpass)"\n'):
            with self.subTest(text=text):
                self.assertEqual(T.redact(text), (text, 0))


class MakeRowTest(unittest.TestCase):
    def test_row_fields(self):
        text = entry("2026-09-26T10:00:00Z", "hi password=secretvalue1") + "\n"
        row = T.make_row("j", "k1", "Homer", "subagent", text, final=True, host="h", session_id="s")
        body = lzma.decompress(row.body).decode()
        self.assertNotIn("secretvalue1", body)
        self.assertEqual((row.job, row.agent_key, row.agent_name, row.role, row.final, row.host,
                          row.session_id, row.redactions), ("j", "k1", "Homer", "subagent", True, "h", "s", 1))
        self.assertEqual(row.raw_bytes, len(body.encode()))
        self.assertEqual(row.sha256, hashlib.sha256(body.encode()).hexdigest())
        self.assertIsNone(row.captured_at)

    def test_oversize_keeps_head_and_tail_with_marker(self):
        import random
        rnd = random.Random(1)
        lines = [json.dumps({"i": i, "noise": "".join(rnd.choice("0123456789abcdef") for _ in range(400))})
                 for i in range(400)]
        text = "\n".join(lines) + "\n"
        row = T.make_row("j", "k", "n", "subagent", text, max_bytes=20_000)
        self.assertLessEqual(len(row.body), 20_000)
        out = lzma.decompress(row.body).decode().splitlines()
        parsed = [json.loads(line) for line in out]
        marker = [p for p in parsed if p.get("type") == T.TRUNCATED_TYPE]
        self.assertEqual(len(marker), 1)
        self.assertEqual(parsed[0]["i"], 0)
        self.assertEqual(parsed[-1]["i"], 399)
        kept = len(parsed) - 1
        self.assertEqual(marker[0]["omitted_lines"], 400 - kept)
        self.assertGreater(marker[0]["omitted_bytes"], 0)
        # sha256 is of the full redacted text: an unchanged oversize transcript is still skipped
        self.assertEqual(row.sha256, hashlib.sha256(text.encode()).hexdigest())

    def test_a_capture_is_cut_to_what_transcript_body_reads_back(self):
        # compressible text: tiny compressed, but over the read cap uncompressed (TRANSCRIPT_MAX_RAW)
        from unittest import mock
        lines = [json.dumps({"i": i, "pad": "a" * 400}) for i in range(400)]
        text = "\n".join(lines) + "\n"
        with mock.patch.object(T, "TRANSCRIPT_MAX_RAW", 20_000):
            row = T.make_row("j", "k", "n", "subagent", text)
        self.assertLessEqual(row.raw_bytes, 20_000)
        parsed = [json.loads(line) for line in lzma.decompress(row.body).decode().splitlines()]
        self.assertEqual((parsed[0]["i"], parsed[-1]["i"]), (0, 399))
        self.assertEqual(sum(p.get("type") == T.TRUNCATED_TYPE for p in parsed), 1)

    def test_deadline_passed_raises(self):
        with self.assertRaises(T.OutOfTime):
            T.make_row("j", "k", "n", "subagent", "x\n" * 10, deadline=0.0)


class SliceTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="swarm-tr-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.dir, ignore_errors=True))

    def write(self, lines) -> Path:
        p = self.dir / "session.jsonl"
        p.write_text("".join(line + "\n" for line in lines))
        return p

    def test_slice_by_timestamps(self):
        lines = [entry(f"2026-09-26T10:{m:02d}:00.000Z", f"m{m}") for m in range(60)]
        lines.insert(30, json.dumps({"type": "summary", "summary": "no timestamp"}))
        p = self.write(lines)
        start = dt.datetime(2026, 9, 26, 10, 20, tzinfo=UTC)
        end = dt.datetime(2026, 9, 26, 10, 40, tzinfo=UTC)
        got = T.read_slice(p, start, end).splitlines()
        texts = [json.loads(g).get("message", {}).get("content", [{}])[0].get("text") for g in got]
        self.assertEqual(texts[0], "m20")
        self.assertEqual(texts[-1], "m40")
        self.assertIn(None, texts)            # the untimestamped line inside the window is kept
        self.assertEqual(len(got), 22)
        self.assertEqual(len(T.read_slice(p, start, None).splitlines()), 41)
        self.assertEqual(T.read_slice(p, dt.datetime(2027, 1, 1, tzinfo=UTC), None), "")

    def test_missing_file_is_empty(self):
        self.assertEqual(T.read_slice(self.dir / "nope.jsonl", dt.datetime.now(UTC), None), "")


class StoreFunctionsTest(unittest.TestCase):
    def setUp(self):
        self.h = MemoryHarness("transcripts-store")
        self.h.reset()
        self.b = self.h.board()
        self.addCleanup(self.b.close)
        self.cfg = self.h.cfg
        self.cfg["transcripts"] = dict(T.DEFAULTS, enabled=True)
        self.dir = Path(tempfile.mkdtemp(prefix="swarm-tr-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.dir, ignore_errors=True))

    def test_settings_and_enabled(self):
        self.assertFalse(T.enabled({}))
        self.assertFalse(T.enabled({"transcripts": {"enabled": False}}))
        self.assertTrue(T.enabled(self.cfg))
        s = T.settings({"transcripts": {"enabled": True, "max_mb": 1}})
        self.assertEqual((s["retention_days"], s["max_total_mb"], s["snapshot_minutes"], s["max_mb"]),
                         (30, 2048, 15, 1))

    def test_capture_subagent_writes_once_then_skips_unchanged(self):
        p = self.dir / "agent-k1.jsonl"
        p.write_text(entry("2026-09-26T10:00:00Z", "hello api_key=abcdef123456") + "\n")
        self.b.open_job("j", None, None, None, "me")
        name = self.b.allocate_name("k1", "j")
        self.assertTrue(T.capture_subagent(self.b, self.cfg, "j", "k1", p, final=False))
        self.assertFalse(T.capture_subagent(self.b, self.cfg, "j", "k1", p, final=False))
        # unchanged content made final: the row is refreshed (captured_at, final), not rewritten
        self.assertFalse(T.capture_subagent(self.b, self.cfg, "j", "k1", p, final=True))
        [s] = self.b.transcripts()
        self.assertEqual((s.agent_name, s.role, s.final, s.redactions), (name, "subagent", True, 1))
        self.assertIn(b"[REDACTED:", self.b.transcript_body("j", "k1"))

    def test_capture_is_a_no_op_when_disabled_or_missing(self):
        p = self.dir / "agent-k1.jsonl"
        p.write_text("{}\n")
        cfg = dict(self.cfg, transcripts={"enabled": False})
        with mock.patch.object(self.b, "save_transcript", side_effect=AssertionError("touched")), \
                mock.patch.object(self.b, "transcripts", side_effect=AssertionError("touched")):
            self.assertFalse(T.capture_subagent(self.b, cfg, "j", "k1", p, final=True))
            self.assertFalse(T.capture_orchestrator(self.b, cfg, "j", p, dt.datetime.now(UTC), None, True))
            self.assertEqual(T.run_snapshots(self.b, cfg), 0)
            self.assertEqual(T.rotate(self.b, cfg), 0)
        self.assertFalse(T.capture_subagent(self.b, self.cfg, "j", "k1", self.dir / "none.jsonl", final=True))

    def test_capture_orchestrator_slice(self):
        lines = [entry(f"2026-09-26T10:{m:02d}:00Z", f"m{m}") for m in range(10)]
        p = self.dir / "sess.jsonl"
        p.write_text("\n".join(lines) + "\n")
        start = dt.datetime(2026, 9, 26, 10, 3, tzinfo=UTC)
        end = dt.datetime(2026, 9, 26, 10, 5, tzinfo=UTC)
        self.assertTrue(T.capture_orchestrator(self.b, self.cfg, "j", p, start, end, True, session_id="S"))
        [s] = self.b.transcripts(role="orchestrator")
        self.assertEqual((s.agent_key, s.agent_name, s.session_id, s.final), ("orchestrator", "orchestrator", "S", True))
        body = self.b.transcript_body("j", "orchestrator").decode().splitlines()
        self.assertEqual(len(body), 3)

    def test_rotate_uses_config_and_warns_when_active_jobs_exceed(self):
        self.b.open_job("act", None, None, None, "me")
        big = os.urandom(3000).hex()
        self.b.save_transcript(T.make_row("act", "k", "n", "subagent", f'{{"x":"{big}"}}\n'))
        self.b.save_transcript(T.make_row("old", "k", "n", "subagent", "{}\n",
                                          captured_at=dt.datetime.now(UTC) - dt.timedelta(days=45)))
        self.cfg["transcripts"]["max_total_mb"] = 0.001
        warned = []
        self.assertEqual(T.rotate(self.b, self.cfg, warn=warned.append), 1)
        self.assertEqual([s.job for s in self.b.transcripts()], ["act"])
        self.assertEqual(len(warned), 1)
        self.assertIn("active", warned[0])


if __name__ == "__main__":
    unittest.main()
