"""Goals and the judge: `activate --goal`, the `[swarm role: judge]` tag, `swarm verdict`, and the
completion gate in `deactivate`. The hooks read the role tag from the same place as the job tag:
the first user message of the subagent's transcript (see test_routing)."""
from __future__ import annotations

import json

from test_routing import RoutingEnv  # noqa: E402  (sets sys.path)

from swarm import spool  # noqa: E402

GOAL = "The HA cluster survives the loss of any one node with no lost writes."


class GoalEnv(RoutingEnv):
    def activate_goal(self, job: str = "J", goal: str = GOAL, **kw):
        return self.activate(job, "--goal", goal, **kw)

    def judge_prompt(self, job: str = "J") -> str:
        return f"[swarm job: {job}]\n[swarm role: judge]\nDecide whether the goal is met."

    def spawn_judge(self, agent_id: str = "judge-1", job: str = "J"):
        return self.spawn(agent_id, self.judge_prompt(job))

    def spawn_worker(self, agent_id: str, job: str = "J"):
        return self.spawn(agent_id, f"[swarm job: {job}]\nDo the work.")

    def job(self, job: str = "J"):
        with self.board() as b:
            return b.job_status(job)


class GoalCliTests(GoalEnv):
    def test_goal_is_stored_marked_and_both_tag_lines_printed(self):
        out = self.activate_goal()
        self.assertIn("\n[swarm job: J]\n", out)
        self.assertIn("\n[swarm role: judge]\n", out)
        self.assertIn("spawn exactly one judge", out)
        self.assertEqual(self.job().goal, GOAL)
        self.assertTrue(json.loads((self.markers / "J.json").read_text())["goal"])

    def test_goal_from_stdin(self):
        rc, _, _ = self.cli("activate", "--job", "J", "--session", "sess-1", "--goal", "-",
                            stdin="  line one\nline two\n")
        self.assertEqual(rc, 0)
        self.assertEqual(self.job().goal, "line one\nline two")

    def test_goal_and_task_cannot_both_come_from_stdin(self):
        rc, _, err = self.cli("activate", "--job", "J", "--goal", "-", "--task", "-", stdin="x")
        self.assertEqual(rc, 2)
        self.assertIn("only one of --goal and --task can be -", err)

    def test_no_goal_no_judge_line(self):
        out = self.activate("J")
        self.assertNotIn("[swarm role: judge]", out)
        self.assertFalse(json.loads((self.markers / "J.json").read_text()).get("goal"))

    def test_status_and_detail_show_goal_and_verdict(self):
        self.activate_goal()
        self.activate("K")
        _, out, _ = self.cli("status", "--no-color")
        lines = out.splitlines()
        self.assertRegex(lines[0], r"\sVERDICT\s+WAITING ON\s+DESCRIPTION$")
        self.assertRegex(next(l for l in lines if l.startswith("J ")), r"\snone(\s|$)")
        self.assertRegex(next(l for l in lines if l.startswith("K ")), r"\s-(\s|$)")
        _, out, _ = self.cli("status", "--job", "J", "--no-color")
        self.assertIn(f"goal       {GOAL}\n", out)
        self.assertIn("verdict    none yet (no judge on the job yet)\n", out)
        self.spawn_judge()
        judge = self.member("judge-1").name
        self.cli("verdict", "--job", "J", "--as", judge, "not_met", "--reason", "no failover drill", "--next", "run the db-2 failover drill")
        _, out, _ = self.cli("status", "--job", "J", "--no-color")
        self.assertRegex(out, rf"verdict    not_met by {judge}, \d+s ago: no failover drill\n")
        self.assertIn("\nnext       run the db-2 failover drill\n", out)
        self.assertRegex(out, rf"\n{judge}\s+judge\s+claude\s+running\s")
        _, out, _ = self.cli("status", "--no-color")
        self.assertRegex(next(l for l in out.splitlines() if l.startswith("J ")), r"\snot_met(\s|$)")


