"""Schema 22 upgrade path (Postgres, SQLite): a schema-21 board with posts, including raw INSERTs
no client wrote, gets agents.message_count backfilled once by setup, and keeps it exact after."""
import os
import unittest

import support  # noqa: F401 (sets sys.path)
from support import PostgresHarness, SqliteHarness
from swarm.board import SCHEMA_VERSION, setup_board

POOL = {"simpsons": ["Homer Simpson", "Lisa Simpson"], "english": []}


class UpgradeCounts:
    def setUp(self):
        self.h = self.harness_factory()
        self.addCleanup(self.h.close)
        self.h.reset()
        self.downgrade()

    def counts(self):
        with self.h.board() as b:
            return {a.agent_key: a.messages for a in b.agents("j")}

    def test_backfill_then_idempotent_then_raw_inserts_count(self):
        setup_board(self.h.cfg, POOL)
        with self.h.board() as b:
            self.assertEqual(type(b).schema_version(self.h.cfg), SCHEMA_VERSION)
        self.assertEqual(self.counts(), {"k": 2, "k2": 1})   # the pre-join message is not counted
        setup_board(self.h.cfg, POOL)                        # again: nothing changes
        self.assertEqual(self.counts(), {"k": 2, "k2": 1})
        self.raw_insert("Homer")                             # a raw INSERT after the upgrade
        self.assertEqual(self.counts(), {"k": 3, "k2": 1})


class SqliteUpgradeCounts(UpgradeCounts, unittest.TestCase):
    harness_factory = SqliteHarness
    NOW = "2026-06-01T00:00:00.000000+00:00"
    OLD = "2026-05-01T00:00:00.000000+00:00"

    def downgrade(self):
        c = self.h._db()
        for t in ("messages_count", "messages_uncount", "agents_recount_messages"):
            c.execute(f"DROP TRIGGER IF EXISTS {t}")
        c.execute("ALTER TABLE agents DROP COLUMN message_count")
        c.execute("PRAGMA user_version = 21")
        c.execute("INSERT INTO jobs (job, created_at) VALUES ('j', ?)", (self.OLD,))
        for key, name in (("k", "Homer"), ("k2", "Lisa")):
            c.execute("INSERT INTO agents (agent_key, name, job, joined_at, last_seen) VALUES (?, ?, 'j', ?, ?)",
                      (key, name, self.NOW, self.NOW))
        for name, at in (("Homer", self.OLD), ("Homer", self.NOW), ("Homer", self.NOW), ("Lisa", self.NOW)):
            c.execute("INSERT INTO messages (job, agent_name, created_at, message) VALUES ('j', ?, ?, 'x')",
                      (name, at))

    def raw_insert(self, name):
        self.h._db().execute("INSERT INTO messages (job, agent_name, created_at, message) VALUES ('j', ?, ?, 'x')",
                             (name, "2026-06-02T00:00:00.000000+00:00"))


@unittest.skipUnless(os.environ.get("SWARM_TEST_CONFIG"), "needs throwaway PostgreSQL")
class PostgresUpgradeCounts(UpgradeCounts, unittest.TestCase):
    harness_factory = staticmethod(lambda: PostgresHarness(os.environ["SWARM_TEST_CONFIG"]))

    def setUp(self):
        super().setUp()
        self.addCleanup(self.h.reset)

    def downgrade(self):
        c = self.h.conn
        c.execute("DROP VIEW IF EXISTS job_status")
        c.execute("DROP VIEW IF EXISTS agent_status")
        c.execute("DROP TRIGGER IF EXISTS messages_count ON messages")
        c.execute("DROP TRIGGER IF EXISTS messages_uncount ON messages")
        c.execute("DROP TRIGGER IF EXISTS agents_recount_messages ON agents")
        c.execute("ALTER TABLE agents DROP COLUMN message_count")
        c.execute("UPDATE board_meta SET value = '21' WHERE key = 'schema_version'")
        c.execute("INSERT INTO jobs (job) VALUES ('j')")
        for key, name in (("k", "Homer"), ("k2", "Lisa")):
            c.execute("INSERT INTO agents (agent_key, name, job) VALUES (%s, %s, 'j')", (key, name))
        c.execute("INSERT INTO messages (job, agent_name, message, created_at) "
                  "VALUES ('j', 'Homer', 'x', now() - interval '1 day')")   # before it joined
        for name in ("Homer", "Homer", "Lisa"):
            c.execute("INSERT INTO messages (job, agent_name, message) VALUES ('j', %s, 'x')", (name,))

    def raw_insert(self, name):
        self.h.conn.execute("INSERT INTO messages (job, agent_name, message) VALUES ('j', %s, 'x')", (name,))


if __name__ == "__main__":
    unittest.main()
