"""File backend specifics: real concurrency from separate PROCESSES, crash safety, retention,
change detection across processes and the sandbox path (an unwritable board directory).

The contract suite (test_board_contract.FileBoardContract) proves the semantics; this proves
they hold when many short-lived processes (one per hook call in real life) share the directory.
"""
from __future__ import annotations

import contextlib
import dataclasses
import io
import json
import lzma
import multiprocessing
import os
import queue
import signal
import tempfile
import time
import unittest
from pathlib import Path

from support import join_processes, SMALL_POOL, FileHarness, base_config, tq, posix_only  # noqa: F401  (sets sys.path)

from swarm import cli as swarm  # noqa: E402
from swarm.board import BoardError, BoardUnavailable, open_board, setup_board  # noqa: E402
from swarm.board import file as fileboard  # noqa: E402

CTX = multiprocessing.get_context("spawn")   # fresh interpreters: nothing shared but the files


def _cfg(path: str, **board) -> dict:
    cfg = base_config(backend="file", **board)
    cfg["file"] = {"path": path}
    return cfg


# ---- worker processes (top level: spawn imports this module by name) -----------------------

def _allocate(path, key, barrier, out):
    with open_board(_cfg(path)) as b:
        barrier.wait(timeout=120)
        out.put((key, b.allocate_name(key, "j")))


def _claim_judge(path, key, barrier, out):
    with open_board(_cfg(path)) as b:
        barrier.wait(timeout=120)
        out.put((key, b.claim_judge(key, "j")))


def _spawn(path, key, tries, barrier, out):
    with open_board(_cfg(path)) as b:
        barrier.wait(timeout=120)
        out.put((key, [b.reserve_spawn(key, "j", 2, 5).granted for _ in range(tries)]))


def _poster(path, who, n, barrier, out):
    ids = []
    barrier.wait(timeout=120)
    for i in range(n):
        with open_board(_cfg(path)) as b:   # a fresh board per post, like one CLI call each
            ids.append(b.post("j", who, f"{who} {i}").id)
            out.put((None, None))  # liveness progress; never used as a correctness oracle
    out.put((who, ids))



def _reader(path, key, expect, barrier, out, writers_done):
    """Drain through writer completion, including a final read after its publication."""
    got = []
    with open_board(_cfg(path)) as b:
        b.subscribe(messages_only=True)
        barrier.wait(timeout=120)
        while len(got) < expect:
            got += [m.id for m in b.read_new(agent_key=key)]
            if writers_done.is_set():
                while page := b.read_new(agent_key=key):
                    got += [m.id for m in page]
                break
            if len(got) < expect:
                b.wait_for_change(1)
    out.put((key, got))


def _post_forever(path, who, ready):
    i = 0
    while True:
        with open_board(_cfg(path)) as b:
            b.post("j", who, f"{who} {i}")
            ready.set()
            b.tool_started(who, "Bash")
        i += 1


def _run(target, argsets, timeout=120):
    """Start one process per args tuple (a barrier and a queue are appended), collect one result
    from each, join them all."""
    barrier, out = CTX.Barrier(len(argsets)), CTX.Queue()
    procs = [CTX.Process(target=target, args=(*a, barrier, out)) for a in argsets]
    for p in procs:
        p.start()
    try:
        results = dict(out.get(timeout=timeout) for _ in procs)
    finally:
        join_processes(procs, timeout)
    return results


# ---- tests ------------------------------------------------------------------------------------

