"""The SubagentStop memory gate (hooks._memory_stop): an agent that ends normally must have stored a
memory (`swarm remember`) or declared none (`swarm remember --none`). Otherwise the stop is blocked
once, and the next stop goes through, marked unharvested for the learnings step. Never loops."""
from unittest import mock

from test_hindsight import HindsightEnv
from swarm import learnings


class MemoryStopTests(HindsightEnv):
    def setUp(self):
        super().setUp()
        self.enable()
        self.assertEqual(self.cli("activate", "--job", "J")[0], 0)
        self.hook("start", agent_id="agent-1")
        self.name = self.agent("agent-1").name

    def status(self, key: str = "agent-1"):
        with self.board() as b:
            return next(a for a in b.agents("J", include_departed=True) if a.agent_key == key)

    def stop(self, key: str = "agent-1", again: bool = False) -> dict | None:
        return self.hook("stop", agent_id=key, stop_hook_active=again)

    @staticmethod
    def blocked(out: dict | None) -> bool:
        return bool(out) and out.get("decision") == "block"

    def test_stored_memory_stops_cleanly(self):
        rc, _, err = self.cli("remember", "--job", "J", "--as", self.name, "Root cause: the lock was held.")
        self.assertEqual(rc, 0, err)
        out = self.stop()
        self.assertFalse(self.blocked(out), out)
        self.assertIsNotNone(self.status().ended_at)
        with self.board() as b:
            self.assertEqual(learnings.unharvested(b, "J"), [])

    def test_declared_none_stops_cleanly(self):
        rc, _, err = self.cli("remember", "--job", "J", "--as", self.name, "--none", "Only routine edits here.")
        self.assertEqual(rc, 0, err)
        out = self.stop()
        self.assertFalse(self.blocked(out), out)
        self.assertIsNotNone(self.status().ended_at)
        with self.board() as b:
            self.assertEqual(learnings.unharvested(b, "J"), [])

    def test_neither_is_blocked_once_then_allowed_and_marked_unharvested(self):
        out = self.stop()
        self.assertTrue(self.blocked(out), out)
        self.assertIn("swarm remember", out["reason"])
        self.assertIn("--none", out["reason"])
        self.assertIsNone(self.status().ended_at)          # still active: the stop was refused

        out = self.stop(again=True)
        self.assertFalse(self.blocked(out), out)
        self.assertIsNotNone(self.status().ended_at)       # now completed
        with self.board() as b:
            marks = learnings.unharvested(b, "J")
        self.assertEqual(len(marks), 1, marks)
        self.assertEqual(marks[0]["name"], self.name)
        self.assertEqual(marks[0]["key"], "agent-1")

    def test_a_third_stop_never_blocks_again(self):
        self.stop()
        self.stop(again=True)
        out = self.stop(again=True)
        self.assertFalse(self.blocked(out), out)
        with self.board() as b:
            self.assertEqual(len(learnings.unharvested(b, "J")), 1)

    def test_a_gate_error_never_blocks_the_stop_and_leaves_the_agent_unharvested(self):
        with mock.patch.object(learnings, "_counts", side_effect=RuntimeError("boom")):
            out = self.stop()
        self.assertFalse(self.blocked(out), out)
        self.assertIsNotNone(self.status().ended_at)
        with self.board() as b:
            marks = learnings.unharvested(b, "J")
        self.assertEqual([m["key"] for m in marks], ["agent-1"])
        self.assertIn("unverified", marks[0]["why"])
