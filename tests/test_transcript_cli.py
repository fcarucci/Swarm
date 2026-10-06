"""`swarm transcript list|show|export` and the transcript sizes in `swarm status`, end to end on
the backend under test (memory by default), seeded with transcripts.make_row."""
from __future__ import annotations

import dataclasses
import datetime as dt
import contextlib
import io
import json
import re
from unittest import mock

from support import ROOT, tq  # noqa: F401  (sets sys.path)

from swarm import cli as swarm  # noqa: E402
from swarm import transcripts  # noqa: E402
from test_hooks_cli import Env  # noqa: E402

UTC = dt.timezone.utc


def jsonl(*pairs) -> str:
    """A small Claude Code transcript: (kind, text) user/assistant turns."""
    return "\n".join(json.dumps({"type": k, "timestamp": "2026-09-26T12:00:00Z",
                                 "message": {"role": k, "content": t}}) for k, t in pairs) + "\n"


class TranscriptEnv(Env):
    ENABLED = True

    def setUp(self):
        super().setUp()
        if self.ENABLED:
            with open(self.config, "a") as fh:
                fh.write("[transcripts]\nenabled = true\nretention_days = 30\nmax_total_mb = 2048\n")
        from swarm.cli import load_config
        self.cfg = load_config(self.config)

    def seed(self, job, key, name, text, role="subagent", captured_days_ago=0, final=True):
        row = transcripts.make_row(job, key, name, role, text, final=final, host="h1", session_id="s1")
        if captured_days_ago:
            row = dataclasses.replace(row, captured_at=dt.datetime.now(UTC) - dt.timedelta(days=captured_days_ago))
        with self.board() as b:
            b.save_transcript(row)
        return row


class DisabledTests(TranscriptEnv):
    ENABLED = False

    def test_transcript_commands_say_the_feature_is_off(self):
        for argv in (("list",), ("show", "--job", "J", "--agent", "Homer Simpson"), ("export", "--job", "J")):
            rc, out, err = self.cli("transcript", *argv)
            self.assertEqual((rc, out), (1, ""))
            self.assertIn("transcripts are off", err)
            self.assertIn("[transcripts] enabled = true", err)

    def test_status_has_no_transcript_lines(self):
        self.cli("activate", "--job", "J")
        self.cli("join", "--job", "J", "--key", "k1")
        self.assertNotIn("transcripts", self.cli("status")[1])
        _, out, _ = self.cli("status", "--job", "J")
        self.assertNotIn("transcripts", out)
        self.assertNotIn("STORED", out)


class ListTests(TranscriptEnv):
    def setUp(self):
        super().setUp()
        self.seed("J1", "k1", "Homer Simpson", jsonl(("user", "hello " * 200)))
        self.seed("J1", "orch", "orchestrator", jsonl(("user", "go")), role="orchestrator", final=False)
        self.seed("J2", "k2", "Homer Simpson", jsonl(("user", "other job")))

    def test_list_all_with_total(self):
        rc, out, _ = self.cli("transcript", "list")
        self.assertEqual(rc, 0)
        lines = out.splitlines()
        self.assertRegex(lines[0], r"^JOB\s+AGENT\s+ROLE\s+HOST\s+RAW\s+STORED\s+RATIO\s+IMAGES\s+REDACTED\s+FINAL\s+CAPTURED\s+REPLACES\s+KEY$")
        self.assertEqual(len(lines), 5)
        self.assertTrue(any(l.startswith("J1") and "orchestrator" in l and " no " in l for l in lines))
        self.assertRegex(lines[-1], r"^total: 3 transcripts, \S+ \S+ stored \(\S+ \S+ raw, ratio [\d.]+x\)$")

    def test_list_filters(self):
        _, out, _ = self.cli("transcript", "list", "--job", "J1")
        self.assertEqual(len(out.splitlines()), 4)
        _, out, _ = self.cli("transcript", "list", "--agent", "Homer Simpson")
        body = out.splitlines()[1:-1]
        self.assertEqual(sorted(l.split()[0] for l in body), ["J1", "J2"])

    def test_list_nothing(self):
        rc, out, _ = self.cli("transcript", "list", "--job", "nope")
        self.assertEqual((rc, out), (0, "no transcripts (kept 30 days / up to 2048 MB)\n"))


