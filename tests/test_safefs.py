"""safefs: every host-side file in a place a sandbox can write
is reached through a checked directory descriptor and opened without following links, blocking
or reading unbounded data. Each case runs under a temporary HOME."""
from __future__ import annotations

import fcntl
import os
import shutil
import stat
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from support import ROOT, home_env  # noqa: F401

from swarm import paths, safefs  # noqa: E402


def within(fn, timeout: float = 1.0):
    """Run fn in a thread; fail (without hanging the suite) if it hasn't returned in `timeout`.
    Returns ("ok", value) or ("raised", exception)."""
    box = {}

    def run():
        try:
            box["r"] = ("ok", fn())
        except BaseException as exc:   # noqa: BLE001 - reported to the test
            box["r"] = ("raised", exc)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        raise AssertionError(f"{fn} blocked for more than {timeout}s")
    return box["r"]


class HomeCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="swarm-safefs-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = self.tmp / "home"
        self.home.mkdir()
        p = mock.patch.dict(os.environ, {**home_env(self.home)})
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.base = self.home / ".local" / "share" / "swarm" / "host"
        self.outside = self.tmp / "victim"
        self.outside.write_text("precious\n")

    def open(self, **kw):
        fd = safefs.open_base(self.base, **kw)
        self.addCleanup(os.close, fd)
        return fd

    def untouched(self):
        self.assertEqual(self.outside.read_text(), "precious\n")


class OpenBaseTests(HomeCase):
    def test_creates_the_path_under_home_0700(self):
        fd = self.open(strict_mode=0o700)
        st = os.fstat(fd)
        self.assertTrue(stat.S_ISDIR(st.st_mode))
        self.assertEqual(stat.S_IMODE(st.st_mode), 0o700)
        self.assertEqual(os.path.samestat(st, os.stat(self.base)), True)

    def test_create_false_on_a_missing_dir(self):
        with self.assertRaises(FileNotFoundError):
            safefs.open_base(self.base, create=False)
        self.assertFalse((self.home / ".local").exists())

    def test_a_symlinked_component_is_refused(self):
        (self.home / ".local" / "share").mkdir(parents=True)
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir()
        (self.home / ".local" / "share" / "swarm").symlink_to(elsewhere)
        with self.assertRaises(safefs.UnsafePathError):
            safefs.open_base(self.base)
        self.assertEqual(list(elsewhere.iterdir()), [])

    def test_a_symlinked_final_dir_is_refused(self):
        self.base.parent.mkdir(parents=True)
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir(mode=0o700)
        self.base.symlink_to(elsewhere)
        with self.assertRaises(safefs.UnsafePathError):
            safefs.open_base(self.base, strict_mode=0o700)

    def test_a_file_in_place_of_a_dir_is_refused(self):
        self.base.parent.mkdir(parents=True)
        self.base.write_text("")
        with self.assertRaises(safefs.UnsafePathError):
            safefs.open_base(self.base)

    def test_a_loose_mode_is_refused_only_when_strict(self):
        self.base.mkdir(parents=True, mode=0o755)
        self.base.chmod(0o755)
        with self.assertRaises(safefs.UnsafePathError):
            safefs.open_base(self.base, strict_mode=0o700)
        os.close(safefs.open_base(self.base))

    def test_another_owner_is_refused(self):
        self.open()
        with mock.patch("os.getuid", return_value=os.getuid() + 1):
            with self.assertRaises(safefs.UnsafePathError):
                safefs.open_base(self.base)

    def test_unsafe_path_error_is_an_oserror(self):
        # hook code catches OSError around its best-effort writes
        self.assertTrue(issubclass(safefs.UnsafePathError, OSError))

    def test_relative_or_dotdot_paths_are_refused(self):
        with self.assertRaises(ValueError):
            safefs.open_base(Path("relative/dir"))
        with self.assertRaises(ValueError):
            safefs.open_base(self.home / "a" / ".." / ".." / "escape")

    def test_tilde_is_expanded(self):
        fd = safefs.open_base("~/.local/share/swarm/host")
        self.addCleanup(os.close, fd)
        self.assertTrue(os.path.samestat(os.fstat(fd), os.stat(self.base)))

    def test_a_path_outside_home_under_tmp(self):
        # the spool/marker dirs may live under the sticky /tmp: every component below it is ours
        target = self.tmp / "spool" / "q"
        fd = safefs.open_base(target, strict_mode=0o700)
        self.addCleanup(os.close, fd)
        self.assertTrue(target.is_dir())
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o700)

    def test_a_symlink_outside_home_is_refused(self):
        real = self.tmp / "real"
        real.mkdir()
        (self.tmp / "link").symlink_to(real)
        with self.assertRaises(safefs.UnsafePathError):
            safefs.open_base(self.tmp / "link" / "q")
        self.assertEqual(list(real.iterdir()), [])

    def test_another_users_dir_outside_home_is_refused(self):
        target = self.tmp / "spool"
        target.mkdir()
        with mock.patch("os.getuid", return_value=os.getuid() + 1):
            with self.assertRaises(safefs.UnsafePathError):
                safefs.open_base(target)

    def test_a_group_or_world_writable_own_dir_is_refused(self):
        self.base.mkdir(parents=True, mode=0o700)
        for mode in (0o770, 0o707, 0o777):
            with self.subTest(mode=oct(mode)):
                self.base.parent.chmod(mode)
                with self.assertRaises(safefs.UnsafePathError):
                    safefs.open_base(self.base)
        self.base.parent.chmod(0o1777)   # sticky: entries can't be swapped by others
        os.close(safefs.open_base(self.base))
        self.base.parent.chmod(0o755)
        self.base.chmod(0o775)
        with self.assertRaises(safefs.UnsafePathError):
            safefs.open_base(self.base)
        d = safefs.open_base(self.base.parent)
        self.addCleanup(os.close, d)
        with self.assertRaises(safefs.UnsafePathError):
            safefs.open_sub(d, "host")

    def test_open_sub(self):
        d = self.open()
        sub = safefs.open_sub(d, "enrolled", strict_mode=0o700)
        self.addCleanup(os.close, sub)
        self.assertEqual(stat.S_IMODE((self.base / "enrolled").stat().st_mode), 0o700)
        os.symlink(self.tmp, self.base / "evil")
        with self.assertRaises(safefs.UnsafePathError):
            safefs.open_sub(d, "evil")
        with self.assertRaises(ValueError):
            safefs.open_sub(d, "../x")

    def test_dir_fd_context(self):
        with safefs.dir_fd(self.base) as d:
            safefs.append(d, "log", "x\n")
        self.assertEqual((self.base / "log").read_text(), "x\n")


