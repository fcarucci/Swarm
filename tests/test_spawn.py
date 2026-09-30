"""Swarm agents spawning subagents of their own. The PreToolUse hook lets a member's Agent call
through only with a `[swarm spawn: <why>]` justification and the job tag in the child's prompt,
within the [spawn] caps (per agent, per job, depth). The depth comes from Claude Code's
agent-<id>.meta.json next to the subagent transcript (verified on 2.1: {"spawnDepth": 1} for
the orchestrator's agents, 2 plus "parentAgentId" for theirs)."""
from __future__ import annotations

import json

from test_goals import GoalEnv  # noqa: E402  (sets sys.path)

WHY = "[swarm spawn: the three log sources can be checked in parallel and each takes long]"


class SpawnEnv(GoalEnv):
    def setUp(self):
        super().setUp()
        self.activate("J")

    def meta(self, agent_id: str, depth, session: str = "sess-1") -> None:
        path = self.projects / session / "subagents" / f"agent-{agent_id}.meta.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"agentType": "general-purpose", "spawnDepth": depth}))

    def worker(self, agent_id: str = "w1", depth=1) -> str:
        self.meta(agent_id, depth)
        self.spawn(agent_id, "[swarm job: J]\nDo the work.")
        return self.member(agent_id).name

    def agent_call(self, agent_id: str, prompt: str, description: str = "log checker"):
        return self.hook("turn", agent_id=agent_id, session="sess-1", tool_name="Agent",
                         tool_input={"description": description, "prompt": prompt,
                                     "subagent_type": "general-purpose"},
                         transcript_path=self.main_transcript())

    def decision(self, out) -> tuple[str | None, str]:
        o = (out or {}).get("hookSpecificOutput", {})
        return o.get("permissionDecision"), o.get("permissionDecisionReason", "")

    def messages(self) -> list[str]:
        with self.board() as b:
            return [m.message for m in b.recent_messages(50, job="J")]

    def child(self, why: str = WHY, job: str = "J") -> str:
        return f"[swarm job: {job}]\n{why}\nCheck the host logs."


class SpawnGateTests(SpawnEnv):
    def test_custom_child_keeps_role_and_uses_its_model(self):
        self.cfg["models"] = {"claude": {"engineer": "sonnet", "helper": "haiku"}}
        self.worker()
        prompt = self.child() + "\n[swarm role: engineer]"
        out = self.agent_call("w1", prompt)
        self.assertEqual(out["hookSpecificOutput"]["updatedInput"]["model"], "sonnet")
        self.meta("e1", 2)
        self.spawn("e1", prompt)
        self.assertEqual(self.member("e1").role, "engineer")
        decision, reason = self.decision(self.agent_call("e1", self.child()))
        self.assertEqual(decision, "deny")
        self.assertIn("depth", reason)

    def granted_spawn(self):
        """A member's justified spawn (allowed); returns the hook output."""
        self.worker()
        return self.agent_call("w1", self.child())

    def refused_spawn(self):
        """A member's spawn refused for a missing reason; returns the hook output."""
        self.worker()
        return self.agent_call("w1", "[swarm job: J]\nCheck the logs.")

    def test_granted_spawn_gets_helper_model(self):
        self.cfg["models"] = {"mode": "default", "claude": {"worker": "opus", "helper": "haiku"}}
        out = self.granted_spawn()
        self.assertEqual(out["hookSpecificOutput"]["updatedInput"]["model"], "haiku")
        self.assertNotIn("permissionDecision", out["hookSpecificOutput"])       # Claude: updatedInput alone

    def test_denied_spawn_has_no_updated_input(self):
        self.cfg["models"] = {"mode": "enforce", "claude": {"worker": "opus", "helper": "haiku"}}
        out = self.refused_spawn()
        self.assertNotIn("updatedInput", out["hookSpecificOutput"])
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_justified_spawn_goes_ahead_and_is_announced(self):
        name = self.worker()
        decision, _ = self.decision(self.agent_call("w1", self.child()))
        self.assertIsNone(decision)   # not allowed outright either: normal permissions apply
        posted = self.messages()[-1]
        self.assertTrue(posted.startswith("spawning log checker (1/4 for the job): the three log"), posted)
        with self.board() as b:
            self.assertEqual(b.recent_messages(1, job="J")[-1].agent_name, name)

    def test_missing_or_short_justification_is_refused(self):
        self.worker()
        for prompt in ("[swarm job: J]\nCheck the logs.", self.child("[swarm spawn: faster]")):
            decision, reason = self.decision(self.agent_call("w1", prompt))
            self.assertEqual(decision, "deny")
            self.assertIn("[swarm spawn: <why it is strictly needed>]", reason)
            self.assertIn("at least 30 characters", reason)
        self.assertFalse(any(m.startswith("spawning") for m in self.messages()))

    def test_child_must_join_this_job(self):
        self.worker()
        for prompt in (f"{WHY}\nno tag", self.child(job="other")):
            decision, reason = self.decision(self.agent_call("w1", prompt))
            self.assertEqual(decision, "deny")
            self.assertIn("[swarm job: J]", reason)

    def test_child_cannot_be_a_judge(self):
        self.worker()
        decision, reason = self.decision(self.agent_call("w1", self.child() + "\n[swarm role: judge]"))
        self.assertEqual(decision, "deny")
        self.assertIn("can't be a judge", reason)

    def test_helpers_cannot_spawn_helpers(self):
        self.worker("h1", depth=2)
        decision, reason = self.decision(self.agent_call("h1", self.child()))
        self.assertEqual(decision, "deny")
        self.assertIn("depth 2 and the limit is 2", reason)

    def test_unknown_depth_is_refused(self):
        self.spawn("w1", "[swarm job: J]\nDo the work.")   # no meta.json
        decision, reason = self.decision(self.agent_call("w1", self.child()))
        self.assertEqual(decision, "deny")
        self.assertIn("depth can't be determined", reason)

    def test_per_agent_and_per_job_caps(self):
        self.worker("w1")
        self.worker("w2")
        self.worker("w3")
        for _ in range(2):
            self.assertIsNone(self.decision(self.agent_call("w1", self.child()))[0])
        decision, reason = self.decision(self.agent_call("w1", self.child()))
        self.assertEqual(decision, "deny")
        self.assertIn("already spawned 2 (limit 2 per agent)", reason)
        for _ in range(2):
            self.assertIsNone(self.decision(self.agent_call("w2", self.child()))[0])
        decision, reason = self.decision(self.agent_call("w3", self.child()))
        self.assertEqual(decision, "deny")
        self.assertIn('already spawned 4 (limit 4 per job)', reason)

    def test_refusal_clears_the_in_flight_tool(self):
        self.worker()
        self.agent_call("w1", "no justification")
        self.assertIsNone(self.member("w1").current_tool)

    def test_unreachable_board_fails_closed_for_spawns_only(self):
        self.worker()
        from unittest import mock
        with mock.patch("swarm.board.open_board", side_effect=RuntimeError("board down")):
            decision, reason = self.decision(self.agent_call("w1", self.child()))
            self.assertEqual(decision, "deny")
            self.assertIn("could not be reached", reason)
            self.assertIsNone(self.turn("w1", tool="Bash"))   # other tools: never broken

    def test_other_tools_are_not_gated(self):
        self.worker()
        self.assertIsNone(self.decision(self.turn("w1", tool="Bash"))[0])

    def test_switched_off(self):
        self.cfg["spawn"]["max_per_job"] = 0
        self.meta("w1", 1)
        self.assertIn("Don't spawn subagents: that is switched off", self.context(self.start("w1")))
        decision, reason = self.decision(self.agent_call("w1", self.child()))
        self.assertEqual(decision, "deny")
        self.assertIn("switched off", reason)

    def test_workers_are_told_the_rules(self):
        self.meta("w1", 1)
        start = self.start("w1")
        ctx = self.context(start)
        self.assertIn("only when strictly needed", ctx)
        self.assertIn("2 per agent, 4 for the whole job, and no deeper than depth 2", ctx)
        self.assertIn("`[swarm job: J]`", ctx)


