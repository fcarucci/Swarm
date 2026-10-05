"""A board created before memory writers were configurable has CHECK (writer IN (...)) on
memory_refs.writer; setup drops it (idempotently) so a configured writer's name is accepted."""
import lzma
import sqlite3
import unittest

from support import SMALL_POOL, SqliteHarness  # noqa: F401  (sets sys.path)
from swarm.board import MemoryRef, setup_board
from swarm.board.base import SCHEMA_VERSION

OLD_WRITERS = "'swarm-remember', 'legacy-a', 'legacy-b'"


def ref(doc, writer):
    return MemoryRef(document_id=doc, bank="notes", job="j", agent_key="k", agent_name="Homer Simpson",
                     harness="claude", host="h", session_id="s", tool_call_id="t", writer=writer,
                     excerpt=lzma.compress(b"{}\n"), raw_bytes=3, redactions=0)


class WriterCheckMigrationTests(unittest.TestCase):
    def setUp(self):
        self.h = SqliteHarness()
        self.addCleanup(self.h.close)
        self.h.reset()

    def make_old_board(self):
        c = self.h._db()
        sql = c.execute("SELECT sql FROM sqlite_master WHERE name = 'memory_refs'").fetchone()[0]
        old = sql.replace("writer       TEXT NOT NULL,", f"writer       TEXT NOT NULL CHECK (writer IN ({OLD_WRITERS})),")
        self.assertNotEqual(old, sql)
        c.execute("DROP TABLE memory_refs")
        c.execute(old)
        c.execute("INSERT INTO memory_refs (document_id, bank, job, agent_key, agent_name, writer, created_at) "
                  "VALUES ('old-1', 'notes', 'j', 'k', 'Lisa', 'legacy-a', '2026-01-01T00:00:00.000000+00:00')")
        with self.assertRaises(sqlite3.IntegrityError):
            c.execute("INSERT INTO memory_refs (document_id, bank, job, agent_key, agent_name, writer, created_at) "
                      "VALUES ('x', 'b', 'j', 'k', 'Lisa', 'note-tool', '2026-01-01T00:00:00.000000+00:00')")

    def test_setup_drops_the_old_check_and_keeps_rows(self):
        self.make_old_board()
        for outcome in ("inserted", "updated"):   # setup twice: idempotent
            setup_board(self.h.cfg, SMALL_POOL)
            with self.h.board() as b:
                self.assertEqual(b.save_memory_ref(ref("new-1", "note-tool")), outcome)
                self.assertEqual(sorted((r.document_id, r.writer) for r in b.memory_refs()),
                                 [("new-1", "note-tool"), ("old-1", "legacy-a")])
        c = self.h._db()
        self.assertNotIn("CHECK (writer", c.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'memory_refs'").fetchone()[0])
        self.assertTrue(c.execute("SELECT 1 FROM sqlite_master WHERE name = 'check_memory_refs_name_ins'").fetchone())
        self.assertTrue(c.execute("SELECT 1 FROM sqlite_master WHERE name = 'memory_refs_job'").fetchone())
        self.assertIsNone(c.execute("SELECT 1 FROM sqlite_master WHERE name = 'memory_refs_new'").fetchone())

    def test_a_schema_9_board_gets_both_the_check_drop_and_verdict_next_idempotently(self):
        self.make_old_board()
        c = self.h._db()
        for column in ("verdict_next", "max_hours", "waiting_until"):   # as a schema-9 board has it
            c.execute(f"ALTER TABLE jobs DROP COLUMN {column}")
        c.execute("PRAGMA user_version = 9")
        for _ in range(2):   # setup twice: idempotent
            setup_board(self.h.cfg, SMALL_POOL)
        c = self.h._db()
        self.assertEqual(c.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
        self.assertEqual(SCHEMA_VERSION, 14)
        for column in ("verdict_next", "max_hours", "waiting_until"):
            self.assertIn(column, [r[1] for r in c.execute("PRAGMA table_info(jobs)")])
        self.assertNotIn("CHECK (writer", c.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'memory_refs'").fetchone()[0])
        with self.h.board() as b:
            self.assertEqual(b.save_memory_ref(ref("new-2", "note-tool")), "inserted")
            self.assertEqual([r.document_id for r in b.memory_refs()].count("old-1"), 1)

    def test_a_bad_writer_name_is_still_refused(self):
        setup_board(self.h.cfg, SMALL_POOL)
        with self.h.board() as b, self.assertRaises(ValueError):
            b.save_memory_ref(ref("d", "bad name!"))


if __name__ == "__main__":
    unittest.main()
