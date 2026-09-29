"""SQLite backend specifics: concurrency across real PROCESSES on one database file (what the
hooks do: one short-lived process per tool call), setup/migration, the message cap, change
detection across processes, and the sandbox path (a file the process can't write -> spool).

The backend-agnostic behaviour is in test_board_contract.SqliteBoardContract; the hooks and the
CLI run on sqlite with SWARM_TEST_BACKEND=sqlite."""
from __future__ import annotations

import copy
import datetime as dt
import multiprocessing as mp
import os
import sqlite3
import stat
import time
import unittest
from unittest import mock

from support import SMALL_POOL, SqliteHarness, base_config  # noqa: F401  (sets sys.path)

from swarm.board import BoardUnavailable, open_board, setup_board  # noqa: E402

CTX = mp.get_context("spawn")   # fresh interpreters: no state shared with the test process
TIMEOUT = 120


# --------------------------------------------------------------------------- workers (child processes)

def _allocate(cfg, keys, start, out):
    start.wait(TIMEOUT)
    for key in keys:
        with open_board(cfg) as b:           # one board per call, like one hook per tool call
            out.put((key, b.allocate_name(key, "j")))


def _claim_judge(cfg, key, start, out):
    with open_board(cfg) as b:
        start.wait(TIMEOUT)
        out.put((key, b.claim_judge(key, "j")))


def _reserve(cfg, key, tries, start, out):
    start.wait(TIMEOUT)
    for _ in range(tries):
        with open_board(cfg) as b:
            g = b.reserve_spawn(key, "j", 2, 5)
            out.put((key, g.granted, g.agent_spawns, g.job_spawns))


def _post(cfg, writer, count, start, out):
    start.wait(TIMEOUT)
    for i in range(count):
        with open_board(cfg) as b:
            out.put((writer, b.post("j", writer, f"{writer} {i}").id))


def _read(cfg, key, expect, start, out):
    """Read with advance until `expect` messages were delivered; report every id in order."""
    start.wait(TIMEOUT)
    seen, deadline = [], time.monotonic() + TIMEOUT
    while len(seen) < expect and time.monotonic() < deadline:
        with open_board(cfg) as b:
            seen += [m.id for m in b.read_new(agent_key=key)]
    out.put((key, seen))


def _post_later(cfg, delay):
    time.sleep(delay)
    with open_board(cfg) as b:
        b.post("j", "Child", "from another process")


def _run(target, argsets):
    """Start one process per args tuple (each gets the shared start event and queue appended),
    release them together, and return everything they put on the queue."""
    start, out = CTX.Event(), CTX.Queue()
    procs = [CTX.Process(target=target, args=(*a, start, out)) for a in argsets]
    for p in procs:
        p.start()
    start.set()
    results = []
    deadline = time.monotonic() + TIMEOUT
    while any(p.is_alive() for p in procs) or not out.empty():
        try:
            results.append(out.get(timeout=0.5))
        except Exception:
            if time.monotonic() > deadline:
                break
    for p in procs:
        p.join(10)
    codes = [p.exitcode for p in procs]
    if codes != [0] * len(procs):
        raise AssertionError(f"worker exit codes {codes}")
    return results


class SqliteProcessConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.h = SqliteHarness()
        self.addCleanup(self.h.close)
        self.h.reset({"simpsons": [f"Simpson {i}" for i in range(20)], "english": ["Alice", "Bob"]})
        self.b = self.h.board()
        self.addCleanup(self.b.close)

    def test_many_processes_allocating_get_unique_names(self):
        # 8 processes x 6 keys = 48 agents for a pool of 22: the pool runs out and the
        # "<english> NNN" fallback is exercised under contention too.
        keys = [[f"p{p}k{k}" for k in range(6)] for p in range(8)]
        got = dict(_run(_allocate, [(self.h.cfg, ks) for ks in keys]))
        self.assertEqual(len(got), 48)
        self.assertEqual(len(set(got.values())), 48, sorted(got.values()))
        active = {a.agent_key: a.name for a in self.b.agents("j", include_departed=False)}
        self.assertEqual(active, got)
        self.assertTrue({f"Simpson {i}" for i in range(20)} <= set(got.values()))

    def test_concurrent_claim_judge_has_exactly_one_winner(self):
        keys = [f"k{i}" for i in range(10)]
        for k in keys:
            self.b.allocate_name(k, "j")
        wins = [k for k, won in _run(_claim_judge, [(self.h.cfg, k) for k in keys]) if won]
        self.assertEqual(len(wins), 1, wins)
        self.assertEqual(self.b.job_status("j").judge, self.b.active_agent_name(wins[0]))
        judges = [a for a in self.b.agents("j") if a.role == "judge"]
        self.assertEqual([a.agent_key for a in judges], wins)

    def test_concurrent_reserve_spawn_never_exceeds_the_caps(self):
        keys = [f"k{i}" for i in range(6)]
        for k in keys:
            self.b.allocate_name(k, "j")
        results = _run(_reserve, [(self.h.cfg, k, 3) for k in keys])   # 18 tries, job cap 5
        self.assertEqual(len(results), 18)
        granted = [r for r in results if r[1]]
        self.assertEqual(len(granted), 5)
        self.assertEqual(sorted(r[3] for r in granted), [1, 2, 3, 4, 5])  # each grant its own count
        per_agent = {k: sum(1 for r in granted if r[0] == k) for k in keys}
        self.assertTrue(all(n <= 2 for n in per_agent.values()), per_agent)
        spawns = self.h._db().execute("SELECT spawns FROM jobs WHERE job = 'j'").fetchone()[0]
        self.assertEqual(spawns, 5)

    def test_concurrent_posts_have_unique_ordered_ids_and_readers_skip_nothing(self):
        writers, per_writer, readers = 6, 40, 3
        reader_keys = [f"r{i}" for i in range(readers)]
        with self.h.board(join_history=0) as b:
            for k in reader_keys:
                b.allocate_name(k, "j")
        cfg = copy.deepcopy(self.h.cfg)
        cfg["board"]["read_limit"] = 7          # small pages: readers page while writers write
        start, out = CTX.Event(), CTX.Queue()
        procs = [CTX.Process(target=_post, args=(cfg, f"W{w}", per_writer, start, out)) for w in range(writers)]
        procs += [CTX.Process(target=_read, args=(cfg, k, writers * per_writer, start, out)) for k in reader_keys]
        for p in procs:
            p.start()
        start.set()
        posted, reads = [], {}
        deadline = time.monotonic() + TIMEOUT
        while (len(posted) < writers * per_writer or len(reads) < readers) and time.monotonic() < deadline:
            try:
                item = out.get(timeout=1)
            except Exception:
                continue
            if isinstance(item[1], list):
                reads[item[0]] = item[1]
            else:
                posted.append(item)
        for p in procs:
            p.join(10)
        self.assertEqual([p.exitcode for p in procs], [0] * len(procs))
        ids = [i for _, i in posted]
        self.assertEqual(len(ids), writers * per_writer)
        self.assertEqual(len(set(ids)), len(ids))                      # unique across processes
        for w in range(writers):                                       # increasing per writer
            mine = [i for name, i in posted if name == f"W{w}"]
            self.assertEqual(mine, sorted(mine))
        for key, seen in reads.items():
            self.assertEqual(seen, sorted(ids), f"reader {key} skipped or repeated a message")


