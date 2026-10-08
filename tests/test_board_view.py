"""The board view must not mislead (swarm.agentview): finished agents are not dead or idle, a
CLI-only orchestrator is not an idle worker, an agent parked on something is not a stalled one,
the job rollup is truthful, and an agent that runs `swarm join` does not appear twice.

The fixtures reproduce `swarm who --job swarm-stale-jobs` of 2026-10-08. The contract runs on the
memory, sqlite and file backends (Postgres with SWARM_TEST_CONFIG); the CLI and hook tests on
SWARM_TEST_BACKEND.
"""
from __future__ import annotations

import contextlib
import io
import os
import types
import unittest

from support import FileHarness, MemoryHarness, PostgresHarness, SqliteHarness  # noqa: F401  (sets sys.path)
from test_hooks_cli import Env  # noqa: E402

from swarm import cli as swarm  # noqa: E402

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

    def who(self, job="j"):
        """`swarm who` as {name: (role, state text, tool text)}: what a person reads."""
        self.age_contact()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            swarm._board_who(self.b, {}, types.SimpleNamespace(job=job))
        rows = {}
        for line in out.getvalue().splitlines():
            name, _harness, role, _title, state, _contact, tool = line.split("\t")
            rows[name] = (role, state, tool)
        return rows

    def shown(self, job="j"):
        return {n: types.SimpleNamespace(role=r, state=st, tool=t) for n, (r, st, t) in self.who(job).items()}

    def status(self, name, job="j"):
        return self.shown(job)[name].state.split(" (")[0]

    def rollup_line(self, job="j"):
        """The `agents` line of `swarm status --job`."""
        self.age_contact()
        text = swarm.job_detail(self.b, job, False)
        return next((ln.split(None, 1)[1].strip() for ln in text.splitlines() if ln.startswith("agents ")), None)

    # ---- the evidence of 2026-10-08, row by row
    def evidence(self):
        self.job()
        arnie = self.agent("arnie", "engineer", seen=19 * MIN)
        qa = self.agent("qa", "QA", seen=11 * HOUR)
        akira = self.agent("orchestrator-2", None, seen=6 * MIN)   # `swarm join --key orchestrator-2`: no role
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
        self.assertEqual(self.who()[n["arnie"]][1], "finished (posted DONE fix landed)")

    def test_orchestrator_is_not_an_idle_worker(self):
        n = self.evidence()
        self.assertEqual(self.who()[n["akira"]][:2], ("orchestrator", "standby (orchestrator, not a worker)"))

    def test_running_agent_is_untouched(self):
        n = self.evidence()
        self.assertEqual(self.who()[n["dewey"]][1:], ("running", "in Bash"))

    def test_rollup_of_the_evidence_job(self):
        self.evidence()
        self.assertEqual(self.rollup_line(), "1 working, 0 waiting, 3 finished, 0 lost, 1 orchestrator")

    # ---- finished vs dead edge cases
    def test_silent_while_owing_work_is_dead(self):
        self.job()
        w = self.agent("w", "worker", seen=2 * HOUR)
        self.post(w, "starting on the parser", 2 * HOUR)
        self.assertEqual(self.status(w), "dead")
        self.assertEqual(self.who()[w][1:], ("dead (silent, no DONE or verdict)", ""))

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
        table = swarm.agents_table(self.b, "j", False, self.b.now())
        self.assertRegex(table, rf"{a}.*completed")
        self.assertRegex(table, rf"{b}.*left")
        self.assertEqual(self.rollup_line(), "0 working, 0 waiting, 2 finished, 0 lost")

    def test_stuck_closed_agent_is_lost_unless_replaced(self):
        self.job()
        a = self.agent("a", "worker")
        self.b.close_agent("a", "stuck:dead")
        self.assertEqual(self.rollup_line(), "0 working, 0 waiting, 0 finished, 1 lost")

    # ---- idle: waiting vs stalled
    def test_more_than_400_later_messages_do_not_hide_a_done_post(self):
        """astroloom-m0: two agents posted DONE, then 450 other posts followed."""
        self.job()
        a = self.agent("a", "worker", seen=2 * HOUR)
        b = self.agent("b", "worker", seen=2 * HOUR)
        chatty = self.agent("c", "worker", seen=0)
        self.post(a, "DONE alpha", 3 * HOUR)
        self.post(b, "DONE beta, see PR", 3 * HOUR)
        for i in range(450):
            self.b.post("j", chatty, f"progress {i}")
        shown = self.who()
        self.assertEqual((shown[a][1].split(" (")[0], shown[b][1].split(" (")[0]), ("finished", "finished"))
        self.assertEqual(self.rollup_line(), "1 working, 0 waiting, 2 finished, 0 lost")

    def test_idle_with_a_declared_wait_is_waiting(self):
        self.job()
        w = self.agent("w", "worker", seen=10 * MIN)
        self.b.set_waiting("j", "GitHub Actions run 42", None)
        self.assertEqual(self.status(w), "waiting")
        self.assertEqual(self.who()[w][1], "waiting (on GitHub Actions run 42)")

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
        self.assertEqual(self.who()[w][1], "idle (silent, no wait stated)")

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
        self.assertEqual(self.rollup_line(), "1 working, 0 waiting, 0 finished, 0 lost, 1 orchestrator")

    def test_rollup_names_what_the_waiting_agents_wait_on(self):
        self.job()
        self.agent("a", "worker", seen=10 * MIN)
        self.agent("b", "worker", seen=10 * MIN)
        self.b.set_waiting("j", "CI", None)
        self.assertEqual(self.rollup_line(), "0 working, 2 waiting (on CI), 0 finished, 0 lost")


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


