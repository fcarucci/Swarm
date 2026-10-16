"""The drive tests' backends (support.drive_backends): the helper itself, a contract the drive
claim needs from the database (one attempt of an action once), checked on every backend, and the
Postgres-only checks that make CI's postgres job fail when a Postgres-only difference creeps in."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import support
from support import SANDBOX, add_backend_variants, drive_backends, home_env  # noqa: F401

PG_CONFIG = os.environ.get("SWARM_TEST_CONFIG")


class DriveBackendsHelper(unittest.TestCase):
    def test_offline_backends_are_always_there(self):
        with mock.patch.dict(os.environ, {"SWARM_TEST_CONFIG": ""}):
            self.assertEqual(sorted(drive_backends()), ["file", "memory", "sqlite"])

    def test_postgres_is_included_exactly_when_a_test_config_is_set(self):
        with mock.patch.dict(os.environ, {"SWARM_TEST_CONFIG": "/some/throwaway.toml"}):
            self.assertEqual(sorted(drive_backends()), ["file", "memory", "postgres", "sqlite"])
        with mock.patch.dict(os.environ):
            os.environ.pop("SWARM_TEST_CONFIG", None)
            self.assertNotIn("postgres", drive_backends())

    def test_variants_are_added_per_backend_except_the_skipped_one(self):
        class Case(unittest.TestCase):
            def test_x(self):
                pass
        ns = {"__name__": "somemodule"}
        with mock.patch.dict(os.environ, {"SWARM_TEST_CONFIG": "/some/throwaway.toml"}):
            os.environ.pop("SWARM_TEST_BACKEND", None)
            add_backend_variants(ns, Case)
        self.assertEqual(sorted(k for k in ns if k != "__name__"), ["CaseFile", "CasePostgres", "CaseSqlite"])
        self.assertTrue(all(issubclass(ns[k], Case) and ns[k].__module__ == "somemodule"
                            for k in ("CaseFile", "CasePostgres", "CaseSqlite")))

    def test_a_leg_runs_only_its_own_backend_and_unset_runs_all(self):
        class Case(unittest.TestCase):
            def test_x(self):
                pass
        for chosen, expect in (("memory", ["Case"]), ("sqlite", ["CaseSqlite"]), ("file", ["CaseFile"]),
                               ("postgres", ["Case", "CaseFile", "CaseSqlite"]), (None, ["Case", "CaseFile", "CaseSqlite"])):
            with self.subTest(chosen=chosen), mock.patch.dict(os.environ, {"SWARM_TEST_CONFIG": ""}):
                os.environ.pop("SWARM_TEST_BACKEND", None)
                if chosen:
                    os.environ["SWARM_TEST_BACKEND"] = chosen
                ns = {"__name__": "somemodule", "Case": Case}
                add_backend_variants(ns, Case)
                self.assertEqual(sorted(k for k in ns if k != "__name__"), expect)

    def test_gate_backends_drops_the_unselected_hard_coded_copies(self):
        class A(unittest.TestCase):
            pass

        class B(unittest.TestCase):
            pass
        for chosen, expect in (("memory", ["A"]), ("sqlite", ["B"]), (None, ["A", "B"])):
            with self.subTest(chosen=chosen), mock.patch.dict(os.environ):
                os.environ.pop("SWARM_TEST_BACKEND", None)
                if chosen:
                    os.environ["SWARM_TEST_BACKEND"] = chosen
                ns = {"A": A, "B": B}
                support.gate_backends(ns, memory=[A], sqlite=[B])
                self.assertEqual(sorted(ns), expect)

    def test_postgres_harness_refuses_to_run_without_a_test_config(self):
        with mock.patch.dict(os.environ):
            os.environ.pop("SWARM_TEST_CONFIG", None)
            with self.assertRaises(RuntimeError):
                support.PostgresHarness.for_env(Path("."), "m.C.t")


class ClaimContract:
    """One attempt of an action is claimed once, whatever the backend (the unique (job, action_id,
    attempt) of spec 4.5): the second claim of it is refused and moves no seat."""
    harness_factory = staticmethod(drive_backends()["memory"])

    def setUp(self):
        self.home = Path(tempfile.mkdtemp(dir=SANDBOX, prefix="claim-home-"))
        env = mock.patch.dict(os.environ, home_env(self.home))
        env.start()
        self.addCleanup(env.stop)
        self.h = self.harness_factory(self.id().rsplit(".", 1)[0])
        self.addCleanup(self.h.close)
        self.h.reset()
        self.b = self.h.board()
        self.addCleanup(self.b.close)
        self.b.open_job("J", None, None, None, "me", goal="G")

    def get_launch(self, name):
        return self.b.queue_launch("J", 1, name, role="engineer", harness="claude", model="haiku",
                                   workdir=f"/w/{name}", brief=f"/w/{name}/B.md", queued_by="lead")

    def claim(self, seat, launch_id, action="gen1|seat|eng|-|1|1", attempt=1):
        return self.b.claim_drive_launch(job="J", generation=1, action_id=action, attempt=attempt,
                                         launch_id=launch_id, launch_row_id=seat.id, queue_version=seat.revision,
                                         harness="claude", reserve_usd=1, reserve_minutes=1, locks=[],
                                         lock_seconds=60, max_host_running=4)

    def test_the_same_attempt_of_an_action_is_claimed_once(self):
        a, b = self.get_launch("eng-a"), self.get_launch("eng-b")
        self.assertIsNotNone(self.claim(a, "L1"))
        self.assertIsNone(self.claim(b, "L2"), "the same (job, action, attempt) under another launch id")
        self.assertEqual({s.name: s.state for s in self.b.launches("J")}["eng-b"], "queued", "no launch moved")
        self.assertEqual(len(self.b.restarts()), 1)

    def test_another_attempt_and_another_action_are_not_refused(self):
        a, b, c = self.get_launch("eng-a"), self.get_launch("eng-b"), self.get_launch("eng-c")
        self.assertIsNotNone(self.claim(a, "L1"))
        self.assertIsNotNone(self.claim(b, "L2", attempt=2))
        self.assertIsNotNone(self.claim(c, "L3", action="gen1|seat|eng|-|1|2"))


class MemoryClaimContract(ClaimContract, unittest.TestCase):
    pass


add_backend_variants(globals(), MemoryClaimContract)


@unittest.skipUnless(PG_CONFIG, "set SWARM_TEST_CONFIG to a throwaway Postgres board config to run")
class PostgresOnly(unittest.TestCase):
    """What only the real database can break. CI's postgres job runs these, so e.g. dropping the
    restarts_action_attempt index from the Postgres DDL turns that job red."""

    def test_the_drive_unique_index_is_in_the_database(self):
        h = support.PostgresHarness.for_env(Path("."), "probe.Index.x")
        self.addCleanup(h.close)
        row = h.conn.execute("SELECT indexdef FROM pg_indexes WHERE indexname = 'restarts_action_attempt'").fetchone()
        self.assertIsNotNone(row, "no unique index on restarts (job, action_id, attempt)")
        self.assertIn("UNIQUE", row[0])
        self.assertIn("(job, action_id, attempt)", row[0])

    def test_each_test_class_gets_a_database_of_its_own(self):
        a1 = support.PostgresHarness.for_env(Path("."), "probe.ClassA.t1")
        a2 = support.PostgresHarness.for_env(Path("."), "probe.ClassA.t2")
        b = support.PostgresHarness.for_env(Path("."), "probe.ClassB.t1")
        for h in (a1, a2, b):
            self.addCleanup(h.close)
        names = [h.cfg["database"]["dbname"] for h in (a1, a2, b)]
        self.assertEqual(names[0], names[1], "one database per class")
        self.assertNotEqual(names[0], names[2])
        base = support.swarm.load_config(Path(PG_CONFIG).expanduser())["database"]["dbname"]
        self.assertTrue(all(n.startswith(base + "_") and n != base for n in names), "never the config's own database")

    def test_the_config_lines_of_the_harness_reach_the_database_it_made(self):
        h = support.PostgresHarness.for_env(Path("."), "probe.Toml.x")
        self.addCleanup(h.close)
        self.assertIn(f'dbname = "{h.cfg["database"]["dbname"]}"', h.toml)
        self.assertNotIn("backend", h.toml, "[board] backend is the caller's line")

    def test_end_to_end_tests_on_postgres_run_on_a_postgres_board(self):
        with mock.patch.object(support, "E2E_BACKEND", "postgres"):
            h = support.e2e_harness(Path("."), "probe.E2e.x")
        self.addCleanup(h.close)
        h.reset()
        b = h.board()
        self.addCleanup(b.close)
        self.assertEqual(type(b).__name__, "PostgresBoard")


def _summary_module():
    import importlib.util
    path = support.ROOT / ".github/scripts/drive_pg_summary.py"
    spec = importlib.util.spec_from_file_location("drive_pg_summary", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _log(*rows):
    return "".join(f"{t.rsplit('.', 1)[1]} ({t}) ... {r}\n" for t, r in rows)


class PostgresStepSummary(unittest.TestCase):
    """.github/scripts/drive_pg_summary.py, the check CI's "Drive suites on Postgres" step ends with."""

    def setUp(self):
        self.s = _summary_module()
        self.modules = {"test_drive_a": True, "test_drive_b": False}

    def problems(self, log):
        return self.s.summarise(log, self.modules)[1]

    def good(self):
        return _log(("test_drive_a.CasePostgres.test_x", "ok"), ("test_drive_a.Case.test_x", "ok"),
                    ("test_drive_b.Other.test_y", "ok"))

    def test_a_complete_run_has_no_problems_and_counts_the_postgres_variants(self):
        lines, problems = self.s.summarise(self.good(), self.modules)
        self.assertEqual(problems, [])
        self.assertRegex(lines[1], r"test_drive_a\s+2\s+1\s+0")
        self.assertRegex(lines[2], r"test_drive_b\s+1\s+0\s+0")

    def test_a_module_that_ran_nothing_is_a_problem(self):
        log = _log(("test_drive_a.CasePostgres.test_x", "ok"))
        self.assertEqual(self.problems(log), ["test_drive_b: no test ran"])

    def test_a_module_with_variants_that_ran_none_on_postgres_is_a_problem(self):
        log = _log(("test_drive_a.Case.test_x", "ok"), ("test_drive_b.Other.test_y", "ok"))
        self.assertEqual(self.problems(log), ["test_drive_a: builds Postgres variants but none ran"])

    def test_a_module_without_variants_needs_none(self):
        self.assertEqual(self.problems(self.good()), [])

    def test_a_skip_is_a_problem_unless_it_is_windows_or_systemd_only(self):
        for reason, bad in (("skipped 'needs a database'", True), ("skipped 'Windows has no fcntl'", False),
                            ("skipped 'needs systemd --user'", False),
                            ("skipped 'memory-only: raw row forgery'", False),
                            ("skipped 'not memory-only: raw row forgery'", True)):
            log = self.good() + _log(("test_drive_b.Other.test_z", reason))
            got = self.problems(log)
            self.assertEqual(bool(got), bad, reason)
            if bad:
                self.assertIn("test_drive_b.Other.test_z: skipped", got[0])

    def test_a_docstring_line_between_name_and_result_is_parsed(self):
        log = ("test_x (test_drive_a.CasePostgres.test_x)\nA docstring first line ... ok\n"
               + _log(("test_drive_b.Other.test_y", "ok")))
        self.assertEqual(self.problems(log), [])

    def test_a_module_that_builds_a_board_without_variants_is_a_problem(self):
        with tempfile.TemporaryDirectory(dir=SANDBOX) as d:
            tests = Path(d)
            (tests / "test_drive_ok.py").write_text("x = 1\n")                                    # pure logic
            (tests / "test_drive_cli.py").write_text("from test_hooks_cli import Env\n")          # CLI: SWARM_TEST_BACKEND
            (tests / "test_drive_variants.py").write_text("h = MemoryHarness()\nadd_backend_variants(globals(), A)\n")
            self.assertEqual(self.s.source_problems(tests), [])
            for word in ("MemoryHarness", "FileHarness", "SqliteHarness", "PostgresHarness", "harness_factory",
                         "drive_backends", "open_board"):
                (tests / "test_drive_bad.py").write_text(f"h = {word}()\n")
                self.assertEqual(self.s.source_problems(tests),
                                 ["test_drive_bad: builds a board harness but never calls add_backend_variants"], word)
            (tests / "test_drive_bad.py").write_text("h = MemoryHarness()\n# add_backend_variants( in a comment\n")
            self.assertEqual(len(self.s.source_problems(tests)), 1)

    def test_a_pure_logic_module_need_not_run_on_postgres(self):
        """The Postgres job runs only what Postgres adds: a drive module with neither variants nor
        Env (CLI) tests runs in the offline jobs only."""
        log = _log(("test_drive_a.CasePostgres.test_x", "ok"))
        self.assertEqual(self.s.summarise(log, self.modules, expected={"test_drive_a"})[1], [])
        self.assertEqual(self.s.summarise(log, self.modules, expected={"test_drive_a", "test_drive_b"})[1],
                         ["test_drive_b: no test ran"])

    def test_main_reads_several_logs(self):
        with tempfile.TemporaryDirectory(dir=SANDBOX) as d:
            tests = Path(d) / "tests"
            tests.mkdir()
            (tests / "test_drive_a.py").write_text("add_backend_variants(globals(), A)\n")
            (tests / "test_drive_c.py").write_text("from test_hooks_cli import Env\n")
            (tests / "test_drive_p.py").write_text("pass\n")
            one, two = Path(d) / "one", Path(d) / "two"
            one.write_text(_log(("test_drive_a.CasePostgres.test_x", "ok")))
            two.write_text(_log(("test_drive_c.Cli.test_y", "ok")))
            self.assertEqual(self.s.main(["x", str(one), str(two), "--tests", str(tests)]), 0)
            self.assertEqual(self.s.main(["x", str(one), "--tests", str(tests)]), 1)   # the CLI module ran nothing

    def test_main_exit_status_follows_the_problems(self):
        with tempfile.TemporaryDirectory(dir=SANDBOX) as d:
            tests = Path(d) / "tests"
            tests.mkdir()
            (tests / "test_drive_a.py").write_text("add_backend_variants(globals(), A)\n")
            (tests / "test_drive_b.py").write_text("pass\n")
            log = Path(d) / "log"
            log.write_text(self.good())
            self.assertEqual(self.s.main(["x", str(log), str(tests)]), 0)
            log.write_text(_log(("test_drive_b.Other.test_y", "ok")))
            self.assertEqual(self.s.main(["x", str(log), str(tests)]), 1)
            (tests / "test_drive_a.py").write_text("def add_backend_variants(): pass\n")   # the helper's own file
            log.write_text(self.good())
            self.assertEqual(self.s.main(["x", str(log), str(tests)]), 0)


if __name__ == "__main__":
    unittest.main()
