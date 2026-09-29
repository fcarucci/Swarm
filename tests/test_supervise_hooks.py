"""Hooks and supervisor replacements: a resume-marker session is the replacement agent."""
from __future__ import annotations

from test_hooks_cli import Env

import json

from swarm import hooks as swarm_hooks
from swarm.supervisor import markers


class SupervisedEnv(Env):
    def enable_supervisor(self):
        """The kill switches hold at a replacement's enrolment too: the supervisor must be on."""
        from swarm import cli
        self.config.write_text(self.config.read_text() + "\n[supervise]\nenabled = true\n")
        self.cfg = cli.load_config(self.config)


class ResumeHookTests(SupervisedEnv):
    def setUp(self):
        super().setUp()
        self.enable_supervisor()
        self.cli("activate", "--job", "J", "--session", "sess-1")
        self.hook("start", agent_id="orig", session="sess-1")
        self.name = self.agent("orig").name
        with self.board() as b:
            b.post("J", self.name, "my own earlier post")
            b.close_agent("orig", "stuck:dead")
            b.post("J", "Seymour Skinner", "step 1 done")   # posted after it stopped
            # the supervisor's restart row: a resume marker is only honoured with one
            self.rid = b.record_restart("J", "orig", "orig", "stuck:dead", "claude", 60).id

    def resume(self, rid=None, session="11111111-2222-4333-8444-555555555555"):
        return markers.write_resume_marker(self.cfg, "J", rid or self.rid, resume_of="orig", name=self.name,
                                           harness="claude", session_id=session)

    def test_replacement_joins_under_predecessor_name(self):
        self.resume()
        out = self.hook("turn", agent_id=None, session="11111111-2222-4333-8444-555555555555",
                        tool_name="Bash", tool_input={"command": "ls"})
        text = self.context(out)
        self.assertIn(self.name, text)
        self.assertIn("step 1 done", text)
        self.assertIn(f"messages since {self.name} stopped", text)
        self.assertIn("restarted you", text)
        a = self.agent("11111111-2222-4333-8444-555555555555")
        self.assertEqual((a.name, a.resume_of, a.status, a.harness), (self.name, "orig", "running", "claude"))
        self.assertFalse((self.markers / "J--resume-r1.seen").exists())

    def test_second_tool_call_is_a_normal_turn(self):
        self.resume()
        sid = "11111111-2222-4333-8444-555555555555"
        self.hook("turn", agent_id=None, session=sid, tool_name="Bash", tool_input={"command": "ls"})
        self.hook("done", agent_id=None, session=sid, tool_name="Bash")
        out = self.hook("turn", agent_id=None, session=sid, tool_name="Bash", tool_input={"command": "ls"})
        self.assertTrue(out is None or "Stop now" not in str(out))
        self.assertEqual(self.agent(sid).tool_calls, 2)

    def test_replacement_cannot_spawn(self):
        self.resume()
        sid = "11111111-2222-4333-8444-555555555555"
        self.hook("turn", agent_id=None, session=sid, tool_name="Bash", tool_input={"command": "ls"})
        out = self.hook("turn", agent_id=None, session=sid, tool_name="Agent",
                        tool_input={"prompt": "help me", "description": "x"})
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("can't spawn", out["hookSpecificOutput"]["permissionDecisionReason"])

    def _denied_and_untouched(self, out):
        """The original's call is refused with the stop message; its row keeps the stuck close,
        nothing is posted and nobody is enrolled."""
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        why = out["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("Stop now", why)
        self.assertIn('supervisor of job "J" closed you', why)       # not its own success
        self.assertIn("do not report your task as done", why)
        a = self.agent("orig")
        self.assertEqual((a.name, a.status, a.left_reason), (self.name, "left", "stuck:dead"))
        self.assertIsNotNone(a.ended_at)

    def _messages(self):
        with self.board() as b:
            return [m.message for m in b.recent_messages(50, job="J")]

    def test_unreplaced_original_is_denied_and_stays_closed(self):
        before = self._messages()
        out = self.hook("turn", agent_id="orig", session="sess-1", tool_name="Bash", tool_input={"command": "ls"})
        self._denied_and_untouched(out)
        self.assertEqual(self._messages(), before)
        self.assertEqual(self._active(), [])

    def test_original_denied_after_replacement_completed(self):
        """the replacement finished before the original's hung call returned."""
        self.resume()
        sid = "11111111-2222-4333-8444-555555555555"
        self.hook("turn", agent_id=None, session=sid, tool_name="Bash", tool_input={"command": "ls"})
        with self.board() as b:
            b.post("J", self.name, "DONE: result.txt written")
            b.agent_stopped(sid)
        self.assertEqual(self.agent(sid).status, "completed")
        before = self._messages()
        for _ in range(2):
            out = self.hook("turn", agent_id="orig", session="sess-1", tool_name="Bash",
                            tool_input={"command": "echo again > result.txt"})
            self._denied_and_untouched(out)
        self.assertIn(self.name, out["hookSpecificOutput"]["permissionDecisionReason"])
        self.assertEqual(self._messages(), before)
        self.assertEqual(self._active(), [])
        self.assertEqual(self.agent(sid).status, "completed")

    def test_original_named_by_a_restart_row_is_denied(self):
        """Even if its row lost the stuck reason, a key a restart replaced never comes back."""
        with self.board() as b:
            b.record_restart("J", "orig", "orig", "stuck:dead", "claude", 60)
        self.h.update_agent("orig", left_reason=None)
        out = self.hook("turn", agent_id="orig", session="sess-1", tool_name="Bash", tool_input={"command": "ls"})
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("Stop now", out["hookSpecificOutput"]["permissionDecisionReason"])
        self.assertIsNotNone(self.agent("orig").ended_at)
        self.assertEqual(self._active(), [])

    def test_cli_join_of_a_stuck_closed_key_is_refused(self):
        rc, out, err = self.cli("join", "--job", "J", "--key", "orig")
        self.assertEqual((rc, out), (1, ""))
        self.assertIn("closed as stuck", err)
        self.assertEqual(self.agent("orig").left_reason, "stuck:dead")

    def test_cli_join_of_a_replaced_key_with_cleared_reason_is_refused(self):
        with self.board() as b:
            b.record_restart("J", "orig", "orig", "stuck:dead", "claude", 60)
        self.h.update_agent("orig", left_reason=None)
        rc, out, err = self.cli("join", "--job", "J", "--key", "orig")
        self.assertEqual((rc, out), (1, ""))
        self.assertIn("closed as stuck", err)
        self.assertIsNotNone(self.agent("orig").ended_at)

    def test_already_revived_lost_key_is_denied(self):
        """A lost key the pre-fix bug revived (active row, reason cleared) is still refused:
        the restart row is checked before the active-member path."""
        with self.board() as b:
            b.record_restart("J", "orig", "orig", "stuck:dead", "claude", 60)
        self.h.update_agent("orig", left_at=None, left_reason=None, state="started")
        self.assertIsNone(self.agent("orig").ended_at)
        calls = self.agent("orig").tool_calls
        out = self.hook("turn", agent_id="orig", session="sess-1", tool_name="Bash", tool_input={"command": "ls"})
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("Stop now", out["hookSpecificOutput"]["permissionDecisionReason"])
        self.assertEqual(self.agent("orig").tool_calls, calls)

    def test_subagent_start_of_a_stuck_key_gets_the_stop_message(self):
        before = self._messages()
        out = self.hook("start", agent_id="orig", session="sess-1")
        self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "SubagentStart")
        self.assertIn("Stop now", self.context(out))
        a = self.agent("orig")
        self.assertEqual((a.status, a.left_reason), ("left", "stuck:dead"))
        self.assertEqual(self._messages(), before)
        self.assertEqual(self._active(), [])

    def test_completed_agent_resume_unaffected_by_other_restarts(self):
        """A restart row for another key doesn't touch a normally completed agent's resume."""
        with self.board() as b:
            b.record_restart("J", "orig", "orig", "stuck:dead", "claude", 60)
        self.hook("start", agent_id="k3", session="sess-1")
        name = self.agent("k3").name
        self.hook("stop", agent_id="k3", session="sess-1")
        out = self.hook("turn", agent_id="k3", session="sess-1", tool_name="Bash", tool_input={"command": "ls"})
        self.assertNotIn("deny", str(out))
        self.assertEqual((self.agent("k3").name, self.agent("k3").ended_at), (name, None))

    def test_finished_agent_resumed_via_sendmessage_gets_its_name_back(self):
        self.hook("start", agent_id="k2", session="sess-1")
        name = self.agent("k2").name
        self.hook("stop", agent_id="k2", session="sess-1")
        self.assertEqual(self.agent("k2").status, "completed")
        out = self.hook("turn", agent_id="k2", session="sess-1", tool_name="Bash", tool_input={"command": "ls"})
        self.assertNotIn("deny", str(out))
        a = self.agent("k2")
        self.assertEqual((a.name, a.ended_at), (name, None))

    def test_replacement_cannot_spawn_on_its_first_call(self):
        prev, rid = "orig", self.rid
        for n, tool in ((1, "Agent"), (2, "Task")):
            sid = f"11111111-2222-4333-8444-55555555555{n}"
            markers.write_resume_marker(self.cfg, "J", rid, resume_of=prev, name=self.name,
                                        harness="claude", session_id=sid)
            out = self.hook("turn", agent_id=None, session=sid, tool_name=tool,
                            tool_input={"prompt": "help me", "description": "x"})
            self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny", tool)
            self.assertIn("can't spawn", out["hookSpecificOutput"]["permissionDecisionReason"])
            self.assertNotIn("updatedInput", out["hookSpecificOutput"])
            a = self.agent(sid)
            self.assertEqual((a.name, a.current_tool), (self.name, None))   # enrolled; call not running
            with self.board() as b:
                b.close_agent(sid, "stuck:dead")      # free the name for the next round
                prev, rid = sid, b.record_restart("J", "orig", sid, "stuck:dead", "claude", 60).id

    def test_codex_replacement_cannot_spawn_on_its_first_call(self):
        tid = "00000000-0000-4000-8000-00000000000b"
        markers.write_resume_marker(self.cfg, "J", self.rid, resume_of="orig", name=self.name,
                                    harness="codex", session_id=tid)
        out = self.hook("turn", agent_id=None, session=tid, host="codex", turn_id="t1",
                        tool_name="spawn_agent", tool_input={"message": "help", "task_name": "helper"})
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("can't spawn", out["hookSpecificOutput"]["permissionDecisionReason"])
        self.assertEqual(self.agent(tid).name, self.name)

    def test_replaced_original_is_denied(self):
        self.resume()
        sid = "11111111-2222-4333-8444-555555555555"
        self.hook("turn", agent_id=None, session=sid, tool_name="Bash", tool_input={"command": "ls"})
        out = self.hook("turn", agent_id="orig", session="sess-1", tool_name="Bash", tool_input={"command": "ls"})
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("has taken over your work", out["hookSpecificOutput"]["permissionDecisionReason"])
        self.assertIsNotNone(self.agent("orig").ended_at)       # never revived under a new name
        self.assertEqual([a.name for a in self._active()], [self.name])

    def test_original_back_before_its_replacement_is_denied_and_replacement_proceeds(self):
        out = self.hook("turn", agent_id="orig", session="sess-1", tool_name="Bash", tool_input={"command": "ls"})
        self._denied_and_untouched(out)
        self.resume()
        sid = "11111111-2222-4333-8444-555555555555"
        out = self.hook("turn", agent_id=None, session=sid, tool_name="Bash", tool_input={"command": "ls"})
        self.assertNotIn("deny", str(out))
        self.assertEqual(self.agent(sid).name, self.name)

    def test_unbound_codex_resume_marker_is_ignored_until_bound(self):
        p = markers.write_resume_marker(self.cfg, "J", self.rid, resume_of="orig", name=self.name,
                                        harness="codex")
        tid = "00000000-0000-4000-8000-00000000000a"
        self.assertIsNone(self.hook("turn", agent_id=None, session=tid, host="codex", turn_id="t1",
                                    tool_name="Bash", tool_input={"command": "ls"}))
        markers.bind_session(p, tid)
        out = self.hook("turn", agent_id=None, session=tid, host="codex", turn_id="t2",
                        tool_name="Bash", tool_input={"command": "ls"})
        self.assertIn(self.name, self.context(out))
        self.assertEqual(self.agent(tid).harness, "codex")

    def test_orchestrator_session_still_touches_seen(self):
        self.hook("turn", agent_id=None, session="sess-1", tool_name="Bash", tool_input={"command": "ls"})
        self.assertTrue((self.markers / "J.seen").exists())

    def _active(self):
        with self.board() as b:
            return b.agents("J", include_departed=False)


