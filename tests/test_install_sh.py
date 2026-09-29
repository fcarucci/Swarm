"""Python unittest wrapper for e2e/install_test.sh (the swarm installer, install.sh).

`unittest discover` only finds *.py, and the actual test logic -- scratch HOMEs, stub claude/codex
CLIs, this checkout as a local --marketplace frozen tree -- is a shell script, following the repo's
e2e/*.sh convention (fail()/pass-by-falling-through, one process per scenario). This file is a thin
subprocess wrapper so the suite picks it up.

Never touches the real HOME, ~/.claude, ~/.codex, or the live swarm skill/board: every HOME the
shell script uses is a fresh mktemp directory of its own (see e2e/install_test.sh SCRATCH_ROOT).
Root is simulated, never real: a stub `id` answers uid 0, and a stub `sudo` runs each per-user
command with that scratch user's HOME, stub CLIs and a non-zero fake uid, snapshotting the user's
home around the run so the script can prove root itself wrote nothing into it.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import unittest

from support import ROOT


@unittest.skipUnless(shutil.which("bash"), "bash not on PATH")
class InstallShTests(unittest.TestCase):
    def test_e2e_install_sh(self):
        script = ROOT / "e2e" / "install_test.sh"
        self.assertTrue(script.exists(), f"missing {script}")
        # A scratch-only environment: nothing from the caller (e.g. a real
        # SWARM_INSTALL_TEST_USERS or SWARM_CONFIG) can leak into the scenarios.
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("SWARM_") and k not in ("CLAUDE_CONFIG_DIR", "CODEX_HOME")}
        res = subprocess.run(["bash", str(script)], capture_output=True, text=True, timeout=600,
                             env=env)
        self.assertEqual(res.returncode, 0,
                          f"e2e/install_test.sh failed:\nSTDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}")
        self.assertIn("E2E OK: install", res.stdout)
        # Every scenario must have run and passed, not just the script as a whole.
        for scenario in ("help", "no-tty", "detection", "idempotent", "refuses-active-job",
                         "no-config-file-board", "non-root-notes-other-users", "all-users-needs-root",
                         "root-all-users", "root-piped", "uses-installed-plugin-not-launcher"):
            self.assertIn(f"{scenario}: ok", res.stdout)


if __name__ == "__main__":
    unittest.main()