class ShowTests(TranscriptEnv):
    def setUp(self):
        super().setUp()
        self.seed("J1", "k1", "Homer Simpson",
                  jsonl(("user", "first question"), ("assistant", "an answer"), ("user", "second question")))
        self.seed("J1", "orch", "orchestrator", jsonl(("user", "orchestrating")), role="orchestrator")
        self.seed("J2", "k2", "Homer Simpson", jsonl(("user", "elsewhere")))
        self.seed("J2", "k3", "Marge Simpson", jsonl(("user", "only marge")))

    def test_show_by_job_and_agent_as_text(self):
        rc, out, _ = self.cli("transcript", "show", "--job", "J1", "--agent", "Homer Simpson")
        self.assertEqual(rc, 0)
        self.assertRegex(out, r"(?m)^── \d\d:\d\d:\d\d user\nfirst question$")
        self.assertIn("an answer", out)
        self.assertNotIn("elsewhere", out)

    def test_show_by_unique_name_across_jobs(self):
        rc, out, _ = self.cli("transcript", "show", "--agent", "Marge Simpson")
        self.assertEqual(rc, 0)
        self.assertIn("only marge", out)

    def test_ambiguous_name_lists_matches_and_asks_for_job(self):
        rc, out, err = self.cli("transcript", "show", "--agent", "Homer Simpson")
        self.assertEqual((rc, out), (1, ""))
        self.assertIn("2 transcripts for Homer Simpson", err)
        self.assertIn("--job", err)
        self.assertRegex(err, r"(?m)^\s+J1\s+subagent\s+\d{4}-\d\d-\d\d \d\d:\d\d\s+\S+ \S+\s+key k1$")
        self.assertRegex(err, r"(?m)^\s+J2\s+subagent\s")

    def test_orchestrator_and_key(self):
        _, out, _ = self.cli("transcript", "show", "--job", "J1", "--orchestrator")
        self.assertIn("orchestrating", out)
        _, out, _ = self.cli("transcript", "show", "--key", "k3")
        self.assertIn("only marge", out)
        _, out, _ = self.cli("transcript", "show", "--job", "J2", "--key", "k2")
        self.assertIn("elsewhere", out)

    def test_jsonl_tail_grep(self):
        _, out, _ = self.cli("transcript", "show", "--job", "J1", "--agent", "Homer Simpson",
                             "--format", "jsonl")
        self.assertEqual([json.loads(l)["message"]["content"] for l in out.splitlines()],
                         ["first question", "an answer", "second question"])
        _, out, _ = self.cli("transcript", "show", "--job", "J1", "--agent", "Homer Simpson",
                             "--format", "jsonl", "--tail", "1")
        self.assertEqual(len(out.splitlines()), 1)
        self.assertIn("second question", out)
        _, out, _ = self.cli("transcript", "show", "--job", "J1", "--agent", "Homer Simpson",
                             "--grep", "QUESTION", "--tail", "1")
        self.assertIn("second question", out)
        self.assertNotIn("first question", out)

    def test_output_file(self):
        target = self.tmp / "out.txt"
        rc, out, err = self.cli("transcript", "show", "--job", "J1", "--agent", "Homer Simpson",
                                "-o", str(target))
        self.assertEqual(rc, 0)
        self.assertEqual(out, "")
        self.assertIn(str(target), err)
        self.assertIn("first question", target.read_text())

    def test_unknown(self):
        rc, out, err = self.cli("transcript", "show", "--job", "J1", "--agent", "Nobody")
        self.assertEqual((rc, out), (1, ""))
        self.assertEqual(err, "no transcript for Nobody in J1; transcripts are kept 30 days / up to 2048 MB\n")
        rc, _, err = self.cli("transcript", "show", "--agent", "Nobody")
        self.assertEqual(err, "no transcript for Nobody; transcripts are kept 30 days / up to 2048 MB\n")
        rc, _, err = self.cli("transcript", "show", "--job", "J2", "--orchestrator")
        self.assertEqual((rc, err), (1, "no transcript for the orchestrator in J2; transcripts are kept "
                                        "30 days / up to 2048 MB\n"))

    def test_needs_a_selector(self):
        rc, _, err = self.cli("transcript", "show", "--job", "J1")
        self.assertEqual(rc, 2)
        self.assertIn("--agent", err)

    def test_bad_grep(self):
        rc, _, err = self.cli("transcript", "show", "--key", "k3", "--grep", "(")
        self.assertEqual(rc, 2)
        self.assertIn("--grep", err)

    def test_secrets_are_not_shown(self):
        secret = "sk-ant-api03-" + "A1b2C3d4" * 6
        self.seed("J3", "k9", "Bart Simpson", jsonl(("user", f"key is {secret}")))
        _, out, _ = self.cli("transcript", "show", "--job", "J3", "--agent", "Bart Simpson", "--format", "jsonl")
        self.assertNotIn(secret, out)
        self.assertIn("[REDACTED", out)


