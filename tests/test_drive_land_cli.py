"""Task 11b: `swarm land` (the integrator's request) and `swarm land hosts add` (swarm.land_cli)."""
from __future__ import annotations

import argparse
import contextlib
import io
import os
import re
import shutil
import tempfile
import unittest
from unittest import mock

import support  # noqa: F401
from swarm import land, land_cli
from swarm.supervisor import mirror

SHA = "a" * 40
ART = f"feat/x@{SHA}"
EXAMPLE_LINE = "example.test ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl"


def ns(**kw):
    base = dict(job="J", artifact=ART, name=None, key="k1", land_cmd=None)
    base.update(kw)
    return argparse.Namespace(**base)


def run(fn, *a):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = fn(*a)
    return code, out.getvalue(), err.getvalue()


class LandCli(unittest.TestCase):
    def setUp(self):
        self.root = os.path.realpath(tempfile.mkdtemp(prefix="landcli-", dir=support.SANDBOX))
        self.addCleanup(shutil.rmtree, self.root, True)
        p = mock.patch.dict(os.environ, support.home_env(self.root))
        p.start()
        self.addCleanup(p.stop)
        self.h = support.MemoryHarness()
        self.h.reset()
        self.board = self.h.board()
        self.addCleanup(self.board.close)
        self.board.open_job("J", None, None, None, "me", goal="G")
        self.board.set_job_data("J", "drive.enabled", "1")
        self.name = self.board.allocate_name("k1", "J", "engineer")

    def messages(self):
        return [m for m in self.board.messages_after(0, "J") if m.message.startswith("LAND-REQUEST")]

    def test_request_posts_the_exact_text_and_prints_the_record_line_last(self):
        code, out, err = run(land_cli._board_land, self.board, {}, ns())
        self.assertEqual(code, 0, err)
        msgs = self.messages()
        self.assertEqual([m.message for m in msgs], [f"LAND-REQUEST {ART}"])
        self.assertEqual(msgs[0].agent_name, self.name)
        self.assertIsNone(msgs[0].to_agent)
        last = out.strip().splitlines()[-1]
        m = re.fullmatch(r"swarm-record: kind=([a-z_]+) id=(\d+) rev=(\d+)", last)
        self.assertEqual((m.group(1), int(m.group(2)), m.group(3)), ("post", msgs[0].id, "1"))
        self.assertIn("INTEGRATED", out)

    def test_not_a_drive_job_is_refused(self):
        self.board.set_job_data("J", "drive.enabled", None)
        code, out, err = run(land_cli._board_land, self.board, {}, ns())
        self.assertEqual(code, 1)
        self.assertIn("not a drive job", err)
        self.assertEqual(self.messages(), [])

    def test_bad_artifact_and_missing_arguments_refused(self):
        for artifact in ("feat/x", f"feat/x@{SHA[:12]}", "x@" + "Z" * 40):
            code, out, err = run(land_cli._board_land, self.board, {}, ns(artifact=artifact))
            self.assertEqual(code, 1, artifact)
            self.assertIn("refused", err)
        for kw in ({"job": None}, {"artifact": None}):
            code, out, err = run(land_cli._board_land, self.board, {}, ns(**kw))
            self.assertEqual(code, 2)
        self.assertEqual(self.messages(), [])

    def test_author_must_be_an_agent_of_the_job(self):
        code, out, err = run(land_cli._board_land, self.board, {}, ns(key=None, name="nobody-here"))
        self.assertEqual(code, 1)
        self.assertIn("not requested", err)
        code, out, err = run(land_cli._board_land, self.board, {}, ns(key=None, name=None))
        self.assertEqual(code, 2)
        self.assertEqual(self.messages(), [])

    def test_name_and_key_must_agree(self):
        code, out, err = run(land_cli._board_land, self.board, {}, ns(key="other-key", name=self.name))
        self.assertEqual(code, 1)
        self.assertEqual(self.messages(), [])

    def test_the_parser_accepts_both_forms_and_cli_dispatches_land(self):
        from swarm import cli
        self.assertIs(cli.BOARD_COMMANDS["land"], land_cli._board_land)
        parser = cli._parser()
        a = parser.parse_args(["land", "--job", "J", "--artifact", ART, "--as", "n"])
        self.assertEqual((a.cmd, a.job, a.artifact, a.land_cmd), ("land", "J", ART, None))
        a = parser.parse_args(["land", "hosts", "add", "git@example.test:o/r.git"])
        self.assertEqual((a.land_cmd, a.hosts_cmd, a.url), ("hosts", "add", "git@example.test:o/r.git"))