class ViewCliTests(Env):
    """`join`, `who`, `status` and the hooks, end to end."""

    def rows(self, job="J"):
        with self.board() as b:
            return b.agents(job)

    def test_default_role_of_a_cli_join(self):
        self.assertEqual(self.cli("activate", "--job", "J")[0], 0)
        for key, role, want in (("orchestrator", None, "orchestrator"), ("orchestrator-2", None, "orchestrator"),
                                ("orchestrators", None, None), ("worker-1", None, None),
                                ("orchestrator-3", "helper", "helper")):
            with self.subTest(key=key):
                rc, name, err = self.cli("join", "--job", "J", "--key", key, *(["--role", role] if role else []))
                self.assertEqual(rc, 0, err)
                self.assertEqual([a.role for a in self.rows() if a.name == name.strip()], [want])

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
        self.member()
        self.assertIsNone(self.rewritten("swarm join --job J --key agent-1"))

    def test_other_commands_and_the_orchestrator_are_not_rewritten(self):
        self.assertEqual(self.cli("activate", "--job", "J")[0], 0)
        self.hook("start", agent_id="agent-1")
        out = self.hook("turn", agent_id="agent-1", tool_name="Bash",
                        tool_input={"command": "swarm post --job J --as X --key k hi"})
        self.assertNotIn("updatedInput", (out or {}).get("hookSpecificOutput", {}))
        out = self.hook("turn", agent_id=None, tool_name="Bash",
                        tool_input={"command": "swarm join --job J --key orchestrator-2"})
        self.assertNotIn("updatedInput", (out or {}).get("hookSpecificOutput", {}))

    def rewritten(self, command, agent="agent-1"):
        """The command the hook makes the caller run, or None when it leaves it alone."""
        out = self.hook("turn", agent_id=agent, tool_name="Bash", tool_input={"command": command})
        return ((out or {}).get("hookSpecificOutput", {}).get("updatedInput") or {}).get("command")

    def member(self):
        self.assertEqual(self.cli("activate", "--job", "J")[0], 0)
        self.hook("start", agent_id="agent-1")

    def test_real_join_invocations_are_rewritten(self):
        self.member()
        for before, after in (
                ("swarm join --job J --key=k1", "swarm join --job J --key=agent-1"),
                ("cd x && swarm join --job J --key k1; ls", "cd x && swarm join --job J --key agent-1; ls"),
                ("swarm join --job J --key \"k1\" --judge", "swarm join --job J --key agent-1 --judge"),
                ("FOO=1 /p/bin/swarm join --key k1 --job J", "FOO=1 /p/bin/swarm join --key agent-1 --job J")):
            with self.subTest(before):
                self.assertEqual(self.rewritten(before), after)

    def test_mentions_of_join_are_never_rewritten(self):
        """A judge prompt for `codex exec`, an echo, a heredoc, a substitution: not a join call."""
        self.member()
        for command in (
                'codex exec "run swarm join --job J --key judge-1 --judge, then vote"',
                "codex exec 'swarm join --job J --key judge-1 --judge'",
                "echo swarm join --job J --key k1",
                "echo 'swarm join --job J --key k1'",
                "cat > p.md <<EOF\nswarm join --job J --key k1 --judge\nEOF",
                "cat <<'EOF'\nswarm join --job J --key k1\nEOF",
                "x=$(swarm join --job J --key k1)",
                "swarm join --job J --key $KEY",
                "swarm read --job J --key k1",
                "swarm join --job J --key agent-1"):
            with self.subTest(command):
                self.assertIsNone(self.rewritten(command))

    def test_a_join_of_another_job_is_not_rewritten(self):
        """Rewriting to the caller's key would MOVE it to the other job: never."""
        self.member()
        for command in ("swarm join --job OTHER --key k1", "swarm join --key k1 --job=OTHER"):
            with self.subTest(command):
                self.assertIsNone(self.rewritten(command))
        with self.board() as b:
            self.assertEqual(b.active_agent_name("k1"), None)

    def test_codex_host_is_not_rewritten(self):
        self.assertEqual(self.cli("activate", "--job", "J")[0], 0)
        self.hook("start", agent_id="c1", host="codex")
        out = self.hook("turn", agent_id="c1", host="codex", tool_name="Bash",
                        tool_input={"command": "swarm join --job J --key other"})
        self.assertNotIn("updatedInput", (out or {}).get("hookSpecificOutput", {}))


MIN = 60

if __name__ == "__main__":
    unittest.main()