class ExportTests(TranscriptEnv):
    def test_export_writes_every_transcript_and_an_index(self):
        self.seed("J1", "k1", "Homer Simpson", jsonl(("user", "one")))
        self.seed("J1", "k2", "Homer Simpson", jsonl(("user", "same name, other key")))
        self.seed("J1", "orch", "orchestrator", jsonl(("user", "orch")), role="orchestrator")
        self.seed("J2", "k3", "Marge Simpson", jsonl(("user", "not exported")))
        target = self.tmp / "exp"
        rc, out, _ = self.cli("transcript", "export", "--job", "J1", str(target))
        self.assertEqual(rc, 0)
        self.assertIn(f"exported 3 transcripts of J1 to {target}", out)
        files = sorted(p.name for p in target.glob("*.jsonl"))
        self.assertEqual(len(files), 3)
        self.assertIn("orchestrator.jsonl", files)
        index = (target / "index.tsv").read_text().splitlines()
        self.assertEqual(index[0].split("\t"), ["file", "agent_name", "agent_key", "role", "host",
                                                "session_id", "captured_at", "final", "raw_bytes",
                                                "stored_bytes", "redactions", "images", "harness"])
        self.assertEqual(len(index), 4)
        for line in index[1:]:
            name = line.split("\t")[0]
            self.assertTrue((target / name).is_file())
        texts = "".join((target / f).read_text() for f in files)
        self.assertIn("same name, other key", texts)
        self.assertNotIn("not exported", texts)

    def test_export_default_dir_and_unknown_job(self):
        import os
        os.chdir(self.tmp)
        self.addCleanup(os.chdir, str(ROOT))
        self.seed("J/1", "k1", "Homer Simpson", jsonl(("user", "one")))
        rc, out, _ = self.cli("transcript", "export", "--job", "J/1")
        self.assertEqual(rc, 0)
        self.assertTrue((self.tmp / "transcripts-J_1" / "index.tsv").is_file())
        rc, out, err = self.cli("transcript", "export", "--job", "nope")
        self.assertEqual((rc, out), (1, ""))
        self.assertIn("no transcripts for nope", err)


class StatusTests(TranscriptEnv):
    def test_status_footer_and_job_detail(self):
        self.cli("activate", "--job", "J")
        name = self.cli("join", "--job", "J", "--key", "k1")[1].strip()
        other = self.cli("join", "--job", "J", "--key", "k2")[1].strip()
        self.seed("J", "k1", name, jsonl(("user", "x" * 5000)))
        self.seed("J", "orch", "orchestrator", jsonl(("user", "o")), role="orchestrator")
        self.seed("OLD", "k0", "Alice", jsonl(("user", "old")), captured_days_ago=3)
        _, out, _ = self.cli("status")
        self.assertRegex(out,
                         r"(?m)^transcripts: \S+ \S+ stored \(\S+ \S+ raw, ratio [\d.]+x\), limit 2048 MB/30d, "
                         r"2 jobs, oldest \d{4}-\d\d-\d\d$")
        self.assertRegex(out.splitlines()[-1], r"^supervisor: on, ")
        _, out, _ = self.cli("status", "--job", "J")
        self.assertRegex(out, r"(?m)^transcripts 2 stored, \S+ \S+ \(\S+ \S+ raw\)$")
        self.assertRegex(out, r"\nAGENT\s+ROLE\s+HOST\s+MODEL\s+STATUS\s+CALLS\s+MSGS\s+JOINED\s+LAST CONTACT\s+STORED\s+TOOL\n")
        row = next(l for l in out.splitlines() if l.startswith(name))
        self.assertRegex(row, r"\d+(\.\d)? (B|KB)\s*$")
        row = next(l for l in out.splitlines() if l.startswith(other))
        self.assertRegex(row, r"\s-\s*$")

    def test_empty_archive(self):
        _, out, _ = self.cli("status")
        self.assertIn("transcripts: none stored, limit 2048 MB/30d", out.splitlines())
        self.assertRegex(out.splitlines()[-1], r"^supervisor: on, ")



EVIL = "\x1b]0;pwn\x07\x1b[2J\x9b31m‮\x00"
FORBIDDEN = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f  ‪-‮⁦-⁩]")