class HostsAdd(unittest.TestCase):
    def setUp(self):
        self.root = os.path.realpath(tempfile.mkdtemp(prefix="landhosts-", dir=support.SANDBOX))
        self.addCleanup(shutil.rmtree, self.root, True)
        p = mock.patch.dict(os.environ, support.home_env(self.root))
        p.start()
        self.addCleanup(p.stop)
        self.url = "git@example.test:o/r.git"
        from swarm import person
        anc = mock.patch.object(person, "_ancestor_host", lambda: None)   # these tests run under an agent host
        anc.start()
        self.addCleanup(anc.stop)
        for var in person.HOST_ENV:   # nor with any agent host variable (CI has none; this box's shells do)
            if var in os.environ:
                p = mock.patch.dict(os.environ)
                p.start()
                self.addCleanup(p.stop)
                for v in person.HOST_ENV:
                    os.environ.pop(v, None)
                break

    def add(self, answer=None, tty=True, keys=None, stdout_tty=None):
        fake_keys = [(EXAMPLE_LINE, "ssh-ed25519 SHA256:abc")] if keys is None else keys
        stdin = mock.Mock(isatty=lambda: tty)
        out_tty = tty if stdout_tty is None else stdout_tty
        with mock.patch.object(land_cli.sys, "stdin", stdin), mock.patch.object(
                mirror, "scan_host_keys", lambda h, p, *a: fake_keys), mock.patch(
                "builtins.input", lambda prompt="": answer):
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stderr(err):
                with mock.patch.object(land_cli.sys, "stdout", out):
                    stdout_isatty = mock.patch.object(out, "isatty", lambda: out_tty, create=True)
                    with stdout_isatty:
                        code = land_cli._hosts_add(argparse.Namespace(url=self.url))
        return code, out.getvalue(), err.getvalue()

    def trusted(self):
        return mirror.known_host_present(mirror.known_hosts_file(), "example.test", 22)

    def test_confirming_the_host_name_trusts_the_keys(self):
        code, out, err = self.add(answer="example.test")
        self.assertEqual(code, 0, err)
        self.assertIn("SHA256:abc", out)
        self.assertTrue(self.trusted())

    def test_a_wrong_or_empty_answer_trusts_nothing(self):
        for answer in ("", "yes", "y", "example.test.evil", None, "Example.test"):
            code, out, err = self.add(answer=answer or "")
            self.assertEqual(code, 1, answer)
            self.assertFalse(self.trusted(), answer)

    def test_no_terminal_means_no_trust(self):
        code, out, err = self.add(answer="example.test", tty=False)
        self.assertEqual(code, 1)
        self.assertIn("person at a terminal", err)
        self.assertFalse(self.trusted())

    def test_a_redirected_stdout_is_not_a_terminal_either(self):
        code, out, err = self.add(answer="example.test", tty=True, stdout_tty=False)
        self.assertEqual(code, 1)
        self.assertFalse(self.trusted())

    def test_an_agent_session_cannot_trust_a_host_key_even_on_a_terminal(self):
        scanned = []
        for var in ("CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID", "CODEX_SESSION_ID"):
            with self.subTest(var=var), mock.patch.dict(os.environ, {var: "1"}):
                with mock.patch.object(mirror, "scan_host_keys", lambda *a: scanned.append(a) or []):
                    code, out, err = self.add(answer="example.test", tty=True)
                self.assertEqual(code, 1)
                self.assertIn("agent session", err)
                self.assertFalse(self.trusted())
        self.assertEqual(scanned, [])

    def test_an_agent_that_unset_its_session_variables_is_still_refused(self):
        """S2: `env -u CLAUDECODE ...` inside an agent's pty: no host variable is left, but a claude ancestor is."""
        from swarm import person
        env = {k: v for k, v in os.environ.items() if k not in person.HOST_ENV}
        scanned = []
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(person, "_ancestor_host", lambda: "claude"), \
                mock.patch.object(mirror, "scan_host_keys", lambda *a: scanned.append(a) or []):
            code, out, err = self.add(answer="example.test", tty=True)
        self.assertEqual(code, 1)
        self.assertIn("claude process", err)
        self.assertFalse(self.trusted())
        self.assertEqual(scanned, [])

    def test_a_person_outside_any_session_still_can(self):
        from swarm import person
        env = {k: v for k, v in os.environ.items() if k not in person.HOST_ENV}
        with mock.patch.dict(os.environ, env, clear=True):
            code, out, err = self.add(answer="example.test")
        self.assertEqual(code, 0, err)

    def test_scan_never_runs_without_a_terminal(self):
        called = []
        with mock.patch.object(mirror, "scan_host_keys", lambda *a: called.append(a) or []):
            stdin = mock.Mock(isatty=lambda: False)
            with mock.patch.object(land_cli.sys, "stdin", stdin), contextlib.redirect_stderr(io.StringIO()):
                land_cli._hosts_add(argparse.Namespace(url=self.url))
        self.assertEqual(called, [])

    def test_non_ssh_url_and_already_trusted(self):
        code, out, err = run(land_cli._hosts_add, argparse.Namespace(url="https://example.test/o/r.git"))
        self.assertEqual(code, 1)
        self.assertIn("not an ssh remote", err)
        mirror.add_known_host(mirror.known_hosts_file(), "example.test", 22, [EXAMPLE_LINE], confirmed=True)
        code, out, err = run(land_cli._hosts_add, argparse.Namespace(url=self.url))
        self.assertEqual(code, 0)
        self.assertIn("already trusted", out)

    def test_a_scan_failure_is_reported_not_trusted(self):
        def boom(h, p, *a):
            raise mirror.MirrorError("example.test:22 offered no host keys")
        stdin = mock.Mock(isatty=lambda: True)
        out = io.StringIO()
        out.isatty = lambda: True
        with mock.patch.object(land_cli.sys, "stdin", stdin), mock.patch.object(land_cli.sys, "stdout", out), \
                mock.patch.object(mirror, "scan_host_keys", boom), contextlib.redirect_stderr(io.StringIO()) as err:
            code = land_cli._hosts_add(argparse.Namespace(url=self.url))
        self.assertEqual(code, 1)
        self.assertIn("no host keys", err.getvalue())
        self.assertFalse(self.trusted())


if __name__ == "__main__":
    unittest.main()
