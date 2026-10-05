"""Pause/resume at the board layer (schema 12): the paused status, the join/post block, the
manifest, begin_resume, retention, and the SQLite rebuild of an older jobs table. A contract mixin:
runs on memory, file and sqlite (and Postgres with $SWARM_TEST_CONFIG, like the board contract)."""
from __future__ import annotations

import datetime as dt
import os
import sqlite3
import unittest

from support import FileHarness, MemoryHarness, PostgresHarness, SqliteHarness  # noqa: F401  (sets sys.path)

from swarm.board import (LEFT_PAUSED, PAUSE_WRITER, JobPaused, SCHEMA_VERSION, TranscriptRow,  # noqa: E402
                         open_board, setup_board)


class PauseContract:
    harness_factory = None

    @classmethod
    def setUpClass(cls):
        cls.h = cls.harness_factory()

    @classmethod
    def tearDownClass(cls):
        cls.h.close()

    def setUp(self):
        self.h.reset()
        self.b = self.h.board()
        self.addCleanup(self.b.close)
        self.b.open_job("j", "desc", "do the thing", None, "tester", goal="ship it")

    def team(self):
        a = self.b.allocate_name("k1", "j", "engineer")
        c = self.b.allocate_name("k2", "j", "qa")
        self.b.set_agent_runtime("k1", "claude", "sonnet")
        self.b.record_route("k1", "sess-1", "final", "j")
        self.b.post("j", a, "one")
        self.b.post("j", a, "two")
        self.b.read_new(agent_key="k2")   # k2 reads up to 2
        self.b.tool_started("k1", "Bash")
        return a, c

    def test_schema_version(self):
        self.assertEqual(SCHEMA_VERSION, 16)
