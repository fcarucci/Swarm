"""A resumed subagent keeps its own name and role, and never gets the judge seat unless its own
prompt asks for it.

Field case (job ha-energy, 2026-10-09): the engineer subagent was spawned at 18:19, before the job
was activated at 18:24, so its hooks never adopted it (no --adopt-running). It was put on the board
by hand with `swarm join --key energy-eng --role engineer` (Otto Mann). When the orchestrator resumed
it through SendMessage, SubagentStart found no row for its real agent id and enrolled it afresh: a
random pool name, "Judge Constance Harm", with the agent type as its role. It never held the judge
seat (judge=false on its row), but the name read like one.

Fixed: such a subagent's own `swarm join` runs with its agent id as the key (the rewrite members
already had), and pool names that read like a seat are never given out."""
from __future__ import annotations

import shlex

from test_goals import GoalEnv  # noqa: E402  (sets sys.path)
from test_supervise_hooks import SupervisedEnv  # noqa: E402

from swarm.board.base import assignable_name  # noqa: E402

JOIN = "/x/bin/swarm join --job J --key energy-eng --role engineer --title 'Eng: energy'"
VERDICT = '/x/bin/swarm verdict --job J not_met --reason "x" --next "fix y"'
ORCH_DENY = "[swarm] a subagent cannot join as the orchestrator; only the main session is the orchestrator"


class ResumeIdentityTests(GoalEnv):
    def setUp(self):
        super().setUp()
        # the engineer was already running when the job was activated: its transcript exists,
        # and no SubagentStart of it is seen after the activation
        self.write_prompt("eng-1", "Configure the HA energy panel and a dashboard.")
        self.activate_goal()

    def bash(self, agent, command):
        out = self.hook("turn", agent_id=agent, session="sess-1", tool_name="Bash",
                        transcript_path=self.main_transcript(), tool_input={"command": command})
        return ((out or {}).get("hookSpecificOutput", {}).get("updatedInput") or {}).get("command")

    def run_cli(self, command):
        rc, out, err = self.cli(*shlex.split(command)[1:])
        self.assertEqual(rc, 0, err)
        return out.strip()

    def join_engineer_by_hand(self) -> str:
        command = self.bash("eng-1", JOIN)
        self.assertIsNotNone(command, "the join was not rewritten to the agent's own key")
        self.assertIn("--key eng-1", command)
        self.assertNotIn("energy-eng", command)
        return self.run_cli(command)

    def test_running_subagent_joins_under_its_own_agent_id(self):
        self.assertIsNone(self.member("eng-1"))   # not adopted: it predates the activation
        name = self.join_engineer_by_hand()
        me = self.member("eng-1")
        self.assertEqual((me.name, me.role, me.title), (name, "engineer", "Eng: energy"))
        self.assertIsNone(self.agent("energy-eng"))

    def bash_out(self, agent, command) -> dict:
        out = self.hook("turn", agent_id=agent, session="sess-1", tool_name="Bash",
                        transcript_path=self.main_transcript(), tool_input={"command": command})
        return (out or {}).get("hookSpecificOutput", {})

    def test_two_jobs_in_one_command_only_the_own_join_is_rewritten(self):
        self.activate("K")
        self.write_prompt("eng-2", "[swarm job: J]\nConfigure the HA energy panel.")
        other = "/x/bin/swarm join --job K --key k-other --role engineer"
        new = self.bash("eng-2", f"{JOIN} && {other}")
        self.assertEqual(new, f"{JOIN.replace('energy-eng', 'eng-2')} && {other}")

    def test_untagged_subagent_in_a_two_job_session_moves_no_join(self):
        # its own job is unknown (no [swarm job: ...] tag, two jobs bound): no join is rewritten
        self.activate("K")
        command = f"{JOIN} && /x/bin/swarm join --job K --key k-other --role engineer"
        self.assertIsNone(self.bash("eng-1", command))

    def test_subagent_cannot_join_as_orchestrator(self):
        for command in ("/x/bin/swarm join --job J --key orchestrator --role engineer",
                        "/x/bin/swarm join --job J --key=orchestrator --role engineer"):
            o = self.bash_out("eng-1", command)
            self.assertEqual(o.get("permissionDecision"), "deny", command)
            self.assertEqual(o.get("permissionDecisionReason"),
                             "[swarm] a subagent cannot join as the orchestrator; "
                             "only the main session is the orchestrator")
            self.assertNotIn("updatedInput", o)
        self.assertIsNone(self.member("eng-1"))
        self.assertIsNone(self.agent("orchestrator"))

    def test_resumed_after_judge_finished_keeps_name_and_role_and_no_seat(self):
        name = self.join_engineer_by_hand()
        self.spawn_judge("judge-1")
        judge = self.member("judge-1").name
        self.run_cli(self.bash("judge-1", VERDICT))
        self.hook("stop", agent_id="judge-1")   # the judge has finished: its seat may be handed over
        self.hook("stop", agent_id="eng-1")
        self.start("eng-1")                       # SendMessage resumes the engineer
        me = self.member("eng-1")
        self.assertEqual((me.name, me.role), (name, "engineer"))
        self.assertNotEqual(self.job().judge, name)
        out = self.turn("eng-1")
        self.assertNotIn("Your prompt makes you a judge", str(out))
        self.assertEqual(self.member("eng-1").name, name)
        self.assertNotEqual(self.member("eng-1").role, "judge")
        self.assertIn(self.job().judge, (None, judge))

    def test_hook_enrolled_worker_resumed_after_judge_finished_keeps_identity(self):
        self.spawn_worker("w1")
        before = self.member("w1")
        self.spawn_judge("judge-1")
        self.hook("stop", agent_id="judge-1")
        self.hook("stop", agent_id="w1")
        self.start("w1")
        self.turn("w1")
        after = self.member("w1")
        self.assertEqual((after.name, after.role), (before.name, before.role))
        self.assertNotEqual(after.role, "judge")
        self.assertNotEqual(self.job().judge, after.name)

    def test_join_for_another_sessions_job_is_left_alone(self):
        self.assertIsNone(self.bash("eng-1", "/x/bin/swarm join --job OTHER --key k1"))
        self.assertIsNone(self.bash("eng-1", "echo swarm join --job J --key k1"))

    def test_seat_like_pool_names_are_never_given(self):
        self.assertFalse(assignable_name("Judge Constance Harm"))
        self.assertFalse(assignable_name("judge snyder"))
        self.assertTrue(assignable_name("Homer Simpson"))
        self.h.reset({"simpsons": ["Judge Constance Harm", "Judge Snyder", "Homer Simpson"],
                      "english": ["Alice", "Bob"]})
        names = [self.peer(key=f"k{i}") for i in range(4)]
        self.assertFalse([n for n in names if n.lower().startswith("judge")], names)

    def test_agent_type_role_is_no_event_target(self):
        # a fresh enrolment's role can be the Claude agent type; it used to be looked up as
        # '@general-purpose' and fail on every turn (hook-errors.log: invalid event target)
        from swarm import hooks
        self.assertEqual(hooks._event_targets_of("Homer Simpson", "general-purpose"), ("Homer Simpson",))
        self.assertIn("@engineer", hooks._event_targets_of("Homer Simpson", "engineer"))


