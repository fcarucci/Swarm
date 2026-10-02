"""Resume markers: how a headless replacement's session is bound to its job and predecessor."""
from __future__ import annotations

import json
import os
import stat
import threading
import time
from unittest import mock
import shutil
import tempfile
import unittest
from pathlib import Path

from support import base_config, posix_only  # noqa: F401

from swarm import cli
from swarm.supervisor import markers


class ResumeMarkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="swarm-mk-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.cfg = base_config()
        self.cfg["hook"]["marker_dir"] = str(self.tmp / "markers")

    def test_path_is_a_job_marker_deactivate_removes(self):
        p = markers.resume_marker_path(self.cfg, "my job", 7)
        self.assertEqual(p.name, f"{cli.safe_job('my job')}--resume-r7.json")

    @posix_only("needs POSIX file modes (Windows has ACLs)")
    def test_write_unbound_then_bind_once(self):
        p = markers.write_resume_marker(self.cfg, "J", 3, resume_of="old-key", name="Homer Simpson",
                                        harness="codex")
        m = json.loads(p.read_text())
        self.assertEqual(m, {"job": "J", "session_id": None, "adopt_running": False,
                             "resume": {"agent_key": None, "resume_of": "old-key",
                                        "name": "Homer Simpson", "harness": "codex", "restart_id": 3}})
        self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o600)
        self.assertIsNone(markers.resume_binding(self.cfg, "thread-1"))
        self.assertTrue(markers.bind_session(p, "thread-1"))
        self.assertFalse(markers.bind_session(p, "thread-2"))   # first binding wins
        b = markers.resume_binding(self.cfg, "thread-1")
        self.assertEqual((b["resume"]["agent_key"], b["_path"]), ("thread-1", p))

    def test_prebound_claude_marker(self):
        p = markers.write_resume_marker(self.cfg, "J", 4, resume_of="k", name="Bart Simpson",
                                        harness="claude", session_id="uuid-1")
        self.assertEqual(markers.resume_binding(self.cfg, "uuid-1")["resume"]["agent_key"], "uuid-1")

    def test_orchestrator_markers_are_not_resume_bindings(self):
        d = self.tmp / "markers"
        d.mkdir(parents=True)
        (d / "J.json").write_text(json.dumps({"job": "J", "session_id": "s1"}))
        self.assertIsNone(markers.resume_binding(self.cfg, "s1"))

    def test_remove(self):
        p = markers.write_resume_marker(self.cfg, "J", 5, resume_of="k", name="N", harness="claude",
                                        session_id="u")
        self.assertTrue(markers.remove_resume_marker(p))
        self.assertFalse(p.exists())
        self.assertIsNone(markers.resume_binding(self.cfg, "u"))

    def test_bind_on_removed_marker_is_false(self):
        p = markers.write_resume_marker(self.cfg, "J", 6, resume_of="k", name="N", harness="codex")
        markers.remove_resume_marker(p)
        self.assertFalse(markers.bind_session(p, "t"))

    def test_new_marker_appears_complete_and_private(self):
        p = markers.write_resume_marker(self.cfg, "J", 8, resume_of="k", name="N", harness="codex")
        self.assertEqual(json.loads(p.read_text())["resume"]["restart_id"], 8)
        self.assertEqual(sorted(x.name for x in p.parent.iterdir()), sorted([p.name, markers.LOCK_NAME]))   # no temp file left

    @posix_only("needs POSIX file modes (Windows has ACLs)")
    def test_rewrite_of_an_existing_marker_waits_for_its_lock(self):
        p = markers.write_resume_marker(self.cfg, "J", 9, resume_of="k", name="N", harness="codex")
        before = p.read_text()
        with cli.locked_marker(p, 1.0) as fh, mock.patch.object(markers, "LOCK_WAIT", 0.2):
            self.assertIsNotNone(fh)
            with self.assertRaises(TimeoutError):
                markers.write_resume_marker(self.cfg, "J", 9, resume_of="k2", name="N", harness="codex")
        self.assertEqual(p.read_text(), before)
        p2 = markers.write_resume_marker(self.cfg, "J", 9, resume_of="k2", name="N", harness="codex")
        self.assertEqual(json.loads(p2.read_text())["resume"]["resume_of"], "k2")
        self.assertEqual(stat.S_IMODE(p2.stat().st_mode), 0o600)

    @posix_only("needs POSIX file modes (Windows has ACLs)")
    def test_bind_keeps_the_marker_private(self):
        p = markers.write_resume_marker(self.cfg, "J", 10, resume_of="k", name="N", harness="codex")
        self.assertTrue(markers.bind_session(p, "t"))
        self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o600)
        self.assertEqual(sorted(x.name for x in p.parent.iterdir()), sorted([p.name, markers.LOCK_NAME]))

    def test_marker_removed_after_file_exists_is_not_recreated(self):
        # A removal outside the supervisor (deactivate) lands between "it exists" and the rewrite:
        # the marker stays gone.
        p = markers.write_resume_marker(self.cfg, "J", 11, resume_of="k", name="N", harness="codex")

        def exists_then_removed(path, text):
            cli.remove_marker(path, wait=1.0)
            raise FileExistsError(path)
        with mock.patch.object(markers, "_create", exists_then_removed):
            with self.assertRaises(FileNotFoundError):
                markers.write_resume_marker(self.cfg, "J", 11, resume_of="k", name="N", harness="codex")
        self.assertFalse(p.exists())

    def test_remove_racing_a_create_never_leaves_a_marker(self):
        # remove_resume_marker waits for a create in flight, then removes what it created.
        real = markers._create
        entered = threading.Event()

        def slow_create(path, text):
            entered.set()
            time.sleep(0.3)
            real(path, text)
        p = markers.resume_marker_path(self.cfg, "J", 12)
        with mock.patch.object(markers, "_create", slow_create):
            t = threading.Thread(target=markers.write_resume_marker, args=(self.cfg, "J", 12),
                                 kwargs={"resume_of": "k", "name": "N", "harness": "codex"})
            t.start()
            self.assertTrue(entered.wait(5))
            self.assertTrue(markers.remove_resume_marker(p))
            t.join(5)
        self.assertFalse(p.exists())

    @posix_only("needs POSIX file modes (Windows has ACLs)")
    def test_the_lock_file_is_private_and_not_a_marker(self):
        markers.write_resume_marker(self.cfg, "J", 13, resume_of="k", name="N", harness="codex")
        lock = markers.lock_path(self.tmp / "markers")
        self.assertEqual(stat.S_IMODE(lock.stat().st_mode), 0o600)
        self.assertFalse(lock.match("*.json"))

    def _lock(self):
        d = self.tmp / "markers"
        d.mkdir(parents=True, exist_ok=True)
        return markers.lock_path(d)

    @posix_only("needs Unix uids (no ownership checks on Windows)")
    def test_a_foreign_lock_file_is_refused(self):
        self._lock().touch(mode=0o600)
        with mock.patch.object(markers.os, "getuid", return_value=os.getuid() + 1):
            with self.assertRaises(PermissionError):
                markers.write_resume_marker(self.cfg, "J", 14, resume_of="k", name="N", harness="codex")
        self.assertFalse(markers.resume_marker_path(self.cfg, "J", 14).exists())

    @posix_only("needs os.mkfifo (POSIX FIFOs)")
    def test_a_lock_that_is_not_a_regular_file_is_refused(self):
        os.mkfifo(self._lock())
        with self.assertRaises(PermissionError):
            markers.write_resume_marker(self.cfg, "J", 15, resume_of="k", name="N", harness="codex")
        self.assertFalse(markers.resume_marker_path(self.cfg, "J", 15).exists())

    def test_a_symlinked_lock_is_refused(self):
        target = self.tmp / "elsewhere"
        target.write_text("")
        self._lock().symlink_to(target)
        with self.assertRaises(OSError):
            markers.write_resume_marker(self.cfg, "J", 16, resume_of="k", name="N", harness="codex")
        self.assertFalse(markers.resume_marker_path(self.cfg, "J", 16).exists())

    @posix_only("needs POSIX file modes (Windows has ACLs)")
    def test_a_loose_lock_file_is_tightened(self):
        lock = self._lock()
        lock.touch()
        os.chmod(lock, 0o666)
        markers.write_resume_marker(self.cfg, "J", 17, resume_of="k", name="N", harness="codex")
        self.assertEqual(stat.S_IMODE(lock.stat().st_mode), 0o600)