class PlantedEntryTests(HomeCase):
    """Each operation, with each kind of planted entry at the name: it refuses (or, for
    write_atomic, replaces the entry itself), leaves the outside target untouched, and never
    blocks."""

    NAME = "f"

    def setUp(self):
        super().setUp()
        self.d = self.open()
        self.at = self.base / self.NAME

    # --- the plants
    def plant_symlink(self):
        self.at.symlink_to(self.outside)

    def plant_dangling(self):
        self.dangling_target = self.tmp / "not-there"
        self.at.symlink_to(self.dangling_target)

    def plant_hardlink(self):
        os.link(self.outside, self.at)

    def plant_fifo(self):
        os.mkfifo(self.at)

    def plant_dir(self):
        self.at.mkdir()

    PLANTS = ("symlink", "dangling", "hardlink", "fifo", "dir")

    def ops(self):
        d, n = self.d, self.NAME
        return {
            "read": lambda: safefs.read(d, n),
            "append": lambda: safefs.append(d, n, "evil\nline\n"),
            "write_atomic": lambda: safefs.write_atomic(d, n, "new"),
            "touch": lambda: safefs.touch(d, n),
            "lock": lambda: os.close(safefs.lock(d, n)),
            "open_lock": lambda: os.close(safefs.open_lock(d, n)),
            "scan": lambda: safefs.scan(d, ""),
        }

    def reset(self):
        for p in list(self.base.iterdir()):
            if p.is_dir() and not p.is_symlink():
                p.rmdir()
            else:
                p.unlink()
        self.outside.write_text("precious\n")
        self.outside.chmod(0o644)

    def check(self, op, plant, result):
        kind, value = result
        self.untouched()
        self.assertEqual(stat.S_IMODE(self.outside.stat().st_mode), 0o644, (op, plant))
        if plant == "dangling":
            self.assertFalse(self.dangling_target.exists(), (op, plant))
        if op == "read":
            self.assertEqual((kind, value), ("ok", None), (op, plant))
        elif op == "scan":
            self.assertEqual((kind, value), ("ok", []), (op, plant))
        elif op == "write_atomic":
            if plant == "dir":
                self.assertEqual(kind, "raised", (op, plant))
                self.assertIsInstance(value, OSError)
            else:   # the entry itself was replaced by a new regular file, nothing followed
                self.assertEqual(kind, "ok", (op, plant, value))
                st = os.lstat(self.at)
                self.assertTrue(stat.S_ISREG(st.st_mode))
                self.assertEqual((st.st_nlink, self.at.read_text()), (1, "new"))
        else:
            self.assertEqual(kind, "raised", (op, plant, value))
            self.assertIsInstance(value, OSError, (op, plant))

    def test_every_op_against_every_plant(self):
        for plant in self.PLANTS:
            for op, fn in self.ops().items():
                with self.subTest(op=op, plant=plant):
                    self.reset()
                    getattr(self, f"plant_{plant}")()
                    self.check(op, plant, within(fn))

    def test_every_op_against_another_users_file(self):
        for op, fn in self.ops().items():
            if op == "write_atomic":
                continue   # replaces the entry: nothing of the other file is read or written
            with self.subTest(op=op):
                self.reset()
                self.at.write_text("theirs")
                with mock.patch("os.getuid", return_value=os.getuid() + 1):
                    kind, value = within(fn)
                self.assertEqual(self.at.read_text(), "theirs")
                if op == "read":
                    self.assertEqual((kind, value), ("ok", None))
                elif op == "scan":
                    self.assertEqual((kind, value), ("ok", []))
                else:
                    self.assertEqual(kind, "raised", value)
                    self.assertIsInstance(value, OSError)