class OrchestratorKeyTests(GoalEnv):
    def setUp(self):
        super().setUp()
        self.write_prompt("eng-1", "Configure the HA energy panel and a dashboard.")
        self.activate_goal()

    def bash_out(self, agent, command) -> dict:
        out = self.hook("turn", agent_id=agent, session="sess-1", tool_name="Bash",
                        transcript_path=self.main_transcript(), tool_input={"command": command})
        return (out or {}).get("hookSpecificOutput", {})

    def test_subagent_cannot_join_under_an_orchestrator_prefixed_key(self):
        # `orchestrator-2` (a second main session's key) and the like take the orchestrator role
        # too (agentview.ORCHESTRATOR_KEY): an untagged multi-job subagent must not get them
        for key in ("orchestrator", "orchestrator-2", "orchestrator_x", "orchestrator.2", "Orchestrator-2"):
            for command in (f"/x/bin/swarm join --job J --key {key} --role engineer",
                            f"/x/bin/swarm join --job J --key={key} --role engineer"):
                o = self.bash_out("eng-1", command)
                self.assertEqual(o.get("permissionDecision"), "deny", command)
                self.assertEqual(o.get("permissionDecisionReason"), ORCH_DENY, command)
                self.assertNotIn("updatedInput", o)
        self.assertIsNone(self.member("eng-1"))
        self.assertIsNone(self.agent("orchestrator-2"))

    def test_key_that_only_resembles_orchestrator_is_not_denied(self):
        o = self.bash_out("eng-1", "/x/bin/swarm join --job J --key orchestra --role engineer")
        self.assertNotEqual(o.get("permissionDecision"), "deny", o)


class ReplacementJoinTests(SupervisedEnv):
    """A supervisor replacement's own `swarm join`: the restarted coordinator is the orchestrator
    again (its join as `orchestrator` is legitimate); a restarted worker is not."""
    SID = "11111111-2222-4333-8444-555555555555"
    JOIN_ORCH = "/x/bin/swarm join --job J --key orchestrator --role coordinator"

    def setUp(self):
        super().setUp()
        self.enable_supervisor()
        self.cli("activate", "--job", "J", "--session", "sess-1")
        self.hook("start", agent_id="orig", session="sess-1")
        self.name = self.agent("orig").name
        with self.board() as b:
            b.close_agent("orig", "stuck:dead")
            self.rid = b.record_restart("J", "orig", "orig", "stuck:dead", "claude", 60).id

    def replacement_join(self):
        from swarm.supervisor import markers
        markers.write_resume_marker(self.cfg, "J", self.rid, resume_of="orig", name=self.name,
                                    harness="claude", session_id=self.SID)
        out = self.hook("turn", agent_id=None, session=self.SID, tool_name="Bash",
                        tool_input={"command": self.JOIN_ORCH})
        return (out or {}).get("hookSpecificOutput", {})

    def test_restarted_coordinator_may_join_as_orchestrator(self):
        self.h.update_agent("orig", role="coordinator")
        o = self.replacement_join()
        self.assertNotEqual(o.get("permissionDecision"), "deny", o)
        me = self.agent(self.SID)
        self.assertEqual((me.name, me.resume_of), (self.name, "orig"))

    def test_restarted_worker_still_cannot_join_as_orchestrator(self):
        o = self.replacement_join()
        self.assertEqual(o.get("permissionDecision"), "deny", o)
        self.assertEqual(o.get("permissionDecisionReason"), ORCH_DENY)
        # the replacement is still enrolled under its predecessor's name, as any replacement is
        self.assertEqual(self.agent(self.SID).name, self.name)