class FileBoardProcessTests(unittest.TestCase):
    def setUp(self):
        self.h = FileHarness()
        self.addCleanup(self.h.close)
        self.h.reset()
        self.path = self.h.cfg["file"]["path"]
        self.b = self.h.board()
        self.addCleanup(self.b.close)

    def validate(self) -> tuple[dict, list[dict]]:
        """The files parse completely and are self-consistent; returns (state, messages)."""
        d = Path(self.path)
        state = fileboard.loads((d / "state.json").read_text())
        raw = (d / "messages.jsonl").read_bytes() if (d / "messages.jsonl").exists() else b""
        self.assertTrue(raw == b"" or raw.endswith(b"\n"), "torn last line")
        msgs = [fileboard.loads(line) for line in raw.decode().splitlines()]
        ids = [m["id"] for m in msgs]
        self.assertEqual(ids, sorted(set(ids)), "ids not unique and increasing")
        if ids:
            self.assertGreater(state["next_id"], ids[-1])
        active = [a["name"] for a in state["agents"].values() if a["left_at"] is None]
        self.assertEqual(len(active), len(set(active)), "duplicate active names")
        return state, msgs

    def test_concurrent_allocation_never_duplicates_an_active_name(self):
        res = _run(_allocate, [(self.path, f"k{i}") for i in range(12)])
        names = list(res.values())
        self.assertEqual(len(names), 12)
        self.assertEqual(len(set(names)), 12, names)
        self.assertTrue(set(SMALL_POOL["simpsons"] + SMALL_POOL["english"]) <= set(names))
        self.assertEqual({a.agent_key: a.name for a in self.b.agents("j")}, res)
        self.validate()

    def test_concurrent_judge_claims_seat_exactly_one(self):
        for i in range(10):
            self.b.allocate_name(f"k{i}", "j")
        res = _run(_claim_judge, [(self.path, f"k{i}") for i in range(10)])
        winners = [k for k, won in res.items() if won]
        self.assertEqual(len(winners), 1, res)
        self.assertEqual(self.b.job_status("j").judge, self.b.active_agent_name(winners[0]))

    def test_concurrent_spawns_never_exceed_the_caps(self):
        for i in range(8):
            self.b.allocate_name(f"k{i}", "j")
        res = _run(_spawn, [(self.path, f"k{i}", 3) for i in range(8)])
        per_agent = {k: sum(g) for k, g in res.items()}
        self.assertEqual(sum(per_agent.values()), 5, res)       # per_job cap
        self.assertTrue(all(n <= 2 for n in per_agent.values()), res)
        self.assertEqual(self.b.reserve_spawn("k0", "j", 99, 99).job_spawns, 6)  # count kept on disk

    def test_many_writers_unique_increasing_ids_and_readers_never_skip(self):
        writers, per, nreaders = 20, 50, 3
        for i in range(nreaders):
            self.b.allocate_name(f"r{i}", "j")
        barrier, out = CTX.Barrier(writers + nreaders), CTX.Queue()
        writers_done = CTX.Event()
        procs = [CTX.Process(target=_poster, args=(self.path, f"w{i}", per, barrier, out))
                 for i in range(writers)]
        procs += [CTX.Process(target=_reader, args=(self.path, f"r{i}", writers * per, barrier, out, writers_done))
                  for i in range(nreaders)]
        for p in procs:
            p.start()
        res = {}
        try:
            while len(res) < len(procs):
                who, ids = out.get(timeout=120)
                if who is None:  # a committed post; keep waiting while writers make progress
                    continue
                res[who] = ids
                if sum(k.startswith("w") for k in res) == writers:
                    writers_done.set()
        finally:
            writers_done.set()
            join_processes(procs)
            out.close()
        self.assertEqual([p.exitcode for p in procs], [0] * len(procs))
        all_ids = sorted(i for w in range(writers) for i in res[f"w{w}"])
        self.assertEqual(all_ids, list(range(1, writers * per + 1)))      # unique, no gaps
        for w in range(writers):
            ids = res[f"w{w}"]
            self.assertEqual(ids, sorted(ids))                            # each poster: increasing
        for r in range(nreaders):
            got = res[f"r{r}"]
            self.assertEqual(got, all_ids, f"reader r{r} skipped or repeated a message")
        state, msgs = self.validate()
        self.assertEqual(len(msgs), writers * per)
        self.assertEqual(state["next_id"], writers * per + 1)

    @posix_only("needs SIGKILL (POSIX signals)")
    def test_killed_writers_never_leave_a_corrupt_board(self):
        ready = [CTX.Event() for _ in range(6)]
        procs = [CTX.Process(target=_post_forever, args=(self.path, f"w{i}", ready[i])) for i in range(6)]
        for i in range(6):
            self.b.allocate_name(f"w{i}", "j")
        for p in procs:
            p.start()
        try:
            for event in ready:
                self.assertTrue(event.wait(60), "writer never completed its first post")
        finally:
            for p in procs:
                if p.is_alive():
                    os.kill(p.pid, signal.SIGKILL)
            join_processes(procs)
        # whatever instant they died at, the next transaction finds a sound board
        before = self.b.last_message_id("j")
        self.assertGreater(before, 0)
        new = self.b.post("j", "after", "still fine").id
        self.assertGreater(new, before)
        self.validate()

    def test_a_torn_append_is_cut_and_ids_are_not_reused(self):
        first = self.b.post("j", "A", "one").id
        with open(Path(self.path) / "messages.jsonl", "ab") as fh:
            fh.write(b'{"id":2,"job":"j","agent_na')      # a crash mid-append
        self.assertEqual([m.id for m in self.b.messages_after(0, "j")], [first])
        second = self.b.post("j", "A", "two").id
        self.assertEqual(second, first + 1)
        self.assertEqual([m.message for m in self.b.messages_after(0, "j")], ["one", "two"])
        self.validate()

    def test_a_crash_between_message_and_state_write_does_not_reuse_the_id(self):
        first = self.b.post("j", "A", "one").id
        state = Path(self.path) / "state.json"
        doc = fileboard.loads(state.read_text())
        doc["next_id"] = first            # as if the state write after the append never happened
        state.write_text(fileboard.dumps(doc))
        self.assertEqual(self.b.post("j", "A", "two").id, first + 1)

    def test_a_raising_transaction_persists_nothing(self):
        self.b.allocate_name("k1", "j")
        store = self.h.store
        with self.assertRaises(RuntimeError):
            with store.lock:
                store.agents["k1"]["name"] = "Changed"
                raise RuntimeError("boom")
        self.assertNotEqual(self.b.active_agent_name("k1"), "Changed")

    def test_retention_rewrites_messages_and_never_reuses_ids(self):
        ids = [self.b.post("j", "A", f"m{i}").id for i in range(3)]
        for i in ids:
            self.h.backdate_message(i, 8 * 86400)
        self.b.purge()
        self.assertEqual(self.b.messages_after(0), [])
        self.assertEqual((Path(self.path) / "messages.jsonl").read_bytes(), b"")
        self.assertEqual(self.b.post("j", "A", "new").id, ids[-1] + 1)
        self.validate()

    def test_reads_write_nothing(self):
        self.b.allocate_name("k1", "j")
        self.b.post("j", "A", "x")
        sig = self.h.store.signature(False)
        self.b.agents("j"), self.b.jobs(), self.b.recent_messages(5), self.b.sync_state("k1")
        self.b.read_new(agent_key="k1", advance=False)
        self.assertEqual(self.h.store.signature(False), sig)

    def test_newer_format_is_refused_not_overwritten(self):
        state = Path(self.path) / "state.json"
        state.write_text(json.dumps({"format": fileboard.FORMAT + 1}))
        with self.assertRaises(BoardError):
            self.b.ensure_job("j")
        self.assertEqual(json.loads(state.read_text())["format"], fileboard.FORMAT + 1)


