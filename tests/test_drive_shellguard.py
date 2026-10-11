"""Drive-seat command refusals (spec 3.4, 4.0): target pushes, force pushes, headless sessions."""
from __future__ import annotations

import unittest

from support import SANDBOX  # noqa: F401  (puts lib/ on sys.path)

from swarm.shellguard import drive_command_refusal as refuse


class DriveRefusals(unittest.TestCase):
    def test_push_to_target_refused_in_every_spelling(self):
        for cmd in ("git push origin HEAD:main", "git push origin main", "git push origin feat:refs/heads/main",
                    "git -C /w/a push origin HEAD:main", "git push origin :main"):
            with self.subTest(cmd=cmd):
                self.assertIn("target branch", refuse(cmd, ["main"]))

    def test_push_to_another_branch_allowed(self):
        self.assertIsNone(refuse("git push origin HEAD:feat/a", ["main"]))
        self.assertIsNone(refuse("git push origin feat/mainline", ["main"]))

    def test_force_pushes_refused(self):
        for cmd in ("git push --force origin feat/a", "git push -f origin feat/a", "git push origin +feat/a",
                    "git push --force-with-lease origin feat/a", "git push -fu origin feat/a"):
            with self.subTest(cmd=cmd):
                self.assertIn("force", refuse(cmd, ["main"]))

    def test_headless_sessions_refused(self):
        self.assertIn("claude -p", refuse("claude -p 'do it'", ["main"]))
        self.assertIn("claude -p", refuse("claude --print x", ["main"]))
        self.assertIn("codex exec", refuse("codex exec --json 'x'", ["main"]))

    def test_ordinary_commands_allowed(self):
        for cmd in ("git status", "git fetch origin", "swarm done --job J", "python3 -m unittest test_x",
                    "echo 'git push origin main'", "codex --version", "claude"):
            with self.subTest(cmd=cmd):
                self.assertIsNone(refuse(cmd, ["main"]))

    def test_no_targets_still_refuses_force_and_headless(self):
        self.assertIsNone(refuse("git push origin main", ()))
        self.assertIn("force", refuse("git push -f origin main", ()))


if __name__ == "__main__":
    unittest.main()
