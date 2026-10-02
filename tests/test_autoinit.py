"""Automatic board setup (bin/board/autoinit.py): every CLI command and hook sets the board up
for this code's SCHEMA_VERSION, once per machine (stamp), under a lock; the CLI also registers
missing hooks, the hooks never do. Runs on memory, sqlite and file; on Postgres too when
SWARM_TEST_CONFIG names a throwaway server (a scratch database `<its dbname>_autoinit` is
created and dropped)."""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

from support import ROOT, tq, home_env  # noqa: F401  (sets sys.path)

from swarm import cli as swarm  # noqa: E402
from swarm import hooks as swarm_hooks  # noqa: E402
from swarm.board import SCHEMA_VERSION, BoardUnavailable, backend_class, ensure_initialized, open_board  # noqa: E402
from swarm.board import autoinit  # noqa: E402


class AutoInitBase:
    """Per-backend: make_cfg() (an uninitialised store), set_version(n) (None: as if from before
    versions were recorded; also drops the transcripts table where it can), drop_store()."""
    backend = ""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="swarm-autoinit-", dir=os.environ.get("TMPDIR")))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.settings = self.home / ".claude" / "settings.json"
        patcher = mock.patch.dict(os.environ, {**home_env(self.home), "SWARM_AUTO_INIT": "1",
                                               "CLAUDE_SETTINGS": str(self.settings), "USER": "tester"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.markers = self.tmp / "markers"
        self.cfg = self.make_cfg()
        self.cfg["board"]["spool_dir"] = str(self.tmp / "spool")
        self.cfg["hook"]["marker_dir"] = str(self.markers)
        self.cls = backend_class(self.cfg)
        self.config = self.tmp / "config.toml"
        self.config.write_text(self.toml())
        self.error_log = self.home / ".local/share/swarm/host/hook-errors.log"   # host-only

    def toml(self) -> str:
        return (f'[board]\nbackend = "{self.backend}"\nspool_dir = {tq(self.tmp / "spool")}\n'
                f'[hook]\nmarker_dir = {tq(self.markers)}\n')

    def stamp(self) -> Path | None:
        return autoinit.stamp_path(self.cfg)

    def forget_stamp(self) -> None:
        if self.stamp() is not None:
            self.stamp().unlink(missing_ok=True)

    def cli(self, *argv) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = swarm.main(["--config", str(self.config), *argv])
        return rc, out.getvalue(), err.getvalue()

    # ---- missing / old / current / newer

    def test_missing_store_is_set_up_on_first_open(self):
        self.assertIsNone(self.cls.schema_version(self.cfg))
        res = ensure_initialized(self.cfg)
        self.assertEqual(res.action, "initialized")
        self.assertEqual(self.cls.schema_version(self.cfg), SCHEMA_VERSION)
        with open_board(self.cfg) as b:
            self.assertTrue(b.allocate_name("k1", "J"))   # the name pool is there
            self.assertEqual(b.transcripts(job="J"), [])   # and the newest table
        if self.stamp() is not None:
            self.assertTrue(self.stamp().exists())

    def test_old_version_is_migrated_keeping_data(self):
        ensure_initialized(self.cfg)
        with open_board(self.cfg) as b:
            b.ensure_job("J")
            name = b.allocate_name("k1", "J")
            b.post("J", name, "before the upgrade")
        self.set_version(None)          # as a board set up before 3566c5b (no transcripts table)
        self.forget_stamp()
        self.assertLess(self.cls.schema_version(self.cfg) or 0, SCHEMA_VERSION)
        res = ensure_initialized(self.cfg)
        self.assertEqual(res.action, "initialized")
        self.assertEqual(self.cls.schema_version(self.cfg), SCHEMA_VERSION)
        with open_board(self.cfg) as b:
            self.assertEqual(b.transcripts(job="J"), [])   # the table the migration added
            b.allocate_name("k2", "J")                      # a new agent sees the job's history
            self.assertIn("before the upgrade",
                          [m.message for m in b.read_unread(agent_key="k2", job="J").messages])

    def test_v5_store_migrates_to_v8_keeping_data(self):
        """A store as schema 5 left it (no agents.left_reason/resume_of, no jobs.supervise, no
        restarts, no memory_refs/memory_ref_images) is migrated in place: rows kept, the new columns at their defaults, and the
        supervisor's operations work on the migrated rows."""
        ensure_initialized(self.cfg)
        with open_board(self.cfg) as b:
            b.open_job("J", "d", None, None, "tester")
            name = b.allocate_name("k1", "J", "worker")
            b.post("J", name, "before the upgrade")
            b.allocate_name("k2", "J")
            b.leave(agent_key="k2")
        self.make_v5()
        self.forget_stamp()
        self.assertEqual(self.cls.schema_version(self.cfg), 5)
        self.assertEqual(ensure_initialized(self.cfg).action, "initialized")
        self.assertEqual(self.cls.schema_version(self.cfg), SCHEMA_VERSION)
        with open_board(self.cfg) as b:
            js = b.job_status("J")
            self.assertEqual((js.description, js.supervise, js.messages), ("d", True, 1))
            got = {a.agent_key: a for a in b.agents("J")}
            self.assertEqual((got["k1"].name, got["k1"].role, got["k1"].ended_at), (name, "worker", None))
            self.assertIsNotNone(got["k2"].ended_at)
            self.assertEqual([(a.left_reason, a.resume_of) for a in got.values()], [(None, None)] * 2)
            self.assertEqual(b.restarts(), [])
            self.assertTrue(b.close_agent("k1", "stuck:dead"))
            self.assertEqual(b.claim_resume("k3", "k1", "J"), name)
            self.assertIsNotNone(b.record_restart("J", "k1", "k1", "stuck:dead", "claude", 60.0))
            self.assertTrue(b.set_job_supervise("J", False))
            self.assertFalse(b.job_status("J").supervise)
            self.assertEqual([m.message for m in b.messages_after(0, job="J")], ["before the upgrade"])
            # and schema 8's tables (make_v5 dropped them too)
            from swarm.board import MemoryRef
            self.assertEqual(b.memory_refs(), [])
            self.assertEqual(b.save_memory_ref(MemoryRef(
                document_id="d1", bank="notes", job="J", agent_key="k1", agent_name=name, harness="claude",
                host="h", session_id="s", tool_call_id="t", writer="note-tool")), "inserted")
            self.assertEqual([r.document_id for r in b.memory_refs(job="J")], ["d1"])

    def _migrates_to_v8_keeping_data(self, old: int):
        """A store as schema `old` left it (no memory_refs, memory_ref_images; for 6 also none of
        7's name and sha256 checks) is migrated in place: rows kept, the new tables empty and usable."""
        import hashlib
        import lzma
        from swarm.board import MemoryRef, TranscriptImage
        from support import fake_image
        ensure_initialized(self.cfg)
        with open_board(self.cfg) as b:
            b.open_job("J", "d", None, None, "tester")
            name = b.allocate_name("k1", "J")
            b.post("J", name, "before the upgrade")
        (self.make_v6 if old == 6 else self.make_v7)()
        self.forget_stamp()
        self.assertEqual(self.cls.schema_version(self.cfg), old)
        self.assertEqual(ensure_initialized(self.cfg).action, "initialized")
        self.assertEqual(self.cls.schema_version(self.cfg), SCHEMA_VERSION)
        data = fake_image(5)
        img = TranscriptImage(hashlib.sha256(data).hexdigest(), "image/png", len(data), data)
        with open_board(self.cfg) as b:
            self.assertEqual(b.memory_refs(job="J"), [])
            self.assertEqual(b.save_memory_ref(MemoryRef(
                document_id="d1", bank="notes", job="J", agent_key="k1", agent_name=name, harness="claude",
                host="h", session_id="s", tool_call_id="t", writer="note-tool",
                excerpt=lzma.compress(b"x\n"), raw_bytes=2, images=(img,))), "inserted")
            self.assertEqual(b.memory_ref_excerpt("d1"), b"x\n")
            self.assertEqual(b.transcript_image(img.sha256).data, data)
            self.assertEqual([m.message for m in b.messages_after(0, job="J")], ["before the upgrade"])
            with self.assertRaises(ValueError):
                b.post("J", "bad\nname", "the v7 checks hold after the upgrade")

    def test_v7_store_migrates_to_v8_keeping_data(self):
        self._migrates_to_v8_keeping_data(7)

    def test_v6_store_migrates_to_v8_keeping_data(self):
        self._migrates_to_v8_keeping_data(6)

    def test_v8_store_migrates_to_v9_keeping_transcripts(self):
        """A store as schema 8 left it (no transcripts.capture_failed) is migrated in place: its
        transcript rows are kept, read as not failed, and a capture-failed row can be stored."""
        from swarm.transcripts import capture_failed_row, make_row
        self.cfg.setdefault("transcripts", {})["enabled"] = True
        ensure_initialized(self.cfg)
        with open_board(self.cfg) as b:
            b.open_job("J", "d", None, None, "tester")
            name = b.allocate_name("k1", "J")
            b.save_transcript(make_row("J", "k1", name, "subagent", '{"a": 1}\n', final=True))
        self.make_v8()
        self.forget_stamp()
        self.assertEqual(self.cls.schema_version(self.cfg), 8)
        self.assertEqual(ensure_initialized(self.cfg).action, "initialized")
        self.assertEqual(self.cls.schema_version(self.cfg), SCHEMA_VERSION)
        with open_board(self.cfg) as b:
            [row] = b.transcripts(job="J")
            self.assertEqual((row.agent_key, row.final, row.failed), ("k1", True, None))
            self.assertEqual(b.transcript_body("J", "k1"), b'{"a": 1}\n')
            self.assertEqual(b.mark_capture_failed("J", "k2", "ran out of time", capture_failed_row("J", "k2", "Bart", "ran out of time")), "stored")
            got = {r.agent_key: r.failed for r in b.transcripts(job="J")}
            self.assertEqual(got, {"k1": None, "k2": "ran out of time"})
            self.assertIsNone(b.transcript_body("J", "k2"))

    def test_current_store_runs_no_setup_and_the_stamp_skips_the_query(self):
        ensure_initialized(self.cfg)
        with mock.patch.object(self.cls, "setup", side_effect=AssertionError("setup ran")):
            if self.stamp() is None:    # memory: no stamp, a cheap in-process check each time
                self.assertEqual(ensure_initialized(self.cfg).action, "current")
                return
            self.forget_stamp()
            self.assertEqual(ensure_initialized(self.cfg).action, "current")
            self.assertTrue(self.stamp().exists())
            with mock.patch.object(self.cls, "schema_version", side_effect=AssertionError("queried")):
                self.assertEqual(ensure_initialized(self.cfg).action, "stamped")

    def test_newer_store_is_left_alone_with_one_warning(self):
        ensure_initialized(self.cfg)
        self.set_version(SCHEMA_VERSION + 5)
        self.forget_stamp()
        with mock.patch.object(self.cls, "setup", side_effect=AssertionError("setup ran")):
            rc, _, err = self.cli("status")
            self.assertEqual(rc, 0)
            self.assertIn(f"schema version {SCHEMA_VERSION + 5}, newer than this code's "
                          f"{SCHEMA_VERSION}; left untouched", err)
            if self.stamp() is not None:  # once: the stamp says it was looked at
                rc, _, err = self.cli("status")
                self.assertEqual((rc, err), (0, ""))
        self.assertEqual(self.cls.schema_version(self.cfg), SCHEMA_VERSION + 5)

    def test_concurrent_opens_migrate_once(self):
        calls = []
        real = self.cls.setup.__func__

        def slow_setup(cls, cfg, names):
            calls.append(1)
            time.sleep(0.3)
            return real(cls, cfg, names)

        results, errors = [], []

        def worker():
            try:
                results.append(ensure_initialized(self.cfg).action)
            except Exception as exc:  # pragma: no cover (reported below)
                errors.append(exc)

        with mock.patch.object(self.cls, "setup", classmethod(slow_setup)):
            threads = [threading.Thread(target=worker) for _ in range(6)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(30)
        self.assertEqual(errors, [])
        self.assertEqual(len(calls), 1)
        self.assertEqual(results.count("initialized"), 1)
        self.assertEqual(self.cls.schema_version(self.cfg), SCHEMA_VERSION)

    # ---- a store dropped behind a stamp

    def test_cli_recreates_a_store_dropped_after_the_stamp(self):
        rc, _, _ = self.cli("job", "J")
        self.assertEqual(rc, 0)
        self.drop_store()
        rc, out, err = self.cli("job", "J")
        self.assertEqual((rc, out), (0, "J\n"), err)
        self.assertEqual(self.cls.schema_version(self.cfg), SCHEMA_VERSION)
        if self.stamp() is not None:
            self.assertTrue(self.stamp().exists())
        rc, out, _ = self.cli("join", "--job", "J", "--key", "k1")
        self.assertEqual(rc, 0)
        self.assertTrue(out.strip())            # the name pool is back

    def test_hook_recreates_a_store_dropped_after_the_stamp(self):
        self._activate_job()
        self.drop_store()
        with mock.patch.object(swarm_hooks, "HOOK_INIT_TIMEOUT", 3.0):
            self.hook("start")                  # the job itself went with the store
        self.assertEqual(self.cls.schema_version(self.cfg), SCHEMA_VERSION)
        self.assertFalse(self.settings.exists())

    def test_a_connection_error_on_a_present_store_never_reinitialises(self):
        ensure_initialized(self.cfg)
        stamp = self.stamp()
        err = BoardUnavailable("network is down")
        with mock.patch.object(self.cls, "__init__", side_effect=err), \
                mock.patch.object(self.cls, "setup", side_effect=AssertionError("setup ran")):
            with self.assertRaises(BoardUnavailable):
                open_board(self.cfg)
        if stamp is not None:
            self.assertTrue(stamp.exists())

    def test_recovery_retries_the_open_once(self):
        ensure_initialized(self.cfg)
        calls = []
        err = BoardUnavailable("still failing")
        with mock.patch.object(self.cls, "__init__", side_effect=err), \
                mock.patch.object(self.cls, "store_missing", return_value=True), \
                mock.patch.object(autoinit, "ensure_initialized",
                                  side_effect=lambda *a, **k: calls.append(1)):
            with self.assertRaises(BoardUnavailable):
                open_board(self.cfg)
        self.assertEqual(len(calls), 1)

    # ---- the CLI

    def test_cli_works_without_init_and_leaves_claude_settings_alone(self):
        self.settings.parent.mkdir(parents=True)
        before = json.dumps({"theme": "dark", "hooks": {"PreToolUse": [
            {"hooks": [{"type": "command", "command": "other"}]}]}})
        self.settings.write_text(before)
        rc, out, err = self.cli("job", "J")
        self.assertEqual((rc, out), (0, "J\n"))
        self.assertIn("swarm: board set up", err)
        self.assertNotIn("hooks", err)
        self.assertEqual(self.settings.read_text(), before)       # the plugin's hooks.json replaces it
        self.assertFalse(list(self.settings.parent.glob("settings.json.pre-swarm-*")))
        rc, out, err = self.cli("post", "--job", "J", "--as", "Homer Simpson", "hi")
        self.assertEqual((rc, err), (0, ""))            # nothing to do the second time

    def test_cli_init_stays_explicit_and_stamps(self):
        rc, out, _ = self.cli("init", "--no-hooks")
        self.assertEqual(rc, 0)
        self.assertIn("schema ready", out)
        self.assertFalse(self.settings.exists())
        self.assertEqual(self.cls.schema_version(self.cfg), SCHEMA_VERSION)
        if self.stamp() is not None:
            self.assertTrue(self.stamp().exists())

    def test_disabled_by_environment(self):
        with mock.patch.dict(os.environ, {"SWARM_AUTO_INIT": "0"}):
            self.assertEqual(ensure_initialized(self.cfg).action, "disabled")
            self.cli("status")
        self.assertLess(self.cls.schema_version(self.cfg) or 0, SCHEMA_VERSION)
        self.assertFalse(self.settings.exists())

    # ---- the hooks

    def _activate_job(self) -> None:
        rc, _, _ = self.cli("activate", "--job", "J", "--session", "sess-1")
        self.assertEqual(rc, 0)
        self.assertFalse(self.settings.exists())   # the CLI registers no hooks (they come from the plugin)

    def hook(self, event: str, agent_id: str = "agent-1") -> dict | None:
        out = io.StringIO()
        payload = json.dumps({"agent_id": agent_id, "session_id": "sess-1"})
        with contextlib.redirect_stdout(out), mock.patch("sys.stdin", io.StringIO(payload)):
            self.assertEqual(swarm_hooks.run_hook(event, self.cfg), 0)
        text = out.getvalue().strip()
        return json.loads(text) if text else None

    def test_hook_migrates_but_never_registers_hooks(self):
        self._activate_job()
        self.set_version(None)
        self.forget_stamp()
        out = self.hook("start")
        self.assertIn("[swarm] You are **", out["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(self.cls.schema_version(self.cfg), SCHEMA_VERSION)
        self.assertFalse(self.settings.exists())
        self.assertIn("auto-init: board set up", self.error_log.read_text())

    def test_hook_failure_is_logged_and_the_hook_carries_on(self):
        self._activate_job()
        self.forget_stamp()
        with mock.patch.object(self.cls, "schema_version", side_effect=RuntimeError("boom")):
            out = self.hook("start")
        self.assertIn("[swarm] You are **", out["hookSpecificOutput"]["additionalContext"])
        self.assertIn("RuntimeError: boom", self.error_log.read_text())
        self.assertFalse(self.settings.exists())

    def test_hook_waits_for_a_held_lock_only_until_its_deadline(self):
        self._activate_job()
        self.set_version(None)
        self.forget_stamp()
        started = time.monotonic()
        with mock.patch.object(swarm_hooks, "HOOK_INIT_TIMEOUT", 0.3), \
                autoinit.setup_lock(self.cfg, 5):
            done = threading.Event()
            box = {}

            def run():
                box["out"] = self.hook("turn")
                done.set()
            t = threading.Thread(target=run)
            t.start()
            t.join(10)
        self.assertTrue(done.is_set())
        self.assertLess(time.monotonic() - started, 8)
        self.assertIn("TimeoutError", self.error_log.read_text())

    # ---- stamps and the lock file are host-only and never followed

    def _host(self) -> Path:
        return self.home / ".local/share/swarm/host"

    def test_stamp_and_lock_live_in_the_host_dir(self):
        if self.stamp() is None:
            self.skipTest("no stamp for this backend")
        self.assertEqual(self.stamp().parent, self._host())
        ensure_initialized(self.cfg)
        self.assertTrue(self.stamp().is_file())
        self.assertEqual(self._host().stat().st_mode & 0o777, 0o700)
        self.assertFalse((self.home / ".local/state/swarm").exists())

    def test_planted_stamp_symlinks_are_not_followed(self):
        if self.stamp() is None:
            self.skipTest("no stamp for this backend")
        victim = self.tmp / "created-by-touch"
        old = self.home / ".local/state/swarm" / self.stamp().name
        old.parent.mkdir(parents=True)
        old.symlink_to(victim)
        self._host().mkdir(parents=True, mode=0o700)
        self.stamp().symlink_to(victim)
        self.assertEqual(ensure_initialized(self.cfg).action, "initialized")   # a link is no stamp
        self.assertFalse(victim.exists())
        self.assertTrue(self.stamp().is_symlink())   # refused, left for the user to look at

    def test_a_fifo_as_the_lock_file_does_not_block(self):
        if self.stamp() is None or getattr(self.cls, "setup_lock", None):
            self.skipTest("no local lock file for this backend")
        self._host().mkdir(parents=True, mode=0o700)
        os.mkfifo(self._host() / (self.stamp().name.rsplit("-", 1)[0] + ".lock"))
        box = {}

        def run():
            try:
                ensure_initialized(self.cfg, timeout=1)
            except Exception as exc:
                box["exc"] = exc
        t = threading.Thread(target=run, daemon=True)
        t.start()
        t.join(5)
        self.assertFalse(t.is_alive(), "a FIFO lock file blocked the setup")
        self.assertIsInstance(box.get("exc"), OSError)


class MemoryAutoInitTests(AutoInitBase, unittest.TestCase):
    backend = "memory"

    def make_cfg(self) -> dict:
        from swarm.board import memory
        from support import base_config
        self.store_name = f"autoinit-{uuid.uuid4().hex}"
        memory.reset_store(self.store_name)
        cfg = base_config()
        cfg["memory"] = {"store": self.store_name}
        return cfg

    def toml(self) -> str:
        return super().toml() + f'[memory]\nstore = {tq(self.store_name)}\n'

    def drop_store(self) -> None:
        from swarm.board import memory
        memory.reset_store(self.store_name)

    def set_version(self, version: int | None) -> None:
        from swarm.board import memory
        store = memory.get_store(self.store_name)
        with store.lock:
            store.schema_version = version

    def make_v5(self) -> None:
        from swarm.board import memory
        store = memory.get_store(self.store_name)
        with store.lock:
            for a in store.agents.values():
                a.pop("left_reason", None)
                a.pop("resume_of", None)
            for j in store.jobs.values():
                j.pop("supervise", None)
            store.restarts, store.next_restart_id = [], 1
            store.memory_refs = {}
            for t in store.transcripts.values():
                t.pop("capture_failed", None)
            store.schema_version = 5

    def make_v8(self) -> None:
        from swarm.board import memory
        store = memory.get_store(self.store_name)
        with store.lock:
            for t in store.transcripts.values():
                t.pop("capture_failed", None)
            store.schema_version = 8

    def make_v7(self) -> None:
        from swarm.board import memory
        self.make_v8()
        store = memory.get_store(self.store_name)
        with store.lock:
            store.memory_refs = {}
            store.schema_version = 7

    def make_v6(self) -> None:
        self.make_v7()
        self.set_version(6)


class SqliteAutoInitTests(AutoInitBase, unittest.TestCase):
    backend = "sqlite"

    def make_cfg(self) -> dict:
        from support import base_config
        cfg = base_config(backend="sqlite")
        cfg["sqlite"] = {"path": str(self.tmp / "db" / "board.sqlite3"), "busy_timeout_ms": 10000}
        return cfg

    def toml(self) -> str:
        return super().toml() + f'[sqlite]\npath = {tq(self.tmp / "db" / "board.sqlite3")}\n'

    def drop_store(self) -> None:
        for p in Path(self.cfg["sqlite"]["path"]).parent.glob("board.sqlite3*"):
            p.unlink()

    def set_version(self, version: int | None) -> None:
        import sqlite3
        db = sqlite3.connect(self.cfg["sqlite"]["path"], isolation_level=None)
        try:
            if version is None:
                db.execute("DROP TABLE IF EXISTS memory_ref_images")
                db.execute("DROP TABLE IF EXISTS memory_refs")
                db.execute("DROP TABLE IF EXISTS transcript_image_refs")
                db.execute("DROP TABLE IF EXISTS transcript_images")
                db.execute("DROP TABLE IF EXISTS transcripts")
            db.execute(f"PRAGMA user_version = {2 if version is None else version}")
        finally:
            db.close()

    def make_v5(self) -> None:
        import sqlite3
        db = sqlite3.connect(self.cfg["sqlite"]["path"], isolation_level=None)
        try:
            for stmt in ("DROP TABLE memory_ref_images", "DROP TABLE memory_refs",
                         "ALTER TABLE transcripts DROP COLUMN capture_failed",
                         "DROP TABLE restarts", "ALTER TABLE agents DROP COLUMN left_reason",
                         "ALTER TABLE agents DROP COLUMN resume_of", "ALTER TABLE jobs DROP COLUMN supervise",
                         "PRAGMA user_version = 5"):
                db.execute(stmt)
        finally:
            db.close()

    def make_v8(self) -> None:
        import sqlite3
        db = sqlite3.connect(self.cfg["sqlite"]["path"], isolation_level=None)
        try:
            for stmt in ("ALTER TABLE transcripts DROP COLUMN capture_failed", "PRAGMA user_version = 8"):
                db.execute(stmt)
        finally:
            db.close()

    def make_v7(self) -> None:
        import sqlite3
        self.make_v8()
        db = sqlite3.connect(self.cfg["sqlite"]["path"], isolation_level=None)
        try:
            for stmt in ("DROP TABLE memory_ref_images", "DROP TABLE memory_refs", "PRAGMA user_version = 7"):
                db.execute(stmt)
        finally:
            db.close()

    def make_v6(self) -> None:
        """make_v7, and without schema 7's check triggers."""
        import sqlite3
        self.make_v7()
        db = sqlite3.connect(self.cfg["sqlite"]["path"], isolation_level=None)
        try:
            for (trigger,) in db.execute("SELECT name FROM sqlite_master WHERE type = 'trigger' "
                                         "AND name LIKE 'check_%'").fetchall():
                db.execute(f"DROP TRIGGER {trigger}")
            db.execute("PRAGMA user_version = 6")
        finally:
            db.close()


class FileAutoInitTests(AutoInitBase, unittest.TestCase):
    backend = "file"

    def make_cfg(self) -> dict:
        from support import base_config
        cfg = base_config(backend="file")
        cfg["file"] = {"path": str(self.tmp / "board")}
        return cfg

    def toml(self) -> str:
        return super().toml() + f'[file]\npath = {tq(self.tmp / "board")}\n'

    def drop_store(self) -> None:
        shutil.rmtree(self.cfg["file"]["path"])

    def set_version(self, version: int | None) -> None:
        path = Path(self.cfg["file"]["path"]) / "schema_version"
        if version is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(f"{version}\n")

    def make_v5(self) -> None:
        from swarm.board.file import dumps, loads
        state_file = Path(self.cfg["file"]["path"]) / "state.json"
        state = loads(state_file.read_text())
        for a in state["agents"].values():
            a.pop("left_reason", None)
            a.pop("resume_of", None)
        for j in state["jobs"].values():
            j.pop("supervise", None)
        state.pop("restarts", None)
        state.pop("next_restart_id", None)
        state_file.write_text(dumps(state))
        self.make_v8()
        (Path(self.cfg["file"]["path"]) / "transcripts" / "memory_refs.json").unlink(missing_ok=True)
        self.set_version(5)

    def make_v8(self) -> None:
        from swarm.board.file import dumps, loads
        index = Path(self.cfg["file"]["path"]) / "transcripts" / "index.json"
        if index.exists():
            rows = loads(index.read_text())
            for t in rows.values():
                t.pop("capture_failed", None)
            index.write_text(dumps(rows))
        self.set_version(8)

    def make_v7(self) -> None:
        self.make_v8()
        (Path(self.cfg["file"]["path"]) / "transcripts" / "memory_refs.json").unlink(missing_ok=True)
        self.set_version(7)

    def make_v6(self) -> None:
        self.make_v7()
        self.set_version(6)


@unittest.skipUnless(os.environ.get("SWARM_TEST_CONFIG"), "set SWARM_TEST_CONFIG to a throwaway Postgres")
class PostgresAutoInitTests(AutoInitBase, unittest.TestCase):
    backend = "postgres"

    @classmethod
    def setUpClass(cls):
        from swarm.board.postgres import _password
        cls.PASSWORD = _password(swarm.load_config(Path(os.environ["SWARM_TEST_CONFIG"]).expanduser())["database"])

    def make_cfg(self) -> dict:
        cfg = swarm.load_config(Path(os.environ["SWARM_TEST_CONFIG"]).expanduser())
        cfg["board"]["backend"] = "postgres"
        live = swarm.load_config(Path(os.environ.get("SWARM_CONFIG", "~/.config/swarm/config.toml")).expanduser())
        cfg["database"]["dbname"] = self.dbname = f"{cfg['database']['dbname']}_autoinit"
        if self.dbname == live["database"]["dbname"]:
            raise RuntimeError("refusing to test against the production board database")
        # $HOME is the test's own by now: hand the password over in the environment
        self._password = self.PASSWORD
        if self._password:
            os.environ["PGPASSWORD"] = self._password   # restored with the rest of os.environ
        self._drop()
        self.addCleanup(self._drop)
        return cfg

    def toml(self) -> str:
        db = self.cfg["database"]
        lines = [f'{k} = {json.dumps(db[k])}' for k in ("host", "port", "user", "dbname", "admin_dbname",
                                                          "password_env_file", "sslmode")]
        return super().toml() + "[database]\n" + "\n".join(lines) + "\n"

    def _admin(self):
        import psycopg
        cfg = swarm.load_config(Path(os.environ["SWARM_TEST_CONFIG"]).expanduser())
        db = cfg["database"]
        return psycopg.connect(host=db["host"], port=db["port"], user=db["user"], password=self._password,
                               dbname=db["admin_dbname"], sslmode=db["sslmode"], autocommit=True)

    def _drop(self) -> None:
        from psycopg import sql
        with self._admin() as conn:
            conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(self.dbname)))

    def drop_store(self) -> None:
        self._drop()

    def set_version(self, version: int | None) -> None:
        import psycopg
        db = self.cfg["database"]
        with psycopg.connect(host=db["host"], port=db["port"], user=db["user"], password=self._password,
                             dbname=self.dbname, sslmode=db["sslmode"], autocommit=True) as conn:
            if version is None:
                conn.execute("DROP TABLE IF EXISTS board_meta")
                conn.execute("DROP TABLE IF EXISTS memory_ref_images, memory_refs, transcript_image_refs, "
                             "transcript_images, transcripts")
            else:
                conn.execute("UPDATE board_meta SET value = %s WHERE key = 'schema_version'", (str(version),))

    def make_v5(self) -> None:
        import psycopg
        db = self.cfg["database"]
        with psycopg.connect(host=db["host"], port=db["port"], user=db["user"], password=self._password,
                             dbname=self.dbname, sslmode=db["sslmode"], autocommit=True) as conn:
            for stmt in ("DROP TABLE memory_ref_images", "DROP TABLE memory_refs",
                         "DROP VIEW job_status", "DROP VIEW agent_status", "DROP TABLE restarts",
                         "ALTER TABLE agents DROP COLUMN left_reason, DROP COLUMN resume_of",
                         "ALTER TABLE jobs DROP COLUMN supervise",
                         "ALTER TABLE transcripts DROP COLUMN capture_failed"):
                conn.execute(stmt)
        self.set_version(5)

    def _pg(self):
        import psycopg
        db = self.cfg["database"]
        return psycopg.connect(host=db["host"], port=db["port"], user=db["user"], password=self._password,
                               dbname=self.dbname, sslmode=db["sslmode"], autocommit=True)

    def make_v8(self) -> None:
        with self._pg() as conn:
            conn.execute("ALTER TABLE transcripts DROP COLUMN capture_failed")
        self.set_version(8)

    def make_v7(self) -> None:
        self.make_v8()
        with self._pg() as conn:
            conn.execute("DROP TABLE memory_ref_images")
            conn.execute("DROP TABLE memory_refs")
        self.set_version(7)

    def make_v6(self) -> None:
        """make_v7, and without schema 7's CHECK constraints."""
        self.make_v7()
        with self._pg() as conn:
            for table, con in conn.execute("SELECT conrelid::regclass::text, conname FROM pg_constraint "
                                           "WHERE conname LIKE 'check\\_%'").fetchall():
                conn.execute(f'ALTER TABLE {table} DROP CONSTRAINT "{con}"')
        self.set_version(6)


if __name__ == "__main__":
    unittest.main()