class JudgeHookTests(GoalEnv):
    def setUp(self):
        super().setUp()
        self.activate_goal()

    def test_a_goal_job_routes_at_the_first_tool_call(self):
        # the role tag isn't readable at SubagentStart, so nobody is enrolled blind
        start, turn = self.spawn_worker("w1")
        self.assertIsNone(start)
        self.assertIn('working on job "J"', self.context(turn))

    def test_judge_gets_judge_instructions(self):
        start, turn = self.spawn_judge()
        self.assertIsNone(start)
        ctx = self.context(turn)
        name = self.member("judge-1").name
        self.assertTrue(ctx.startswith(f'[swarm] You are **{name}**, the JUDGE of job "J".'))
        self.assertIn(f"Goal: {GOAL}", ctx)
        self.assertIn("You don't do the work", ctx)
        self.assertIn("Judge strictly against the goal text", ctx)
        self.assertIn(f"verdict --job 'J' --as '{name}' met \"<short reason>\"", ctx)
        self.assertIn(f"verdict --job 'J' --as '{name}' not_met --reason \"<why it is not met>\" "
                      f"--next \"<what to change, where, and what you will re-check>\"", ctx)
        self.assertIn("REFUSED without both --reason", ctx)
        self.assertIn("Only after you have recorded a not_met verdict may you spawn subagents", ctx)
        self.assertIn("Spawning subagents: only when strictly needed", ctx)
        self.assertIn("--to", ctx)
        self.assertEqual(self.job().judge, name)
        self.assertEqual(self.member("judge-1").role, "judge")

    def test_workers_are_told_who_the_judge_is(self):
        self.spawn_judge()
        judge = self.member("judge-1").name
        _, turn = self.spawn_worker("w1")
        ctx = self.context(turn)
        self.assertIn(f"Goal: {GOAL}", ctx)
        self.assertIn(f"The judge, {judge}, decides whether it is met: the job is not done until "
                      f"{judge} records the verdict met.", ctx)
        self.assertIn(f"- {judge} (judge): ", ctx)  # and it is in the roster
        self.assertNotIn("the JUDGE", ctx)

    def test_worker_before_the_judge_is_told_one_is_coming(self):
        _, turn = self.spawn_worker("w1")
        self.assertIn("The judge (shown as (judge) in the roster once it joins) decides whether it is met",
                      self.context(turn))

    def test_second_judge_is_refused_politely(self):
        self.spawn_judge("judge-1")
        first = self.member("judge-1").name
        _, turn = self.spawn_judge("judge-2")
        ctx = self.context(turn)
        self.assertIn(f"[swarm] Your prompt makes you a judge, but {first} is already the judge of "
                      f"job \"J\" (one per job).", ctx)
        self.assertIn('working on job "J"', ctx)  # on the board as a worker
        self.assertEqual(self.job().judge, first)
        self.assertNotEqual(self.member("judge-2").role, "judge")

    def test_judge_tag_in_a_job_without_goal_is_just_a_worker(self):
        self.activate("K")
        _, turn = self.spawn("x", "[swarm job: K]\n[swarm role: judge]\n")
        ctx = self.context(turn)
        self.assertIn('working on job "K"', ctx)
        self.assertNotIn("JUDGE", ctx)
        self.assertIsNone(self.job("K").judge)

    def test_resumed_judge_gets_its_seat_back(self):
        self.spawn_judge()
        name = self.member("judge-1").name
        self.hook("stop", agent_id="judge-1")
        self.assertIsNone(self.job().judge)
        out = self.start("judge-1")  # resumed through SendMessage: the transcript exists
        self.assertIn(f"You are **{name}**, the JUDGE", self.context(out))
        self.assertEqual(self.job().judge, name)


class ExternalAgentRoleTests(GoalEnv):
    """Agents outside Claude Code (e.g. a Codex CLI judge) get no hooks: `join --judge` or
    `--verifier` gives them the seat, and the CLI (read, post, verdict) is all they need."""

    def setUp(self):
        super().setUp()
        self.activate_goal()

    def test_join_as_judge_takes_the_seat_and_may_record_a_verdict(self):
        rc, out, err = self.cli("join", "--job", "J", "--key", "codex-judge", "--role", "codex", "--judge")
        self.assertEqual(rc, 0, err)
        name = out.strip()
        self.assertEqual(self.job().judge, name)
        rc, out, _ = self.cli("verdict", "--job", "J", "--as", name, "met", "checked", "it")
        self.assertEqual(rc, 0)
        self.assertEqual(self.job().verdict, "met")

    def test_second_judge_is_refused_and_keeps_no_seat(self):
        self.spawn_judge()
        first = self.member("judge-1").name
        rc, out, err = self.cli("join", "--job", "J", "--key", "codex-judge", "--judge")
        self.assertEqual(rc, 1)
        self.assertIn(f"{first} is already the judge", err)
        self.assertEqual(self.job().judge, first)

    def test_join_as_verifier(self):
        rc, out, err = self.cli("join", "--job", "J", "--key", "codex-verifier", "--verifier")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.member("codex-verifier").role, "verifier")

    def test_judge_and_verifier_are_exclusive(self):
        with self.assertRaises(SystemExit) as cm:   # argparse rejects it before anything runs
            self.cli("join", "--job", "J", "--key", "x", "--judge", "--verifier")
        self.assertEqual(cm.exception.code, 2)