class ShowControlsTests(TranscriptEnv):
    """`transcript show` (text and jsonl) never writes a terminal control from a stored
    transcript; jsonl stays valid JSON with the same values."""

    def setUp(self):
        super().setUp()
        self.text = (json.dumps({"type": "user", "timestamp": "2026-09-26T12:00:00Z",
                                 "message": {"role": "user", "content": "hi " + EVIL}}, ensure_ascii=False)
                     + "\nraw line " + EVIL + "\n")
        self.seed("J1", "k1", "Homer Simpson", self.text)

    def test_text(self):
        rc, out, _ = self.cli("transcript", "show", "--job", "J1", "--agent", "Homer Simpson")
        self.assertEqual(rc, 0)
        self.assertIsNone(FORBIDDEN.search(out), repr(out))
        self.assertIn(r"\x1b]0;pwn\x07", out)

    def test_jsonl(self):
        rc, out, _ = self.cli("transcript", "show", "--job", "J1", "--agent", "Homer Simpson",
                              "--format", "jsonl")
        self.assertEqual(rc, 0)
        self.assertIsNone(FORBIDDEN.search(out), repr(out))
        first, second = out.splitlines()
        self.assertEqual(json.loads(first)["message"]["content"], "hi " + EVIL)   # same value
        self.assertTrue(second.startswith("raw line "))

    def test_ambiguity_list(self):
        self.seed("J2" + EVIL, "k2" + EVIL, "Homer Simpson", "x\n")
        rc, out, err = self.cli("transcript", "show", "--agent", "Homer Simpson")
        self.assertEqual(rc, 1)
        self.assertIn("pick one", err)
        self.assertIsNone(FORBIDDEN.search(err), repr(err))

    def test_run_headers(self):
        self.seed("J1", "k1b", "Homer Simpson" + EVIL, "x\n")
        with self.board() as b:
            rows = b.transcripts(job="J1")
        self.assertTrue(rows)
        with mock.patch.object(swarm, "_render_transcript", lambda board, body, args, refs=(): "body"):
            import types
            args = types.SimpleNamespace(agent=None, orchestrator=False, key=None, job="J1", grep=None,
                                         output=None, format="text", tail=None)
            with self.board() as b:
                real = b.transcripts
                b.transcripts = lambda **kw: [r for r in real(job="J1") if r.role == "subagent"]
                args.agent = "x"
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    swarm._transcript_show(b, self.cfg, args)
        self.assertIn("=== Homer Simpson", out.getvalue())
        self.assertIsNone(FORBIDDEN.search(out.getvalue()), repr(out.getvalue()))


class _ImageBoard:
    """The board, with transcript image rows as a forged or pre-CHECK row could have them."""

    def __init__(self, inner, shas):
        self.inner, self.shas = inner, shas

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def transcript_images(self, job=None, agent_key=None):
        from swarm.board.base import TranscriptImage
        return [TranscriptImage(s, "image/png", 3) for s in self.shas]

    def transcript_image(self, sha256):
        from swarm.board.base import TranscriptImage
        return TranscriptImage(sha256, "image/png", 3, b"PNG")