class OperationTests(HomeCase):
    def setUp(self):
        super().setUp()
        self.d = self.open()

    def mode(self, name):
        return stat.S_IMODE((self.base / name).stat().st_mode)

    def test_read(self):
        (self.base / "a").write_bytes(b"hello")
        self.assertEqual(safefs.read(self.d, "a"), b"hello")
        self.assertIsNone(safefs.read(self.d, "missing"))

    def test_read_refuses_more_than_the_limit(self):
        (self.base / "big").write_bytes(b"x" * 101)
        self.assertIsNone(safefs.read(self.d, "big", limit=100))
        self.assertEqual(safefs.read(self.d, "big", limit=101), b"x" * 101)

    def test_read_text(self):
        (self.base / "t").write_bytes("é\n".encode())
        self.assertEqual(safefs.read_text(self.d, "t"), "é\n")
        (self.base / "bad").write_bytes(b"\xff")
        self.assertEqual(safefs.read_text(self.d, "bad"), "�")

    def test_append_creates_0600_appends_and_tightens(self):
        safefs.append(self.d, "log", "a\n")
        self.assertEqual(self.mode("log"), 0o600)
        (self.base / "log").chmod(0o644)
        safefs.append(self.d, "log", b"b\n")
        self.assertEqual((self.base / "log").read_text(), "a\nb\n")
        self.assertEqual(self.mode("log"), 0o600)

    def test_write_atomic(self):
        safefs.write_atomic(self.d, "s.json", "{}")
        safefs.write_atomic(self.d, "s.json", b"{\"a\": 1}", mode=0o640)
        self.assertEqual((self.base / "s.json").read_text(), '{"a": 1}')
        self.assertEqual(self.mode("s.json"), 0o640)
        self.assertEqual(sorted(p.name for p in self.base.iterdir()), ["s.json"])

    def test_write_atomic_never_truncates_in_place(self):
        (self.base / "s").write_text("old")
        before = os.stat(self.base / "s").st_ino
        safefs.write_atomic(self.d, "s", "new")
        self.assertNotEqual(os.stat(self.base / "s").st_ino, before)

    def test_new_files_get_the_mode_whatever_the_umask(self):
        old = os.umask(0o777)
        try:
            safefs.append(self.d, "log", "x")
            safefs.append(self.d, "log640", "x", mode=0o640)
            safefs.touch(self.d, "stamp")
            os.close(safefs.open_lock(self.d, "lock"))
        finally:
            os.umask(old)
        self.assertEqual([self.mode(n) for n in ("log", "log640", "stamp", "lock")],
                         [0o600, 0o640, 0o600, 0o600])
        self.assertEqual((self.base / "log").read_text(), "x")

    def test_touch(self):
        safefs.touch(self.d, "stamp")
        self.assertEqual(((self.base / "stamp").read_bytes(), self.mode("stamp")), (b"", 0o600))
        os.utime(self.base / "stamp", (1000, 1000))
        safefs.touch(self.d, "stamp")
        self.assertGreater((self.base / "stamp").stat().st_mtime, time.time() - 60)

    def test_lock_holds_an_exclusive_flock(self):
        fd = safefs.lock(self.d, "l")
        try:
            other = os.open(self.base / "l", os.O_RDONLY)
            try:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(other)
            with self.assertRaises(BlockingIOError):
                safefs.lock(self.d, "l", blocking=False)
        finally:
            os.close(fd)
        os.close(safefs.lock(self.d, "l", blocking=False))

    def test_locked_context(self):
        with safefs.locked(self.d, "l"):
            with self.assertRaises(BlockingIOError):
                safefs.lock(self.d, "l", blocking=False)
        os.close(safefs.lock(self.d, "l", blocking=False))

    def test_scan(self):
        (self.base / "b.json").write_text("B")
        (self.base / "a.json").write_text("A")
        (self.base / "c.txt").write_text("C")
        (self.base / "big.json").write_text("x" * 50)
        os.mkfifo(self.base / "fifo.json")
        (self.base / "link.json").symlink_to(self.outside)
        (self.base / "dir.json").mkdir()
        skipped = []
        got = within(lambda: safefs.scan(self.d, ".json", limit=10, skipped=skipped))
        self.assertEqual(got, ("ok", [("a.json", b"A"), ("b.json", b"B")]))
        self.assertEqual(sorted(skipped), ["big.json", "dir.json", "fifo.json", "link.json"])

    def test_scan_with_a_name_filter(self):
        (self.base / "j--resume-r1.json").write_text("1")
        (self.base / "j.json").write_text("2")
        self.assertEqual(safefs.scan(self.d, ".json", match=lambda n: "--resume-r" in n),
                         [("j--resume-r1.json", b"1")])

    def test_names_with_a_slash_are_refused(self):
        for fn in (lambda: safefs.append(self.d, "../x", "y"),
                   lambda: safefs.write_atomic(self.d, "a/b", "y"),
                   lambda: safefs.touch(self.d, ".."),
                   lambda: safefs.read(self.d, "")):
            with self.assertRaises(ValueError):
                fn()

    def test_fresh_file_moves_a_plant_aside(self):
        (self.base / "out").symlink_to(self.outside)
        fd, moved = safefs.fresh_file(self.d, "out")
        os.close(fd)
        self.assertTrue(moved.startswith("out.stale-"))
        self.assertTrue((self.base / moved).is_symlink())
        self.untouched()

    def test_small_helpers(self):
        self.assertFalse(safefs.exists(self.d, "x"))
        (self.base / "x").symlink_to(self.tmp / "nowhere")
        self.assertTrue(safefs.exists(self.d, "x"))
        self.assertIsNotNone(safefs.mtime(self.d, "x"))
        safefs.unlink(self.d, "x")
        safefs.unlink(self.d, "x")
        self.assertFalse(safefs.exists(self.d, "x"))

    def test_read_tail(self):
        (self.base / "t").write_bytes(b"0123456789")
        self.assertEqual(safefs.read_tail(self.d, "t", 4), (b"6789", True))
        self.assertEqual(safefs.read_tail(self.d, "t", 40), (b"0123456789", False))