class VerdictTests(GoalEnv):
    def setUp(self):
        super().setUp()
        self.activate_goal()
        self.spawn_judge()
        self.spawn_worker("w1")
        self.judge = self.member("judge-1").name
        self.worker = self.member("w1").name

    def test_only_the_judge_may_record_a_verdict(self):
        rc, out, err = self.cli("verdict", "--job", "J", "--as", self.worker, "met", "trust", "me")
        self.assertEqual((rc, out), (1, ""))
        self.assertEqual(err, f"refused: {self.worker} is not the judge of job J\n")
        rc, _, err = self.cli("verdict", "--job", "J", "--as", "Nobody", "met", "x")
        self.assertEqual(rc, 1)
        self.assertIsNone(self.job().verdict)
        rc, out, _ = self.cli("verdict", "--job", "J", "--as", self.judge, "met", "drills", "pass")
        self.assertEqual((rc, out.splitlines()[0]), (0, "verdict met recorded for J, and posted on the board"))
        j = self.job()
        self.assertEqual((j.verdict, j.verdict_reason, j.verdict_by), ("met", "drills pass", self.judge))

    def test_not_met_is_refused_without_reason_and_next(self):
        for extra, missing in (([], "--reason"), (["--reason", "why"], "--next"),
                               (["--next", "do x"], "--reason"), (["--reason", " ", "--next", "x"], "--reason")):
            rc, out, err = self.cli("verdict", "--job", "J", "--as", self.judge, "not_met", *extra)
            self.assertEqual((rc, out), (1, ""), extra)
            self.assertIn("a not_met verdict must say why and what to do to meet the goal", err)
            self.assertIn(missing, err)
        self.assertIsNone(self.job().verdict)
        # refused before anything is queued, even with the board unreachable
        self.h.set_available(False)
        rc, _, _ = self.cli("verdict", "--job", "J", "--as", self.judge, "not_met", "--reason", "why")
        self.assertEqual(rc, 1)
        self.assertEqual(list(self.spool_dir.glob("*.vrd")), [])

    def test_not_met_stores_reason_and_next_and_met_clears_next(self):
        rc, out, _ = self.cli("verdict", "--job", "J", "--as", self.judge, "not_met",
                              "--reason", "no drill", "--next", "run the drill in scripts/drill.sh")
        self.assertEqual((rc, out), (0, "verdict not_met recorded for J, and posted on the board\n"))
        j = self.job()
        self.assertEqual((j.verdict_reason, j.verdict_next), ("no drill", "run the drill in scripts/drill.sh"))
        self.cli("verdict", "--job", "J", "--as", self.judge, "met", "drill", "ran")
        j = self.job()
        self.assertEqual((j.verdict, j.verdict_next), ("met", None))
        _, out, _ = self.cli("status", "--job", "J", "--no-color")
        self.assertNotIn("\nnext ", out)

    def test_not_met_is_broadcast_to_the_workers(self):
        self.turn("w1")  # catch up first
        self.cli("verdict", "--job", "J", "--as", self.judge, "not_met", "--reason", "no test of db-2 loss",
                 "--next", "add a db-2 loss test in tests/test_pg.py")
        ctx = self.context(self.turn("w1"))
        self.assertIn(f"{self.judge}: VERDICT not_met: no test of db-2 loss", ctx)
        self.assertIn("NEXT (to meet the goal; full text in swarm status): add a db-2 loss test", ctx)

    def test_verdict_spooled_when_unreachable_and_delivered_by_a_hook(self):
        self.h.set_available(False)
        rc, out, _ = self.cli("verdict", "--job", "J", "--as", self.judge, "not_met", "--reason", "missing drill", "--next", "run it")
        self.assertEqual(rc, 0)
        self.assertTrue(out.startswith("queued (board not reachable from here: ConnectionError)"))
        self.assertEqual(len(list(self.spool_dir.glob("*.vrd"))), 1)
        self.h.set_available(True)
        ctx = self.context(self.turn("w1"))
        self.assertIn("VERDICT not_met: missing drill", ctx)
        self.assertEqual(self.job().verdict, "not_met")
        self.assertEqual(self.job().verdict_next, "run it")
        self.assertEqual(list(self.spool_dir.iterdir()), [])

    def test_spooled_verdict_without_next_from_an_older_release_is_still_delivered(self):
        import json as _json
        path = spool.spool_verdict(self.cfg, "J", self.judge, "not_met", "old style")
        rec = _json.loads(path.read_text())
        rec.pop("next")
        path.write_text(_json.dumps(rec))
        self.context(self.turn("w1"))
        j = self.job()
        self.assertEqual((j.verdict, j.verdict_reason, j.verdict_next), ("not_met", "old style", None))

    def test_spooled_verdict_from_a_non_judge_is_refused_and_the_sender_told(self):
        spool.spool_verdict(self.cfg, "J", self.worker, "met", "sneaky")
        ctx = self.context(self.turn("w1"))
        self.assertIn(f"swarm → {self.worker}: verdict refused: {self.worker} is not the judge of job J", ctx)
        self.assertIsNone(self.job().verdict)
        self.assertEqual(len(list(self.spool_dir.glob("*.bad"))), 1)


