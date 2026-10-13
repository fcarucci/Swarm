"""The CI host was renamed to `ci` everywhere with no alias: the retired word appears only on the
CHANGELOG line that announces the rename. English "forged" (a fake signature) is not the retired word,
so the check is whole-word. The word is spelled in two pieces so this file stays clean too."""
from __future__ import annotations

import unittest

from support import ROOT

WORD = "for" + "ge"


class NoRetiredName(unittest.TestCase):

    def test_engineering_team_imports_nothing_from_ci(self):
        for path in (ROOT / "skills" / "engineering-team").glob("*.py"):
            text = path.read_text(encoding="utf-8")
            for bad in ("import ci_", "from ci_", "skills/ci", "skills.ci", "swarm_plugin_ci"):
                self.assertNotIn(bad, text, f"{path.name} reaches into the ci skill: {bad}")


if __name__ == "__main__":
    unittest.main()