class ExportSafetyTests(TranscriptEnv):
    """Export writes only inside its target dir, never through a planted link, and not into
    a directory a sandboxed agent can write unless --force."""

    GOOD = "ab" * 32

    def setUp(self):
        super().setUp()
        self.seed("J1", "k1", "Homer Simpson", jsonl(("user", "one")))
        self.seed("J1", "orch", "orchestrator", jsonl(("user", "orch")), role="orchestrator")
        self.target = self.tmp / "exp"
        self.outside = self.tmp / "victim"
        self.outside.write_text("precious\n")

    def export(self, board, target=None, force=False):
        import types
        args = types.SimpleNamespace(job="J1", dir=str(target or self.target), force=force)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = swarm._transcript_export(board, self.cfg, args)
        return rc, out.getvalue(), err.getvalue()

    def test_export_rejects_non_hex_sha(self):
        import os
        bad = ["../../victim", "../" * 20 + f"tmp/swarm-m7-{os.getpid()}", "AB" * 32, "ab" * 31,
               "ab" * 32 + "\n", "*"]
        before = sorted(p.name for p in self.tmp.iterdir())
        with self.board() as b:
            rc, out, err = self.export(_ImageBoard(b, bad + [self.GOOD]))
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.outside.read_text(), "precious\n")
        self.assertFalse(os.path.exists(f"/tmp/swarm-m7-{os.getpid()}.png"))
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), sorted(before + ["exp"]))
        written = sorted(p.relative_to(self.target).as_posix() for p in self.target.rglob("*"))
        self.assertEqual(written, ["Homer_Simpson.jsonl", "images", f"images/{self.GOOD}.png", "index.tsv",
                                   "orchestrator.jsonl"])
        self.assertIn("skipped", err)
        self.assertNotIn("../", (self.target / "index.tsv").read_text())

    def plant(self, name, kind):
        import os
        path = self.target / name
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if kind == "symlink":
            path.symlink_to(self.outside)
        elif kind == "dangling":
            path.symlink_to(self.tmp / "nothing-here")
        elif kind == "hardlink":
            os.link(self.outside, path)

    def test_export_does_not_follow_planted_links(self):
        for name in ("orchestrator.jsonl", f"images/{self.GOOD}.png", "index.tsv"):
            for kind in ("symlink", "dangling", "hardlink"):
                with self.subTest(name=name, kind=kind):
                    import shutil
                    shutil.rmtree(self.target, ignore_errors=True)
                    self.target.mkdir(mode=0o700)
                    self.plant(name, kind)
                    with self.board() as b:
                        rc, _, err = self.export(_ImageBoard(b, [self.GOOD]))
                    self.assertEqual(rc, 1)
                    self.assertIn("refus", err)
                    self.assertEqual(self.outside.read_text(), "precious\n")
                    self.assertFalse((self.tmp / "nothing-here").exists())

    def test_export_refuses_a_symlinked_target_or_images_dir(self):
        real = self.tmp / "real"
        real.mkdir(mode=0o700)
        self.target.symlink_to(real)
        with self.board() as b:
            rc, _, err = self.export(b)
        self.assertEqual(rc, 1)
        self.assertEqual(list(real.iterdir()), [])
        self.target.unlink()
        self.target.mkdir(mode=0o700)
        (self.target / "images").symlink_to(real)
        with self.board() as b:
            rc, _, err = self.export(_ImageBoard(b, [self.GOOD]))
        self.assertEqual(rc, 1)
        self.assertEqual(list(real.iterdir()), [])

    def test_export_refuses_sandbox_writable_dirs_unless_forced(self):
        import os
        codex = self.tmp / "codex"
        codex.mkdir()
        shared = self.tmp / "shared"
        shared.mkdir(mode=0o700)
        (codex / "config.toml").write_text(f'[sandbox_workspace_write]\nwritable_roots = [{tq(shared)}]\n')
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(codex)}):
            for target in (self.spool_dir / "t", self.markers / "t", shared / "t"):
                with self.subTest(target=target), self.board() as b:
                    rc, _, err = self.export(b, target)
                    self.assertEqual(rc, 1)
                    self.assertIn("--force", err)
                    self.assertFalse(target.exists())
            with self.board() as b:
                rc, _, err = self.export(b, shared / "t", force=True)
            self.assertEqual(rc, 0, err)
            self.assertTrue((shared / "t" / "index.tsv").is_file())

    def test_export_cli_has_force(self):
        rc, out, err = self.cli("transcript", "export", "--job", "J1", "--force", str(self.target))
        self.assertEqual(rc, 0, err)
        self.assertTrue((self.target / "orchestrator.jsonl").is_file())


class ActivateSessionTests(TranscriptEnv):
    """The session id `activate` binds the job to later becomes a transcript lookup; only a
    plain id (a UUID in practice) is accepted, never a glob or a path."""

    def test_glob_and_path_sessions_are_refused(self):
        for bad in ("*", "?", "[a-f]*", "..", "../x", "a/b", "x\n", "\x1b[2J", "*" * 3, "a" * 200, ".hidden"):
            with self.subTest(session=bad):
                rc, out, err = self.cli("activate", "--job", "J", "--session", bad)
                self.assertEqual(rc, 2)
                self.assertIn("--session", err)
                with self.board() as b:
                    self.assertIsNone(b.job_status("J"))
                self.assertFalse(self.markers.exists() and any(self.markers.iterdir()))

    def test_env_session_is_checked_too(self):
        import os
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "*"}):
            rc, _, err = self.cli("activate", "--job", "J")
        self.assertEqual(rc, 2)

    def test_uuid_and_plain_ids_are_accepted(self):
        for good in ("0b4a36f1-0a3c-4c32-9a53-9d6c2c1f7e11", "sess-1"):
            with self.subTest(session=good):
                rc, _, err = self.cli("activate", "--job", "J", "--session", good)
                self.assertEqual(rc, 0, err)
                with self.board() as b:
                    self.assertEqual(b.job_status("J").session_id, good)


