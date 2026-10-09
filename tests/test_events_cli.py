"""`swarm event post|list|ack|wait` and the hook surfacing of pending events, end to end on the
backend named by $SWARM_TEST_BACKEND (default memory), through the same Env as test_hooks_cli."""
from __future__ import annotations

import json
import threading
import time
import unittest

from test_hooks_cli import Env   # noqa: F401  (also sets sys.path)


class EventCliTests(Env):
    def setUp(self):
        super().setUp()
        rc, _, err = self.cli("activate", "--job", "J", "--session", "sess-1")
        self.assertEqual(rc, 0, err)

    def post(self, kind, key, text, *extra):
        return self.cli("event", "post", "--job", "J", "--kind", kind, "--key", key, *extra, *text.split())

    def test_post_is_idempotent_and_reports_the_id(self):
        rc, out, _ = self.post("NEEDS-REVIEW", "7@abc", "PR 7 opened", "--source", "gitea")
        self.assertEqual((rc, out), (0, "event 1 posted\n"))
        rc, out, _ = self.post("NEEDS-REVIEW", "7@abc", "PR 7 opened again")
        self.assertEqual((rc, out), (0, "event 1 exists\n"))
        rc, out, _ = self.cli("event", "post", "--job", "J", "--kind", "NEEDS-REVIEW", "--key", "7@abc", "--json", "x")
        self.assertEqual(json.loads(out), {"id": 1, "created": False})

    def test_post_refuses_bad_input_with_exit_1(self):
        rc, _, err = self.cli("event", "post", "--job", "J", "--kind", "no good", "--key", "1", "t")
        self.assertEqual(rc, 1)
        self.assertIn("kind", err)
        rc, _, err = self.cli("event", "post", "--job", "nosuch", "--kind", "A", "--key", "1", "t")
        self.assertEqual(rc, 1)

    def test_list_pending_and_json(self):
        self.post("A", "1", "for the pm", "--to", "@PM")
        self.post("B", "1", "for nobody in particular")
        self.cli("event", "ack", "--job", "J", "1")
        rc, out, _ = self.cli("event", "list", "--job", "J")
        self.assertEqual(rc, 0)
        self.assertIn("1 acked A 1 @pm: for the pm", out)
        self.assertIn("2 pending B 1: for nobody in particular", out)
        rc, out, _ = self.cli("event", "list", "--job", "J", "--pending", "--json")
        rows = json.loads(out)
        self.assertEqual([r["id"] for r in rows], [2])
        self.assertEqual(set(rows[0]), {"id", "job", "kind", "key", "to", "text", "source", "created_at",
                                        "acked_at", "acked_by"})
        self.assertIsNone(rows[0]["to"])
        rc, out, _ = self.cli("event", "list", "--job", "J", "--to", "@pm")
        self.assertEqual([line.split()[0] for line in out.splitlines()], ["1", "2"])   # @pm hears the unaddressed too
        rc, out, _ = self.cli("event", "list", "--job", "J", "--pending", "--to", "@el")
        self.assertEqual(out, "(no events)\n")

    def test_ack_counts_and_records_who(self):
        self.post("A", "1", "one")
        self.post("A", "2", "two")
        rc, out, _ = self.cli("event", "ack", "--job", "J", "1", "2", "99", "--as", "Alice")
        self.assertEqual((rc, out), (0, "acked 2 events\n"))
        rows = json.loads(self.cli("event", "list", "--job", "J", "--json")[1])
        self.assertEqual({r["acked_by"] for r in rows}, {"Alice"})
        self.assertEqual(self.cli("event", "ack", "--job", "J", "1")[1], "acked 0 events\n")

    def test_wait_returns_pending_at_once_and_prints_it(self):
        self.post("READY-TO-LAND", "7@abc", "PR 7 ready", "--to", "@pm")
        rc, out, _ = self.cli("event", "wait", "--job", "J", "--to", "@pm", "--timeout", "30")
        self.assertEqual(rc, 0)
        self.assertIn("READY-TO-LAND 7@abc", out)
        rc, out, _ = self.cli("event", "wait", "--job", "J", "--to", "@pm", "--timeout", "30", "--json")
        self.assertEqual([r["kind"] for r in json.loads(out)], ["READY-TO-LAND"])

    def test_wait_exits_124_on_timeout(self):
        t0 = time.monotonic()
        rc, out, _ = self.cli("event", "wait", "--job", "J", "--to", "@pm", "--timeout", "0.5")
        self.assertEqual((rc, out), (124, ""))
        self.assertGreaterEqual(time.monotonic() - t0, 0.4)

    def test_wait_wakes_on_a_later_post(self):
        def later():
            with self.board() as b:
                b.post_event("J", "CI-FAILED", "7@abc", "ci failed", to="@pm")
        timer = threading.Timer(0.6, later)   # the board API: the CLI's output redirection is process-wide
        timer.start()
        self.addCleanup(timer.join)
        t0 = time.monotonic()
        rc, out, _ = self.cli("event", "wait", "--job", "J", "--to", "@pm", "--timeout", "30")
        self.assertEqual(rc, 0)
        self.assertIn("CI-FAILED", out)
        self.assertLess(time.monotonic() - t0, 10)


