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
        self.assertEqual(SCHEMA_VERSION, 18)

    def test_pause_marks_job_records_manifest_and_closes_agents(self):
        a, c = self.team()
        rec = self.b.pause_job("j", "francesco", "moving box", {"k1": "/work/repo"})
        self.assertEqual(self.b.job_status("j").status, "paused")
        self.assertEqual((rec.job, rec.paused_by, rec.reason, rec.resumed_at), ("j", "francesco", "moving box", None))
        m = rec.manifest
        self.assertEqual(m["version"], 1)
        self.assertEqual(m["job_state"]["goal"], "ship it")
        self.assertEqual(m["job_state"]["task"], "do the thing")
        self.assertEqual(m["job_state"]["last_message_id"], self.b.recent_messages(10, "j")[-1].id)
        by = {e["agent_key"]: e for e in m["agents"]}
        self.assertEqual(set(by), {"k1", "k2"})
        e = by["k1"]
        self.assertEqual((e["agent_name"], e["role"], e["harness"], e["model"], e["session_id"], e["cwd"],
                          e["last_tool"], e["kind"], e["task"]),
                         (a, "engineer", "claude", "sonnet", "sess-1", "/work/repo", "Bash", "subagent",
                          "do the thing"))
        self.assertEqual(by["k2"]["cursor"], self.b.recent_messages(10, "j")[-1].id)
        self.assertIsNone(by["k2"]["cwd"])
        for key in ("k1", "k2"):
            row = next(x for x in self.b.agents("j") if x.agent_key == key)
            self.assertIsNotNone(row.ended_at)
            self.assertEqual(row.left_reason, LEFT_PAUSED)
        self.assertFalse(any(r.active for r in self.b.roster("j")))
        self.assertEqual(self.b.open_pause("j").id, rec.id)

    def test_pause_is_idempotent_and_only_for_active_jobs(self):
        self.team()
        first = self.b.pause_job("j", "u", "r")
        again = self.b.pause_job("j", "other", "other")
        self.assertEqual((again.id, again.paused_by), (first.id, "u"))
        self.assertEqual(len(self.b.pauses("j")), 1)
        self.assertIsNone(self.b.pause_job("missing", "u", None))
        self.b.begin_resume("j", first.id, "u", "box2")
        self.b.close_job("j", "completed", "done")
        self.assertIsNone(self.b.pause_job("j", "u", None))

    def test_paused_job_refuses_join_and_post_with_a_clear_message(self):
        a, _ = self.team()
        self.b.pause_job("j", "francesco", "lunch")
        with self.assertRaises(JobPaused) as cm:
            self.b.allocate_name("new", "j")
        text = str(cm.exception)
        self.assertIn("job j is paused", text)
        self.assertIn("francesco", text)
        self.assertIn("lunch", text)
        self.assertIn("swarm resume --job j", text)
        with self.assertRaises(JobPaused):
            self.b.allocate_name("k1", "j")        # a paused agent is not revived by joining again
        with self.assertRaises(JobPaused):
            self.b.post("j", a, "hello?")
        # the system writer may; another job is unaffected; reads work
        self.b.post("j", PAUSE_WRITER, "job paused")
        self.b.allocate_name("other", "j2")
        self.assertTrue(self.b.read_new(name=a, job="j") is not None)
        self.assertEqual(self.b.recent_messages(10, "j")[-1].agent_name, PAUSE_WRITER)

    def test_resume_reopens_and_claim_resume_keeps_names_and_cursors(self):
        a, c = self.team()
        rec = self.b.pause_job("j", "u", None)
        self.assertEqual(self.b.claim_resume("n2", "k2", "j"), c)        # names claimed while paused
        self.assertTrue(self.b.begin_resume("j", rec.id, "me", "box2"))
        self.assertFalse(self.b.begin_resume("j", rec.id, "me", "box2"))  # a second resume loses
        js = self.b.job_status("j")
        self.assertEqual((js.status, js.goal), ("active", "ship it"))
        done = self.b.pauses("j")[0]
        self.assertEqual((done.resumed_by, done.resumed_host), ("me", "box2"))
        self.assertIsNone(self.b.open_pause("j"))
        self.assertEqual(self.b.claim_resume("n1", "k1", "j"), a)
        names = {r.name for r in self.b.roster("j") if r.active}
        self.assertEqual(names, {a, c})
        self.assertEqual(self.b.allocate_name("n1", "j"), a)             # joining works again
        self.b.post("j", a, "back")
        self.assertTrue(self.b.record_resume_outcome(rec.id, {"k1": {"status": "launched"}}))
        self.assertEqual(self.b.pauses("j")[0].outcome, {"k1": {"status": "launched"}})

    def test_begin_resume_needs_the_open_pause(self):
        self.team()
        rec = self.b.pause_job("j", "u", None)
        self.assertFalse(self.b.begin_resume("j", rec.id + 99, "u", None))
        self.assertEqual(self.b.job_status("j").status, "paused")

    def test_a_paused_job_keeps_its_history_past_retention(self):
        a, c = self.team()
        rec = self.b.pause_job("j", "u", None)
        for row in self.b.recent_messages(10, "j"):
            self.h.backdate_message(row.id, 30 * 86400)
        self.h.backdate_agent("k2", left_at=30 * 86400)
        self.b.purge()
        self.assertEqual(len(self.b.recent_messages(10, "j")), 2)
        self.assertEqual(self.b.claim_resume("n2", "k2", "j"), c)
        self.assertTrue(self.b.begin_resume("j", rec.id, "u", None))

    def test_a_closed_paused_job_can_be_deactivated(self):
        self.team()
        self.b.pause_job("j", "u", None)
        self.assertTrue(self.b.close_job("j", "cancelled", "abandoned"))
        self.assertEqual(self.b.job_status("j").status, "cancelled")

    def test_orchestrator_transcript_marks_the_manifest_entry(self):
        self.team()
        import hashlib, lzma
        body = lzma.compress(b"{}\n")
        self.b.save_transcript(TranscriptRow(job="j", agent_key="k2", agent_name="x", role="orchestrator",
                                             host="h", session_id="s", final=False, raw_bytes=3,
                                             redactions=0, sha256=hashlib.sha256(b"{}\n").hexdigest(), body=body))
        rec = self.b.pause_job("j", "u", None)
        kinds = {e["agent_key"]: e["kind"] for e in rec.manifest["agents"]}
        self.assertEqual(kinds, {"k1": "subagent", "k2": "orchestrator"})


