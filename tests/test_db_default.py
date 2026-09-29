from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from support import ROOT  # noqa: F401

from swarm import bootstrap, cli  # noqa: E402


class DatabaseDefaultTests(unittest.TestCase):
    def load(self, text: str | None):
        d = Path(tempfile.mkdtemp(prefix="swarm-dbdef-"))
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        path = d / "config.toml"
        if text is not None:
            path.write_text(text)
        return path, cli.load_config(path)

    def test_new_default_without_a_config(self):
        _, cfg = self.load(None)
        self.assertEqual((cfg["database"]["user"], cfg["database"]["dbname"]), ("swarm", "swarm_board"))

    def test_config_without_database_section_gets_the_new_default(self):
        _, cfg = self.load('[board]\nbackend = "postgres"\n')
        self.assertEqual(cfg["database"]["dbname"], "swarm_board")

    def test_database_section_without_dbname_or_user_keeps_the_old_default(self):
        _, cfg = self.load('[database]\nhost = "db"\n')
        self.assertEqual(cfg["database"]["dbname"], "agent-message-board")
        self.assertEqual(cfg["database"]["user"], "agent_board")
        self.assertEqual(cfg["database"]["host"], "db")

    def test_explicit_values_win(self):
        _, cfg = self.load('[database]\nuser = "u"\ndbname = "swarm_board"\n')
        self.assertEqual((cfg["database"]["user"], cfg["database"]["dbname"]), ("u", "swarm_board"))

    def test_only_the_missing_key_falls_back(self):
        _, cfg = self.load('[database]\nuser = "u"\n')
        self.assertEqual((cfg["database"]["user"], cfg["database"]["dbname"]), ("u", "agent-message-board"))

    def test_example_config_sets_both_explicitly(self):
        cfg = cli.load_config(ROOT / "config.example.toml")
        self.assertEqual((cfg["database"]["user"], cfg["database"]["dbname"]), ("swarm", "swarm_board"))
        self.assertEqual(cli.implicit_legacy_database_keys(
            __import__("tomllib").loads((ROOT / "config.example.toml").read_text())), [])

    def test_doctor_warns_when_dbname_is_implicit(self):
        path, cfg = self.load('[database]\nhost = "db"\n')
        (c,) = bootstrap._legacy_database_check(path, cfg)
        self.assertIsNone(c.ok)   # WARN
        self.assertIn("dbname", c.detail)
        self.assertIn("agent-message-board", c.detail)
        self.assertIn("set user and dbname explicitly", c.fix)
        self.assertIn("WARN", bootstrap.format_checks([c]))

    def test_doctor_is_quiet_when_explicit_or_not_postgres(self):
        path, cfg = self.load('[database]\nuser = "u"\ndbname = "x"\n')
        self.assertEqual(bootstrap._legacy_database_check(path, cfg), [])
        path, cfg = self.load('[board]\nbackend = "sqlite"\n[database]\nhost = "db"\n')
        self.assertEqual(bootstrap._legacy_database_check(path, cfg), [])
        path, cfg = self.load(None)
        self.assertEqual(bootstrap._legacy_database_check(path, cfg), [])


if __name__ == "__main__":
    unittest.main()
