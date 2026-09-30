"""`swarm move`, `swarm job merge <from> --into <to>` and `swarm job <job> --goal`: the board
primitives on every backend (contract mixin), and the CLI plus a simulated LIVE agent's hook calls
across a move (the end-to-end tests run on $SWARM_TEST_BACKEND, like test_routing)."""
from __future__ import annotations

import json
import os
import unittest

from test_routing import RoutingEnv  # noqa: E402  (sets sys.path)
from support import SMALL_POOL, FileHarness, MemoryHarness, PostgresHarness, SqliteHarness  # noqa: E402,F401


class MoveContract:
    harness_factory = None

    @classmethod
    def setUpClass(cls):
        cls.h = cls.harness_factory()

    @classmethod
    def tearDownClass(cls):
        cls.h.close()

    def setUp(self):
        self.h.reset()
        self.b = self.h.board()
        self.addCleanup(self.b.close)
        self.b.open_job("a", "job a", None, "s1", "u")
        self.b.open_job("b", "job b", None, "s1", "u")

    def test_move_keeps_name_drops_seats_and_sets_a_catch_up_cursor(self):
        name = self.b.allocate_name("k1", "a", "worker")
        for i in range(5):
            self.b.post("b", "Someone", f"old {i}")
        self.b.post("a", "Someone", "on a")
        self.assertTrue(self.b.claim_judge("k1", "a"))
        self.b.tool_started("k1", "Bash")
        self.assertEqual(self.b.move_agent("k1", "b"), "a")
        row = next(x for x in self.b.agents("b") if x.agent_key == "k1")
        self.assertEqual((row.name, row.job, row.role, row.ended_at), (name, "b", "judge" if False else row.role, None))
        self.assertNotEqual(row.role, "judge")
        self.assertIsNone(self.b.job_status("a").judge)
        self.assertEqual(self.b.tool_started("k1", "Bash").job, "b")
        got = self.b.read_unread(agent_key="k1", job="b")   # the new job's recent history, not a's
        self.assertEqual([m.message for m in got.messages], [f"old {i}" for i in range(5)])
        self.assertEqual(self.b.read_unread(agent_key="k1", job="b").messages, [])   # once
        self.b.post("b", "Someone", "new")
        self.assertEqual([m.message for m in self.b.read_unread(agent_key="k1").messages], ["new"])

    def test_move_is_bounded_by_join_history(self):
        self.b.board_cfg["join_history"] = 2
        self.b.allocate_name("k1", "a")
        for i in range(6):
            self.b.post("b", "Someone", f"m{i}")
        self.b.move_agent("k1", "b")
        self.assertEqual([m.message for m in self.b.read_unread(agent_key="k1").messages], ["m4", "m5"])

    def test_move_leaves_a_notice_for_the_hook_and_resets_reminders(self):
        from swarm.board.base import MOVED_PREFIX
        self.b.allocate_name("k1", "a")
        self.b.record_roster_sync("k1", "[]", True)
        self.b.move_agent("k1", "b")
        st = self.b.sync_state("k1")
        self.assertEqual(st.roster_seen, MOVED_PREFIX + "a")
        self.assertIsNone(st.roster_synced_at)

    def test_move_refusals(self):
        self.b.allocate_name("k1", "a")
        self.b.close_job("b", "completed", "done")
        self.assertIsNone(self.b.move_agent("k1", "b"))          # closed target
        self.assertIsNone(self.b.move_agent("k1", "nope"))       # missing target
        self.assertIsNone(self.b.move_agent("ghost", "a"))       # unknown agent
        self.assertEqual(self.b.move_agent("k1", "a"), "a")      # same job: no-op
        self.b.leave(agent_key="k1")
        self.b.open_job("b", None, None, None, "u")
        self.assertIsNone(self.b.move_agent("k1", "b"))          # departed
        self.assertEqual(self.b.tool_started("k1", "x"), None)

    def test_move_updates_a_route(self):
        self.b.allocate_name("k1", "a")
        self.b.record_route("k1", "s1", "unverified", "a")
        self.b.move_agent("k1", "b")
        r = self.b.route("k1")
        self.assertEqual((r.state, r.job, r.member_job, r.session_id), ("final", "b", "b", "s1"))

    def test_moving_the_judge_frees_the_seat_for_another(self):
        self.b.allocate_name("j1", "a"); self.b.allocate_name("j2", "a")
        self.b.claim_judge("j1", "a")
        self.b.move_agent("j1", "b")
        self.assertTrue(self.b.claim_judge("j2", "a"))

    def test_set_job_goal(self):
        self.assertTrue(self.b.set_job_goal("a", "g1"))
        self.assertEqual(self.b.job_status("a").goal, "g1")
        self.b.allocate_name("j", "a"); self.b.claim_judge("j", "a")
        self.b.record_verdict("a", self.b.job_status("a").judge, "met", "ok")
        self.assertTrue(self.b.set_job_goal("a", "g1"))
        self.assertEqual(self.b.job_status("a").verdict, "met")   # same goal: kept
        self.assertTrue(self.b.set_job_goal("a", "g2"))
        js = self.b.job_status("a")
        self.assertEqual((js.goal, js.verdict, js.judge is not None), ("g2", None, True))
        self.assertFalse(self.b.set_job_goal("missing", "x"))
        self.b.close_job("a", "completed", None)
        self.assertFalse(self.b.set_job_goal("a", "g3"))

    def test_read_only_board_refuses_both(self):
        from swarm.board.base import WRITE_METHODS
        self.assertIn("move_agent", WRITE_METHODS)
        self.assertIn("set_job_goal", WRITE_METHODS)


