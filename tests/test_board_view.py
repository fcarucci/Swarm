"""The board view must not mislead (swarm.agentview): finished agents are not dead or idle, a
CLI-only orchestrator is not an idle worker, an agent parked on something is not a stalled one,
the job rollup is truthful, and an agent that runs `swarm join` does not appear twice.

The fixtures reproduce `swarm who --job swarm-stale-jobs` of 2026-10-08. The contract runs on the
memory, sqlite and file backends (Postgres with SWARM_TEST_CONFIG); the CLI and hook tests on
SWARM_TEST_BACKEND.
"""
from __future__ import annotations

import os
import unittest

from support import FileHarness, MemoryHarness, PostgresHarness, SqliteHarness  # noqa: F401  (sets sys.path)
from test_hooks_cli import Env  # noqa: E402

from swarm import agentview  # noqa: E402
from swarm import hooks as swarm_hooks  # noqa: E402

MIN = 60
HOUR = 3600


class ViewContract:
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
        self.contact = {}

    # ---- helpers
    def job(self, job="j", goal="ship it"):
        self.b.open_job(job, "d", None, None, "me", goal=goal)
        self.h.backdate_job(job, created_at=30 * HOUR, activated_at=30 * HOUR)

    def agent(self, key, role=None, joined=24 * HOUR, seen=0, job="j"):
        name = self.b.allocate_name(key, job, role)
        self.h.backdate_agent(key, joined_at=joined)
        self.contact[key] = seen      # applied when the view is read: a post refreshes last_seen
        return name

    def post(self, name, text, ago, job="j"):
        self.h.backdate_message(self.b.post(job, name, text).id, ago)

    def age_contact(self):
        for key, seen in self.contact.items():
            self.h.backdate_agent(key, last_seen=seen)

    def shown(self, job="j"):
        self.age_contact()
        rows = agentview.annotate(self.b, job, self.b.agents(job))
        return {a.name: a for a in rows}

    def rollup(self):
        self.age_contact()
        return agentview.rollup(agentview.annotate(self.b, "j", self.b.agents("j")))

    def status(self, name, job="j"):
        return self.shown(job)[name].status

    # ---- the evidence of 2026-10-08, row by row
    def evidence(self):
        self.job()
        arnie = self.agent("arnie", "engineer", seen=19 * MIN)
        qa = self.agent("qa", "QA", seen=11 * HOUR)
        akira = self.agent("orchestrator-2", "orchestrator", seen=6 * MIN)
        judge = self.agent("judge", "judge", seen=10 * HOUR)
        dewey = self.agent("dewey", "general-purpose", seen=0)
        self.post(arnie, "DONE fix landed", 11 * HOUR)
        self.post(qa, "QA1: committed tests", 11 * HOUR + 5 * MIN)
        self.b.claim_judge("judge", "j")
        self.b.record_verdict("j", judge, "met", "all good")
        self.h.backdate_job("j", verdict_at=10 * HOUR)
        self.b.tool_started("dewey", "Bash")
        return dict(arnie=arnie, qa=qa, akira=akira, judge=judge, dewey=dewey)

    def test_raw_statuses_are_the_misleading_ones(self):
        n = self.evidence()
        self.age_contact()
        raw = {a.name: a.status for a in self.b.agents("j")}
        self.assertEqual((raw[n["arnie"]], raw[n["qa"]], raw[n["judge"]], raw[n["akira"]], raw[n["dewey"]]),
                         ("idle", "dead", "dead", "idle", "running"))

    def test_finished_agents_are_finished_not_idle_or_dead(self):
        n = self.evidence()
        self.assertEqual(self.status(n["arnie"]), "finished")   # posted DONE, then went quiet
        self.assertEqual(self.status(n["qa"]), "finished")      # quiet before the goal was met
        self.assertEqual(self.status(n["judge"]), "finished")   # recorded verdict met
        self.assertIn("DONE", self.shown()[n["arnie"]].current_tool)

    def test_orchestrator_is_not_an_idle_worker(self):
        n = self.evidence()
        a = self.shown()[n["akira"]]
        self.assertEqual((a.role, a.status), ("orchestrator", "standby"))

    def test_running_agent_is_untouched(self):
        n = self.evidence()
        a = self.shown()[n["dewey"]]
        self.assertEqual((a.status, a.current_tool), ("running", "Bash"))

    def test_rollup_of_the_evidence_job(self):
        self.evidence()
        r = self.rollup()
        self.assertEqual((r.working, r.waiting, r.idle, r.finished, r.lost, r.orchestrators),
                         (1, 0, 0, 3, 0, 1))
        self.assertEqual(r.text(), "1 working, 0 waiting, 3 finished, 0 lost, 1 orchestrator")

    # ---- finished vs dead edge cases
    def test_silent_while_owing_work_is_dead(self):
        self.job()
        w = self.agent("w", "worker", seen=2 * HOUR)
        self.post(w, "starting on the parser", 2 * HOUR)
        self.assertEqual(self.status(w), "dead")
        self.assertIn("no DONE or verdict", self.shown()[w].current_tool)

    def test_done_then_more_work_is_not_finished(self):
        self.job()
        w = self.agent("w", "worker", seen=2 * HOUR)
        self.post(w, "DONE part one", 3 * HOUR)
        self.post(w, "part two: started the follow-up", 2 * HOUR)
        self.assertEqual(self.status(w), "dead")

    def test_done_inside_a_sentence_is_not_a_hand_off(self):
        self.job()
        w = self.agent("w", "worker", seen=2 * HOUR)
        self.post(w, "not DONE yet", 2 * HOUR)
        self.assertEqual(self.status(w), "dead")

    def test_a_done_post_of_a_previous_incarnation_does_not_count(self):
        self.job()
        w = self.agent("w", "worker", joined=1 * HOUR, seen=40 * MIN)
        self.post(w, "DONE long ago", 5 * HOUR)   # before this row joined
        self.assertEqual(self.status(w), "dead")

    def test_not_met_verdict_judge_that_went_quiet_is_lost(self):
        self.job()
        j = self.agent("judge", "judge", seen=2 * HOUR)
        self.b.claim_judge("judge", "j")
        self.b.record_verdict("j", j, "not_met", "fix it", "do x")
        self.h.backdate_job("j", verdict_at=2 * HOUR)
        self.assertEqual(self.status(j), "dead")

    def test_verifier_failed_post_is_finished(self):
        self.job()
        v = self.agent("v", "verifier", seen=2 * HOUR)
        self.post(v, "FAILED tests red: see log", 2 * HOUR)
        self.assertEqual(self.status(v), "finished")

    def test_completed_and_left_keep_their_word(self):
        self.job()
        a = self.agent("a", "worker")
        b = self.agent("b", "worker")
        self.b.agent_stopped("a")
        self.b.close_agent("b", "left")
        shown = self.shown()
        self.assertEqual((shown[a].status, shown[b].status), ("completed", "left"))

    def test_stuck_closed_agent_is_lost_unless_replaced(self):
        self.job()
        a = self.agent("a", "worker")
        self.b.close_agent("a", "stuck:dead")
        self.assertEqual(self.status(a), "dead")
        self.assertIn("lost", self.shown()[a].current_tool)

    # ---- idle: waiting vs stalled
    def test_idle_with_a_declared_wait_is_waiting(self):
        self.job()
        w = self.agent("w", "worker", seen=10 * MIN)
        self.b.set_waiting("j", "GitHub Actions run 42", None)
        self.assertEqual(self.status(w), "waiting")
        self.assertIn("GitHub Actions run 42", self.shown()[w].current_tool)

    def test_idle_whose_last_post_says_it_waits_is_waiting(self):
        self.job()
        w = self.agent("w", "worker", seen=10 * MIN)
        self.post(w, "pushed, waiting for CI", 10 * MIN)
        self.assertEqual(self.status(w), "waiting")

    def test_idle_with_nothing_said_is_idle(self):
        self.job()
        w = self.agent("w", "worker", seen=10 * MIN)
        self.post(w, "refactoring the parser", 10 * MIN)
        self.assertEqual(self.status(w), "idle")
        self.assertIn("no wait stated", self.shown()[w].current_tool)

    def test_bounded_wait_covers_a_long_silent_agent_but_unbounded_does_not(self):
        self.job()
        w = self.agent("w", "worker", seen=2 * HOUR)
        self.b.set_waiting("j", "nightly build", None)
        self.assertEqual(self.status(w), "dead")
        import datetime as dt
        self.b.set_waiting("j", "nightly build", self.b.now() + dt.timedelta(hours=3))
        self.assertEqual(self.status(w), "waiting")

    # ---- the rollup line
    def test_job_with_one_running_worker_and_an_idle_orchestrator_has_no_idle_workers(self):
        self.job()
        self.agent("o", "orchestrator", seen=10 * MIN)
        self.agent("w", "worker")
        self.b.tool_started("w", "Bash")
        r = self.rollup()
        self.assertEqual((r.working, r.idle, r.orchestrators), (1, 0, 1))
        self.assertNotIn("idle", r.text())

    def test_rollup_names_what_the_waiting_agents_wait_on(self):
        self.job()
        self.agent("a", "worker", seen=10 * MIN)
        self.agent("b", "worker", seen=10 * MIN)
        self.b.set_waiting("j", "CI", None)
        r = self.rollup()
        self.assertEqual(r.text(), "0 working, 2 waiting (on CI), 0 finished, 0 lost")


