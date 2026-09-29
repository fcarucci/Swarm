"""Read-only boards (`open_read_only`, used by `swarm supervise --dry-run`): the file and SQLite
backends read without creating, writing, truncating or renaming anything, and every write
raises ReadOnlyBoard. Runs on both backends whatever SWARM_TEST_BACKEND is."""
from __future__ import annotations

import hashlib
import lzma
import os
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from support import SMALL_POOL, base_config  # noqa: F401  (sets sys.path)

from swarm.board import MemoryRef, ReadOnlyBoard, open_board, open_read_only, setup_board  # noqa: E402
from swarm.board import file as fileboard  # noqa: E402
from swarm.board import sqlite as sqliteboard  # noqa: E402


def tree(root: Path, skip=()) -> dict:
    out = {}
    for p in sorted(root.rglob("*")):
        if p.name.endswith(tuple(skip)):
            continue
        st = p.stat()
        out[str(p)] = (hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else "dir",
                       st.st_mtime_ns, st.st_mode)
    return out


class _Base:
    SKIP: tuple = ()

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="swarm-ro-", dir=os.environ.get("TMPDIR")))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.cfg = self.make_cfg()
        setup_board(self.cfg, SMALL_POOL)
        with open_board(self.cfg) as b:
            b.open_job("J", None, None, None, None)
            self.name = b.allocate_name("k", "J")
            b.post("J", self.name, "hello", agent_key="k")
            b.save_memory_ref(MemoryRef(
                document_id="d1", bank="notes", job="J", agent_key="k", agent_name=self.name, harness=None,
                host=None, session_id=None, tool_call_id=None, writer="note-tool", excerpt=lzma.compress(b"x\n"),
                raw_bytes=2))

    def writes(self, b):
        """(label, call) for a spread of write methods."""
        return [
            ("post", lambda: b.post("J", self.name, "more", agent_key="k")),
            ("open_job", lambda: b.open_job("J2", None, None, None, None)),
            ("allocate_name", lambda: b.allocate_name("k2", "J")),
            ("close_agent", lambda: b.close_agent("k", "stuck:dead")),
            ("tool_started", lambda: b.tool_started("k", "Bash")),
            ("set_job_supervise", lambda: b.set_job_supervise("J", False)),
            ("record_restart", lambda: b.record_restart("J", "k", "k", "stuck:dead", "claude", 30.0)),
            ("purge", lambda: b.purge()),
            ("save_memory_ref", lambda: b.save_memory_ref(MemoryRef(
                document_id="d2", bank="notes", job="J", agent_key="k", agent_name=self.name, harness=None,
                host=None, session_id=None, tool_call_id=None, writer="note-tool"))),
            ("mark_memory_refs_checked", lambda: b.mark_memory_refs_checked(["d1"])),
            ("delete_memory_refs", lambda: b.delete_memory_refs(["d1"])),
        ]

    def test_reads_work(self):
        with open_read_only(self.cfg) as b:
            self.assertTrue(b.read_only)
            self.assertEqual([m.message for m in b.recent_messages(10, job="J")], ["hello"])
            self.assertEqual([j.job for j in b.jobs(True)], ["J"])
            self.assertEqual([r.document_id for r in b.memory_refs()], ["d1"])
            self.assertEqual(b.memory_ref_excerpt("d1"), b"x\n")

    def test_write_methods_raise_and_change_nothing(self):
        before = tree(self.tmp, self.SKIP)
        with open_read_only(self.cfg) as b:
            for label, call in self.writes(b):
                with self.subTest(label):
                    with self.assertRaises(ReadOnlyBoard):
                        call()
        self.assertEqual(tree(self.tmp, self.SKIP), before)
        with open_board(self.cfg) as b:
            self.assertEqual([m.message for m in b.recent_messages(10, job="J")], ["hello"])

    def test_normal_board_is_not_read_only(self):
        with open_board(self.cfg) as b:
            self.assertFalse(b.read_only)