class MemoryMove(MoveContract, unittest.TestCase):
    harness_factory = MemoryHarness


class SqliteMove(MoveContract, unittest.TestCase):
    harness_factory = SqliteHarness


class FileMove(MoveContract, unittest.TestCase):
    harness_factory = FileHarness


@unittest.skipUnless(os.environ.get("SWARM_TEST_CONFIG"), "set SWARM_TEST_CONFIG to run")
class PostgresMove(MoveContract, unittest.TestCase):
    harness_factory = staticmethod(lambda: PostgresHarness(os.environ["SWARM_TEST_CONFIG"]))


# ------------------------------------------------------------------ CLI and live hooks

class MoveEnv(RoutingEnv):
    def worker(self, key: str, job: str):
        self.spawn(key, f"[swarm job: {job}]\nDo the work.")
        return self.member(key).name

    def judge(self, key: str, job: str):
        self.spawn(key, f"[swarm job: {job}]\n[swarm role: judge]\nDecide.")
        return self.member(key).name

    def jstat(self, job: str):
        with self.board() as b:
            return b.job_status(job)

    def post(self, job: str, name: str, text: str):
        rc, _, err = self.cli("post", "--job", job, "--as", name, text)
        self.assertEqual(rc, 0, err)


class MoveTests(MoveEnv):
    def test_live_agent_next_turn_uses_the_new_job_once(self):
        self.activate("A"); self.activate("B", "--description", "the backend", "--task", "Port the API")
        n = self.worker("w1", "A")
        self.worker("w2", "B")
        self.post("B", self.member("w2").name, "we use FastAPI")
        self.post("B", self.member("w2").name, "schema is in db/")
        self.post("A", n, "still on A")
        self.turn("w1")   # a normal turn on A
        rc, out, err = self.cli("move", "--as", n, "--to", "B")
        self.assertEqual(rc, 0, err)
        self.assertIn(f"moved {n} from A to B", out)
        ctx = self.context(self.turn("w1"))   # the very next PreToolUse
        self.assertIn('moved from job "A" to job "B"', ctx)
        self.assertIn("the backend", ctx)
        self.assertIn("Port the API", ctx)
        self.assertIn("post a short hello", ctx)
        self.assertIn("--job 'B'", ctx)
        self.assertIn("we use FastAPI", ctx)          # the catch-up
        self.assertIn("schema is in db/", ctx)
        self.assertIn("other agents on job \"B\"", ctx)
        self.assertNotIn("still on A", ctx)
        again = self.turn("w1")                       # exactly once
        self.assertTrue(again is None or "moved from" not in self.context(again))
        self.assertTrue(again is None or "FastAPI" not in self.context(again))
        self.assertEqual(self.job_of("w1"), "B")
        self.assertEqual(self.member("w1").name, n)
        # status and heartbeat count under the new job
        self.assertEqual(next(a for a in self.board().__enter__().agents("B") if a.agent_key == "w1").status, "running")
        self.post("B", self.member("w2").name, "later")
        self.assertIn("later", self.context(self.turn("w1")))
        # the command it was shown (--job A) still lands on its new job
        rc, out, err = self.cli("post", "--job", "A", "--as", n, "hello from B")
        self.assertEqual(rc, 0, err)
        with self.board() as b:
            self.assertIn("hello from B", [m.message for m in b.recent_messages(5, job="B")])

    def test_move_binds_the_session_of_a_job_of_another_session(self):
        self.activate("A", session="sess-1"); self.activate("B", session="sess-2")
        n = self.worker("w1", "A")
        self.assertEqual(self.cli("move", "--as", n, "--to", "B")[0], 0)
        self.assertTrue((self.markers / "B--sess-1.json").exists())
        self.assertIn("moved from", self.context(self.turn("w1", session="sess-1")))

    def test_move_by_key_and_refusals(self):
        self.activate("A"); self.activate("B")
        n = self.worker("w1", "A")
        self.assertEqual(self.cli("move", "--key", "w1", "--to", "B")[0], 0)
        rc, _, err = self.cli("move", "--as", n, "--to", "B")
        self.assertEqual(rc, 1); self.assertIn("already on B", err)
        rc, _, err = self.cli("move", "--as", "Nobody", "--to", "B")
        self.assertEqual(rc, 1); self.assertIn("no active agent", err)
        rc, _, err = self.cli("move", "--as", n, "--to", "Z")
        self.assertEqual(rc, 1); self.assertIn("not an open job", err)
        self.assertEqual(self.cli("move", "--to", "A")[0], 2)
        self.assertEqual(self.cli("deactivate", "--job", "A")[0], 0)
        rc, _, err = self.cli("move", "--as", n, "--to", "A")
        self.assertEqual(rc, 1); self.assertIn("not an open job", err)

    def test_moving_the_judge_says_so(self):
        self.activate("A", "--goal", "ship it"); self.activate("B")
        j = self.judge("j1", "A")
        rc, out, _ = self.cli("move", "--as", j, "--to", "B")
        self.assertEqual(rc, 0)
        self.assertIn("was the judge of A", out)
        self.assertIsNone(self.jstat("A").judge)