class MemoryView(ViewContract, unittest.TestCase):
    harness_factory = MemoryHarness


class SqliteView(ViewContract, unittest.TestCase):
    harness_factory = SqliteHarness


class FileView(ViewContract, unittest.TestCase):
    harness_factory = FileHarness


@unittest.skipUnless(os.environ.get("SWARM_TEST_CONFIG"),
                     "set SWARM_TEST_CONFIG to a throwaway Postgres board config to run")
class PostgresView(ViewContract, unittest.TestCase):
    harness_factory = staticmethod(lambda: PostgresHarness(os.environ["SWARM_TEST_CONFIG"]))


class JoinRoleTests(unittest.TestCase):
    def test_default_role(self):
        for key in ("orchestrator", "orchestrator-2", "Orchestrator_x"):
            self.assertEqual(agentview.default_role(key, None), "orchestrator", key)
        for key in ("orchestrators", "worker-1", "my-orchestrator"):
            self.assertIsNone(agentview.default_role(key, None), key)
        self.assertEqual(agentview.default_role("orchestrator-2", "helper"), "helper")


class ViewCliTests(Env):
    """`join`, `who`, `status` and the hooks, end to end."""

    def rows(self, job="J"):
        with self.board() as b:
            return b.agents(job)

    def test_cli_orchestrator_join_gets_the_orchestrator_role(self):
        self.assertEqual(self.cli("activate", "--job", "J")[0], 0)
        rc, name, err = self.cli("join", "--job", "J", "--key", "orchestrator-2")
        self.assertEqual(rc, 0, err)
        self.assertEqual([a.role for a in self.rows() if a.name == name.strip()], ["orchestrator"])
        rc, out, _ = self.cli("who", "--job", "J")
        self.assertIn("orchestrator", out)

    def test_status_job_shows_the_rollup_line_and_one_working_agent_is_not_idle(self):
        self.assertEqual(self.cli("activate", "--job", "J")[0], 0)
        self.cli("join", "--job", "J", "--key", "orchestrator-2")
        self.hook("start", agent_id="w1")
        self.hook("turn", agent_id="w1", tool_name="Bash")
        self.h.backdate_agent("orchestrator-2", last_seen=10 * MIN)
        rc, out, err = self.cli("status", "--job", "J", "--no-color")
        self.assertEqual(rc, 0, err)
        self.assertRegex(out, r"agents\s+1 working, 0 waiting, 0 finished, 0 lost, 1 orchestrator")
        self.assertNotRegex(out, r"\bidle\b")
        rc, out, _ = self.cli("status", "--no-color")
        self.assertRegex(out, r"WORKERS")
        self.assertIn("1 working", out)

    def test_hook_agent_running_join_keeps_one_row(self):
        """The duplicate-identity bug: a hook-registered subagent runs `swarm join --key X`."""
        self.assertEqual(self.cli("activate", "--job", "J")[0], 0)
        self.hook("start", agent_id="agent-1")
        with self.board() as b:
            name = b.active_agent_name("agent-1")
        out = self.hook("turn", agent_id="agent-1", tool_name="Bash",
                        tool_input={"command": "/x/bin/swarm join --job J --key my-own-key --role engineer"})
        spec = out["hookSpecificOutput"]
        self.assertEqual(spec["updatedInput"]["command"],
                         "/x/bin/swarm join --job J --key agent-1 --role engineer")
        self.assertIn("already have a swarm identity", spec["additionalContext"])
        # the rewritten call is what runs: join returns the name the agent already has
        rc, out, err = self.cli("join", "--job", "J", "--key", "agent-1", "--role", "engineer")
        self.assertEqual((rc, out.strip()), (0, name), err)
        self.assertEqual(len(self.rows()), 1)

    def test_join_with_the_agents_own_key_is_left_alone(self):
        self.assertEqual(self.cli("activate", "--job", "J")[0], 0)
        self.hook("start", agent_id="agent-1")
        out = self.hook("turn", agent_id="agent-1", tool_name="Bash",
                        tool_input={"command": "swarm join --job J --key agent-1"})
        self.assertNotIn("updatedInput", (out or {}).get("hookSpecificOutput", {}))

    def test_other_commands_and_the_orchestrator_are_not_rewritten(self):
        self.assertEqual(self.cli("activate", "--job", "J")[0], 0)
        self.hook("start", agent_id="agent-1")
        out = self.hook("turn", agent_id="agent-1", tool_name="Bash",
                        tool_input={"command": "swarm post --job J --as X --key k hi"})
        self.assertNotIn("updatedInput", (out or {}).get("hookSpecificOutput", {}))
        out = self.hook("turn", agent_id=None, tool_name="Bash",
                        tool_input={"command": "swarm join --job J --key orchestrator-2"})
        self.assertNotIn("updatedInput", (out or {}).get("hookSpecificOutput", {}))

    def test_own_key_command_forms(self):
        f = swarm_hooks.own_key_command
        self.assertEqual(f("swarm join --job J --key=k1", "A"), "swarm join --job J --key=A")
        self.assertEqual(f("cd x && swarm join --job J --key k1; ls", "A"), "cd x && swarm join --job J --key A; ls")
        self.assertEqual(f("swarm join --job J --key \"k1\" --judge", "A"), "swarm join --job J --key A --judge")
        self.assertIsNone(f("swarm join --job J --key A", "A"))
        self.assertIsNone(f("swarm read --key k1", "A"))
        self.assertIsNone(f("echo swarm join", "A"))

    def test_codex_host_is_not_rewritten(self):
        self.assertEqual(self.cli("activate", "--job", "J")[0], 0)
        self.hook("start", agent_id="c1", host="codex")
        out = self.hook("turn", agent_id="c1", host="codex", tool_name="Bash",
                        tool_input={"command": "swarm join --job J --key other"})
        self.assertNotIn("updatedInput", (out or {}).get("hookSpecificOutput", {}))


MIN = 60

if __name__ == "__main__":
    unittest.main()
