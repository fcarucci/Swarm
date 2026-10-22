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
        self.assertIsNone(decision, reason)

    def granted_spawn(self):
        """A member's justified spawn (allowed); returns the hook output."""
        self.worker()
        return self.agent_call("w1", self.child())

    def refused_spawn(self):
        """A member's spawn without a rationale line; returns the hook output."""
        self.worker()
        return self.agent_call("w1", "[swarm job: J]\nCheck the logs.")

    def test_granted_spawn_gets_helper_model(self):
        self.cfg["models"] = {"mode": "default", "claude": {"worker": "opus", "helper": "haiku"}}
        out = self.granted_spawn()
        self.assertEqual(out["hookSpecificOutput"]["updatedInput"]["model"], "haiku")
        self.assertNotIn("permissionDecision", out["hookSpecificOutput"])       # Claude: updatedInput alone

    def test_policy_spawn_keeps_model_and_receives_job_identity(self):
        self.cfg["models"] = {"mode": "enforce", "claude": {"worker": "opus", "helper": "haiku"}}
        out = self.refused_spawn()
        updated = out["hookSpecificOutput"]["updatedInput"]
        self.assertEqual(updated["model"], "haiku")
        self.assertTrue(updated["prompt"].startswith("[swarm job: J]"))
        self.assertNotIn("permissionDecision", out["hookSpecificOutput"])

    def test_justified_spawn_goes_ahead_and_is_announced(self):
        name = self.worker()
        decision, _ = self.decision(self.agent_call("w1", self.child()))
        self.assertIsNone(decision)   # not allowed outright either: normal permissions apply
        posted = self.messages()[-1]
        self.assertTrue(posted.startswith("spawning log checker (1 total)"), posted)
        with self.board() as b:
            self.assertEqual(b.recent_messages(1, job="J")[-1].agent_name, name)

    def test_policy_refusals_do_not_block_an_unadorned_spawn(self):
        self.worker()
        out = self.agent_call("w1", "Do the assigned work.")
        decision, reason = self.decision(out)
        self.assertIsNone(decision, reason)
        self.assertTrue(out["hookSpecificOutput"]["updatedInput"]["prompt"].startswith("[swarm job: J]"))

    def test_missing_or_short_justification_is_not_a_spawn_barrier(self):
        self.worker()
        for prompt in ("[swarm job: J]\nCheck the logs.", self.child("[swarm spawn: faster]")):
            out = self.agent_call("w1", prompt)
            decision, reason = self.decision(out)
            self.assertIsNone(decision, reason)
        self.assertEqual(sum(m.startswith("spawning") for m in self.messages()), 2)

    def test_child_job_identity_is_added_automatically(self):
        self.worker()
        for prompt in (f"{WHY}\nno tag", self.child(job="other")):
            out = self.agent_call("w1", prompt)
            decision, reason = self.decision(out)
            self.assertIsNone(decision, reason)
            self.assertTrue(out["hookSpecificOutput"]["updatedInput"]["prompt"].startswith("[swarm job: J]"))

    def test_child_may_be_assigned_judge_role(self):
        self.worker()
        decision, reason = self.decision(self.agent_call("w1", self.child() + "\n[swarm role: judge]"))
        self.assertIsNone(decision, reason)

    def test_helpers_can_spawn_helpers_beyond_policy_depth(self):
        self.worker("h1", depth=2)
        decision, reason = self.decision(self.agent_call("h1", self.child()))
        self.assertIsNone(decision, reason)

    def test_unknown_depth_is_not_a_spawn_barrier(self):
        self.spawn("w1", "[swarm job: J]\nDo the work.")   # no meta.json
        decision, reason = self.decision(self.agent_call("w1", self.child()))
        self.assertIsNone(decision, reason)

    def test_spawn_accounting_continues_past_per_agent_and_job_caps(self):
        self.worker("w1")
        self.worker("w2")
        self.worker("w3")
        for _ in range(2):
            self.assertIsNone(self.decision(self.agent_call("w1", self.child()))[0])
        for _ in range(2):
            self.assertIsNone(self.decision(self.agent_call("w2", self.child()))[0])
        self.assertIsNone(self.decision(self.agent_call("w1", self.child()))[0])
        self.assertIsNone(self.decision(self.agent_call("w3", self.child()))[0])
        self.assertTrue(self.messages()[-1].startswith("spawning log checker (6 total)"))

    def test_allowed_spawn_remains_in_flight_until_tool_finishes(self):
        self.worker()
        self.agent_call("w1", "no justification")
        self.assertEqual(self.member("w1").current_tool, "Agent")

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

    def test_zero_configured_spawn_limit_is_not_a_barrier(self):
        self.cfg["spawn"]["max_per_job"] = 0
        self.meta("w1", 1)
        self.assertIn("Spawn helpers directly when useful", self.context(self.start("w1")))
        decision, reason = self.decision(self.agent_call("w1", self.child()))
        self.assertIsNone(decision, reason)

    def test_workers_are_told_the_rules(self):
        self.meta("w1", 1)
        start = self.start("w1")
        ctx = self.context(start)
        self.assertIn("Spawn helpers directly when useful", ctx)
        self.assertIn("child is assigned to this job automatically", ctx)
        self.assertNotIn("[swarm spawn:", ctx)


class JudgeSpawnTests(GoalEnv):
    def judge_spawn(self, prompt: str):
        out = self.hook("turn", agent_id="judge-1", session="sess-1", tool_name="Agent",
                        tool_input={"prompt": prompt}, transcript_path=self.main_transcript())
        return (out or {}).get("hookSpecificOutput", {})

    def test_judge_spawn_is_not_blocked_by_role_policy(self):
        self.activate_goal()
        _, turn = self.spawn_judge()
        self.assertIn("reviewing goal evidence", self.context(turn))
        self.assertNotIn("Judges only judge", self.context(turn))
        judge = self.member("judge-1").name
        out = self.judge_spawn(f"[swarm job: J]\n{WHY}")
        self.assertNotEqual(out.get("permissionDecision"), "deny")