class FileBoardChangeDetectionTests(unittest.TestCase):
    """wait_for_change sees writes made by OTHER processes."""

    def setUp(self):
        self.h = FileHarness()
        self.addCleanup(self.h.close)
        self.h.reset()
        self.path = self.h.cfg["file"]["path"]

    def _in_other_process(self, target, *args):
        p = CTX.Process(target=target, args=args)
        p.start()
        self.addCleanup(join_processes, [p])
        return p

    def test_a_post_from_another_process_wakes_tail_and_watch(self):
        with self.h.board() as tail, self.h.board() as watch:
            tail.subscribe(messages_only=True)
            watch.subscribe(messages_only=False)
            self.assertFalse(tail.wait_for_change(0.05))
            p = self._in_other_process(_post_once, self.path)
            self.assertTrue(tail.wait_for_change(30))
            self.assertTrue(watch.wait_for_change(30))
            p.join(60)
            self.assertFalse(tail.wait_for_change(0.05))   # drained: a burst counts once

    def test_the_first_post_creating_the_log_then_appending_is_one_change(self):
        # The first post creates messages.jsonl empty and appends to it: two steps a poller in
        # another process can see between (Windows CI did, ~7% of posts). Replay them in order.
        log = Path(self.path) / "messages.jsonl"
        with self.h.board() as tail:
            tail.subscribe(messages_only=True)
            log.unlink(missing_ok=True)
            fd = os.open(log, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0))
            try:
                self.assertFalse(tail.wait_for_change(0.05))   # created, still empty: nothing yet
                os.write(fd, b'{"id": 1}\n')
                os.fsync(fd)
            finally:
                os.close(fd)
            self.assertTrue(tail.wait_for_change(5))
            self.assertFalse(tail.wait_for_change(0.3))   # one change, not two

    def test_an_agent_change_wakes_watch_but_not_tail(self):
        with self.h.board() as tail, self.h.board() as watch:
            tail.subscribe(messages_only=True)
            watch.subscribe(messages_only=False)
            p = self._in_other_process(_join_once, self.path)
            self.assertTrue(watch.wait_for_change(30))
            p.join(60)
            self.assertFalse(tail.wait_for_change(0.2))