class ColorTests(TranscriptEnv):
    """--color/--no-color/NO_COLOR on `transcript show` and `transcript list` (piped output, the
    default in these tests since self.cli redirects to io.StringIO, stays byte-identical unless
    --color always forces it)."""

    def setUp(self):
        super().setUp()
        self.seed("J1", "k1", "Homer Simpson", jsonl(("user", "hi there")))
        # a distinct key (not overwriting k1: save_transcript is a no-op for an unchanged sha256
        # on an already-final row), with a harness and a real secret so redactions is > 0.
        text = jsonl(("user", "key AKIAABCDEFGHIJKLMNOP done"))
        self.marge_row = transcripts.make_row("J1", "k2", "Marge Simpson", "subagent", text,
                                              host="h1", session_id="s1", harness="codex")
        with self.board() as b:
            b.save_transcript(self.marge_row)
        self.assertGreater(self.marge_row.redactions, 0)   # sanity: the fake AWS key was redacted

    def test_show_default_is_uncolored(self):
        rc, out, _ = self.cli("transcript", "show", "--job", "J1", "--agent", "Homer Simpson")
        self.assertEqual(rc, 0)
        self.assertNotIn("\033[", out)

    def test_show_color_always_adds_codes(self):
        rc, out, _ = self.cli("transcript", "show", "--job", "J1", "--agent", "Homer Simpson", "--color", "always")
        self.assertEqual(rc, 0)
        self.assertIn("\033[1;34muser\033[0m", out)

    def test_show_no_color_overrides_color_always(self):
        rc, out, _ = self.cli("transcript", "show", "--job", "J1", "--agent", "Homer Simpson",
                              "--color", "always", "--no-color")
        self.assertEqual(rc, 0)
        self.assertNotIn("\033[", out)

    def test_show_no_color_env_overrides_color_always(self):
        with mock.patch.dict("os.environ", {"NO_COLOR": "1"}):
            rc, out, _ = self.cli("transcript", "show", "--job", "J1", "--agent", "Homer Simpson", "--color", "always")
        self.assertEqual(rc, 0)
        self.assertNotIn("\033[", out)

    def test_show_jsonl_is_never_colored(self):
        rc, out, _ = self.cli("transcript", "show", "--job", "J1", "--agent", "Homer Simpson",
                              "--format", "jsonl", "--color", "always")
        self.assertEqual(rc, 0)
        self.assertNotIn("\033[", out)

    def test_show_output_file_is_never_colored(self):
        path = self.tmp / "out.txt"
        rc, _, _ = self.cli("transcript", "show", "--job", "J1", "--agent", "Homer Simpson",
                            "--color", "always", "-o", str(path))
        self.assertEqual(rc, 0)
        self.assertNotIn("\033[", path.read_text())

    def test_list_default_is_uncolored(self):
        rc, out, _ = self.cli("transcript", "list")
        self.assertEqual(rc, 0)
        self.assertNotIn("\033[", out)

    def test_list_color_always_bolds_header_and_paints_columns(self):
        rc, out, _ = self.cli("transcript", "list", "--color", "always")
        self.assertEqual(rc, 0)
        lines = out.splitlines()
        self.assertTrue(lines[0].startswith("\033[1m"))   # bold header row
        self.assertIn("\033[35mcodex", out)                 # HOST: codex (cell is padded before the reset)
        self.assertIn(f"\033[33m{self.marge_row.redactions}", out)   # REDACTED > 0, yellow
        self.assertIn("\033[32myes", out)                    # FINAL: yes, green

    def test_list_no_color_overrides_color_always(self):
        rc, out, _ = self.cli("transcript", "list", "--color", "always", "--no-color")
        self.assertEqual(rc, 0)
        self.assertNotIn("\033[", out)

    def test_list_alignment_unaffected_by_color(self):
        """Column widths come from the plain text either way, so a coloured and an uncoloured run
        have the same visible layout once ANSI codes are stripped."""
        _, plain, _ = self.cli("transcript", "list")
        _, colored, _ = self.cli("transcript", "list", "--color", "always")
        stripped = re.sub(r"\033\[[0-9;]*m", "", colored)
        self.assertEqual(plain, stripped)
