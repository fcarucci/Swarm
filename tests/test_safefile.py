from __future__ import annotations

import os
import stat
import shutil
import tempfile
import unittest
from pathlib import Path

from support import ROOT  # noqa: F401

from swarm import safefile  # noqa: E402


class SafeFileTests(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp(prefix="swarm-safe-"))
        self.addCleanup(shutil.rmtree, self.d, ignore_errors=True)

    def mode(self, p):
        return stat.S_IMODE(p.stat().st_mode)

    def test_keeps_restrictive_mode_and_backs_up_with_it(self):
        f = self.d / "config.toml"; f.write_text("a = 1\n"); f.chmod(0o600)
        b = safefile.backup(f)
        safefile.write_preserving(f, "a = 2\n")
        self.assertEqual((f.read_text(), self.mode(f)), ("a = 2\n", 0o600))
        self.assertEqual((b.read_text(), self.mode(b)), ("a = 1\n", 0o600))
        self.assertEqual([p.name for p in self.d.iterdir() if "swarm-tmp" in p.name], [])

    def test_wider_mode_is_capped_at_0600_and_backup_is_0600(self):
        f = self.d / "config.toml"; f.write_text("a = 1\n"); f.chmod(0o644)
        b = safefile.backup(f)
        safefile.write_preserving(f, "a = 2\n")
        self.assertEqual((self.mode(f), self.mode(b)), (0o600, 0o600))

    def test_tighter_mode_is_kept(self):
        f = self.d / "secret.toml"; f.write_text("x"); f.chmod(0o400)
        safefile.write_preserving(f, "y")
        self.assertEqual(self.mode(f), 0o400)

    def test_new_file_is_private(self):
        f = self.d / "new.toml"
        old = os.umask(0o022)
        try:
            safefile.write_preserving(f, "x = 1\n")
        finally:
            os.umask(old)
        self.assertEqual(self.mode(f), 0o600)
        self.assertIsNone(safefile.backup(self.d / "missing"))

    def test_explicit_wider_mode_is_capped_at_0600(self):
        f = self.d / "x.toml"
        safefile.write_preserving(f, "x", mode=0o644)
        self.assertEqual(self.mode(f), 0o600)
        safefile.write_preserving(f, "y", mode=0o755)
        self.assertEqual(self.mode(f), 0o600)


class CreateAndAppendTests(unittest.TestCase):
    """create_exclusive (a new file, complete when it appears, never over another) and
    append_private (the supervisor's log: appended lines, 0600, only a regular file of ours)."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="swarm-safefile-"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def leftovers(self):
        return [p.name for p in self.dir.iterdir() if p.name.endswith(".swarm-tmp")]

    def test_create_exclusive_writes_a_private_file(self):
        p = self.dir / "m.json"
        safefile.create_exclusive(p, "{}")
        self.assertEqual((p.read_text(), stat.S_IMODE(p.stat().st_mode)), ("{}", 0o600))
        self.assertEqual(self.leftovers(), [])

    def test_create_exclusive_never_replaces(self):
        p = self.dir / "m.json"
        p.write_text("theirs")
        with self.assertRaises(FileExistsError):
            safefile.create_exclusive(p, "mine")
        self.assertEqual(p.read_text(), "theirs")
        self.assertEqual(self.leftovers(), [])

    def test_append_private_appends_and_caps_mode(self):
        p = self.dir / "log"
        safefile.append_private(p, "a\n")
        self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o600)
        os.chmod(p, 0o644)
        safefile.append_private(p, "b\n")
        self.assertEqual((p.read_text(), stat.S_IMODE(p.stat().st_mode)), ("a\nb\n", 0o600))

    def test_append_private_refuses_a_symlink_or_a_non_file(self):
        target = self.dir / "elsewhere"
        target.write_text("")
        (self.dir / "link").symlink_to(target)
        with self.assertRaises(OSError):
            safefile.append_private(self.dir / "link", "x\n")
        self.assertEqual(target.read_text(), "")
        fifo = self.dir / "fifo"
        os.mkfifo(fifo)
        with self.assertRaises(OSError):
            safefile.append_private(fifo, "x\n")

    def test_append_private_refuses_a_hard_link(self):
        # a hard link passes O_NOFOLLOW: only the link count shows it
        target = self.dir / "elsewhere"
        target.write_text("")
        os.link(target, self.dir / "log")
        with self.assertRaises(OSError):
            safefile.append_private(self.dir / "log", "x\n")
        self.assertEqual(target.read_text(), "")