class ResumeMarkerPoolTests(Env):
    """Carry-in from Phase 0: an unbound resume marker (a Codex replacement before the runner
    binds its thread id) is not an ordinary unbound marker: no session may claim it by routing,
    and it never hides the orchestrator's own unbound marker of the same job."""

    def _unbound_resume(self, rid=1):
        return markers.write_resume_marker(self.cfg, "J", rid, resume_of="orig", name="Homer Simpson",
                                           harness="codex")

    def _plain(self, name, session=None):
        self.markers.mkdir(parents=True, exist_ok=True)
        p = self.markers / name
        p.write_text(json.dumps({"job": "J", "session_id": session}))
        return p

    def test_unbound_resume_marker_is_not_in_the_unbound_pool(self):
        self._unbound_resume()
        self.assertEqual(swarm_hooks._session_markers(self.cfg, "sess-9"), ({}, {}))

    def test_orchestrator_unbound_marker_is_kept(self):
        plain = self._plain("J--aa.json")         # sorts before J--resume-r1.json
        self._unbound_resume()
        _, unbound = swarm_hooks._session_markers(self.cfg, "sess-9")
        self.assertEqual(unbound["J"]["_path"], plain)

    def test_bound_resume_marker_never_replaces_the_orchestrators(self):
        plain = self._plain("J--aa.json", session="sess-1")
        markers.write_resume_marker(self.cfg, "J", 1, resume_of="orig", name="Homer Simpson",
                                    harness="claude", session_id="sess-1")
        bound, _ = swarm_hooks._session_markers(self.cfg, "sess-1")
        self.assertEqual(bound["J"]["_path"], plain)

    def test_bound_resume_marker_is_its_sessions(self):
        p = markers.write_resume_marker(self.cfg, "J", 1, resume_of="orig", name="Homer Simpson",
                                        harness="claude", session_id="sess-r")
        bound, unbound = swarm_hooks._session_markers(self.cfg, "sess-r")
        self.assertEqual((bound["J"]["_path"], unbound), (p, {}))

    def test_unrelated_session_cannot_claim_it(self):
        p = self._unbound_resume()
        self.assertIsNone(self.hook("start", agent_id="stranger", session="sess-9"))
        self.hook("turn", agent_id="stranger", session="sess-9", tool_name="Bash",
                  tool_input={"command": "ls"})
        self.assertIsNone(self.agent("stranger"))
        self.assertIsNone(json.loads(p.read_text())["session_id"])