class SqliteBackendTests(unittest.TestCase):
    def setUp(self):
        self.h = SqliteHarness()
        self.addCleanup(self.h.close)
        self.h.reset()
        self.b = self.h.board()
        self.addCleanup(self.b.close)

    def test_wal_mode_and_schema_version(self):
        db = self.h._db()
        self.assertEqual(db.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        self.assertGreaterEqual(db.execute("PRAGMA user_version").fetchone()[0], 1)

    def test_timestamps_are_stored_utc_and_returned_aware(self):
        self.b.allocate_name("k", "j")
        self.b.post("j", "A", "hi")
        raw = self.h._db().execute("SELECT created_at FROM messages").fetchone()[0]
        self.assertTrue(raw.endswith("+00:00"), raw)
        m = self.b.recent_messages(1, "j")[0]
        self.assertEqual(m.created_at.utcoffset(), dt.timedelta(0))
        self.assertLess(abs((m.created_at - dt.datetime.now(dt.timezone.utc)).total_seconds()), 60)

    def test_uninitialised_or_missing_file_is_unavailable(self):
        cfg = base_config(backend="sqlite")
        cfg["sqlite"] = {"path": str(self.h.dir / "missing.sqlite3")}
        with self.assertRaises(BoardUnavailable) as cm:
            open_board(cfg)
        self.assertIsInstance(cm.exception.__cause__, sqlite3.Error)
        self.assertFalse((self.h.dir / "missing.sqlite3").exists())   # opening never creates it
        empty = self.h.dir / "empty.sqlite3"
        sqlite3.connect(empty).close()
        cfg["sqlite"] = {"path": str(empty)}
        with self.assertRaises(BoardUnavailable) as cm:
            open_board(cfg)
        self.assertIn("swarm init", str(cm.exception))

    def test_setup_creates_the_directory_and_says_so_once(self):
        cfg = base_config(backend="sqlite")
        cfg["sqlite"] = {"path": str(self.h.dir / "sub" / "dir" / "b.sqlite3")}
        first = setup_board(cfg, SMALL_POOL)
        self.assertEqual(len(first.notes), 1)
        self.assertIn("created board database", first.notes[0])
        self.assertEqual(setup_board(cfg, SMALL_POOL).notes, ())

    def test_setup_again_keeps_data(self):
        self.b.allocate_name("k1", "j")
        self.b.post("j", "Other", "kept")
        setup_board(self.h.cfg, {"simpsons": ["Lisa Simpson"], "english": []})
        self.assertEqual([m.message for m in self.b.recent_messages(5, "j")], ["kept"])
        self.assertIsNotNone(self.b.active_agent_name("k1"))

    def test_setup_adds_missing_migration_columns(self):
        from swarm.board import sqlite as sq
        with mock.patch.object(sq, "MIGRATIONS", (("jobs", "extra_note", "TEXT"),)):
            setup_board(self.h.cfg, SMALL_POOL)
            setup_board(self.h.cfg, SMALL_POOL)       # idempotent
        cols = {r[1] for r in self.h._db().execute("PRAGMA table_info(jobs)")}
        self.assertIn("extra_note", cols)

    def test_a_version_1_board_needs_init_which_adds_closed_by(self):
        self.b.open_job("j", "d", None, None, "me")
        self.b.close()
        db = self.h._db()
        db.execute("ALTER TABLE jobs DROP COLUMN closed_by")   # as the previous release left it
        db.execute("PRAGMA user_version = 1")
        with self.assertRaises(BoardUnavailable) as cm:
            self.h.board()
        self.assertIn("swarm init", str(cm.exception))
        setup_board(self.h.cfg, SMALL_POOL)
        self.assertIn("closed_by", {r[1] for r in db.execute("PRAGMA table_info(jobs)")})
        with self.h.board() as b:
            self.assertEqual((b.job_status("j").description, b.job_status("j").closed_by), ("d", None))
            self.assertTrue(b.close_job("j", "completed", "ok", closed_by="me"))
            self.assertEqual(b.job_status("j").closed_by, "me")

    def test_a_board_without_verdict_next_needs_init_and_keeps_its_verdict(self):
        self.b.open_job("j", "d", None, None, "me", goal="g")
        self.b.allocate_name("jk", "j")
        self.b.claim_judge("jk", "j")
        judge = self.b.active_agent_name("jk")
        self.assertTrue(self.b.record_verdict("j", judge, "not_met", "old reason"))
        self.b.close()
        db = self.h._db()
        db.execute("ALTER TABLE jobs DROP COLUMN verdict_next")   # as the previous release left it
        db.execute("PRAGMA user_version = 9")
        with self.assertRaises(BoardUnavailable):
            self.h.board()
        setup_board(self.h.cfg, SMALL_POOL)
        with self.h.board() as b:
            s = b.job_status("j")
            self.assertEqual((s.verdict, s.verdict_reason, s.verdict_next), ("not_met", "old reason", None))
            self.assertTrue(b.record_verdict("j", judge, "not_met", "r", "n"))
            self.assertEqual(b.job_status("j").verdict_next, "n")

    def test_message_cap_is_a_check_sized_at_first_setup(self):
        with self.h.board(message_max_chars=500) as b:   # a cap raised after init: CHECK still 200
            with self.assertRaises(sqlite3.IntegrityError):
                b.post("j", "A", "x" * 300)
        self.assertTrue(self.b.post("j", "A", "é" * 250).truncated)   # characters, not bytes
        self.assertEqual(len(self.b.recent_messages(1, "j")[0].message), 200)

    def test_ids_never_reused_after_purging_the_newest(self):
        first = self.b.post("j", "A", "old").id
        self.h.backdate_message(first, 8 * 86400)
        self.b.purge()
        self.assertEqual(self.b.last_message_id(), 0)
        self.assertGreater(self.b.post("j", "A", "new").id, first)

    def test_wait_for_change_sees_a_post_from_another_process(self):
        self.b.subscribe(messages_only=True)
        self.assertFalse(self.b.wait_for_change(0.2))
        p = CTX.Process(target=_post_later, args=(self.h.cfg, 0.3))
        p.start()
        try:
            self.assertTrue(self.b.wait_for_change(TIMEOUT))
        finally:
            p.join(TIMEOUT)
        self.assertFalse(self.b.wait_for_change(0.2))   # drained

    def test_messages_only_ignores_agent_changes(self):
        self.b.allocate_name("k", "j")
        with self.h.board() as watcher:
            watcher.subscribe(messages_only=True)
            self.b.tool_started("k", "Bash")
            self.assertFalse(watcher.wait_for_change(0.3))


class SqliteSandboxSpoolTests(unittest.TestCase):
    """A process that can read the board file but not write it (a sandboxed agent) must get
    BoardUnavailable at open, so `swarm post` spools, and the hook delivers the post later."""

    def setUp(self):
        import test_hooks_cli
        env = test_hooks_cli.Env
        with mock.patch("support.E2E_BACKEND", "sqlite"):
            self.env = type("E", (env,), {"runTest": lambda s: None})()
            self.env.setUp()
        self.addCleanup(self.env.doCleanups)
        self.path = self.env.h.path

    def test_unwritable_file_spools_and_hook_delivers(self):
        if os.geteuid() == 0:
            self.skipTest("root can write a read-only file")
        env = self.env
        env.cli("activate", "--job", "J")
        env.h.close()      # no connection open: the WAL side files are gone
        self.assertEqual(sorted(p.name for p in self.path.parent.iterdir()), [self.path.name])
        # What a sandbox does to the default path: the file and its directory are readable,
        # not writable (so SQLite can't create the -wal/-shm files either).
        ro = stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH
        self.path.chmod(ro)
        self.path.parent.chmod(ro | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        try:
            with self.assertRaises(BoardUnavailable):
                open_board(env.cfg)
            rc, out, _ = env.cli("post", "--job", "J", "--as", "Homer Simpson", "from", "the", "sandbox")
            self.assertEqual(rc, 0)
            self.assertTrue(out.startswith("queued (board not reachable from here: OperationalError)"), out)
            self.assertEqual(len(list(env.spool_dir.glob("*.json"))), 1)
        finally:
            self.path.parent.chmod(stat.S_IRWXU)
            self.path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        env.hook("start", agent_id="a1")        # the hook runs outside the sandbox: it delivers
        with open_board(env.cfg) as b:
            self.assertEqual([m.message for m in b.recent_messages(10, "J")], ["from the sandbox"])
        self.assertEqual(list(env.spool_dir.glob("*.json")), [])


if __name__ == "__main__":
    unittest.main()
