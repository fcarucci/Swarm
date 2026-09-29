"""Verifiers: `[swarm role: verifier]` in the spawn prompt. Any number per job, with or without a
goal. They get their own instructions (check claims, post VERIFIED/FAILED, read-only), and the
hook refuses their writing tools and spawns. Like the judge tag, the role tag is readable only
from the first tool call: a verifier enrolled blind at SubagentStart (one job, no goal) is
switched to verifier at its first tool call, before that call runs."""
from __future__ import annotations

from test_goals import GoalEnv  # noqa: E402  (sets sys.path)

VERIFIER = "[swarm job: J]\n[swarm role: verifier]\nCheck the others' claims."


class VerifierEnv(GoalEnv):
    def verifier(self, agent_id: str = "v1"):
        """Spawn a verifier; returns (start output, first turn output) with a Bash first call."""
        return self.spawn(agent_id, VERIFIER)

    def call(self, agent_id: str, tool: str, **tool_input):
        return self.hook("turn", agent_id=agent_id, session="sess-1", tool_name=tool,
                         tool_input=tool_input, transcript_path=self.main_transcript())

    def denied(self, out) -> str | None:
        o = (out or {}).get("hookSpecificOutput", {})
        return o.get("permissionDecisionReason") if o.get("permissionDecision") == "deny" else None


class VerifierWithoutGoalTests(VerifierEnv):
    def setUp(self):
        super().setUp()
        self.activate("J")

    def test_switched_to_verifier_at_the_first_tool_call(self):
        start, turn = self.verifier()
        self.assertIn("working on job \"J\"", self.context(start))   # blind: worker text first
        ctx = self.context(turn)
        name = self.member("v1").name
        self.assertIn("Your prompt makes you a verifier: this replaces the worker instructions", ctx)
        self.assertIn(f'You are **{name}**, a VERIFIER on job "J"', ctx)
        self.assertIn("VERIFIED: <claim>", ctx)
        self.assertIn("You are read-only", ctx)
        self.assertEqual(self.member("v1").role, "verifier")

    def test_first_call_already_gated(self):
        self.start("v1")
        self.write_prompt("v1", VERIFIER)
        out = self.call("v1", "Edit", file_path="/x")
        self.assertIn("verifiers are read-only", self.denied(out))
        self.assertIn("a VERIFIER on job", self.context(out))
        self.assertIsNone(self.member("v1").current_tool)

    def test_writing_tools_and_spawns_refused_reading_allowed(self):
        self.verifier()
        for tool in ("Edit", "Write", "MultiEdit", "NotebookEdit", "Agent", "Task"):
            self.assertIsNotNone(self.denied(self.call("v1", tool)), tool)
        for tool in ("Bash", "Read", "Grep"):
            self.assertIsNone(self.denied(self.call("v1", tool)), tool)

    def test_verifier_shell_write_is_refused_and_read_allowed(self):
        self.verifier("v1")                     # the file's helper: spawns verifier "v1"
        out = self.call("v1", "Bash", command="echo x > report.txt")
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("output redirection", out["hookSpecificOutput"]["permissionDecisionReason"])
        out = self.call("v1", "Bash", command="grep -rn 'a > b' .")
        self.assertNotEqual((out or {}).get("hookSpecificOutput", {}).get("permissionDecision"), "deny")

    def test_several_verifiers_and_workers_are_not_gated(self):
        self.verifier("v1")
        self.verifier("v2")
        _, turn = self.spawn_worker("w1")
        self.assertEqual((self.member("v1").role, self.member("v2").role), ("verifier", "verifier"))
        self.assertIn("post `DONE: <what, and how to check it>`", self.context(self.start("w2")))
        self.assertIsNone(self.denied(self.call("w1", "Edit", file_path="/x")))

    def test_status_counts_verifier_posts_only(self):
        self.verifier("v1")
        self.spawn_worker("w1")
        v, w = self.member("v1").name, self.member("w1").name
        self.cli("post", "--job", "J", "--as", w, "DONE: backups run nightly")
        self.cli("post", "--job", "J", "--as", w, "VERIFIED: self-verification does not count")
        self.cli("post", "--job", "J", "--as", v, "--to", w, "VERIFIED: backups run nightly")
        self.cli("post", "--job", "J", "--as", v, "--to", w, "FAILED: restore test: no restore was run")
        rc, out, _ = self.cli("status", "--job", "J", "--no-color")
        self.assertIn("checks     1 verified, 1 failed (verifiers)", out)
        self.assertRegex(out, rf"{v}\s+verifier\s")

    def test_activate_prints_the_verifier_tag(self):
        out = self.activate("K")
        self.assertIn("[swarm role: verifier]", out)


class VerifierWithGoalTests(VerifierEnv):
    def test_goal_job_verifier_knows_the_judge(self):
        self.activate_goal()
        self.spawn_judge()
        judge = self.member("judge-1").name
        start, turn = self.verifier()
        self.assertIsNone(start)   # goal jobs enrol at the first tool call
        ctx = self.context(turn)
        self.assertIn("a VERIFIER on job", ctx)
        self.assertIn(f"{judge} is the one who rules on it", ctx)
        self.assertNotIn("replaces the worker instructions", ctx)
        self.assertEqual(self.job().judge, judge)   # the verifier took no judge seat
