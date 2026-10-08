"""engineering-team: [land] strategy and the PM event procedure text."""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "skills" / "engineering-team"


def load(name):
    spec = importlib.util.spec_from_file_location("t_" + name, ROOT / (name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["t_" + name] = mod
    spec.loader.exec_module(mod)
    return mod


class LandTests(unittest.TestCase):
    def test_land_strategy_default_and_validation(self):
        sp = load("swarm_plugin")
        with tempfile.TemporaryDirectory() as d:
            ctx = type("C", (), {"config_dir": Path(d)})()
            old = os.environ.pop("SWARM_TEAM_CONFIG", None)
            try:
                self.assertEqual(sp.coding_settings(ctx)["land_strategy"], "rebase-ff")
                (Path(d) / "team.toml").write_text('[land]\nstrategy = "squash-ff"\n')
                self.assertEqual(sp.coding_settings(ctx)["land_strategy"], "squash-ff")
                (Path(d) / "team.toml").write_text('[land]\nstrategy = "merge"\n')
                with self.assertRaises(sp.TeamError):
                    sp.coding_settings(ctx)
            finally:
                if old is not None:
                    os.environ["SWARM_TEAM_CONFIG"] = old


class SkillTextTests(unittest.TestCase):
    def test_pm_procedure_covers_every_event(self):
        text = (ROOT / "SKILL.md").read_text()
        for word in ("NEEDS-REVIEW", "READY-TO-LAND", "CI-FAILED", "BRANCH-READY", "swarm event wait --job J --to @pm",
                     "rebase-ff", "squash-ff"):
            self.assertIn(word, text)


if __name__ == "__main__":
    unittest.main()
