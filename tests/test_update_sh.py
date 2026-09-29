"""Python unittest wrapper for e2e/update_test.sh (`swarm update`).

Same convention as test_install_sh.py: `unittest discover` only finds *.py, and the actual test
logic -- scratch HOMEs, stub claude/codex CLIs, fake old plugin roots -- is a shell script,
following the repo's e2e/*.sh convention. This file is a thin subprocess wrapper so the suite
picks it up.

Never touches the real HOME, ~/.claude, ~/.codex, or the live swarm skill/board: every HOME the
shell script uses is a fresh mktemp directory of its own (see e2e/update_test.sh SCRATCH_ROOT).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import unittest

import support
from support import ROOT


@unittest.skipUnless(shutil.which("bash"), "bash not on PATH")
class UpdateShTests(unittest.TestCase):
    def test_e2e_update_sh(self):
        script = ROOT / "e2e" / "update_test.sh"
        self.assertTrue(script.exists(), f"missing {script}")
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("SWARM_") and k not in ("CLAUDE_CONFIG_DIR", "CODEX_HOME")}
        env["E2E_VENV"] = str(support.temp_venv())
        res = subprocess.run(["bash", str(script)], capture_output=True, text=True, timeout=600,
                             env=env)
        self.assertEqual(res.returncode, 0,
                          f"e2e/update_test.sh failed:\nSTDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}")
        self.assertIn("E2E OK: update", res.stdout)
        for scenario in ("update-old-to-new", "already-up-to-date", "force", "host-codex",
                         "host-command-fails", "hooks-changed-detection",
                         "hooks-unchanged-no-reminder"):
            self.assertIn(f"{scenario}: ok", res.stdout)


if __name__ == "__main__":
    unittest.main()