class TestMemoryPause(PauseContract, unittest.TestCase):
    harness_factory = staticmethod(lambda: MemoryHarness())


class TestFilePause(PauseContract, unittest.TestCase):
    harness_factory = staticmethod(lambda: FileHarness())


class TestSqlitePause(PauseContract, unittest.TestCase):
    harness_factory = staticmethod(lambda: SqliteHarness())


@unittest.skipUnless(os.environ.get("SWARM_TEST_CONFIG"), "needs $SWARM_TEST_CONFIG (a throwaway Postgres)")
class TestPostgresPause(PauseContract, unittest.TestCase):
    harness_factory = staticmethod(lambda: PostgresHarness(os.environ["SWARM_TEST_CONFIG"]))


class TestSqliteMigration(unittest.TestCase):
    """An older board file (jobs CHECK without 'paused', schema 11) is upgraded in place, keeping
    every job, agent and message (the rebuild must not cascade-delete them)."""

    def test_rebuild_keeps_rows_and_allows_pause(self):
        import tempfile
        from pathlib import Path
        from support import SMALL_POOL, base_config
        with tempfile.TemporaryDirectory() as tmp:
            cfg = base_config(backend="sqlite")
            cfg["sqlite"] = {"path": str(Path(tmp) / "b.sqlite3")}
            setup_board(cfg, SMALL_POOL)
            with open_board(cfg) as b:
                b.open_job("j", "d", "t", None, "u")
                name = b.allocate_name("k1", "j")
                b.post("j", name, "hello")
            conn = sqlite3.connect(cfg["sqlite"]["path"])
            sql = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'jobs'").fetchone()[0]
            conn.execute("PRAGMA foreign_keys = OFF")
            conn.execute("DROP TABLE job_pauses")
            conn.execute(sql.replace("'paused', ", "").replace("CREATE TABLE jobs", "CREATE TABLE jobs_new", 1))
            conn.execute("INSERT INTO jobs_new SELECT * FROM jobs")
            conn.execute("DROP TABLE jobs")
            conn.execute("ALTER TABLE jobs_new RENAME TO jobs")
            conn.execute("PRAGMA user_version = 11")
            conn.commit()
            self.assertNotIn("'paused'", conn.execute("SELECT sql FROM sqlite_master WHERE name = 'jobs'").fetchone()[0])
            conn.close()
            setup_board(cfg, SMALL_POOL)
            setup_board(cfg, SMALL_POOL)       # idempotent
            with open_board(cfg) as b:
                self.assertEqual(b.job_status("j").agents, 1)
                self.assertEqual(b.recent_messages(5, "j")[0].message, "hello")
                self.assertIsNotNone(b.pause_job("j", "u", "r"))
                self.assertEqual(b.job_status("j").status, "paused")


if __name__ == "__main__":
    unittest.main()
