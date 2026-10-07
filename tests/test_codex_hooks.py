"""The hooks driven with the recorded Codex payloads (fixtures), on $SWARM_TEST_BACKEND.

Codex multi-agent V2 encrypts the spawn message, so the swarm's prompt tags are unreadable there
: an agent joins the job bound to its Codex session (one per
session), its role comes from its task name (agent path "/root/verifier-1" -> verifier), and a
member's spawn is checked against the caps and the depth only."""
from __future__ import annotations

import datetime as _dt
import getpass
import json
import os
from unittest import mock

from test_hooks_cli import Env  # noqa: E402  (sets sys.path)
import codex_fixtures as F  # noqa: E402

from swarm import cli as swarm  # noqa: E402


def _owner() -> tuple[str, str]:
    from swarm import transcripts
    return transcripts._host(), getpass.getuser()


class CodexHookTests(Env):
    def setUp(self):
        super().setUp()
        self.codex_home = F.staged(self.tmp)
        p = mock.patch.dict(os.environ, {"CODEX_HOME": str(self.codex_home)}); p.start(); self.addCleanup(p.stop)
        self.ids = F.ids()
        self.sid = F.payloads("SubagentStart")[0]["session_id"]
        self.child = self.ids["child"]["thread"]

    def replay(self, event_name: str, payload: dict, **override):
        ev = {"SubagentStart": "start", "PreToolUse": "turn", "PostToolUse": "done", "SubagentStop": "stop"}[event_name]
        p = {**F.localize(payload, self.codex_home), **override}
        agent_id, session = p.pop("agent_id", None), p.pop("session_id", None)
        return self.hook(ev, agent_id=agent_id, session=session, host="codex", **p)

    def activate_fixture_job(self, job: str = "fixture", *extra):
        rc, _, err = self.cli("activate", "--job", job, "--session", self.sid, *extra)
        self.assertEqual(rc, 0, err)

    def child_payloads(self, event):
        return [p for p in F.payloads(event) if p.get("agent_id") == self.child]

    def start_child(self):
        self.activate_fixture_job()
        return self.replay("SubagentStart", F.payloads("SubagentStart")[0])

    def rename_child(self, agent_path: str):
        """Give the staged child rollout another agent path (its task name), as a spawn with
        task_name "verifier-1" would have."""
        roll = next(self.codex_home.glob(f"sessions/*/*/*/rollout-*-{self.child}.jsonl"))
        lines = roll.read_text().splitlines()
        meta = json.loads(lines[0])
        meta["payload"]["source"]["subagent"]["thread_spawn"]["agent_path"] = agent_path
        roll.write_text("\n".join([json.dumps(meta), *lines[1:]]) + "\n")

    def denied(self, out) -> str | None:
        o = (out or {}).get("hookSpecificOutput", {})
        return o.get("permissionDecisionReason") if o.get("permissionDecision") == "deny" else None

    # ---- enrolment and routing

    def test_custom_role_from_task_name_is_visible_and_survives_followup(self):
        self.rename_child("/root/product_manager__spec")
        self.start_child()
        self.replay("PreToolUse", self.child_payloads("PreToolUse")[0])
        self.assertEqual(self.agent(self.child, job="fixture").role, "product_manager")
        self.replay("SubagentStop", self.child_payloads("SubagentStop")[0])
        self.replay("PreToolUse", self.child_payloads("PreToolUse")[-1])
        self.assertEqual(self.agent(self.child, job="fixture").role, "product_manager")

    def test_qa_role_can_write_tests_without_verifier_restrictions(self):
        self.rename_child("/root/qa__acceptance")
        self.start_child()
        patch = {**self.child_payloads("PreToolUse")[0], "tool_name": "apply_patch",
                 "tool_input": {"command": "*** Begin Patch\n*** End Patch\n"}}
        self.assertIsNone(self.denied(self.replay("PreToolUse", patch)))
        self.assertEqual(self.agent(self.child, job="fixture").role, "qa")

    def test_custom_child_role_selects_model_and_remains_subject_to_caps(self):
        self.cfg["models"] = {"codex": {"engineer": "gpt-engineer", "helper": "gpt-helper"}}
        self.rename_child("/root/engineering_lead__plan")
        self.start_child()
        spawn = next(p for p in self.child_payloads("PreToolUse") if p.get("tool_name", "").endswith("spawn_agent"))
        spawn = {**spawn, "tool_input": {**spawn["tool_input"], "task_name": "engineer__api"}}
        out = self.replay("PreToolUse", spawn)
        self.assertEqual(out["hookSpecificOutput"]["updatedInput"]["model"], "gpt-engineer")
        self.assertIsNone(self.denied(self.replay("PreToolUse", spawn)))
        self.assertIn("limit 2 per agent", self.denied(self.replay("PreToolUse", spawn)))

    def test_explicit_judge_task_name_cannot_be_spawned_by_member(self):
        self.start_child()
        spawn = next(p for p in self.child_payloads("PreToolUse") if p.get("tool_name", "").endswith("spawn_agent"))
        spawn = {**spawn, "tool_input": {**spawn["tool_input"], "task_name": "judge__acceptance"}}
        self.assertIn("can't be a judge", self.denied(self.replay("PreToolUse", spawn)))

    def test_codex_start_enrols_and_records_host_and_model(self):
        out = self.start_child()
        self.assertIn("[swarm] You are **", self.context(out))
        start = F.payloads("SubagentStart")[0]
        a = self.agent(start["agent_id"], job="fixture")
        self.assertEqual((a.harness, a.model), ("codex", start["model"]))

    def test_codex_stop_records_the_model(self):
        self.start_child()
        stop = self.child_payloads("SubagentStop")[0]
        self.replay("SubagentStop", stop)
        self.assertEqual(self.agent(self.child, job="fixture").model, stop["model"])

    def test_codex_first_tool_call_keeps_the_bound_job(self):
        self.start_child()
        out = self.replay("PreToolUse", self.child_payloads("PreToolUse")[0])
        self.assertIsNone(self.denied(out))
        with self.board() as b:
            route = b.route(self.child)
        self.assertEqual((route.state, route.job), ("final", "fixture"))
        self.assertNotEqual(self.agent(self.child, job="fixture").role, "verifier")

    def test_codex_start_defers_until_first_tool_call_per_answer_q1(self):
        # two jobs bound to the session (activated from outside Codex): the tag can't settle it
        self.activate_fixture_job()
        self.activate_fixture_job("other")
        start = F.payloads("SubagentStart")[0]
        self.replay("SubagentStart", start, transcript_path=str(F.transcript_at_hook("SubagentStart", 1)))
        with self.board() as b:
            self.assertEqual(b.route(start["agent_id"]).state, "pending")
        self.replay("PreToolUse", self.child_payloads("PreToolUse")[0])     # message readable, but encrypted
        with self.board() as b:
            route = b.route(start["agent_id"])
        self.assertEqual((route.state, route.job), ("final", None))           # two jobs, no tag: joins none

    def test_activate_from_codex_refuses_a_second_job_in_the_same_session(self):
        # SWARM_HOST: these tests may themselves run inside Claude Code (its variables are set)
        with mock.patch.dict(os.environ, {"CODEX_SESSION_ID": self.sid, "CODEX_THREAD_ID": self.sid,
                                          "SWARM_HOST": "codex"}):
            rc, _, err = self.cli("activate", "--job", "fixture")
            self.assertEqual(rc, 0, err)
            rc, _, err = self.cli("activate", "--job", "other")
            self.assertEqual(rc, 1)
            self.assertIn('"fixture" is already active in this Codex session', err)
            rc, _, err = self.cli("activate", "--job", "fixture")       # the same job again: fine
            self.assertEqual(rc, 0, err)
        self.assertFalse((self.markers / "other.json").exists())

    def test_attach_from_codex_refuses_a_second_job_in_the_same_session(self):
        rc, _, err = self.cli("activate", "--job", "other")                  # active, bound to no session
        self.assertEqual(rc, 0, err)
        env = {"CODEX_SESSION_ID": self.sid, "CODEX_THREAD_ID": self.sid, "SWARM_HOST": "codex"}
        with mock.patch.dict(os.environ, env):
            rc, _, err = self.cli("activate", "--job", "fixture")
            self.assertEqual(rc, 0, err)
            rc, _, err = self.cli("activate", "--job", "other", "--attach")
        self.assertEqual(rc, 1)
        self.assertIn('"fixture" is already active in this Codex session', err)
        self.assertEqual([p.name for p in self.markers.glob("other--*.json")], [])

    def test_codex_role_from_task_name_verifier(self):
        self.rename_child("/root/verifier-1")
        self.start_child()
        out = self.replay("PreToolUse", self.child_payloads("PreToolUse")[0])
        self.assertIn("a VERIFIER on job", self.context(out))
        self.assertEqual(self.agent(self.child, job="fixture").role, "verifier")

    def test_codex_role_from_task_name_judge(self):
        self.rename_child("/root/judge")
        self.activate_fixture_job("fixture", "--goal", "the fixture is recorded")
        start = F.payloads("SubagentStart")[0]
        self.replay("SubagentStart", start)          # goal job: deferred to the first tool call
        out = self.replay("PreToolUse", self.child_payloads("PreToolUse")[0])
        self.assertIn("judge", self.context(out).lower())
        with self.board() as b:
            self.assertEqual(b.job_status("fixture").judge, self.agent(self.child, job="fixture").name)

    def test_codex_judge_never_spawns_fix_agents(self):
        self.rename_child("/root/judge")
        self.activate_fixture_job("fixture", "--goal", "the fixture is recorded")
        self.replay("SubagentStart", F.payloads("SubagentStart")[0])
        out = self.replay("PreToolUse", self.child_payloads("PreToolUse")[0])
        ctx = self.context(out)
        self.assertIn("Judges only judge: never edit, fix, integrate, push or spawn workers", ctx)
        self.assertIn("Spawning subagents: only when strictly needed", ctx)
        self.assertIn("not_met --reason", ctx)
        spawn = next(p for p in self.child_payloads("PreToolUse") if p.get("tool_name", "").endswith("spawn_agent"))
        self.assertIn("Judges only judge", self.denied(self.replay("PreToolUse", spawn)))
        judge = self.agent(self.child, job="fixture").name
        with self.board() as b:
            self.assertTrue(b.record_verdict("fixture", judge, "not_met", "why", "do this"))
        self.assertIn("Judges only judge", self.denied(self.replay("PreToolUse", spawn)))
        # still never a judge
        as_judge = {**spawn, "tool_input": {**spawn["tool_input"], "task_name": "judge"}}
        self.assertIn("Judges only judge", self.denied(self.replay("PreToolUse", as_judge)))

    # ---- gates

    def test_codex_member_spawn_is_checked_on_caps_and_depth_only(self):
        self.start_child()
        spawn = next(p for p in self.child_payloads("PreToolUse") if p.get("tool_name", "").endswith("spawn_agent"))
        out = self.replay("PreToolUse", spawn)          # ciphertext message: no [swarm spawn:] line to read
        self.assertIsNone(self.denied(out))
        self.assertNotIn("updatedInput", (out or {}).get("hookSpecificOutput", {}))
        with self.board() as b:
            posts = [m.message for m in b.recent_messages(50, job="fixture")]
        self.assertIn("spawning grandchild_fixture (1/4 for the job)", posts)

    def test_codex_member_spawn_gets_helper_model_with_allow(self):
        self.cfg["models"] = {"mode": "default", "codex": {"worker": "gpt-w", "helper": "gpt-h"}}
        self.start_child()
        spawn = next(p for p in self.child_payloads("PreToolUse") if p.get("tool_name", "").endswith("spawn_agent"))
        o = self.replay("PreToolUse", spawn)["hookSpecificOutput"]
        self.assertEqual(o["permissionDecision"], "allow")                     # Codex: only with allow
        self.assertEqual(o["updatedInput"], {**spawn["tool_input"], "model": "gpt-h"})   # ciphertext kept
        for _ in range(2):                                                     # over the cap: a deny alone
            o = self.replay("PreToolUse", spawn)["hookSpecificOutput"]
        self.assertEqual(o["permissionDecision"], "deny")
        self.assertNotIn("updatedInput", o)

    def test_codex_member_instructions_say_what_codex_checks(self):
        ctx = self.context(self.start_child())
        self.assertNotIn("[swarm spawn:", ctx)                                # can't be read on Codex
        self.assertIn("task_name", ctx)

    def test_codex_member_spawn_refused_over_the_cap(self):
        self.start_child()
        spawn = next(p for p in self.child_payloads("PreToolUse") if p.get("tool_name", "").endswith("spawn_agent"))
        for _ in range(2):
            self.assertIsNone(self.denied(self.replay("PreToolUse", spawn)))
        self.assertIn("limit 2 per agent", self.denied(self.replay("PreToolUse", spawn)))

    def test_codex_member_spawn_of_a_judge_is_refused(self):
        self.start_child()
        spawn = next(p for p in self.child_payloads("PreToolUse") if p.get("tool_name", "").endswith("spawn_agent"))
        judge = {**spawn, "tool_input": {**spawn["tool_input"], "task_name": "judge"}}
        self.assertIn("can't be a judge", self.denied(self.replay("PreToolUse", judge)))

    def test_codex_verifier_cannot_apply_patch(self):
        self.start_child()
        with self.board() as b:
            b.claim_verifier(self.child, "fixture")
        patch = {**self.child_payloads("PreToolUse")[0], "tool_name": "apply_patch",
                 "tool_input": {"command": "*** Begin Patch\n*** End Patch\n"}}
        self.assertIsNotNone(self.denied(self.replay("PreToolUse", patch)))

    def test_codex_verifier_cannot_spawn(self):
        self.start_child()
        with self.board() as b:
            b.claim_verifier(self.child, "fixture")
        spawn = next(p for p in self.child_payloads("PreToolUse") if p.get("tool_name", "").endswith("spawn_agent"))
        self.assertIn("verifiers are read-only", self.denied(self.replay("PreToolUse", spawn)))

    def test_codex_verifier_shell_write_is_refused(self):
        self.start_child()
        with self.board() as b:
            b.claim_verifier(self.child, "fixture")
        bash = next(p for p in self.child_payloads("PreToolUse") if p.get("tool_name") == "Bash")
        out = self.replay("PreToolUse", {**bash, "tool_input": {**bash["tool_input"], "command": "echo x > f"}})
        self.assertIn("output redirection", self.denied(out))
        out = self.replay("PreToolUse", {**bash, "tool_input": {**bash["tool_input"], "command": "git status"}})
        self.assertIsNone(self.denied(out))

    # ---- per-turn stop

    def test_codex_followup_turn_keeps_member_without_reenrol(self):
        self.start_child()
        self.replay("SubagentStop", self.child_payloads("SubagentStop")[0])
        a = self.agent(self.child, job="fixture")
        self.assertIsNone(a.ended_at)                               # a turn ended; the agent didn't
        out = self.replay("PreToolUse", self.child_payloads("PreToolUse")[-1])
        self.assertNotIn("[swarm] You are **", self.context(out) if out else "")
        self.assertEqual(self.agent(self.child, job="fixture").name, a.name)

    def test_codex_agent_stays_active_after_quiet_window(self):
        self.start_child()
        self.replay("SubagentStop", self.child_payloads("SubagentStop")[0])
        self.h.backdate_agent(self.child, turn_ended_at=4 * 60)
        with self.board() as b:
            swarm.sweep_jobs(b, self.cfg)
        self.assertIsNone(self.agent(self.child, job="fixture").ended_at)

    def test_codex_session_end_completes_owned_agents(self):
        self.start_child()
        self.hook("session-stop", agent_id=None, session=self.sid, host="codex")
        self.assertIsNone(self.agent(self.child, job="fixture").ended_at)
        (self.markers / "fixture.json").unlink()
        self.hook("session-end", agent_id=None, session=self.sid, host="codex")
        self.assertEqual(self.agent(self.child, job="fixture").status, "completed")

    def test_codex_child_session_end_keeps_sibling_active(self):
        from swarm import enrolment
        from swarm.board.autoinit import store_key
        self.start_child()
        with self.board() as b:
            b.allocate_name("sibling", "fixture")
            b.set_agent_runtime("sibling", "codex", None)
        enrolment.write(store_key(self.cfg), job="fixture", agent_key="sibling",
                        harness="codex", session_id=self.sid, cwd=str(self.tmp))
        self.hook("session-end", agent_id=self.child, session=self.sid, host="codex")
        self.assertEqual(self.agent(self.child, job="fixture").status, "completed")
        self.assertIsNone(self.agent("sibling", job="fixture").ended_at)

    def test_codex_attached_root_completes_only_at_session_end(self):
        from swarm import enrolment
        from swarm.board.autoinit import store_key
        self.activate_fixture_job()
        with self.board() as b:
            b.allocate_name(self.sid, "fixture")
            b.set_agent_runtime(self.sid, "codex", None)
            b.record_route(self.sid, self.sid, "final", "fixture")
        enrolment.write(store_key(self.cfg), job="fixture", agent_key=self.sid,
                        harness="codex", session_id=self.sid, cwd=str(self.tmp))
        self.hook("session-stop", agent_id=None, session=self.sid, host="codex")
        self.hook("stop", agent_id=self.sid, session=self.sid, host="codex")
        self.h.backdate_agent(self.sid, turn_ended_at=9 * 60)
        with self.board() as b:
            swarm.sweep_jobs(b, self.cfg)
        self.assertIsNone(self.agent(self.sid, job="fixture").ended_at)
        self.hook("session-end", agent_id=None, session=self.sid, host="codex")
        self.assertEqual(self.agent(self.sid, job="fixture").status, "completed")

    def test_codex_session_end_ignores_other_sessions_and_unowned_agents(self):
        self.start_child()
        with self.board() as b:
            b.allocate_name("foreign", "fixture")
            b.set_agent_runtime("foreign", "codex", None)
        self.hook("session-end", agent_id=None, session="other-session", host="codex")
        self.assertIsNone(self.agent(self.child, job="fixture").ended_at)
        self.hook("session-end", agent_id=None, session=self.sid, host="codex")
        self.assertIsNone(self.agent("foreign", job="fixture").ended_at)

    def test_codex_followup_keeps_the_agent_active(self):
        # the root's followup_task fires no hook for the child until its next tool call: the
        # follow-up itself must keep a quiet-window sweep from completing a child that is working
        self.start_child()
        self.replay("SubagentStop", self.child_payloads("SubagentStop")[0])
        self.h.backdate_agent(self.child, turn_ended_at=4 * 60)
        followup = next(p for p in F.payloads("PreToolUse") if str(p.get("tool_name")).endswith("followup_task"))
        self.assertNotIn("agent_id", followup)                     # recorded from the root thread
        self.assertIsNone(self.replay("PreToolUse", followup))
        with self.board() as b:
            swarm.sweep_jobs(b, self.cfg)
        self.assertIsNone(self.agent(self.child, job="fixture").ended_at)
        self.replay("SubagentStop", self.child_payloads("SubagentStop")[-1])   # the follow-up turn ended
        self.h.backdate_agent(self.child, turn_ended_at=4 * 60)
        with self.board() as b:
            swarm.sweep_jobs(b, self.cfg)
        self.assertIsNone(self.agent(self.child, job="fixture").ended_at)

    # ---- auto-close: the root thread at work keeps its job open

    def finished_codex_job(self):
        """The fixture job with its child finished (quiet window over) and everything older
        than the default auto-close window (30 minutes)."""
        self.start_child()
        self.replay("SubagentStop", self.child_payloads("SubagentStop")[0])
        self.hook("session-end", agent_id=None, session=self.sid, host="codex")
        self.assertIsNotNone(self.agent(self.child, job="fixture").ended_at)
        quiet = 31 * 60
        self.h.backdate_job("fixture", created_at=quiet + 60, activated_at=quiet + 60)
        self.h.backdate_agent(self.child, joined_at=quiet + 30, last_seen=quiet, left_at=quiet,
                              turn_ended_at=quiet)

    def root_payload(self, event: str) -> dict:
        p = next(p for p in F.payloads(event) if p.get("tool_name") == "Bash" and "agent_id" not in p)
        self.assertEqual(p["session_id"], self.sid)   # the root thread: the session, no agent_id
        return p

    def test_codex_root_tool_calls_keep_a_finished_job_open(self):
        self.finished_codex_job()
        self.assertIsNone(self.replay("PreToolUse", self.root_payload("PreToolUse")))
        self.assertIsNone(self.replay("PostToolUse", self.root_payload("PostToolUse")))
        self.assertTrue((self.markers / f"{swarm.safe_job('fixture')}.seen").exists())
        with self.board() as b:
            self.assertEqual(swarm.sweep_jobs(b, self.cfg), [])
            self.assertEqual(b.job_status("fixture").status, "active")

    def test_codex_finished_job_without_root_activity_auto_closes(self):
        self.finished_codex_job()
        with self.board() as b:
            self.assertEqual([c.job for c in swarm.sweep_jobs(b, self.cfg)], ["fixture"])

    def test_claude_stop_completes_even_if_recording_the_model_fails(self):
        self.cli("activate", "--job", "J")
        self.hook("start", agent_id="c1", session="sess-1")
        with mock.patch.object(type(self.board()), "set_agent_runtime", side_effect=RuntimeError("boom")):
            self.hook("stop", agent_id="c1", session="sess-1")
        self.assertIsNotNone(self.agent("c1").ended_at)

    def test_codex_agent_with_a_recent_turn_end_stays(self):
        self.start_child()
        self.replay("SubagentStop", self.child_payloads("SubagentStop")[0])
        with self.board() as b:
            swarm.sweep_jobs(b, self.cfg)
        self.assertIsNone(self.agent(self.child, job="fixture").ended_at)

    def test_codex_turn_capture_is_not_final_and_the_owner_finalizes_it(self):
        self.cfg = {**self.cfg, "transcripts": {**self.cfg["transcripts"], "enabled": True}}
        self.start_child()
        self.replay("SubagentStop", self.child_payloads("SubagentStop")[0])
        with self.board() as b:
            (row,) = [t for t in b.transcripts() if t.agent_key == self.child]
        self.assertFalse(row.final)                              # a Codex turn: more may come
        self.h.backdate_agent(self.child, turn_ended_at=4 * 60)
        with self.board() as b:
            swarm.sweep_jobs(b, self.cfg)
            self.assertFalse(b.transcripts()[0].final)
        self.hook("session-end", agent_id=None, session=self.sid, host="codex")
        with self.board() as b:
            (row,) = [t for t in b.transcripts() if t.agent_key == self.child]
            self.assertTrue(row.final)
            self.assertEqual(b.pending_final_transcripts(*_owner(), "codex", b.now() - _dt.timedelta(days=1)), [])

    def test_finalize_owned_skips_agents_of_another_user(self):
        from swarm import transcripts
        from swarm.board import backend_class
        self.cfg = {**self.cfg, "transcripts": {**self.cfg["transcripts"], "enabled": True}}
        self.start_child()
        with self.board() as b:
            (before,) = [t for t in b.transcripts() if t.agent_key == self.child]
        self.assertFalse(before.final)                           # the early snapshot (run_snapshots' agent
                                                                   # capture): not final yet
        with self.board() as b:
            b.agent_stopped(self.child)
        # a real non-owner: empty CODEX_HOME (nothing to read even if it tried); any rollout read,
        # any board write, or any touch of a transcript row means it saw/changed something it
        # doesn't own
        empty = self.tmp / "someone-elses-codex-home"; empty.mkdir()
        forbid = AssertionError("a non-owner touched a transcript row")
        board_cls = backend_class(self.cfg)
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(empty)}), \
                mock.patch("getpass.getuser", return_value="someone-else"), \
                mock.patch.object(transcripts, "capture_subagent", side_effect=forbid), \
                mock.patch("swarm.hosts.codex.read_rollout", side_effect=forbid), \
                mock.patch.object(board_cls, "save_transcript", side_effect=forbid), \
                mock.patch.object(board_cls, "refresh_transcript", side_effect=forbid), \
                self.board() as b:
            self.assertEqual(transcripts.finalize_owned(b, self.cfg), 0)
            (after,) = [t for t in b.transcripts() if t.agent_key == self.child]
        # byte-identical: the non-owner's sweep didn't create, rewrite or finalize the row
        self.assertEqual((after.sha256, after.captured_at, after.final, after.harness),
                         (before.sha256, before.captured_at, False, before.harness))
        with self.board() as b:
            # the owner's sweep finalizes it; the row's content is unchanged (the rollout was
            # never touched), so this is a refresh (board.refresh_transcript), not a fresh write
            # -- capture_subagent/finalize_owned only count writes, so this can return 0
            transcripts.finalize_owned(b, self.cfg)
            (final_row,) = [t for t in b.transcripts() if t.agent_key == self.child]
        self.assertTrue(final_row.final)

    def test_sandboxed_post_spools_and_is_delivered_without_network_access(self):
        # the swarm no longer grants Codex network access, so a
        # sandboxed agent can't reach a networked board. Its `swarm post` is queued in the spool
        # (a writable root) and the unsandboxed hook delivers it, exactly once.
        from swarm import codex_config
        need = codex_config.required(self.cfg)
        self.assertNotIn(("sandbox_workspace_write", "network_access"), need)
        roots = need[("sandbox_workspace_write", "writable_roots")]
        self.assertIn(str(self.spool_dir), roots)
        self.start_child()
        name = self.agent(self.child, job="fixture").name
        self.h.set_available(False)                                  # inside the sandbox: no network
        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": self.child}):
            rc, out, _ = self.cli("post", "--job", "fixture", "--as", name, "sandboxed", "hello")
        self.assertEqual(rc, 0)
        self.assertTrue(out.startswith("queued (board not reachable from here"), out)
        self.assertEqual(len(list(self.spool_dir.glob("*.json"))), 1)
        self.h.set_available(True)                                   # the hook runs outside the sandbox
        tool = self.child_payloads("PreToolUse")[0]
        for _ in range(2):
            self.replay("PreToolUse", tool)
        with self.board() as b:
            posts = [(m.agent_name, m.message) for m in b.recent_messages(50, job="fixture")
                     if m.message == "sandboxed hello"]
        self.assertEqual(posts, [(name, "sandboxed hello")])
        self.assertEqual(list(self.spool_dir.glob("*.json")), [])

    def test_owns_needs_a_local_enrolment_record(self):
        # the row's host and os_user are display data; only this host user's enrolment
        # record (for this board, this agent_key and the row's job) makes an agent ours
        from types import SimpleNamespace as NS
        from swarm import enrolment, transcripts
        from swarm.board.autoinit import store_key
        host, user = _owner()
        row = NS(host=host, os_user=user, agent_key="k-own", job="J")
        self.assertFalse(transcripts.owns(row, self.cfg))            # the row claims us: not enough
        enrolment.write(store_key(self.cfg), job="J", agent_key="k-own", harness="codex",
                        session_id=None, cwd=str(self.tmp))
        self.assertTrue(transcripts.owns(row, self.cfg))
        for h, u in ((None, None), ("elsewhere", "someone-else")):  # a record wins over the row
            self.assertTrue(transcripts.owns(NS(host=h, os_user=u, agent_key="k-own", job="J"), self.cfg))
        self.assertFalse(transcripts.owns(row))                       # no cfg: nothing is owned
        self.assertFalse(transcripts.owns(NS(host=host, os_user=user, agent_key="k-own", job="other"),
                                          self.cfg))                  # the record's job must match
        self.assertFalse(transcripts.owns(NS(host=host, os_user=user, agent_key="k-else", job="J"),
                                          self.cfg))

    def test_finalize_skips_lost_rollouts_once_and_never_starves_newer_agents(self):
        from swarm import transcripts
        self.cfg = {**self.cfg, "transcripts": {**self.cfg["transcripts"], "enabled": True}}
        self.start_child()                                           # (its hook's sweep runs before the rest)
        with self.board() as b:
            (early,) = [t for t in b.transcripts() if t.agent_key == self.child]
        self.assertFalse(early.final)                                # the early snapshot: not final yet
        lost = [f"00000000-0000-4000-8000-{n:012x}" for n in range(0xa00, 0xa00 + transcripts.FINALIZE_PER_SWEEP + 2)]
        from swarm import enrolment
        from swarm.board.autoinit import store_key
        with self.board() as b:
            for key in lost:                                         # ended Codex agents whose rollout is gone
                b.allocate_name(key, "fixture")
                b.set_agent_runtime(key, "codex", None)
                b.agent_stopped(key)
                enrolment.write(store_key(self.cfg), job="fixture", agent_key=key, harness="codex",
                                session_id=None, cwd=str(self.tmp))  # enrolled here, as the hook would
            b.agent_stopped(self.child)                             # ended last: pending after the lost ones
        logged = []
        # capture_subagent/finalize_owned only count fresh writes: the child's rollout is
        # unchanged since the early snapshot, so its own eventual finalize is a refresh (returns
        # False/0), not a write -- assert on final directly (per round) rather than on the counts.
        with mock.patch.object(transcripts, "log", side_effect=logged.append), self.board() as b:
            transcripts.finalize_owned(b, self.cfg)                 # 1st sweep: works through the lost batch
            (mid,) = [t for t in b.transcripts() if t.agent_key == self.child]
            self.assertFalse(mid.final)                              # still behind the lost ones this round
            transcripts.finalize_owned(b, self.cfg)                 # 2nd sweep: reaches and finalizes the child
            (after_second,) = [t for t in b.transcripts() if t.agent_key == self.child]
            self.assertTrue(after_second.final)                      # final right after the 2nd sweep, not later
            third = transcripts.finalize_owned(b, self.cfg)
        self.assertEqual(third, 0)                                    # nothing left to do
        self.assertEqual(sorted(k for k in lost if any(k in m for m in logged)), sorted(lost))
        self.assertEqual(len(logged), len(lost))                     # each lost rollout logged once
        with self.board() as b:
            (row,) = [t for t in b.transcripts() if t.agent_key == self.child]
            self.assertTrue(row.final)

    def test_sweep_never_uses_quiet_time_to_complete_agents(self):
        self.activate_fixture_job()
        with self.board() as b, mock.patch.object(type(b), "finish_quiet_agents") as finish:
            swarm.sweep_jobs(b, self.cfg)
            finish.assert_not_called()
        rc, _, err = self.cli("status")
        self.assertEqual(rc, 0, err)

    def test_codex_hook_on_malformed_payload_never_fails(self):
        self.activate_fixture_job()
        for bad in ({"turn_id": "t"}, {"turn_id": "t", "agent_id": self.child, "session_id": self.sid,
                                       "transcript_path": "/nonexistent", "tool_name": "collaborationspawn_agent",
                                       "tool_input": "x"}):
            out = self.hook("turn", agent_id=bad.get("agent_id"), session=bad.get("session_id"), host="codex",
                            **{k: v for k, v in bad.items() if k not in ("agent_id", "session_id")})
            if bad.get("agent_id") is None:
                self.assertIsNone(out)                         # main thread, no tool: nothing to say
            else:                                              # a malformed spawn: never rewritten, never a crash
                self.assertNotIn("updatedInput", (out or {}).get("hookSpecificOutput", {}))
        self.assertFalse(self.error_log.exists() and "Traceback" in self.error_log.read_text())
