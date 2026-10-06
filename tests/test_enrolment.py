"""Local enrolment records: the host-private proof that this
host user's unsandboxed hook enrolled an agent, or activated a job, on a given board. Each case
runs under a temporary HOME."""
from __future__ import annotations

import json
import os
import shutil
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from support import ROOT, home_env, posix_only, abs_  # noqa: F401

from swarm import enrolment, paths  # noqa: E402

BOARD = "sqlite-_home_x_.local_share_swarm-board_board.sqlite3"


class Env(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="swarm-enrol-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = self.tmp / "home"
        self.home.mkdir()
        p = mock.patch.dict(os.environ, {**home_env(self.home)})
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.cwd = str(self.home / "src" / "proj")

    def enrol(self, agent_key="k1", job="job-a", **kw):
        args = dict(job=job, agent_key=agent_key, harness="claude", session_id="s-1", cwd=self.cwd)
        args.update(kw)
        return enrolment.write(BOARD, **args)

    def dir(self):
        return paths.host_dir() / "enrolled"


class WriteFindTests(Env):
    def test_original_prompt_round_trip(self):
        rec = self.enrol(prompt="[swarm job: job-a]\nBuild parser")
        self.assertEqual(enrolment.find(BOARD,"k1").prompt, rec.prompt)

    def test_creation_proof_is_separate_from_coordinator_attachment(self):
        owner = enrolment.write_job_owner(BOARD,job="job-a",harness=None,session_id=None,cwd=self.cwd)
        enrolment.write_job(BOARD,job="job-a",harness="codex",session_id="other",cwd=self.cwd)
        self.assertEqual(enrolment.find_job_owner(BOARD,"job-a"),owner)
        self.assertEqual(enrolment.find_job(BOARD,"job-a").session_id,"other")
        self.assertIsNone(enrolment.find_job_owner("other-board","job-a"))

    def test_round_trip(self):
        rec = self.enrol(now=1000.0)
        got = enrolment.find(BOARD, "k1")
        self.assertEqual(got, rec)
        self.assertEqual((got.kind, got.board_key, got.job, got.agent_key, got.harness,
                          got.session_id, got.cwd, got.created_at),
                         ("agent", BOARD, "job-a", "k1", "claude", "s-1", self.cwd, 1000.0))

    @posix_only("needs POSIX file modes (Windows has ACLs)")
    def test_location_and_modes(self):
        self.enrol()
        files = list(self.dir().iterdir())
        self.assertEqual(len(files), 1)
        self.assertRegex(files[0].name, r"^agent-[0-9a-f]{64}\.json$")
        self.assertEqual(files[0].name, enrolment.record_name(BOARD, "k1"))
        self.assertEqual(stat.S_IMODE(files[0].stat().st_mode), 0o600)
        for d in (self.dir(), paths.host_dir()):
            self.assertEqual(stat.S_IMODE(d.stat().st_mode), 0o700)

    def test_keyed_by_board_and_agent(self):
        self.enrol()
        self.assertIsNone(enrolment.find(BOARD, "k2"))
        self.assertIsNone(enrolment.find("postgres-other", "k1"))
        self.assertNotEqual(enrolment.record_name("ab", "c"), enrolment.record_name("a", "bc"))

    def test_missing_host_dir(self):
        self.assertIsNone(enrolment.find(BOARD, "k1"))
        self.assertFalse(paths.host_dir().exists())   # a lookup creates nothing

    def test_rewrite_replaces(self):
        self.enrol(job="job-a")
        self.enrol(job="job-b", harness="codex")
        got = enrolment.find(BOARD, "k1")
        self.assertEqual((got.job, got.harness), ("job-b", "codex"))

    def test_owns(self):
        self.enrol()
        self.assertTrue(enrolment.owns(BOARD, "k1", "job-a"))
        self.assertFalse(enrolment.owns(BOARD, "k1", "job-b"))   # the row's job must match
        self.assertFalse(enrolment.owns(BOARD, "k9", "job-a"))

    def test_remove(self):
        self.enrol()
        enrolment.remove(BOARD, "k1")
        enrolment.remove(BOARD, "k1")
        self.assertIsNone(enrolment.find(BOARD, "k1"))

    def test_session_id_optional(self):
        self.enrol(session_id=None)
        self.assertIsNone(enrolment.find(BOARD, "k1").session_id)


class ValidationTests(Env):
    def test_bad_values_refused(self):
        bad = [dict(job="a\nb"), dict(job=""), dict(job="x" * 300), dict(agent_key="k\x1b"),
               dict(harness="bash"), dict(cwd="relative/dir"), dict(cwd="/a/\nb"),
               dict(session_id="s\n1"), dict(job=5)]
        for kw in bad:
            with self.subTest(kw=kw):
                with self.assertRaises(ValueError):
                    self.enrol(**kw)
        with self.assertRaises(ValueError):
            enrolment.write("", job="j", agent_key="k", harness="claude", session_id=None, cwd=abs_("/x"))
        self.assertFalse(self.dir().exists() and any(self.dir().iterdir()))

    def test_a_tampered_record_is_not_trusted(self):
        self.enrol()
        p = self.dir() / enrolment.record_name(BOARD, "k1")
        data = json.loads(p.read_text())
        for field, value in (("agent_key", "k2"), ("board_key", "other"), ("kind", "job"),
                             ("harness", "bash"), ("cwd", "rel"), ("job", "a\nb"), ("v", 99)):
            with self.subTest(field=field):
                p.write_text(json.dumps({**data, field: value}))
                self.assertIsNone(enrolment.find(BOARD, "k1"))
        p.write_text("not json")
        self.assertIsNone(enrolment.find(BOARD, "k1"))


class PlantTests(Env):
    """The enrolled dir is host-private, but nothing in it is followed even if it weren't."""

    def setUp(self):
        super().setUp()
        self.victim = self.tmp / "victim"
        self.victim.write_text("precious")

    def test_a_symlinked_record_is_not_read_or_followed(self):
        self.enrol()
        p = self.dir() / enrolment.record_name(BOARD, "k1")
        p.unlink()
        fake = self.tmp / "fake.json"
        fake.write_text("{}")
        p.symlink_to(self.victim)
        self.assertIsNone(enrolment.find(BOARD, "k1"))
        self.enrol()   # replaces the link, doesn't write through it
        self.assertEqual(self.victim.read_text(), "precious")
        self.assertFalse(p.is_symlink())

    def test_a_hard_linked_record_is_refused(self):
        self.enrol()
        p = self.dir() / enrolment.record_name(BOARD, "k1")
        os.link(p, self.tmp / "second")
        self.assertIsNone(enrolment.find(BOARD, "k1"))

    def test_a_symlinked_host_dir_is_refused(self):
        paths.share_dir().mkdir(parents=True)
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir(mode=0o700)
        paths.host_dir().symlink_to(elsewhere)
        with self.assertRaises(OSError):
            self.enrol()
        self.assertEqual(list(elsewhere.iterdir()), [])
        self.assertIsNone(enrolment.find(BOARD, "k1"))

    @posix_only("needs POSIX file modes (Windows has ACLs)")
    def test_a_loose_host_dir_is_refused(self):
        paths.host_dir().mkdir(parents=True)
        paths.host_dir().chmod(0o755)
        with self.assertRaises(OSError):
            self.enrol()


class JobRecordTests(Env):
    def test_round_trip(self):
        rec = enrolment.write_job(BOARD, job="job-a", harness="codex", session_id="t-1",
                                  cwd=self.cwd, now=500.0)
        self.assertEqual(enrolment.find_job(BOARD, "job-a"), rec)
        self.assertEqual((rec.kind, rec.agent_key, rec.created_at), ("job", None, 500.0))
        self.assertIsNone(enrolment.find_job(BOARD, "job-b"))
        self.assertIsNone(enrolment.find(BOARD, "job-a"))   # separate namespaces
        self.assertRegex(enrolment.job_record_name(BOARD, "job-a"), r"^job-[0-9a-f]{64}\.json$")

    def test_remove_job(self):
        enrolment.write_job(BOARD, job="job-a", harness="claude", session_id="s", cwd=self.cwd)
        enrolment.remove_job(BOARD, "job-a")
        self.assertIsNone(enrolment.find_job(BOARD, "job-a"))


class FixRound1Tests(Env):
    def test_huge_created_at_is_skipped_not_raised(self):
        self.enrol("k1", now=1000.0)
        p = self.dir() / enrolment.record_name(BOARD, "k1")
        data = json.loads(p.read_text())
        p.write_text(json.dumps(data).replace('"created_at": 1000.0', '"created_at": ' + "1" + "0" * 400))
        self.assertIn("1" + "0" * 400, p.read_text())
        self.assertIsNone(enrolment.find(BOARD, "k1"))
        self.assertEqual(enrolment.prune(max_age=5000, now=10000.0), 1)
        self.assertFalse(p.exists())

    def test_cwd_must_be_normalised(self):
        for cwd in map(abs_, ("/a/../b", "/a/./b", "/a//b", "/a/b/", "/a/b/..")):
            with self.subTest(cwd=cwd):
                with self.assertRaises(ValueError):
                    self.enrol(cwd=cwd)
        self.enrol(cwd=abs_("/a/b"))

    def test_a_record_with_an_unnormalised_cwd_is_not_trusted(self):
        self.enrol()
        p = self.dir() / enrolment.record_name(BOARD, "k1")
        p.write_text(json.dumps({**json.loads(p.read_text()), "cwd": abs_("/home/x/src/../../etc")}))
        self.assertIsNone(enrolment.find(BOARD, "k1"))

    @posix_only("a file name ending in a newline is not valid on Windows")
    def test_prune_names_must_match_fully(self):
        # "agent-<hex>.json\n" is not a record name: left alone, like any other name
        odd = "agent-" + "3" * 64 + ".json\n"
        self.enrol("keep", now=9000.0)
        (self.dir() / odd).write_text("x")
        self.assertEqual(enrolment.prune(max_age=5000, now=10000.0), 0)
        self.assertTrue((self.dir() / odd).exists())

    def test_format_characters_refused_in_fields(self):
        for bad in ("a\u200bb", "a\u2028b", "\ufeffjob", "j\U000e0041"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    self.enrol(job=bad)


class PruneTests(Env):
    def test_prune_by_age(self):
        self.enrol("old", now=1000.0)
        self.enrol("new", now=9000.0)
        enrolment.write_job(BOARD, job="j-old", harness="claude", session_id=None, cwd=self.cwd,
                            now=1000.0)
        self.assertEqual(enrolment.prune(max_age=5000, now=10000.0), 2)
        self.assertIsNone(enrolment.find(BOARD, "old"))
        self.assertIsNotNone(enrolment.find(BOARD, "new"))
        self.assertIsNone(enrolment.find_job(BOARD, "j-old"))

    @posix_only("needs os.mkfifo (POSIX FIFOs)")
    def test_prune_removes_junk_but_not_links_targets(self):
        self.enrol("keep", now=9000.0)
        victim = self.tmp / "victim"
        victim.write_text("precious")
        (self.dir() / ("agent-" + "0" * 64 + ".json")).symlink_to(victim)
        (self.dir() / ("agent-" + "1" * 64 + ".json")).write_text("garbage")
        os.mkfifo(self.dir() / ("job-" + "2" * 64 + ".json"))
        (self.dir() / "unrelated.txt").write_text("left alone")
        self.assertEqual(enrolment.prune(max_age=5000, now=10000.0), 3)
        self.assertEqual(victim.read_text(), "precious")
        self.assertEqual(sorted(p.name for p in self.dir().iterdir()),
                         sorted([enrolment.record_name(BOARD, "keep"), "unrelated.txt"]))

    def test_prune_keeps_old_records_the_caller_still_needs(self):
        self.enrol("old-keep", job="active", now=1000.0)
        self.enrol("old-drop", job="closed", now=1000.0)
        (self.dir() / ("agent-" + "1" * 64 + ".json")).write_text("garbage")   # junk goes anyway
        seen = []

        def keep(rec):
            seen.append(rec.agent_key)
            return rec.job == "active"
        self.assertEqual(enrolment.prune(max_age=5000, now=10000.0, keep=keep), 2)
        self.assertIsNotNone(enrolment.find(BOARD, "old-keep"))
        self.assertIsNone(enrolment.find(BOARD, "old-drop"))
        self.assertEqual(sorted(seen), ["old-drop", "old-keep"])   # asked only about old, valid records

    def test_prune_without_a_dir(self):
        self.assertEqual(enrolment.prune(max_age=1), 0)
        self.assertFalse(paths.host_dir().exists())


if __name__ == "__main__":
    unittest.main()