class FileReadOnlyTests(_Base, unittest.TestCase):
    def make_cfg(self):
        cfg = base_config(backend="file")
        cfg["file"] = {"path": str(self.tmp / "board")}
        return cfg

    @property
    def dir(self) -> Path:
        return self.tmp / "board"

    def test_missing_state_json_reads_as_empty_and_is_not_created(self):
        (self.dir / "state.json").unlink()
        before = tree(self.tmp)
        with open_read_only(self.cfg) as b:
            self.assertEqual(b.jobs(True), [])
            self.assertEqual([m.message for m in b.recent_messages(10, job="J")], ["hello"])
        self.assertEqual(tree(self.tmp), before)

    def test_torn_last_line_is_skipped_not_repaired(self):
        with open(self.dir / "messages.jsonl", "ab") as fh:
            fh.write(b'{"id": 99, "tor')
        raw = (self.dir / "messages.jsonl").read_bytes()
        before = tree(self.tmp)
        with open_read_only(self.cfg) as b:
            self.assertEqual([m.message for m in b.recent_messages(10, job="J")], ["hello"])
        self.assertEqual((self.dir / "messages.jsonl").read_bytes(), raw)
        self.assertEqual(tree(self.tmp), before)

    def test_missing_lock_file_is_not_created(self):
        (self.dir / "lock").unlink()
        before = tree(self.tmp)
        with open_read_only(self.cfg) as b:
            self.assertEqual([j.job for j in b.jobs(True)], ["J"])
        self.assertEqual(tree(self.tmp), before)

    def test_missing_directory_is_unavailable_and_not_created(self):
        from swarm.board import BoardUnavailable
        cfg = base_config(backend="file")
        cfg["file"] = {"path": str(self.tmp / "nope")}
        with self.assertRaises(BoardUnavailable):
            open_read_only(cfg)
        self.assertFalse((self.tmp / "nope").exists())

    def test_read_only_directory_can_be_read(self):
        if os.geteuid() == 0:
            self.skipTest("root can write anywhere")
        (self.dir / "lock").chmod(0o400)
        self.dir.chmod(0o500)
        self.addCleanup(self.dir.chmod, 0o700)
        with open_read_only(self.cfg) as b:
            self.assertEqual([m.message for m in b.recent_messages(10, job="J")], ["hello"])

    def test_transcript_bodies_refused(self):
        with open_read_only(self.cfg) as b:
            for call in (lambda: b._store.put_transcript_body("k", b"x"),
                         lambda: b._store.drop_transcript_body("k"),
                         lambda: b._store.put_image_body("0" * 64, b"x"),
                         lambda: b._store.drop_image_body("0" * 64)):
                with self.assertRaises(ReadOnlyBoard):
                    call()


class SqliteReadOnlyTests(_Base, unittest.TestCase):
    SKIP = ("-shm", "-wal")   # SQLite's own WAL side files (a reader may create/update them)

    def make_cfg(self):
        cfg = base_config(backend="sqlite")
        cfg["sqlite"] = {"path": str(self.tmp / "board.sqlite3")}
        return cfg

    @property
    def path(self) -> Path:
        return self.tmp / "board.sqlite3"

    def test_opens_with_mode_ro(self):
        with open_read_only(self.cfg) as b:
            with self.assertRaises(sqlite3.OperationalError):
                b._conn.execute("CREATE TABLE x (y)")

    def test_read_only_open_does_not_migrate(self):
        c = sqlite3.connect(self.path)
        version = c.execute("PRAGMA user_version").fetchone()[0]
        c.execute(f"PRAGMA user_version = {version - 1}")    # an older schema: setup would migrate
        c.commit()
        c.close()
        before = tree(self.tmp, self.SKIP)
        from swarm.board import BoardUnavailable
        with self.assertRaises(BoardUnavailable):
            open_read_only(self.cfg)
        self.assertEqual(tree(self.tmp, self.SKIP), before)
        c = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        self.assertEqual(c.execute("PRAGMA user_version").fetchone()[0], version - 1)
        c.close()

    def test_unwritable_file_can_be_read(self):
        if os.geteuid() == 0:
            self.skipTest("root can write a read-only file")
        self.path.chmod(0o400)
        self.addCleanup(self.path.chmod, 0o600)
        with open_read_only(self.cfg) as b:
            self.assertEqual([m.message for m in b.recent_messages(10, job="J")], ["hello"])


class OtherBackendsTests(unittest.TestCase):
    def test_write_methods_are_board_methods(self):
        from swarm.board.base import WRITE_METHODS, Board
        self.assertEqual([m for m in WRITE_METHODS if not callable(getattr(Board, m, None))], [])

    def test_cursor_read_without_advance_is_allowed(self):
        cfg = base_config(backend="file")
        tmp = Path(tempfile.mkdtemp(prefix="swarm-ro-", dir=os.environ.get("TMPDIR")))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        self.addCleanup(shutil.rmtree, tmp, True)
        cfg["file"] = {"path": str(tmp / "b")}
        setup_board(cfg, SMALL_POOL)
        with open_read_only(cfg) as b:
            self.assertEqual(b.read_unread("nobody", advance=False).messages, [])
            self.assertEqual(b.read_new("nobody", None, None, False), [])
            with self.assertRaises(ReadOnlyBoard):
                b.read_new("nobody")

    def test_memory_opens_plainly(self):
        cfg = base_config()
        cfg["memory"] = {"store": "read-only-test"}
        setup_board(cfg, SMALL_POOL)
        with open_read_only(cfg) as b:
            self.assertFalse(b.read_only)


if __name__ == "__main__":
    unittest.main()