def _within(test, fn, seconds=1.0):
    """fn() run in a thread: its result, failing the test if it doesn't return within `seconds`
    (a FIFO opened for reading blocks until a writer comes)."""
    box = {}

    def go():
        box["v"] = fn()
    t = threading.Thread(target=go, daemon=True)
    t.start()
    t.join(seconds)
    if t.is_alive():
        for fifo in getattr(test, "fifos", []):   # unblock the stuck reader
            try:
                os.close(os.open(fifo, os.O_RDWR | os.O_NONBLOCK))
            except OSError:
                pass
        t.join(2)
        test.fail(f"{fn} blocked for more than {seconds}s")
    return box.get("v")


class PlantedMarkerTests(unittest.TestCase):
    """The marker dir is sandbox-writable. A FIFO, a symlink, a hard
    link or an oversized file named like a resume marker never blocks and is never read."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="swarm-mk-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.cfg = base_config()
        self.cfg["hook"]["marker_dir"] = str(self.tmp / "markers")
        self.dir = self.tmp / "markers"
        self.dir.mkdir()
        self.fifos = []
        self.good = markers.write_resume_marker(self.cfg, "J", 9, resume_of="k", name="N", harness="codex")
        markers.set_resume_token(self.good, "tok-good")
        self.victim = self.tmp / "victim.json"
        self.victim.write_text(json.dumps({"job": "J", "session_id": "sess-v",
                                           "resume": {"agent_key": "sess-v", "token": "tok-v",
                                                      "resume_of": "k", "name": "N", "harness": "codex",
                                                      "restart_id": 1}}))

    def fifo(self, name):
        p = self.dir / name
        os.mkfifo(p)
        self.fifos.append(p)
        return p

    @posix_only("needs os.mkfifo (POSIX FIFOs)")
    def test_fifo_marker_does_not_block(self):
        self.fifo("J--resume-r1.json")
        self.assertIsNone(_within(self, lambda: markers.resume_binding(self.cfg, "sess-x")))
        self.assertIsNone(_within(self, lambda: markers.resume_by_token(self.cfg, "tok-x", "sess-x")))
        # the good marker is still found past the FIFO
        b = _within(self, lambda: markers.resume_by_token(self.cfg, "tok-good", "thread-1"))
        self.assertEqual(b["resume"]["agent_key"], "thread-1")
        self.assertIsNone(_within(self, lambda: markers.read_marker(self.dir / "J--resume-r1.json")))

    def test_links_and_oversized_markers_are_skipped(self):
        (self.dir / "J--resume-r2.json").symlink_to(self.victim)
        os.link(self.victim, self.dir / "J--resume-r3.json")
        big = json.loads(self.victim.read_text())
        big["pad"] = "x" * (markers.MARKER_MAX + 1)
        big["session_id"] = "sess-big"
        (self.dir / "J--resume-r4.json").write_text(json.dumps(big))
        self.assertIsNone(markers.resume_binding(self.cfg, "sess-v"))
        self.assertIsNone(markers.resume_by_token(self.cfg, "tok-v", "sess-v"))
        self.assertIsNone(markers.resume_binding(self.cfg, "sess-big"))
        for n in (2, 3, 4):
            self.assertIsNone(markers.read_marker(self.dir / f"J--resume-r{n}.json"))
        self.assertEqual(json.loads(self.victim.read_text())["session_id"], "sess-v")

    def test_a_symlinked_marker_dir_is_not_scanned(self):
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "J--resume-r5.json").write_text(self.victim.read_text())
        cfg = dict(self.cfg, hook=dict(self.cfg["hook"], marker_dir=str(self.tmp / "linked")))
        (self.tmp / "linked").symlink_to(elsewhere)
        self.assertIsNone(markers.resume_binding(cfg, "sess-v"))
        self.assertIsNone(markers.resume_by_token(cfg, "tok-v", "sess-v"))

    def test_read_marker_reads_a_real_marker(self):
        self.assertEqual(markers.read_marker(self.good)["resume"]["restart_id"], 9)
        self.assertIsNone(markers.read_marker(self.dir / "missing.json"))

    @posix_only("needs os.mkfifo (POSIX FIFOs)")
    def test_a_marker_swapped_for_a_fifo_blocks_no_writer_or_remover(self):
        """The runner binds, tokens and removes its own marker; a sandbox swaps it for a FIFO."""
        os.unlink(self.good)
        self.fifo(self.good.name)
        self.assertFalse(_within(self, lambda: markers.bind_session(self.good, "thread-1")))
        self.assertFalse(_within(self, lambda: markers.set_resume_token(self.good, "t")))
        self.assertTrue(_within(self, lambda: markers.remove_resume_marker(self.good)))
        self.assertFalse(os.path.lexists(self.good))

    def test_a_marker_swapped_for_a_symlink_is_removed_not_followed(self):
        os.unlink(self.good)
        self.good.symlink_to(self.victim)
        before = self.victim.read_text()
        self.assertFalse(markers.bind_session(self.good, "thread-1"))
        self.assertFalse(markers.set_resume_token(self.good, "t"))
        self.assertTrue(markers.remove_resume_marker(self.good))
        self.assertFalse(os.path.lexists(self.good))
        self.assertEqual(self.victim.read_text(), before)
