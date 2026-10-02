"""The package layout: lib/swarm is the code, bin/ holds only the two launchers."""
from __future__ import annotations

import os
import subprocess
import sys
import unittest

from support import ROOT, posix_only  # noqa: F401  (sets sys.path)


class LayoutTests(unittest.TestCase):
    def test_bin_holds_only_launchers(self):
        self.assertEqual(sorted(p.name for p in (ROOT / "bin").iterdir()), ["swarm", "swarm-hook", "swarm-hook.cmd", "swarm.cmd"])

    def test_package_modules_import(self):
        import swarm.cli, swarm.hooks, swarm.board, swarm.spool, swarm.transcripts  # noqa: E401,F401
        from swarm import paths
        self.assertEqual(paths.PLUGIN_ROOT, ROOT)
        self.assertTrue((paths.DATA_DIR / "simpsons_names.json").exists())

    def test_hooks_import_stays_stdlib_only(self):
        # importing the hook module must not import the board package or psycopg
        code = ("import sys; sys.path.insert(0, %r); import swarm.hooks; "
                "bad = [m for m in ('swarm.board', 'psycopg') if m in sys.modules]; print(bad)") % str(ROOT / "lib")
        out = subprocess.run([sys.executable, "-B", "-c", code], capture_output=True, text=True, check=True)
        self.assertEqual(out.stdout.strip(), "[]")

    @posix_only("runs a POSIX sh script (the Windows entry points are tested in test_windows_*.py)")
    def test_cli_help_through_launcher(self):
        import support   # a throwaway venv with a matching stamp: the launcher builds nothing
        res = subprocess.run([str(ROOT / "bin" / "swarm"), "--help"], capture_output=True, text=True, timeout=60,
                             env={**os.environ, "SWARM_VENV": str(support.temp_venv())})
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("activate", res.stdout)

    def test_readme_documents_every_supervise_key(self):
        # The concise README doesn't enumerate every key; the full reference does.
        from swarm.supervisor.settings import DEFAULTS
        text = (ROOT / "docs" / "REFERENCE.md").read_text()
        missing = [k for k in DEFAULTS if f"`{k}`" not in text]
        self.assertEqual(missing, [])
        skill = (ROOT / "skills/swarm/SKILL.md").read_text()
        self.assertIn("swarm supervise --dry-run", skill)
        self.assertIn("--no-supervise", skill)