class LogSafeTests(unittest.TestCase):
    def test_one_line(self):
        out = safefs.log_safe("a\nimport os\r\x1b[2J")
        self.assertEqual(out, "a\\x0aimport os\\x0d\\x1b[2J")
        self.assertEqual(len(out.splitlines()), 1)

    def test_all_controls_escaped(self):
        s = "".join(chr(c) for c in range(0x00, 0x20)) + "\x7f" + "".join(chr(c) for c in range(0x80, 0xa0))
        out = safefs.log_safe(s + "\u2028\u2029")
        self.assertTrue(all(0x20 <= ord(c) < 0x7f for c in out), out)
        self.assertEqual(len(out.splitlines()), 1)
        self.assertIn("\\x00", out)
        self.assertIn("\\x9b", out)
        self.assertIn("\\u2028", out)

    def test_plain_text_and_unicode_kept(self):
        self.assertEqual(safefs.log_safe("héllo wörld ✓"), "héllo wörld ✓")

    def test_non_strings_and_surrogates(self):
        self.assertEqual(safefs.log_safe(42), "42")
        out = safefs.log_safe("x\udcff")
        out.encode("utf-8")   # no lone surrogate left
        self.assertEqual(out, "x\\udcff")


class PrivfsWrapperTests(HomeCase):
    """supervisor/privfs is safefs on the supervisor's private dir: one implementation."""

    def test_privfs_file_functions_are_safefs(self):
        from swarm.supervisor import privfs
        for name in ("read", "read_tail", "write_atomic", "append", "fresh_file", "open_existing",
                     "create", "unlink", "exists", "mtime"):
            self.assertIs(getattr(privfs, name), getattr(safefs, name), name)
        self.assertIs(privfs.lock, safefs.open_lock)   # privfs.lock never flocked: the runner does

    def test_privfs_errors_stay_private_dir_errors(self):
        from swarm.supervisor import privfs
        from swarm.supervisor.settings import PrivateDirError, private_dir
        private_dir().parent.mkdir(parents=True)
        private_dir().symlink_to(self.tmp)
        with self.assertRaises(PrivateDirError):
            privfs.open_dir()


class PathsTests(HomeCase):
    def test_host_dir(self):
        self.assertEqual(paths.host_dir(), self.home / ".local" / "share" / "swarm" / "host")


if __name__ == "__main__":
    unittest.main()