class JudgeSpawnTests(GoalEnv):
    def judge_spawn(self, prompt: str):
        out = self.hook("turn", agent_id="judge-1", session="sess-1", tool_name="Agent",
                        tool_input={"prompt": prompt}, transcript_path=self.main_transcript())
        return (out or {}).get("hookSpecificOutput", {})

    def test_the_judge_spawns_only_after_a_not_met_verdict(self):
        self.activate_goal()
        path = self.projects / "sess-1" / "subagents" / "agent-judge-1.meta.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"spawnDepth": 1}))
        _, turn = self.spawn_judge()
        self.assertIn("Only after you have recorded a not_met verdict may you spawn subagents", self.context(turn))
        o = self.judge_spawn(f"[swarm job: J]\n{WHY}")
        self.assertEqual(o["permissionDecision"], "deny")
        self.assertIn("only after it has recorded a not_met verdict", o["permissionDecisionReason"])
        judge = self.member("judge-1").name
        rc, _, _ = self.cli("verdict", "--job", "J", "--as", judge, "not_met",
                            "--reason", "no test", "--next", "add tests/test_x.py")
        self.assertEqual(rc, 0)
        o = self.judge_spawn(f"[swarm job: J]\n{WHY}\nadd tests/test_x.py")
        self.assertNotEqual(o.get("permissionDecision"), "deny", o)
        # never another judge, and never past the caps
        o = self.judge_spawn(f"[swarm job: J]\n[swarm role: judge]\n{WHY}")
        self.assertEqual(o["permissionDecision"], "deny")
        self.assertIn("can't be a judge", o["permissionDecisionReason"])
        self.judge_spawn(f"[swarm job: J]\n{WHY}")
        o = self.judge_spawn(f"[swarm job: J]\n{WHY}")
        self.assertEqual(o["permissionDecision"], "deny")
        self.assertIn("limit 2 per agent", o["permissionDecisionReason"])
        # a met verdict closes the door again
        self.cli("verdict", "--job", "J", "--as", judge, "met", "fixed")
        o = self.judge_spawn(f"[swarm job: J]\n{WHY}")
        self.assertEqual(o["permissionDecision"], "deny")
