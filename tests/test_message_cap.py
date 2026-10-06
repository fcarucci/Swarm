"""The board's message cap (schema 15): one authoritative value stored in the board, set from
[board] message_max_chars at init, changed online with `swarm config board.message_max_chars N`.
The contract runs on every backend (Postgres with SWARM_TEST_CONFIG); the CLI and hook tests run on
SWARM_TEST_BACKEND; the upgrade tests start from boards as schema 12-14 left them."""
from __future__ import annotations

import os
import sqlite3
import unittest
from pathlib import Path

from support import (FileHarness, MemoryHarness, PostgresHarness, SqliteHarness, SMALL_POOL,  # noqa: F401
                     setup_board, tq)
from test_hooks_cli import Env, swarm_names

from swarm import cli as swarm
from swarm.board import (MESSAGE_CAP_MAX, MESSAGE_CAP_MIN, SCHEMA_VERSION, check_message_cap, open_board)
from swarm.board.base import CapExceeded


class CapContract:
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

    def texts(self, job="j"):
        return [m.message for m in self.b.recent_messages(50, job)]

    def test_new_board_starts_with_the_configured_cap(self):
        self.assertEqual(self.b.message_cap(), 200)   # the harness config's value
        self.assertEqual(self.b.post("j", "A", "x" * 200).truncated, False)
        self.assertEqual(self.b.post("j", "A", "x" * 201).truncated, True)

    def test_widen_after_init_posts_up_to_the_new_cap(self):
        self.assertEqual(self.b.set_message_cap(500), (200, 500))
        self.assertEqual(self.b.message_cap(), 500)
        r = self.b.post("j", "A", "y" * 500)
        self.assertFalse(r.truncated)
        r = self.b.post("j", "A", "z" * 600)
        self.assertTrue(r.truncated)
        self.assertEqual([len(t) for t in self.texts()], [500, 500])
        self.assertTrue(self.texts()[-1].endswith("…"))

    def test_shrink_never_touches_existing_messages(self):
        self.b.set_message_cap(1000)
        self.b.post("j", "A", "w" * 900)
        self.assertEqual(self.b.set_message_cap(100), (1000, 100))
        self.assertEqual(self.texts(), ["w" * 900])          # kept whole, still readable
        self.assertEqual(self.b.message_cap(), 100)
        self.b.post("j", "A", "n" * 150)
        self.assertEqual([len(t) for t in self.texts()], [900, 100])   # new posts obey the new cap

    def test_invalid_values_are_refused_and_change_nothing(self):
        for bad in (MESSAGE_CAP_MIN - 1, MESSAGE_CAP_MAX + 1, 0, -5, "abc", "", "12.5", 1.5, None, True, "1e3"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.b.set_message_cap(bad)
        self.assertEqual(self.b.message_cap(), 200)
        self.assertEqual(self.b.set_message_cap(MESSAGE_CAP_MIN), (200, MESSAGE_CAP_MIN))
        self.assertEqual(self.b.set_message_cap(str(MESSAGE_CAP_MAX)), (MESSAGE_CAP_MIN, MESSAGE_CAP_MAX))

    def test_two_clients_with_different_configs_agree_on_the_board_cap(self):
        with self.h.board(message_max_chars=1000) as big, self.h.board(message_max_chars=60) as small:
            self.assertEqual((big.message_cap(), small.message_cap()), (200, 200))
            self.assertTrue(big.post("j", "A", "b" * 300).truncated)
            self.assertFalse(small.post("j", "B", "s" * 200).truncated)
            small.set_message_cap(300)
            self.assertEqual(big.message_cap(), 300)      # the other client sees it at once
            self.assertFalse(big.post("j", "A", "b" * 300).truncated)

    def test_a_poster_holding_a_stale_cap_is_cut_to_the_new_one(self):
        with self.h.board() as other:
            other.set_message_cap(80)
        real = self.b._read_message_cap
        calls = []

        def stale():                                       # the first read, before the change: the old cap
            calls.append(1)
            return 200 if len(calls) == 1 else real()
        self.b._read_message_cap = stale
        try:
            r = self.b.post("j", "A", "q" * 150)
        finally:
            self.b._read_message_cap = real
        self.assertTrue(r.truncated)
        self.assertEqual(len(self.texts()[-1]), 80)

    def test_the_store_itself_refuses_a_longer_message(self):
        with self.assertRaises(CapExceeded) as cm:
            self.b._insert_message("j", "A", "x" * 201, None, None)
        self.assertEqual(cm.exception.cap, 200)

    def test_setup_again_never_resets_the_cap(self):
        self.b.set_message_cap(321)
        setup_board(self.h.cfg, SMALL_POOL)
        with self.h.board(message_max_chars=77) as b:
            self.assertEqual(b.message_cap(), 321)

    def test_set_to_the_same_value_is_a_noop(self):
        self.assertEqual(self.b.set_message_cap(200), (200, 200))
        self.assertEqual(self.b.message_cap(), 200)


class MemoryCap(CapContract, unittest.TestCase):
    harness_factory = MemoryHarness


class FileCap(CapContract, unittest.TestCase):
    harness_factory = FileHarness


class SqliteCap(CapContract, unittest.TestCase):
    harness_factory = SqliteHarness


@unittest.skipUnless(os.environ.get("SWARM_TEST_CONFIG"), "set SWARM_TEST_CONFIG to a throwaway Postgres")
class PostgresCap(CapContract, unittest.TestCase):
    harness_factory = staticmethod(lambda: PostgresHarness(os.environ["SWARM_TEST_CONFIG"]))

    def test_column_is_text_and_the_constraint_is_not_valid(self):
        c = self.h.conn
        self.assertEqual(c.execute("SELECT format_type(atttypid, atttypmod) FROM pg_attribute WHERE "
                                   "attrelid = 'messages'::regclass AND attname = 'message'").fetchone()[0], "text")
        self.b.set_message_cap(300)
        row = c.execute("SELECT convalidated, pg_get_constraintdef(oid) FROM pg_constraint "
                        "WHERE conname = 'messages_message_cap'").fetchone()
        self.assertFalse(row[0])
        self.assertIn("300", row[1])

    def test_set_cap_does_not_wait_on_a_held_table_lock_forever(self):
        import psycopg
        from swarm.board import postgres
        c = self.h.conn
        with psycopg.connect(**{k: v for k, v in c.info.get_parameters().items() if k in
                                ("host", "port", "user", "dbname")},
                             password=self.h._password(self.h.cfg["database"]), autocommit=False) as blocker:
            blocker.execute("LOCK TABLE messages IN ACCESS SHARE MODE")   # an open reader
            old = (postgres._LOCK_TRIES, postgres._LOCK_TIMEOUT)
            postgres._LOCK_TRIES, postgres._LOCK_TIMEOUT = 2, "100ms"
            try:
                with self.assertRaises(psycopg.errors.LockNotAvailable):
                    self.b.set_message_cap(250)
            finally:
                postgres._LOCK_TRIES, postgres._LOCK_TIMEOUT = old
            self.assertEqual(self.b.message_cap(), 200)   # the failed change left nothing half-done
            blocker.rollback()
        self.assertEqual(self.b.set_message_cap(250), (200, 250))


class PureTests(unittest.TestCase):
    def test_schema_setup_leaves_the_outer_retry_in_charge_of_the_lock_budget(self):
        from contextlib import nullcontext
        from unittest.mock import Mock, patch
        from swarm.board import postgres

        conn = Mock(query_timeout=8.0)
        conn.transaction.side_effect = lambda: nullcontext()
        attempts = []

        def execute(statement, params=None):
            if statement.startswith("SELECT 1 FROM board_meta"):
                return Mock(fetchone=lambda: None)
            if statement.startswith("ALTER TABLE messages"):
                attempts.append(statement)
                raise postgres.psycopg.errors.LockNotAvailable("held table lock")
            return Mock()

        conn.execute.side_effect = execute
        # Keep both real retry loops, replacing only database I/O and sleeps.
        with patch.object(postgres, "_message_width", return_value=200), \
                patch.object(postgres.time, "sleep"), \
                patch.object(postgres.sys, "stderr"), \
                self.assertRaises(postgres.psycopg.errors.LockNotAvailable):
            postgres._install_schema_retrying(conn, {"message_max_chars": 200})
        self.assertEqual(len(attempts), 15)  # five setup attempts, three lock attempts each

    def test_check_message_cap(self):
        self.assertEqual(check_message_cap(" 500 "), 500)
        self.assertEqual(check_message_cap(500.0), 500)
        self.assertEqual(SCHEMA_VERSION, 18)


class CliTests(Env):
    def cap(self):
        with self.board() as b:
            return b.message_cap()

    def test_get_prints_the_live_cap(self):
        rc, out, _ = self.cli("config", "board.message_max_chars")
        self.assertEqual((rc, out), (0, "200\n"))

    def test_set_then_get_and_post_truncates_at_the_new_cap(self):
        self.cli("init")
        rc, out, _ = self.cli("config", "board.message_max_chars", "500")
        self.assertEqual(rc, 0)
        self.assertIn("200 -> 500", out)
        self.assertEqual(self.cli("config", "board.message_max_chars")[1], "500\n")
        a = self.cli("join", "--job", "J", "--key", "ka")[1].strip()
        self.assertEqual(self.cli("post", "--job", "J", "--as", a, "x" * 400)[1].count("truncated"), 0)
        _, out, _ = self.cli("post", "--job", "J", "--as", a, "x" * 600)
        self.assertRegex(out, r"^posted #\d+ \(truncated to 500 chars\)\n$")

    def test_role_addressed_posts_report_the_live_board_cap(self):
        self.cli("init")
        author = self.peer()
        judge = self.cli("join", "--job", "J", "--key", "judge", "--judge")[1].strip()
        self.cli("config", "board.message_max_chars", "500")
        rc, out, err = self.cli("post", "--job", "J", "--as", author, "--to", "@judge", "x" * 600)
        self.assertEqual(rc, 0, err)
        self.assertIn(f"to @judge ({judge})", out)
        self.assertIn("truncated to 500 chars", out)
        with self.board() as b:
            message = b.recent_messages(1, "J")[0]
            self.assertEqual((message.to_agent, len(message.message)), (judge, 500))

    def test_a_client_with_another_config_value_still_sees_the_board_cap(self):
        self.cli("init")
        self.cli("config", "board.message_max_chars", "300")
        self.config.write_text(self.config.read_text() + "")
        text = self.config.read_text().replace("[board]\n", "[board]\nmessage_max_chars = 1000\n", 1)
        self.config.write_text(text)
        rc, out, err = self.cli("config", "board.message_max_chars")
        self.assertEqual((rc, out), (0, "300\n"))
        self.assertIn("config file says 1000", err)

    def test_invalid_values_exit_2_and_change_nothing(self):
        self.cli("init")
        for bad in ("10", "99999", "abc", "-1"):
            rc, _, err = self.cli("config", "board.message_max_chars", bad)
            self.assertEqual(rc, 2, bad)
            self.assertIn("between 50 and 4000", err if bad != "abc" else "between 50 and 4000")
        self.assertEqual(self.cap(), 200)
        self.assertEqual(self.cli("config", "nope.key")[0], 2)

    def test_shrink_says_existing_messages_are_kept(self):
        self.cli("init")
        self.cli("config", "board.message_max_chars", "800")
        a = self.cli("join", "--job", "J", "--key", "ka")[1].strip()
        self.cli("post", "--job", "J", "--as", a, "k" * 700)
        rc, out, _ = self.cli("config", "board.message_max_chars", "100")
        self.assertIn("kept as they are", out)
        with self.board() as b:
            self.assertEqual(len(b.recent_messages(5, "J")[0].message), 700)

    def test_save_writes_the_config_file_and_keeps_the_rest(self):
        self.cli("init")
        before = self.config.read_text()
        rc, out, _ = self.cli("config", "board.message_max_chars", "450", "--save")
        self.assertEqual(rc, 0, out)
        cfg = swarm.load_config(self.config)
        self.assertEqual(cfg["board"]["message_max_chars"], 450)
        self.assertEqual(cfg["board"]["backend"], self.h.name)
        self.assertIn("backend", before)
        self.cli("config", "board.message_max_chars", "460", "--save")   # replaced in place, no duplicate
        self.assertEqual(self.config.read_text().count("message_max_chars"), 1)

    def test_save_keeps_a_trailing_comment_and_adds_a_missing_section(self):
        p = self.tmp / "c.toml"
        p.write_text('[board]\nmessage_max_chars = 200   # per message\nbackend = "x"\n')
        swarm._save_config_value(p, "board", "message_max_chars", 300)
        self.assertEqual(p.read_text(), '[board]\nmessage_max_chars = 300   # per message\nbackend = "x"\n')
        q = self.tmp / "d.toml"
        swarm._save_config_value(q, "board", "message_max_chars", 90)
        self.assertEqual(swarm.load_config(q)["board"]["message_max_chars"], 90)
        r = self.tmp / "e.toml"
        r.write_text("[database]\nhost = 'h'\n")
        swarm._save_config_value(r, "board", "message_max_chars", 90)
        self.assertEqual(swarm.load_config(r)["board"]["message_max_chars"], 90)
        self.assertEqual(swarm.load_config(r)["database"]["host"], "h")

    def test_the_start_hook_shows_the_live_cap(self):
        self.cli("init")
        self.cli("config", "board.message_max_chars", "750")
        from swarm import hooks
        from swarm.board import open_board
        with open_board(self.cfg) as b:
            for text in (hooks._instructions("Homer Simpson", "J", self.cfg, cap=b.message_cap()),
                         hooks._verifier_instructions("Homer Simpson", "J", self.cfg, cap=b.message_cap()),
                         hooks._judge_instructions("Homer Simpson", "J", "g", self.cfg, b.message_cap())):
                self.assertIn("Max 750 characters", text)
                self.assertNotIn("Max 200", text)


class StartHookTests(Env):
    def test_start_hook_text_follows_the_board(self):
        import json
        marker = self.markers
        self.cli("init")
        self.cli("config", "board.message_max_chars", "640")
        rc, _, _ = self.cli("activate", "--job", "J")
        self.assertEqual(rc, 0)
        out = self.hook("start", agent_type="Explore")
        self.assertIn("Max 640 characters", self.context(out))


# ---- upgrades from schema 12-14 -------------------------------------------------------------

class SqliteUpgrade(unittest.TestCase):
    def setUp(self):
        self.h = SqliteHarness()
        self.addCleanup(self.h.close)

    def old_board(self, version, cap=200):
        """A schema-<version> board as the previous release made it: the cap is a table CHECK."""
        from swarm.board import sqlite as sb
        old = sb.SCHEMA.replace("CHECK (length(message) >= 1)", f"CHECK (length(message) BETWEEN 1 AND {cap})")
        old = old.split("-- Per-board settings")[0] + old[old.index("-- Change counters"):]   # no board_meta
        db = sqlite3.connect(self.h.path, isolation_level=None)
        db.executescript(old)
        db.execute("DROP TABLE IF EXISTS board_meta")
        db.execute("DROP TRIGGER IF EXISTS check_messages_cap")
        now = "2026-01-01T00:00:00.000000+00:00"
        db.execute("INSERT INTO jobs (job, created_at) VALUES ('j', ?)", (now,))
        for i in range(3):
            db.execute("INSERT INTO messages (job, agent_name, created_at, message) VALUES ('j', 'A', ?, ?)",
                       (now, f"old {i}"))
        db.execute("DELETE FROM messages WHERE id = 3")           # the AUTOINCREMENT counter is ahead of max(id)
        db.execute(f"PRAGMA user_version = {version}")
        db.close()

    def test_schema_15_to_18_keeps_the_cap_and_adds_plugin_data(self):
        self.h.reset()
        with self.h.board() as b:
            b.ensure_job("j")
            b.set_message_cap(777)
            b.post("j", "A", "before schema 16")
        with sqlite3.connect(self.h.path) as c:
            c.execute("ALTER TABLE jobs DROP COLUMN plugin_data")
            c.execute("PRAGMA user_version = 15")
        setup_board(self.h.cfg, SMALL_POOL)
        self.assertEqual(self.h.sqlite_board.SqliteBoard.schema_version(self.h.cfg), 18)
        with self.h.board() as b:
            self.assertEqual(b.message_cap(), 777)
            self.assertEqual([m.message for m in b.recent_messages(10, "j")], ["before schema 16"])
            self.assertEqual(b.job_data("j"), {})
            self.assertTrue(b.set_job_data("j", "engineering-team.optional", "build_engineer"))
        setup_board(self.h.cfg, SMALL_POOL)
        with self.h.board() as b:
            self.assertEqual(b.message_cap(), 777)
            self.assertEqual(b.job_data("j"), {"engineering-team.optional": "build_engineer"})

    def test_upgrade_keeps_rows_ids_and_the_cap_the_board_enforced(self):
        for version in (12, 13, 14):
            with self.subTest(version=version):
                self.h.path.unlink(missing_ok=True)
                self.old_board(version, cap=333)
                self.h.cfg["board"]["message_max_chars"] = 200   # the config differs: the board's N wins
                setup_board(self.h.cfg, SMALL_POOL)
                with self.h.board() as b:
                    self.assertEqual(b.message_cap(), 333)
                    self.assertEqual([m.message for m in b.recent_messages(10, "j")], ["old 0", "old 1"])
                    self.assertEqual(b.post("j", "A", "new").id, 4)       # never reuses id 3
                    self.assertFalse(b.post("j", "A", "z" * 333).truncated)
                    self.assertEqual(b.set_message_cap(2000), (333, 2000))
                    self.assertFalse(b.post("j", "A", "z" * 1500).truncated)
                self.assertEqual(self.h.sqlite_board.SqliteBoard.schema_version(self.h.cfg),
                                 SCHEMA_VERSION)
                setup_board(self.h.cfg, SMALL_POOL)                       # idempotent, cap not reset
                with self.h.board() as b:
                    self.assertEqual(b.message_cap(), 2000)


class MemoryFileUpgrade(unittest.TestCase):
    def test_a_board_without_a_stored_cap_falls_back_to_config_then_setup_stores_it(self):
        h = MemoryHarness()
        h.reset()
        with h.store.lock:
            h.store.message_max_chars = None
            h.store.schema_version = 14
        with h.board(message_max_chars=123) as b:
            self.assertEqual(b.message_cap(), 123)
        with h.board(message_max_chars=7) as b:                            # invalid config: the default
            self.assertEqual(b.message_cap(), 200)
        setup_board({**h.cfg, "board": {**h.cfg["board"], "message_max_chars": 123}}, SMALL_POOL)
        with h.board(message_max_chars=999) as b:
            self.assertEqual(b.message_cap(), 123)

    def test_file_board_persists_the_cap(self):
        h = FileHarness()
        self.addCleanup(h.close)
        h.reset()
        with h.board() as b:
            b.set_message_cap(777)
        with h.board() as b:
            self.assertEqual(b.message_cap(), 777)


@unittest.skipUnless(os.environ.get("SWARM_TEST_CONFIG"), "set SWARM_TEST_CONFIG to a throwaway Postgres")
class PostgresUpgrade(unittest.TestCase):
    def setUp(self):
        self.h = PostgresHarness(os.environ["SWARM_TEST_CONFIG"])
        self.addCleanup(self.h.close)
        self.addCleanup(self.h.reset)

    def old_board(self, version, width=200):
        c = self.h.conn
        c.execute("TRUNCATE messages, agents, jobs CASCADE")
        c.execute("DELETE FROM board_meta WHERE key = 'message_max_chars'")
        c.execute("ALTER TABLE messages DROP CONSTRAINT IF EXISTS messages_message_cap")
        c.execute("ALTER TABLE messages DROP CONSTRAINT IF EXISTS messages_message_nonempty")
        c.execute(f"ALTER TABLE messages ALTER COLUMN message TYPE varchar({width})")
        c.execute("ALTER TABLE messages ADD CONSTRAINT messages_message_check CHECK (length(message) > 0)")
        c.execute("UPDATE board_meta SET value = %s WHERE key = 'schema_version'", (str(version),))
        c.execute("TRUNCATE messages, agents, jobs CASCADE")
        c.execute("INSERT INTO jobs (job) VALUES ('j')")
        c.execute("INSERT INTO messages (job, agent_name, message) VALUES ('j', 'A', 'old one'), ('j', 'A', 'old two')")

    def test_schema_15_to_18_keeps_the_cap_and_adds_plugin_data(self):
        with self.h.board() as b:
            b.ensure_job("j")
            b.set_message_cap(777)
            b.post("j", "A", "before schema 16")
        c = self.h.conn
        c.execute("ALTER TABLE jobs DROP COLUMN plugin_data")
        c.execute("UPDATE board_meta SET value = '15' WHERE key = 'schema_version'")
        setup_board(self.h.cfg, SMALL_POOL)
        self.assertEqual(c.execute("SELECT value FROM board_meta WHERE key = 'schema_version'").fetchone()[0], "18")
        with self.h.board() as b:
            self.assertEqual(b.message_cap(), 777)
            self.assertEqual([m.message for m in b.recent_messages(10, "j")], ["before schema 16"])
            self.assertEqual(b.job_data("j"), {})
            self.assertTrue(b.set_job_data("j", "engineering-team.optional", "build_engineer"))
        setup_board(self.h.cfg, SMALL_POOL)
        with self.h.board() as b:
            self.assertEqual(b.message_cap(), 777)
            self.assertEqual(b.job_data("j"), {"engineering-team.optional": "build_engineer"})

    def test_upgrade_from_12_13_14_is_online_and_keeps_every_message(self):
        for version in (12, 13, 14):
            with self.subTest(version=version):
                self.old_board(version, width=250)
                self.assertEqual(self.h.conn.execute("SELECT count(*) FROM pg_views WHERE schemaname = 'public' "
                                                     "AND viewname IN ('agent_status', 'job_status')").fetchone()[0], 2)
                self.h.cfg["board"]["message_max_chars"] = 200
                setup_board(self.h.cfg, SMALL_POOL)
                with self.h.board() as b:
                    self.assertEqual(b.message_cap(), 250)               # the old varchar width
                    self.assertEqual([m.message for m in b.recent_messages(10, "j")], ["old one", "old two"])
                    self.assertFalse(b.post("j", "A", "z" * 250).truncated)
                    self.assertTrue(b.post("j", "A", "z" * 251).truncated)
                    b.set_message_cap(900)
                    self.assertFalse(b.post("j", "A", "z" * 900).truncated)
                self.assertEqual(self.h.conn.execute("SELECT format_type(atttypid, atttypmod) FROM pg_attribute "
                                                     "WHERE attrelid = 'messages'::regclass AND attname = 'message'"
                                                     ).fetchone()[0], "text")
                setup_board(self.h.cfg, SMALL_POOL)                      # idempotent: cap kept
                with self.h.board() as b:
                    self.assertEqual(b.message_cap(), 900)

    def test_a_dml_stream_is_not_blocked_while_the_cap_changes(self):
        import threading
        stop, first_post, errors, posted = threading.Event(), threading.Event(), [], []

        def poster():
            try:
                with self.h.board() as b:
                    while not stop.is_set():
                        posted.append(b.post("j", "P", "p" * 120).id)
                        first_post.set()
            except Exception as exc:   # noqa: BLE001
                errors.append(exc)
        t = threading.Thread(target=poster)
        t.start()
        try:
            self.assertTrue(first_post.wait(10), "poster did not complete its first post")
            with self.h.board() as b:
                for cap in (300, 60, 1000, 200):
                    b.set_message_cap(cap)
        finally:
            stop.set()
            t.join(30)
        self.assertEqual(errors, [])
        self.assertTrue(posted)


if __name__ == "__main__":
    unittest.main()