class MergeTests(MoveEnv):
    def test_merge_moves_agents_appends_goal_and_closes(self):
        self.activate("A", "--goal", "goal of A"); self.activate("B", "--goal", "goal of B")
        ja = self.judge("ja", "A"); jb = self.judge("jb", "B")
        w1 = self.worker("w1", "A"); w2 = self.worker("w2", "B")
        self.post("B", w2, "context on B")
        self.turn("ja"); self.turn("w1")
        rc, out, err = self.cli("job", "merge", "A", "--into", "B")
        self.assertEqual(rc, 0, err)
        self.assertIn("merged A into B", out)
        self.assertIn(f"{ja} was A's judge", out)
        self.assertIn("Stop it", out)
        a, b = self.jstat("A"), self.jstat("B")
        self.assertEqual((a.status, a.outcome), ("completed", "merged into B"))
        self.assertEqual(b.goal, "goal of B\ngoal of A")
        self.assertEqual(b.judge, jb)
        self.assertIsNone(b.verdict)
        self.assertEqual({x.name for x in self.board().__enter__().agents("B", include_departed=False)},
                         {ja, jb, w1, w2})
        self.assertFalse((self.markers / "A.json").exists())
        # the merged judge is a live member, not stopped, and its next turn shows B
        ctx = self.context(self.turn("ja"))
        self.assertIn('moved from job "A" to job "B"', ctx)
        self.assertIn("context on B", ctx)
        self.assertIn("merged job A into this job", ctx)
        self.assertEqual(self.member("ja").ended_at, None)
        self.assertIn("goal of A", ctx)   # the goal now covers both
        rc, _, err = self.cli("verdict", "--job", "B", "--as", ja, "met", "x")
        self.assertEqual(rc, 1); self.assertIn("is not the judge", err)   # the seat did not move
        self.assertEqual(self.cli("verdict", "--job", "B", "--as", jb, "met", "x")[0], 0)

    def test_merge_goal_only_on_the_absorbed_job_needs_a_judge(self):
        self.activate("A", "--goal", "only A"); self.activate("B")
        self.worker("w1", "A")
        rc, out, _ = self.cli("job", "merge", "A", "--into", "B")
        self.assertEqual(rc, 0)
        self.assertEqual(self.jstat("B").goal, "only A")
        self.assertIn("B has a goal but no judge", out)
        self.assertTrue(json.loads((self.markers / "B.json").read_text())["goal"])

    def test_merge_refusals(self):
        self.activate("A"); self.activate("B")
        for args, msg in ((("A", "--into", "A"), "into itself"), (("Z", "--into", "B"), "no such job"),
                          (("A", "--into", "Z"), "no such job")):
            rc, _, err = self.cli("job", "merge", *args)
            self.assertEqual(rc, 1, args); self.assertIn(msg, err)
        self.assertEqual(self.cli("deactivate", "--job", "B")[0], 0)
        rc, _, err = self.cli("job", "merge", "A", "--into", "B")
        self.assertEqual(rc, 1); self.assertIn("closed job", err)
        rc, _, err = self.cli("job", "merge", "B", "--into", "A")
        self.assertEqual(rc, 1); self.assertIn("already closed", err)
        self.assertEqual(self.jstat("A").status, "active")
        self.assertEqual(self.cli("job", "merge", "A")[0], 2)

    def test_a_job_named_merge_is_still_creatable(self):
        rc, out, _ = self.cli("job", "merge", "--description", "d")
        self.assertEqual((rc, out.strip()), (0, "merge"))
        self.assertEqual(self.jstat("merge").description, "d")


