"""`swarm upgrade` (formerly `swarm update`, kept as a hidden alias) and its automatic migrate --force."""
from __future__ import annotations

import io
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest import mock

from support import ROOT  # noqa: F401

from swarm import cli, update  # noqa: E402


class UpgradeCommandTests(unittest.TestCase):
    def _run(self, *argv):
        seen = {}
        with mock.patch.object(update, "run_update", side_effect=lambda *a, **k: seen.setdefault("a", a) and 0):
            rc = cli.main(["upgrade" if False else argv[0], *argv[1:]])
        return rc, seen

    def test_upgrade_runs_the_update_code(self):
        rc, seen = self._run("upgrade", "--host", "claude", "--force")
        self.assertEqual(rc, 0)
        self.assertEqual(seen["a"][:2], ("claude", True))

    def test_update_is_still_an_alias(self):
        rc, seen = self._run("update", "--host", "codex")
        self.assertEqual(rc, 0)
        self.assertEqual(seen["a"][:2], ("codex", False))

    def test_help_documents_upgrade_and_hides_update(self):
        out = io.StringIO()
        with redirect_stdout(out), self.assertRaises(SystemExit):
            cli.main(["--help"])
        text = out.getvalue()
        self.assertIn("upgrade", text)
        self.assertNotRegex(text, r"(?m)^\s+update\b")
        self.assertNotIn(",update", text)
        self.assertNotIn("update,", text)


if __name__ == "__main__":
    unittest.main()