class CodexBindRaceTests(SupervisedEnv):
    """A Codex replacement's first tool hook may run before the
    runner has bound its thread id (thread.started read) to the resume marker. The runner's token
    in the session's environment makes the hook bind it itself, deterministically."""

    TID = "00000000-0000-4000-8000-0000000000c1"

    def setUp(self):
        super().setUp()
        self.enable_supervisor()
        self.cli("activate", "--job", "J", "--session", "sess-1")
        self.hook("start", agent_id="orig", session="sess-1")
        self.name = self.agent("orig").name
        with self.board() as b:
            b.close_agent("orig", "stuck:dead")
            rid = b.record_restart("J", "orig", "orig", "stuck:dead", "codex", 60).id
        self.p = markers.write_resume_marker(self.cfg, "J", rid, resume_of="orig", name=self.name,
                                             harness="codex")
        self.token = "t" * 32
        self.assertTrue(markers.set_resume_token(self.p, self.token))

    def hook_with_token(self, token, session, turn="t1"):
        from unittest import mock
        with mock.patch.dict("os.environ", {markers.TOKEN_ENV: token}):
            return self.hook("turn", agent_id=None, session=session, host="codex", turn_id=turn,
                             tool_name="Bash", tool_input={"command": "ls"})

    def test_tool_hook_before_the_runner_binds(self):
        out = self.hook_with_token(self.token, self.TID)
        self.assertIn(self.name, self.context(out))
        a = self.agent(self.TID)
        self.assertEqual((a.name, a.resume_of, a.harness), (self.name, "orig", "codex"))
        self.assertEqual(json.loads(self.p.read_text())["session_id"], self.TID)
        self.assertTrue(markers.bind_session(self.p, self.TID))   # the runner's late bind agrees

    def test_runner_binds_first_then_the_hook(self):
        self.assertTrue(markers.bind_session(self.p, self.TID))
        self.hook_with_token(self.token, self.TID)
        self.assertEqual(self.agent(self.TID).name, self.name)

    def test_other_session_with_the_token_is_not_the_replacement_nor_an_orchestrator(self):
        self.hook_with_token(self.token, self.TID)
        seen = self.markers / "J.seen"
        seen.unlink(missing_ok=True)
        other = "00000000-0000-4000-8000-0000000000c2"
        self.assertIsNone(self.hook_with_token(self.token, other))
        self.assertIsNone(self.agent(other))
        self.assertFalse(seen.exists())
        self.assertEqual(json.loads(self.p.read_text())["session_id"], self.TID)

    def test_wrong_token_binds_nothing(self):
        self.assertIsNone(self.hook_with_token("x" * 32, self.TID))
        self.assertIsNone(json.loads(self.p.read_text())["session_id"])
        self.assertIsNone(self.agent(self.TID))