class GoalTests(MoveEnv):
    def test_goal_can_be_set_after_activation_and_a_judge_seated(self):
        self.activate("A")
        rc, out, err = self.cli("job", "A", "--goal", "later goal")
        self.assertEqual(rc, 0, err)
        self.assertIn("goal of A set", out)
        self.assertIn("[swarm role: judge]", out)
        self.assertEqual(self.jstat("A").goal, "later goal")
        self.assertTrue(json.loads((self.markers / "A.json").read_text())["goal"])
        j = self.judge("j1", "A")            # no more "X is not the judge"
        self.assertEqual(self.jstat("A").judge, j)
        self.assertEqual(self.cli("verdict", "--job", "A", "--as", j, "met", "ok")[0], 0)
        rc, out, _ = self.cli("job", "A", "--goal", "-", stdin="new goal\n")
        self.assertEqual(rc, 0)
        self.assertIn("updated", out); self.assertIn("verdict is cleared", out)
        js = self.jstat("A")
        self.assertEqual((js.goal, js.verdict), ("new goal", None))

    def test_goal_refusals(self):
        self.activate("A")
        self.assertEqual(self.cli("deactivate", "--job", "A")[0], 0)
        rc, _, err = self.cli("job", "A", "--goal", "g")
        self.assertEqual(rc, 1); self.assertIn("can't be changed", err)
        self.assertEqual(self.cli("job", "B", "--goal", "  ")[0], 2)


if __name__ == "__main__":
    unittest.main()
