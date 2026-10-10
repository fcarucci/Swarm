"""B4: the venv's dependencies are pinned exactly and hash-checked, installed as wheels only;
psycopg is only installed for a Postgres board (or kept when a venv already has it); the launcher
checks for Python 3.11+ before it creates a venv."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from support import ROOT, home_env, posix_only

REQS = ("requirements.txt", "requirements-postgres.txt")


def requirements(rel: str) -> list[str]:
    """The requirement entries of a file (continuation lines joined, comments dropped)."""
    text = (ROOT / rel).read_text().replace("\\\n", " ")
    return [line.strip() for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]


class PinTests(unittest.TestCase):
    def test_every_requirement_is_pinned_and_hashed(self):
        for rel in REQS:
            entries = requirements(rel)
            self.assertTrue(entries, rel)
            for entry in entries:
                with self.subTest(rel=rel, entry=entry.split()[0]):
                    self.assertRegex(entry.split()[0], r"^[A-Za-z0-9_.-]+==[0-9][0-9A-Za-z.]*$")
                    self.assertRegex(entry, r"--hash=sha256:[0-9a-f]{64}")

    def test_psycopg_only_in_the_postgres_file(self):
        names = lambda rel: {e.split("==")[0].lower() for e in requirements(rel)}
        self.assertNotIn("psycopg", names("requirements.txt"))
        self.assertIn("psycopg", names("requirements-postgres.txt"))
        self.assertIn("zstandard", names("requirements.txt"))

    def test_windows_launcher_uses_the_same_flags(self):
        text = (ROOT / "lib/swarm/winlaunch.py").read_text()
        for flag in ("--require-hashes", "--only-binary=:all:", "requirements-postgres.txt"):
            self.assertIn(flag, text)


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="swarm-deps-"))
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        self.config = self.home / "config.toml"
        self.env = {**home_env(self.home), "PATH": os.environ["PATH"], "SWARM_CONFIG": str(self.config)}
        self.v = self.home / "venv"
        self.env["SWARM_VENV"] = str(self.v)
        self.log = self.home / "python-args"

    def fake_venv(self, packages=("zstandard",), stamp=None):
        """A venv whose python logs its args (so pip never touches the network)."""
        (self.v / "bin").mkdir(parents=True, exist_ok=True)
        (self.v / "bin/python").write_text(f'#!/bin/sh\necho "$@" >> "{self.log}"\n')
        (self.v / "bin/python").chmod(0o755)
        pkgs = self.v / "lib/python3.12/site-packages"
        for pkg in packages:
            (pkgs / pkg).mkdir(parents=True, exist_ok=True)
            (pkgs / pkg / "__init__.py").write_text("")
        if stamp is not None:
            (self.v / ".swarm-requirements").write_text(stamp + "\n")

    def run_swarm(self):
        return subprocess.run([str(ROOT / "bin/swarm"), "status"], capture_output=True, text=True,
                              env=self.env, timeout=30)

    def pip_lines(self):
        return [l.split() for l in self.log.read_text().splitlines() if l.startswith("-m pip")]

    @posix_only("runs the POSIX sh launcher")
    def test_install_is_hash_checked_wheels_only(self):
        self.fake_venv(stamp="stale")
        self.run_swarm()
        (pip,) = self.pip_lines()
        self.assertIn("--require-hashes", pip)
        self.assertIn("--only-binary=:all:", pip)
        self.assertIn(str(ROOT / "requirements.txt"), pip)
        self.assertNotIn(str(ROOT / "requirements-postgres.txt"), pip)   # file board: no psycopg

    @posix_only("runs the POSIX sh launcher")
    def test_postgres_config_installs_psycopg(self):
        self.config.write_text('[board]\nbackend = "postgres"\n')
        self.fake_venv(stamp="stale")
        self.run_swarm()
        (pip,) = self.pip_lines()
        self.assertIn(str(ROOT / "requirements-postgres.txt"), pip)

    @posix_only("runs the POSIX sh launcher")
    def test_legacy_database_section_installs_psycopg(self):
        self.config.write_text('[database]\nhost = "db"\n')   # pre-"file"-default installs: postgres
        self.fake_venv(stamp="stale")
        self.run_swarm()
        (pip,) = self.pip_lines()
        self.assertIn(str(ROOT / "requirements-postgres.txt"), pip)

    @posix_only("runs the POSIX sh launcher")
    def test_a_venv_that_has_psycopg_keeps_it_on_rebuild(self):
        self.fake_venv(packages=("zstandard", "psycopg"), stamp="old-requirements")
        self.run_swarm()
        (pip,) = self.pip_lines()
        self.assertIn(str(ROOT / "requirements-postgres.txt"), pip)

    @posix_only("runs the POSIX sh launcher")
    def test_complete_file_board_venv_needs_no_psycopg(self):
        self.fake_venv(stamp="stale")
        self.run_swarm()
        self.log.unlink()
        self.run_swarm()   # stamped now, zstandard present, no postgres wanted: no pip
        self.assertEqual(self.pip_lines(), [])

    @posix_only("runs the POSIX sh launcher")
    def test_old_python_is_refused_before_a_venv_is_made(self):
        fake_bin = self.home / "fakebin"; fake_bin.mkdir()
        (fake_bin / "python3").write_text(f'#!/bin/sh\necho "$@" >> "{self.home}/py3-args"\nexit 1\n')
        (fake_bin / "python3").chmod(0o755)
        self.env["PATH"] = f"{fake_bin}:{self.env['PATH']}"
        res = self.run_swarm()
        self.assertEqual(res.returncode, 1)
        self.assertIn("3.11", res.stderr)
        self.assertFalse(self.v.exists())
        self.assertNotIn("venv", (self.home / "py3-args").read_text())


if __name__ == "__main__":
    unittest.main()
