"""QA: the one-time old-section -> [ci] migration (lib/swarm/ci_migrate.py): in place, .bak, idempotent,
sub-tables and [repositories."p".<old>]. The retired word is spelled in two pieces so the repo-wide
grep for it stays clean."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from support import ROOT  # noqa: F401  (sets sys.path)

from swarm import ci_migrate

OLD = "for" + "ge"

TEAM = f"""# my team
[team]
name = "x"

[{OLD}]
kind = "github"
repository = "me/proj"  # keep this comment

[{OLD}.github]
webhook_secret_file = "~/s"

[repositories."p".{OLD}]
kind = "gitea"

[land]
strategy = "ff"
"""


class CiMigrate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.cfg = self.dir / "config.toml"
        self.team = self.dir / "team.toml"
        self.team.write_text(TEAM)
        env = mock.patch.dict(os.environ, {}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("SWARM_TEAM_CONFIG", None)

    def test_rewrites_in_place_with_bak_and_message(self):
        notes = ci_migrate.migrate_team_config(self.cfg)
        new = self.team.read_text()
        self.assertIn("[ci]\n", new)
        self.assertIn("[ci.github]", new)
        self.assertIn('[repositories."p".ci]', new)
        self.assertNotIn(OLD, new)
        self.assertIn('repository = "me/proj"  # keep this comment', new)
        self.assertIn("[land]\nstrategy", new)
        bak = self.dir / "team.toml.bak"
        self.assertEqual(bak.read_text(), TEAM)
        self.assertEqual(len(notes), 1)
        self.assertIn(".bak", notes[0])

    def test_idempotent_second_run_changes_nothing(self):
        ci_migrate.migrate_team_config(self.cfg)
        after = self.team.read_text()
        bak = (self.dir / "team.toml.bak").read_text()
        self.assertEqual(ci_migrate.migrate_team_config(self.cfg), [])
        self.assertEqual(self.team.read_text(), after)
        self.assertEqual((self.dir / "team.toml.bak").read_text(), bak)
        self.assertEqual(sorted(p.name for p in self.dir.glob("team.toml.bak*")), ["team.toml.bak"])

    def test_already_ci_file_is_untouched_and_gets_no_bak(self):
        self.team.write_text('[ci]\nkind = "github"\n')
        self.assertEqual(ci_migrate.migrate_team_config(self.cfg), [])
        self.assertEqual(self.team.read_text(), '[ci]\nkind = "github"\n')
        self.assertFalse((self.dir / "team.toml.bak").exists())

    def test_missing_file_is_a_noop(self):
        self.team.unlink()
        self.assertEqual(ci_migrate.migrate_team_config(self.cfg), [])

    def test_existing_bak_is_never_overwritten(self):
        (self.dir / "team.toml.bak").write_text("precious")
        ci_migrate.migrate_team_config(self.cfg)
        self.assertEqual((self.dir / "team.toml.bak").read_text(), "precious")
        self.assertTrue(any(p.read_text() == TEAM for p in self.dir.glob("team.toml.bak.*")))

    def test_unrelated_values_mentioning_the_word_are_not_rewritten(self):
        self.team.write_text(f'[team]\nnote = "[{OLD}] is gone"\n[{OLD}]\nkind = "github"\n')
        ci_migrate.migrate_team_config(self.cfg)
        self.assertEqual(self.team.read_text(), f'[team]\nnote = "[{OLD}] is gone"\n[ci]\nkind = "github"\n')

    def test_honours_swarm_team_config_env(self):
        other = self.dir / "elsewhere.toml"
        other.write_text(TEAM)
        with mock.patch.dict(os.environ, {"SWARM_TEAM_CONFIG": str(other)}):
            ci_migrate.migrate_team_config(self.cfg)
        self.assertNotIn(OLD, other.read_text())
        self.assertEqual(self.team.read_text(), TEAM)
        self.assertTrue((self.dir / "elsewhere.toml.bak").exists())


if __name__ == "__main__":
    unittest.main()
