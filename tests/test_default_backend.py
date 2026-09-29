"""The board backend defaults to "file"; an old config with a [database] section and no
[board] backend stays on "postgres" (doctor says to set it); an explicit backend always wins;
Hindsight memory stays off without a url."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from support import ROOT  # noqa: F401  (sets sys.path)

from swarm import bootstrap, hindsight, hooks  # noqa: E402
from swarm import cli as swarm  # noqa: E402
from swarm.board import board_backend, backend_class  # noqa: E402


class DefaultBackendTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="swarm-defb-")
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.config = self.home / "config.toml"

    def load(self, text: str | None) -> dict:
        if text is not None:
            self.config.write_text(text)
        return swarm.load_config(self.config)

    def test_no_config_is_the_file_board(self):
        cfg = self.load(None)
        self.assertEqual(board_backend(cfg), "file")
        self.assertEqual(backend_class(cfg).__name__, "FileBoard")
        self.assertEqual(board_backend({}), "file")
        self.assertNotIn("backend_implied", cfg["board"])

    def test_empty_or_board_only_config_is_file(self):
        self.assertEqual(board_backend(self.load("")), "file")
        self.assertEqual(board_backend(self.load("[board]\nretention_days = 3\n")), "file")

    def test_database_section_without_backend_stays_postgres(self):
        for db in ('host = "db.internal"', 'dbname = "mine"'):
            cfg = self.load(f"[database]\n{db}\n")
            self.assertEqual(board_backend(cfg), "postgres", db)
            self.assertEqual(backend_class(cfg).__name__, "PostgresBoard")
            self.assertTrue(cfg["board"]["backend_implied"])

    def test_any_database_setting_without_backend_stays_postgres(self):
        cfg = self.load('[database]\nuser = "me"\npassword_env_file = "~/.config/swarm/pg.env"\n')
        self.assertEqual(board_backend(cfg), "postgres")
        self.assertTrue(cfg["board"]["backend_implied"])

    def test_empty_database_section_is_still_file(self):
        self.assertEqual(board_backend(self.load("[database]\n")), "file")

    def test_explicit_backend_wins(self):
        for backend in ("file", "sqlite", "memory", "postgres"):
            cfg = self.load(f'[database]\nhost = "db.internal"\n[board]\nbackend = "{backend}"\n')
            self.assertEqual(board_backend(cfg), backend)
            self.assertNotIn("backend_implied", cfg["board"])
        self.assertEqual(board_backend(self.load('[board]\nbackend = "sqlite"\n')), "sqlite")

    def test_the_config_bootstrap_writes_is_file_and_memory_is_off(self):
        import tomllib
        bootstrap.ensure_config(self.config)
        raw = tomllib.loads(self.config.read_text())
        self.assertEqual(raw["board"]["backend"], "file")
        self.assertFalse((raw.get("hindsight") or {}).get("url"))
        cfg = self.load(None)
        self.assertEqual(board_backend(cfg), "file")
        self.assertFalse(hindsight.enabled(cfg))
        self.assertFalse(hooks._memory_on(cfg))

    def test_memory_is_off_by_default(self):
        cfg = swarm.load_config(self.home / "missing.toml")
        self.assertFalse(hindsight.enabled(cfg))
        self.assertFalse(hooks._memory_on(cfg))


class DoctorNoteTests(unittest.TestCase):
    def setUp(self):
        import support
        tmp = tempfile.TemporaryDirectory(prefix="swarm-defb-")
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        p = mock.patch.dict(os.environ, {"HOME": str(self.home), "SWARM_VENV": str(support.temp_venv()),
                                         "SWARM_AUTO_INIT": "1"})
        p.start(); self.addCleanup(p.stop)
        self.config = self.home / ".config/swarm/config.toml"
        self.config.parent.mkdir(parents=True)

    def board_note(self, text: str):
        self.config.write_text(text)
        return {c.name: c for c in bootstrap.doctor("claude", config=self.config)}.get("board backend")

    def test_database_only_config_gets_a_note(self):
        note = self.board_note('[database]\nhost = "db.invalid"\nconnect_timeout = 1\n')
        self.assertIsNotNone(note)
        self.assertIsNone(note.ok)                       # a warning, not a failure
        self.assertIn('backend = "postgres"', note.fix)
        self.assertIn("postgres", note.detail)

    def test_no_config_is_fine_and_checks_the_file_board(self):
        checks = {c.name: c for c in bootstrap.doctor("claude", config=self.config)}
        self.assertIs(checks["config"].ok, True)
        self.assertIn("file board", checks["config"].detail)
        self.assertIn("board", checks)
        self.assertNotIn("board backend", checks)
        self.assertFalse(self.config.exists())

    def test_explicit_backend_or_no_database_gets_no_note(self):
        self.assertIsNone(self.board_note('[database]\nhost = "db.invalid"\n[board]\nbackend = "postgres"\n'))
        self.assertIsNone(self.board_note('[board]\nbackend = "file"\n'))
        self.assertIsNone(self.board_note(""))


if __name__ == "__main__":
    unittest.main()