class CompletionGateTests(GoalEnv):
    def setUp(self):
        super().setUp()
        self.activate_goal()
        self.spawn_judge()
        self.judge = self.member("judge-1").name

    def test_refuses_without_a_verdict_and_keeps_the_job_running(self):
        rc, out, err = self.cli("deactivate", "--job", "J")
        self.assertEqual((rc, out), (1, ""))
        self.assertIn("not completing J: the judge has not recorded a met verdict (no verdict yet).", err)
        self.assertIn("--force", err)
        self.assertTrue((self.markers / "J.json").exists())
        self.assertEqual(self.job().status, "active")

    def test_refuses_with_not_met_and_prints_the_reason(self):
        self.cli("verdict", "--job", "J", "--as", self.judge, "not_met", "--reason", "no loss drill", "--next", "run it")
        rc, _, err = self.cli("deactivate", "--job", "J", "--status", "completed")
        self.assertEqual(rc, 1)
        self.assertIn(f"latest verdict: not_met by {self.judge}: no loss drill", err)

    def test_completes_with_met(self):
        self.cli("verdict", "--job", "J", "--as", self.judge, "not_met", "--reason", "first", "--next", "n")
        self.cli("verdict", "--job", "J", "--as", self.judge, "met", "all", "drills", "pass")
        rc, out, _ = self.cli("deactivate", "--job", "J")
        self.assertEqual((rc, out.splitlines()[0]), (0, "deactivated J (completed)"))
        j = self.job()
        self.assertEqual((j.status, j.completion_forced), ("completed", False))
        self.assertFalse((self.markers / "J.json").exists())

    def test_force_completes_and_records_the_override(self):
        rc, out, _ = self.cli("deactivate", "--job", "J", "--force")
        self.assertEqual((rc, out.splitlines()[0]), (0, "deactivated J (completed, forced without a met verdict)"))
        j = self.job()
        self.assertEqual((j.status, j.completion_forced), ("completed", True))
        _, out, _ = self.cli("status", "--job", "J", "--no-color")
        self.assertIn("forced     completed without a met verdict\n", out)

    def test_cancelled_and_failed_are_always_allowed(self):
        rc, out, _ = self.cli("deactivate", "--job", "J", "--status", "cancelled")
        self.assertEqual((rc, out.splitlines()[0]), (0, "deactivated J (cancelled)"))
        self.activate_goal()
        rc, out, _ = self.cli("deactivate", "--job", "J", "--status", "failed")
        self.assertEqual((rc, out.splitlines()[0]), (0, "deactivated J (failed)"))
        self.assertFalse(self.job().completion_forced)

    def test_jobs_without_goal_complete_as_before(self):
        self.activate("K")
        rc, out, _ = self.cli("deactivate", "--job", "K")
        self.assertEqual((rc, out.splitlines()[0]), (0, "deactivated K (completed)"))

    def test_unreachable_board_refuses_a_goal_job_unless_forced(self):
        self.h.set_available(False)
        rc, _, err = self.cli("deactivate", "--job", "J")
        self.assertEqual(rc, 1)
        self.assertIn("cannot check the judge's verdict", err)
        self.assertTrue((self.markers / "J.json").exists())
        rc, _, err = self.cli("deactivate", "--job", "J", "--force")
        self.assertEqual(rc, 0)
        self.assertFalse((self.markers / "J.json").exists())
