"""B4: the venv's dependencies are pinned exactly and hash-checked, installed as wheels only;
psycopg is only installed for a Postgres board (or kept when a venv already has it); the launcher
checks for Python 3.11+ before it creates a venv."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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

    def wants_pg(self, config_text, *, args=("status",), env_config=True, extra=None):
        """Run the launcher on a stale file-board venv; whether pip was told to install psycopg."""
        if config_text is not None:
            self.config.write_text(config_text)
        self.log.unlink(missing_ok=True)
        shutil.rmtree(self.v, ignore_errors=True)
        self.fake_venv(stamp="stale")
        env = dict(self.env)
        if not env_config:
            env.pop("SWARM_CONFIG")
        subprocess.run([str(ROOT / "bin/swarm"), *args], capture_output=True, text=True, env=env, timeout=30)
        (pip,) = self.pip_lines()
        return str(ROOT / "requirements-postgres.txt") in pip

    @posix_only("runs the POSIX sh launcher")
    def test_the_shipped_example_config_is_a_file_board(self):
        # config.example.toml has a [database] section and backend = "file": no psycopg
        self.assertFalse(self.wants_pg((ROOT / "config.example.toml").read_text()))

    @posix_only("runs the POSIX sh launcher")
    def test_database_section_with_an_explicit_file_backend_needs_no_psycopg(self):
        self.assertFalse(self.wants_pg('[database]\nhost = "db"\n[board]\nbackend = "file"\n'))
        self.assertFalse(self.wants_pg('[database]\nhost = "db"\n[board]\nbackend = "sqlite"  # one machine\n'))

    @posix_only("runs the POSIX sh launcher")
    def test_single_quoted_postgres_backend_installs_psycopg(self):
        self.assertTrue(self.wants_pg("[board]\nbackend = 'postgres'\n"))
        self.assertTrue(self.wants_pg('[board]\nbackend="postgres"  # shared\n'))
        self.assertTrue(self.wants_pg('board = { backend = "postgres" }\n'))

    @posix_only("runs the POSIX sh launcher")
    def test_a_commented_or_empty_database_section_is_not_postgres(self):
        self.assertFalse(self.wants_pg('# [database]\n[board]\n'))
        self.assertFalse(self.wants_pg('[database]\n'))      # empty: cli.load_config keeps the file default

    @posix_only("runs the POSIX sh launcher")
    def test_config_argument_is_honoured(self):
        other = self.home / "other.toml"
        other.write_text("[board]\nbackend = 'postgres'\n")
        self.config.write_text('[board]\nbackend = "file"\n')          # SWARM_CONFIG says file
        self.assertTrue(self.wants_pg(None, args=("--config", str(other), "status")))
        self.assertTrue(self.wants_pg(None, args=(f"--config={other}", "status")))

    @posix_only("runs the POSIX sh launcher")
    def test_pip_failure_says_what_can_go_wrong(self):
        self.fake_venv(stamp="stale")
        (self.v / "bin/python").write_text('#!/bin/sh\necho "pip says no" >&2\nexit 1\n')
        res = self.run_swarm()
        self.assertEqual(res.returncode, 1)
        for words in ("wheel", "hash", "pip", "3.11"):
            self.assertIn(words, res.stderr)

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


class WantPostgresTests(unittest.TestCase):
    """swarm.pgwant is the one rule both launchers use; it parses the config like cli.load_config."""

    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="swarm-pgwant-"))
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)

    def wanted(self, text):
        from swarm import pgwant
        cfg = self.home / "c.toml"
        cfg.write_text(text)
        return pgwant.wanted(cfg)

    def test_rules(self):
        self.assertFalse(self.wanted((ROOT / "config.example.toml").read_text()))
        self.assertFalse(self.wanted('[database]\nhost = "x"\n[board]\nbackend = "file"\n'))
        self.assertTrue(self.wanted("[board]\nbackend = 'postgres'\n"))
        self.assertTrue(self.wanted('[database]\nhost = "x"\n'))                 # legacy: no backend set
        self.assertFalse(self.wanted(""))
        self.assertFalse(self.wanted("not = [valid"))                                # unreadable: not wanted

    def test_a_missing_config_is_not_postgres(self):
        from swarm import pgwant
        self.assertFalse(pgwant.wanted(self.home / "nope.toml"))

    def test_config_argument_parsing(self):
        from swarm import pgwant
        self.assertEqual(pgwant.config_arg(["--config", "a", "status"]), "a")
        self.assertEqual(pgwant.config_arg(["--config=b", "status"]), "b")
        self.assertEqual(pgwant.config_arg(["--config", "a", "--config=b"]), "b")    # the last wins
        self.assertIsNone(pgwant.config_arg(["status"]))

    def test_windows_launcher_uses_the_same_rule(self):
        from swarm import winlaunch
        cfg = self.home / "c.toml"
        v = self.home / "venv"
        cfg.write_text((ROOT / "config.example.toml").read_text())
        with mock.patch.dict(os.environ, {"SWARM_CONFIG": str(cfg)}):
            self.assertFalse(winlaunch.want_postgres(v))
            cfg.write_text("[board]\nbackend = 'postgres'\n")
            self.assertTrue(winlaunch.want_postgres(v))
            self.assertTrue(winlaunch.want_postgres(v, ["--config", str(cfg)]))
            cfg.write_text('[board]\nbackend = "file"\n[database]\nhost = "x"\n')
            self.assertFalse(winlaunch.want_postgres(v))
            other = self.home / "o.toml"
            other.write_text('[database]\nhost = "x"\n')
            self.assertTrue(winlaunch.want_postgres(v, ["--config", str(other)]))


class MissingPsycopgTests(unittest.TestCase):
    def test_a_postgres_board_without_psycopg_says_what_to_run(self):
        from swarm.board import BoardError, backend_class
        sys.modules.pop("swarm.board.postgres", None)
        with mock.patch.dict(sys.modules, {"psycopg": None}):
            with self.assertRaises(BoardError) as ctx:
                backend_class({"board": {"backend": "postgres"}})
        msg = str(ctx.exception)
        for words in ("psycopg", "requirements-postgres.txt", "--require-hashes", "SWARM_CONFIG"):
            self.assertIn(words, msg)


if __name__ == "__main__":
    unittest.main()