class EventHookTests(Env):
    """Pending events show in the PreToolUse output until acked: to the orchestrator
    (unaddressed or @pm/@orchestrator events), to an agent by name and to it by role."""

    def setUp(self):
        super().setUp()
        rc, _, err = self.cli("activate", "--job", "J", "--session", "sess-1")
        self.assertEqual(rc, 0, err)

    def post(self, kind, key, text, *extra):
        rc, _, err = self.cli("event", "post", "--job", "J", "--kind", kind, "--key", key, *extra, *text.split())
        self.assertEqual(rc, 0, err)

    def orch(self):
        return self.hook("turn", agent_id=None, tool_name="Bash", cwd="/orch")

    def test_orchestrator_sees_pending_events_until_acked(self):
        self.assertIsNone(self.orch())
        self.post("NEEDS-REVIEW", "7@abc", "PR 7 opened")
        self.post("READY-TO-LAND", "8@def", "PR 8 ready", "--to", "@pm")
        self.post("CI-FAILED", "9@x", "for the engineers", "--to", "@el")
        ctx = self.context(self.orch())
        lines = ctx.split("\n")
        self.assertRegex(lines[0], r"^\[swarm events\] 2 pending \(ack when handled: .*swarm.* event ack --job 'J' ID\.\.\.\):$")
        self.assertRegex(lines[1], r"^\[swarm events\] #1 NEEDS-REVIEW 7@abc: PR 7 opened$")
        self.assertRegex(lines[2], r"^\[swarm events\] #2 READY-TO-LAND 8@def: PR 8 ready$")
        self.assertEqual(len(lines), 3)   # the @el event is not the orchestrator's
        self.assertIn("#1", self.context(self.orch()))   # shown again: until acked, not once
        self.cli("event", "ack", "--job", "J", "1", "2")
        self.assertIsNone(self.orch())

    def test_an_ack_whose_generation_lands_elsewhere_still_clears_within_the_check_window(self):
        """Live report (swarm-orphans): acked events kept showing. An ack run through another
        plugin install bumps the generation in that install's fast-path dir, not the hook's; the
        hook's cached result must still expire within EVENT_CHECK_SECONDS."""
        from unittest import mock
        from swarm import fastpath, hooks
        self.post("A", "1", "one")
        self.assertIn("#1", self.context(self.orch()))
        with mock.patch.object(fastpath, "changed"):          # the bump went to another dir
            self.cli("event", "ack", "--job", "J", "1")
        real = fastpath.now()
        with mock.patch.object(fastpath, "now", return_value=real + hooks.EVENT_CHECK_SECONDS + 1):
            self.assertIsNone(self.orch())

    def test_an_ack_from_the_orchestrating_session_is_recorded_as_the_orchestrator(self):
        """Not as whichever subagent of that session the board lists first (they share its id)."""
        from unittest import mock
        from swarm import hosts
        for key in ("sub-1", "sub-2"):
            self.hook("start", agent_id=key, session="sess-1")
        self.post("A", "1", "one")
        self.post("A", "2", "two")
        with mock.patch.object(hosts, "cli_session_id", return_value="sess-1"):
            self.cli("event", "ack", "--job", "J", "1")
        with mock.patch.object(hosts, "cli_session_id", return_value="another-session"):
            self.cli("event", "ack", "--job", "J", "2")
        rows = {r["id"]: r["acked_by"] for r in json.loads(self.cli("event", "list", "--job", "J", "--json")[1])}
        self.assertEqual(rows, {1: "orchestrator", 2: "human"})

    def test_the_cap_keeps_the_output_compact(self):
        for i in range(1, 9):
            self.post("A", str(i), f"event {i}")
        lines = self.context(self.orch()).split("\n")
        shown = [x for x in lines if x.startswith("[swarm events] #")]
        self.assertEqual(len(shown), 5)
        self.assertIn("8 pending", lines[0])
        self.assertIn("3 more", lines[-1])

    def test_long_text_is_cut_to_one_short_line(self):
        self.post("A", "1", "word " * 100)
        (line,) = [x for x in self.context(self.orch()).split("\n") if x.startswith("[swarm events] #")]
        self.assertLessEqual(len(line), 200)
        self.assertTrue(line.endswith("…"))

    def test_an_agent_sees_events_for_its_name_and_its_role_but_not_others(self):
        self.assertEqual(self.cli("join", "--job", "J", "--key", "agent-1", "--role", "engineering_lead")[0], 0)
        name = self.agent("agent-1").name
        self.hook("turn", tool_name="Bash")   # the roster notice, once
        self.post("A", "1", "to me by name", "--to", name)
        self.post("A", "2", "to my role", "--to", "@EL")
        self.post("A", "3", "to someone else", "--to", "Bob")
        self.post("A", "4", "to the orchestrator")
        ctx = self.context(self.hook("turn", tool_name="Read"))
        self.assertIn("#1 A 1: to me by name", ctx)
        self.assertIn("#2 A 2: to my role", ctx)
        self.assertNotIn("someone else", ctx)
        self.assertNotIn("to the orchestrator", ctx)
        self.assertIn("#1", self.context(self.hook("turn", tool_name="Read")))   # again: not acked
        self.cli("event", "ack", "--job", "J", "1", "2")
        self.assertIsNone(self.hook("turn", tool_name="Read"))

    def test_an_agent_holding_the_pm_seat_takes_pm_events_from_the_orchestrator(self):
        self.assertEqual(self.cli("join", "--job", "J", "--key", "agent-1", "--role", "project_manager")[0], 0)
        self.hook("turn", tool_name="Bash")
        self.post("A", "1", "to the pm", "--to", "@pm")
        self.assertIn("#1 A 1: to the pm", self.context(self.hook("turn", tool_name="Read")))
        self.assertIsNone(self.orch())


if __name__ == "__main__":
    unittest.main()
