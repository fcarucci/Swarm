"""stale_reason flags a blocker whose subject is resolved; blocker resolve takes many ids."""
import datetime as dt
import unittest
from support import MemoryHarness, FileHarness, SqliteHarness
from swarm.blockers_stale import stale_reason


class Stale:
    def setUp(self):
        self.h = self.harness_factory()
        self.h.reset()
        self.addCleanup(self.h.close)
        self.b = self.h.board()
        self.addCleanup(self.b.close)
        self.b.open_job("j", None, None, None, None)
        self.b.join("j", "Ann", "engineer") if hasattr(self.b, "join") else None

    def blocker(self, reason):
        return self.b.open_blocker("j", "question", "orchestrator", reason)

    def test_pr_landed_is_stale(self):
        bl = self.blocker("land PR #24 @ abcdef1234")
        self.assertIsNone(stale_reason(bl, self.b))
        self.b.post("j", "swarm", "PR #24 landed on main")
        self.assertIn("PR #24 landed", stale_reason(bl, self.b))

    def test_superseded_sha_is_stale(self):
        bl = self.blocker("review feat/x@abcdef1234")
        self.b.post("j", "swarm", "BRANCH READY feat/x@abcdef1234")
        self.assertIsNone(stale_reason(bl, self.b))
        self.b.post("j", "swarm", "BRANCH READY feat/x@1234567abc")
        self.assertIn("superseded", stale_reason(bl, self.b))

    def test_other_branch_and_no_subject_not_stale(self):
        bl = self.blocker("review feat/x@abcdef1234")
        self.b.post("j", "swarm", "BRANCH READY feat/y@1234567abc")
        self.assertIsNone(stale_reason(bl, self.b))
        self.assertIsNone(stale_reason(self.blocker("pick a colour"), self.b))

    def test_closed_blocker_not_flagged(self):
        bl = self.blocker("land PR #3")
        self.b.post("j", "swarm", "PR #3 merged")
        self.b.resolve_blocker(bl.id, "done")
        self.assertIsNone(stale_reason(self.b.blocker(bl.id), self.b))


class MemoryStale(Stale, unittest.TestCase):
    harness_factory = MemoryHarness


class FileStale(Stale, unittest.TestCase):
    harness_factory = FileHarness


class SqliteStale(Stale, unittest.TestCase):
    harness_factory = SqliteHarness


from test_routing import RoutingEnv


class BlockerCli(RoutingEnv):
    def test_multi_id_resolve_and_stale_flag(self):
        self.activate("J")
        with self.board() as b:
            ids = [b.open_blocker("J", "question", "human", f"land PR #{n}").id for n in (7, 8, 9)]
            b.post("J", "swarm", "PR #7 landed")
        rc, out, err = self.cli("blockers", "--job", "J", "--open")
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(["POSSIBLY STALE" in l for l in out.splitlines()], [True, False, False])
        self.assertEqual(self.cli("blocker", "resolve", str(ids[0]), "999999")[0], 1)
        with self.board() as b:
            self.assertEqual(b.blocker(ids[0]).state, "open")
        rc, out, err = self.cli("blocker", "resolve", *map(str, ids), "--how", "done")
        self.assertEqual((rc, err), (0, ""))
        with self.board() as b:
            self.assertEqual([b.blocker(i).state for i in ids], ["resolved"] * 3)


if __name__ == "__main__":
    unittest.main()
