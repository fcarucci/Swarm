"""`swarm supervise approve <dir>` and `swarm notices --hook-output`: the CLI side.

approve records the project configuration files of a work dir (supervisor.command.approval_candidates / save_approvals
own the format and the store) as
approved for supervisor launches. It is the out-of-band consent the launch check relies on, so
it must be impossible from an agent's shell: it needs a TTY on stdin, a typed confirmation, and
no harness session variable in the environment. Refused, it writes nothing.

notices --hook-output prints the SessionStart hook output built by
bootstrap.hook_output(host) from the host-private notice (a fixed template), and nothing else."""
from __future__ import annotations

import sys as _sys
import unittest as _unittest
if _sys.platform == "win32":
    raise _unittest.SkipTest("swarm supervise is not supported on Windows")

import contextlib
import io
import json
import os
from pathlib import Path
import unittest
from unittest import mock

from support import ROOT  # noqa: F401  (sets sys.path)

from swarm import cli as swarm  # noqa: E402
from test_hooks_cli import Env  # noqa: E402

ENTRIES = [{"dir": "/home/u/src/p", "file": ".claude/settings.json", "sha256": "ab" * 32},
           {"dir": "/home/u/src/p", "file": ".mcp.json\x1b[2J", "sha256": "cd" * 32}]


class TtyInput(io.StringIO):
    """stdin that claims to be a terminal."""

    def isatty(self):
        return True


class ApproveTests(Env):
    def setUp(self):
        super().setUp()
        from swarm.supervisor import command
        self.saved = []
        self.asked = []

        def candidates(cfg, d):
            self.asked.append(d)
            # The random temporary parent may contain either fixture word.
            name = Path(d).name
            if name == "bad":
                raise ValueError("not under an allowed root")
            return [] if name == "empty" else [dict(e) for e in ENTRIES]

        for name, fn in (("approval_candidates", candidates),
                         ("save_approvals", lambda entries: self.saved.append(entries))):
            p = mock.patch.object(command, name, fn)
            p.start()
            self.addCleanup(p.stop)
        # approve needs no board: opening one fails the test
        p = mock.patch("swarm.board.open_board", side_effect=AssertionError("board opened"))
        p.start()
        self.addCleanup(p.stop)
        self.dir = str(self.tmp / "work")

    def approve(self, stdin, env=None, d=None):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch("sys.stdin", stdin), mock.patch.dict(os.environ, env or {}):
            rc = swarm.main(["--config", str(self.config), "supervise", "approve", d or self.dir])
        return rc, out.getvalue(), err.getvalue()

    def test_approve_requires_tty_and_confirmation(self):
        cases = [("no tty", io.StringIO("approve\n"), {}),
                 ("claude session", TtyInput("approve\n"), {"CLAUDE_CODE_SESSION_ID": "s"}),
                 ("codex thread", TtyInput("approve\n"), {"CODEX_THREAD_ID": "t"}),
                 ("claude code", TtyInput("approve\n"), {"CLAUDECODE": "1"}),
                 ("no confirmation", TtyInput("yes\n"), {}),
                 ("empty input", TtyInput(""), {})]
        for label, stdin, env in cases:
            with self.subTest(label):
                rc, _, err = self.approve(stdin, env)
                self.assertNotEqual(rc, 0)
                self.assertEqual(self.saved, [])
                self.assertIn("not approved", err)

    def test_confirmed_on_a_tty_saves_the_entries(self):
        rc, out, err = self.approve(TtyInput("approve\n"))
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.saved, [ENTRIES])
        self.assertEqual(self.asked, [self.dir])
        self.assertIn(".claude/settings.json", out)
        self.assertIn("ab" * 32, out)
        self.assertNotIn("\x1b", out)   # file names are shown escaped
        self.assertIn(r".mcp.json\x1b[2J", out)

    def test_refused_dir(self):
        rc, _, err = self.approve(TtyInput("approve\n"), d=str(self.tmp / "bad"))
        self.assertEqual(rc, 1)
        self.assertIn("not under an allowed root", err)
        self.assertEqual(self.saved, [])

    def test_nothing_to_approve(self):
        rc, out, _ = self.approve(TtyInput("approve\n"), d=str(self.tmp / "bad-parent" / "empty"))
        self.assertEqual(rc, 0)
        self.assertIn("nothing to approve", out)
        self.assertEqual(self.saved, [])

    def test_the_session_check_comes_before_any_listing(self):
        self.approve(TtyInput("approve\n"), {"CLAUDE_CODE_SESSION_ID": "s"})
        self.approve(io.StringIO("approve\n"))
        self.assertEqual(self.asked, [])

    def test_plain_supervise_still_parses(self):
        args = swarm._parser().parse_args(["supervise", "--dry-run", "--job", "J"])
        self.assertTrue(args.dry_run)
        self.assertIsNone(getattr(args, "scmd", None))


class NoticesHookOutputTests(Env):
    OUT = json.dumps({"systemMessage": "[swarm] setup needs you:\n- venv: x",
                      "hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "y"}})

    def run_notices(self, result=None, raises=None, host="claude"):
        from swarm import bootstrap
        calls = []

        def hook_output(h):
            calls.append(h)
            if raises:
                raise raises
            return result
        with mock.patch.object(bootstrap, "hook_output", hook_output), \
                mock.patch("swarm.board.open_board", side_effect=AssertionError("board opened")), \
                mock.patch.object(swarm, "auto_init", side_effect=AssertionError("auto_init")):
            argv = ["notices", "--hook-output"] + (["--host", host] if host else [])
            rc, out, err = self.cli(*argv)
        return rc, out, err, calls

    def test_prints_the_template_output(self):
        rc, out, _, calls = self.run_notices(self.OUT)
        self.assertEqual((rc, out, calls), (0, self.OUT + "\n", ["claude"]))

    def test_nothing_pending(self):
        rc, out, _, calls = self.run_notices(None, host="codex")
        self.assertEqual((rc, out, calls), (0, "", ["codex"]))

    def test_never_fails_the_hook(self):
        rc, out, _, _ = self.run_notices(raises=OSError("boom"))
        self.assertEqual((rc, out), (0, ""))

    def test_only_a_hook_output_object_is_printed(self):
        for bad in ("not json", "[1]", json.dumps({"systemMessage": "x", "decision": "block"}),
                    self.OUT + "\n" + self.OUT):
            with self.subTest(bad=bad):
                rc, out, _, _ = self.run_notices(bad)
                self.assertEqual((rc, out), (0, ""))


if __name__ == "__main__":
    unittest.main()