def _post_once(path):
    with open_board(_cfg(path)) as b:
        b.post("j", "Other", "hello")


def _join_once(path):
    with open_board(_cfg(path)) as b:
        b.allocate_name("kx", "j")


@unittest.skipIf(not hasattr(os, "geteuid") or os.geteuid() == 0,
                 "needs POSIX directory permissions (and a non-root user: root ignores them)")
class FileBoardSandboxTests(unittest.TestCase):
    """An agent that can't write the board directory (its sandbox) can't open the board: it gets
    BoardUnavailable, so `swarm post` spools, and the next process that can deliver does."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="swarm-file-sbx-", dir=os.environ.get("TMPDIR")))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.board_dir = self.tmp / "board"
        self.spool = self.tmp / "spool"
        self.config = self.tmp / "config.toml"
        self.config.write_text(f'[board]\nbackend = "file"\nspool_dir = {tq(self.spool)}\n'
                               f'[hook]\nmarker_dir = {tq(self.tmp / "markers")}\n'
                               f'[file]\npath = {tq(self.board_dir)}\n')
        self.cfg = swarm.load_config(self.config)
        setup_board(self.cfg, SMALL_POOL)
        self.lock = self.board_dir / "lock"
        self.addCleanup(self.lock.chmod, 0o600)

    def cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = swarm.main(["--config", str(self.config), *argv])
        return rc, out.getvalue(), err.getvalue()

    def test_unwritable_directory_is_board_unavailable(self):
        fresh = self.tmp / "ro"
        fresh.mkdir(mode=0o500)
        self.addCleanup(fresh.chmod, 0o700)
        cfg = _cfg(str(fresh / "board"))
        with self.assertRaises(BoardUnavailable) as cm:
            open_board(cfg)
        self.assertIsInstance(cm.exception.__cause__, OSError)

    def test_post_spools_when_the_board_is_read_only_and_is_delivered_later(self):
        with open_board(self.cfg) as b:
            sender = b.allocate_name("sandboxed", "J")
        self.lock.chmod(0o400)
        rc, out, _ = self.cli("post", "--job", "J", "--as", sender, "from the sandbox")
        self.assertEqual(rc, 0)
        self.assertTrue(out.startswith("queued (board not reachable from here: PermissionError)"), out)
        self.assertEqual(len(list(self.spool.glob("*.json"))), 1)
        self.lock.chmod(0o600)
        self.cli("purge")                                  # any command that opens the board flushes
        with open_board(self.cfg) as b:
            self.assertEqual([m.message for m in b.messages_after(0, "J")], ["from the sandbox"])
        self.assertEqual(list(self.spool.glob("*.json")), [])



def _post_in_child(path):
    """Post from a separate process (a FIFO test must not hang the test runner)."""
    try:
        with open_board(_cfg(path)) as b:
            b.post("j", "Lisa", "hello")
        return "posted"
    except BoardUnavailable as exc:
        return f"unavailable: {exc}"


class FileBoardPlantedLinkTests(unittest.TestCase):
    """The board directory is writable by a sandboxed agent, and the
    board is written by host-side processes outside the sandbox. A symlink, hard link or FIFO
    planted at any board file name must never be followed, written through, truncated or block:
    the board refuses (BoardUnavailable) or replaces the entry, and the target stays untouched."""

    PAYLOAD = 'hello $(id > PWNED)'

    def setUp(self):
        self.h = FileHarness()
        self.addCleanup(self.h.close)
        self.h.reset()
        self.dir = Path(self.h.cfg["file"]["path"])
        with self.h.board() as b:
            b.post("j", "Lisa", "first post")
            b.save_transcript(self._row())
        self.outside = self.h.root / "outside"
        self.outside.mkdir()

    def _row(self):
        import hashlib as _h
        from swarm.board import TranscriptImage, TranscriptRow
        img = b"\x89PNG\r\n\x1a\n" + b"x" * 50
        sha = _h.sha256(img).hexdigest()
        return TranscriptRow("j", "k1", "Lisa", "subagent", "h", None, False, 3, 0, "0" * 64, b"abc",
                             images=(TranscriptImage(sha, "image/png", len(img), img),))

    def _victim(self, live: bool) -> Path:
        v = self.outside / "victim_rc"
        v.unlink(missing_ok=True)
        if live:
            v.write_text("# victim\n")
        return v

    def _plant(self, name: str, kind: str) -> Path:
        """Replace board file `name` by a `kind` ("dangling", "live" symlink or "hardlink") to a
        victim outside the board; returns the victim."""
        target = self.dir / name
        victim = self._victim(live=kind != "dangling")
        if target.exists() or target.is_symlink():
            target.replace(target.with_name(target.name + ".old"))
        if kind == "hardlink":
            os.link(victim, target)
        else:
            target.symlink_to(victim)
        return victim

    def _check_victim(self, victim: Path, kind: str) -> None:
        if kind == "dangling":
            self.assertFalse(victim.exists() or victim.is_symlink(), "wrote through a dangling symlink")
        else:
            self.assertEqual(victim.read_text(), "# victim\n", "victim changed")

    def _post(self):
        with self.h.board() as b:
            b.post("j", "Lisa", self.PAYLOAD)

    def test_messages_jsonl_link_is_refused_and_not_written_through(self):
        # the lead's probe: messages.jsonl -> a shell rc file; the post must not land there
        for kind in ("dangling", "live", "hardlink"):
            with self.subTest(kind=kind):
                self.h.reset()
                with self.h.board() as b:
                    b.post("j", "Lisa", "first post")
                victim = self._plant("messages.jsonl", kind)
                with self.assertRaises(BoardUnavailable):
                    self._post()
                self._check_victim(victim, kind)
                (self.dir / "messages.jsonl").unlink()

    def test_state_json_link_is_refused_and_not_written_through(self):
        for kind in ("dangling", "live", "hardlink"):
            with self.subTest(kind=kind):
                victim = self._plant("state.json", kind)
                with self.assertRaises(BoardUnavailable):
                    self._post()
                self._check_victim(victim, kind)
                (self.dir / "state.json").unlink(missing_ok=True)
                (self.dir / "state.json.old").replace(self.dir / "state.json")

    def test_state_tmp_name_is_not_followed(self):
        # the old fixed temp name state.json.tmp was opened O_TRUNC through a planted symlink
        for kind in ("dangling", "live", "hardlink"):
            with self.subTest(kind=kind):
                victim = self._plant("state.json.tmp", kind)
                with self.h.board() as b:
                    b.allocate_name(f"k-{kind}", "j")        # a state change: state.json rewritten
                self._check_victim(victim, kind)
                (self.dir / "state.json.tmp").unlink()

    def test_transcript_index_link_is_refused(self):
        for kind in ("live", "hardlink"):
            with self.subTest(kind=kind):
                victim = self._plant("transcripts/index.json", kind)
                with self.assertRaises(BoardUnavailable), self.h.board() as b:
                    b.save_transcript(dataclasses.replace(self._row(), sha256="1" * 64))
                self._check_victim(victim, kind)
                (self.dir / "transcripts/index.json").unlink()
                (self.dir / "transcripts/index.json.old").replace(self.dir / "transcripts/index.json")

    def _mref(self, doc="d1", **kw):
        from swarm.board import MemoryRef
        fields = dict(document_id=doc, bank="notes", job="j", agent_key="k1", agent_name="Lisa", harness="claude",
                      host="h", session_id="s", tool_call_id="t", writer="note-tool",
                      excerpt=lzma.compress(b"excerpt\n"), raw_bytes=8, images=self._row().images)
        fields.update(kw)
        return MemoryRef(**fields)

    @posix_only("needs POSIX file modes (Windows has ACLs)")
    def test_memory_refs_persist_in_files(self):
        with self.h.board() as b:
            self.assertEqual(b.save_memory_ref(self._mref()), "inserted")
        index = self.dir / "transcripts" / "memory_refs.json"
        self.assertTrue(index.is_file())
        self.assertEqual(index.stat().st_mode & 0o777, 0o600)
        with self.h.board() as b:   # a new board: read back from the files
            [r] = b.memory_refs()
            self.assertEqual((r.document_id, r.agent_name, r.writer, r.stored_bytes > 0),
                             ("d1", "Lisa", "note-tool", True))
            self.assertEqual([i.sha256 for i in r.images], [self._row().images[0].sha256])
            self.assertEqual(b.memory_ref_excerpt("d1"), b"excerpt\n")
            self.assertEqual(b.save_memory_ref(self._mref(agent_key="k2")), "kept")
            self.assertEqual(b.delete_memory_refs(["d1"]), 1)
        with self.h.board() as b:
            self.assertEqual(b.memory_refs(), [])
            self.assertIsNone(b.memory_ref_excerpt("d1"))

    def test_memory_refs_index_link_is_refused(self):
        with self.h.board() as b:
            b.save_memory_ref(self._mref())
        for kind in ("live", "hardlink"):
            with self.subTest(kind=kind):
                victim = self._plant("transcripts/memory_refs.json", kind)
                with self.assertRaises(BoardUnavailable), self.h.board() as b:
                    b.save_memory_ref(self._mref("d2"))
                self._check_victim(victim, kind)
                (self.dir / "transcripts/memory_refs.json").unlink()
                (self.dir / "transcripts/memory_refs.json.old").replace(self.dir / "transcripts/memory_refs.json")

    def test_malformed_memory_refs_index_is_a_board_error(self):
        from swarm.board import BoardError
        with self.h.board() as b:
            b.save_memory_ref(self._mref())
        index = self.dir / "transcripts" / "memory_refs.json"
        good = fileboard.loads(index.read_text())

        def row(**change):
            r = json.loads(json.dumps(fileboard._enc(good["d1"])))
            for k, v in change.items():
                if v is KeyError:
                    r.pop(k)
                else:
                    r[k] = v
            return {"d1": r}
        img = json.loads(json.dumps(fileboard._enc(good["d1"]["images"][0])))
        cases = {
            "not json": "{not json",
            "a list": "[]",
            "row not an object": json.dumps({"d1": 5}),
            "missing key": json.dumps(row(agent_key=KeyError)),
            "missing images": json.dumps(row(images=KeyError)),
            "wrong type": json.dumps(row(raw_bytes="many")),
            "key is not the id": json.dumps({"d2": row()["d1"]}),
            "created_at not a time": json.dumps(row(created_at="yesterday")),
            "bad sha": json.dumps(row(images=[dict(img, sha256="../../x")])),
            "image missing mime": json.dumps(row(images=[{k: v for k, v in img.items() if k != "mime"}])),
            "images not a list": json.dumps(row(images="x")),
        }
        for label, text in cases.items():
            with self.subTest(label):
                index.write_text(text)
                with self.h.board() as b:
                    for call in (b.memory_refs, lambda: b.memory_ref_excerpt("d1"),
                                 lambda: b.transcript_image(img["sha256"]),
                                 lambda: b.delete_memory_refs(["d1"])):
                        with self.assertRaises(BoardError) as cm:
                            call()
                        self.assertNotIsInstance(cm.exception, (KeyError, ValueError))

    def test_image_link_is_not_read_or_written_through(self):
        sha = self._row().images[0].sha256
        victim = self._plant(f"transcripts/images/{sha}", "live")
        with self.h.board() as b:
            img = b.transcript_image(sha)
            self.assertTrue(img is None or img.data != victim.read_bytes())
        self._check_victim(victim, "live")

    def test_lock_link_is_refused(self):
        for kind in ("dangling", "live", "hardlink"):
            with self.subTest(kind=kind):
                victim = self._plant("lock", kind)
                with self.assertRaises(BoardUnavailable):
                    self.h.board()
                self._check_victim(victim, kind)
                (self.dir / "lock").unlink()
                (self.dir / "lock.old").replace(self.dir / "lock")

    def test_schema_version_link_is_not_followed(self):
        # schema_version() refuses it; setup replaces the entry (rename), never writes through it
        victim = self._plant("schema_version", "live")
        with self.assertRaises(BoardUnavailable):
            fileboard.FileBoard.schema_version(self.h.cfg)
        setup_board(self.h.cfg, SMALL_POOL)
        self._check_victim(victim, "live")
        self.assertFalse((self.dir / "schema_version").is_symlink())
        self.assertEqual(fileboard.FileBoard.schema_version(self.h.cfg), fileboard.SCHEMA_VERSION)

    @posix_only("needs os.mkfifo (POSIX FIFOs)")
    def test_fifo_at_messages_does_not_block(self):
        (self.dir / "messages.jsonl").unlink()
        os.mkfifo(self.dir / "messages.jsonl")
        with CTX.Pool(1) as pool:
            res = pool.apply_async(_post_in_child, (str(self.dir),))
            out = res.get(timeout=20)   # a blocking open would hang here
        self.assertTrue(out.startswith("unavailable"), out)

    def test_symlinked_board_directory_is_refused(self):
        real = self.dir
        link = self.h.root / "linked-board"
        link.symlink_to(real, target_is_directory=True)
        with self.assertRaises(BoardUnavailable):
            open_board(_cfg(str(link)))

    def test_line_separators_in_a_row_do_not_break_the_board(self):
        # messages.jsonl is split on "\n" only: a job name holding U+2028, U+0085 or \x1c (which
        # str.splitlines also splits on) must not tear the file into unparsable lines
        with self.h.board() as b:
            for job in ("j\u2028x", "j\x85y", "j\x1cz"):
                b.post(job, "Lisa", "hi")
        with self.h.board() as b:
            self.assertEqual(len(b.messages_after(0)), 4)

    def test_default_path_outside_state_dir(self):
        state = str(Path("~/.local/state/swarm").expanduser())
        self.assertFalse(str(fileboard.board_dir({})).startswith(state), fileboard.board_dir({}))
        self.assertEqual(fileboard.DEFAULT_PATH, "~/.local/share/swarm-board/board")

    def test_probe_fileboard_chain_blocked(self):
        # port of scratch-lead/probe_fileboard.sh: messages.jsonl -> dangling ~/.bash_aliases,
        # a post with $(...) must not create it (sourcing it would run the command)
        victim = self._plant("messages.jsonl", "dangling")
        with self.assertRaises(BoardUnavailable):
            self._post()
        self.assertFalse(victim.exists())


if __name__ == "__main__":
    unittest.main()
