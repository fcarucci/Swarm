"""Drive-job gates of the hooks (spec 3.4, 4.0, 4.4; Task 5 and 5b): who is calling comes from host-private state,
read-only seats stay read-only on every path (the join, the first call, an unbound job, a subagent), seats stay off
the target branch, the MCP allowlist, spawn refusals, the kick, the doctor lines, [B1] marker enrolment."""
from __future__ import annotations

import dataclasses
import os
import sys
import time
import unittest
import types
from pathlib import Path
from unittest import mock

from drive_hooks_support import DriveCase, REQ
from test_hooks_cli import Env

from swarm import bootstrap, drivegate, driveroles, enrolment, fastpath, hooks, paths, shellguard
from swarm.board.autoinit import store_key
from swarm.supervisor import ledger, markers

JUDGE_WRITES = ("echo x > out.txt", "git commit -m x", "sed -i s/a/b/ f.py", "tee out.log",
                "sh -c 'echo x > f'", "bash -lc \"git -c a=b commit -am x\"", "sqlite3 board.db 'update seats set role=1'",
                "git -c user.name=x commit -m y", "eval 'rm -rf x'", "script -qc 'touch f' /dev/null")
READS = ("git log --oneline -3", "cd tests && python3 -B -m unittest test_x", "ls -la", "git diff HEAD~1",
         "sqlite3 -readonly board.db 'select 1'", "swarm status --job J", "swarm verdict --job J show")


class ReadOnlyGate(DriveCase):
    def judge(self):
        return self.launch_seat("judge", role="judge", kind="judge")[0]

    def test_readonly_judge_bash_write_blocked(self):
        sid = self.judge()
        for cmd in JUDGE_WRITES:
            with self.subTest(cmd=cmd):
                self.assertIn("read-only", self.pre(sid, command=cmd))

    def test_readonly_judge_powershell_write_blocked(self):
        sid = self.judge()
        for cmd in ("Set-Content f.txt x", "Remove-Item f", "git commit -m x", "echo hi > out.txt"):
            with self.subTest(cmd=cmd):
                self.assertIn("read-only", self.pre(sid, "PowerShell", command=cmd))

    def test_readonly_judge_monitor_and_mcp_write_blocked(self):
        sid = self.judge()
        self.assertIn("read-only", self.pre(sid, "Monitor", command="tail -f x"))
        self.assertIn("read-only", self.pre(sid, "mcp__jira__create_issue"))
        for tool in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
            self.assertIn("read-only", self.pre(sid, tool, tool_input={"file_path": "x"}))

    def test_readonly_judge_can_read_and_run_targeted_tests(self):
        sid = self.judge()
        for cmd in READS:
            with self.subTest(cmd=cmd):
                self.assertIsNone(self.pre(sid, command=cmd))
        self.assertIsNone(self.pre(sid, "mcp__jira__get_issue"))
        self.assertIsNone(self.pre(sid, "Read", tool_input={"file_path": "x"}))

    def test_a_judge_may_record_its_report_through_the_verdict_heredoc(self):
        sid = self.judge()
        cmd = ("swarm verdict --job J --artifact a@1234567 not_met --reason r --next n --details - <<'EOF'\n"
               "# Report\nrun `git commit` and `echo x > f` and rm -rf x\nEOF")
        self.assertIsNone(self.pre(sid, command=cmd))
        self.assertIn("read-only", self.pre(sid, command=cmd + "\necho x > f"))

    def test_a_reviewer_by_receipted_role_is_read_only_and_an_engineer_is_not(self):
        rev, _ = self.launch_seat("qa-1", role="reviewer")
        eng, _ = self.launch_seat("eng-a", role="engineer")
        self.assertIn("read-only", self.pre(rev, command="git commit -m x"))
        self.assertIsNone(self.pre(eng, command="git commit -m x"))
        self.assertIsNone(self.pre(eng, "Edit", tool_input={"file_path": "x"}))
        self.assertIsNone(self.pre(eng, command="echo x > out.txt"))

    def test_driver_named_judge_and_reviewer_seats_are_read_only_without_a_board_lookup(self):
        for name, kind in (("judge", "judge"), ("goal-judge", "goal"), ("ruling-judge", "ruling"),
                           ("reviewer-feat-a-1234abcd", "review")):
            sid, _ = self.launch_seat(name, role="engineer", kind=kind)     # even a mislabelled row
            with mock.patch.object(drivegate, "_open_board", side_effect=AssertionError("board opened")):
                with self.subTest(seat=name):
                    self.assertIn("read-only", self.pre(sid, command="git commit -m x"))

    def test_the_action_alone_makes_a_seat_read_only_and_a_judge(self):
        """A judge, goal, ruling or review action is read-only whatever the seat is called; only the first three
        may run `swarm verdict`."""
        for name, kind, judge in (("x-1", "judge", True), ("x-2", "goal", True), ("x-3", "ruling", True),
                                  ("x-4", "review", False)):
            sid, _ = self.launch_seat(name, role="engineer", kind=kind)
            with self.subTest(kind=kind), mock.patch.object(drivegate, "_open_board", side_effect=AssertionError("board")):
                self.assertIn("read-only", self.pre(sid, command="git commit -m x"))
                verdict = self.pre(sid, command="swarm verdict --job J --artifact a@1234567 met ok")
                self.assertEqual(verdict is None, judge)

    def test_a_driver_name_alone_makes_a_seat_read_only_and_a_judge(self):
        """The driver's own seat names say what they are, whatever action the session runs."""
        for name, judge in (("judge", True), ("goal-judge", True), ("ruling-judge", True),
                            ("reviewer-feat-a-1234abcd", False)):
            sid, _ = self.launch_seat(name, role="engineer", kind="wake")
            with self.subTest(seat=name), mock.patch.object(drivegate, "_open_board", side_effect=AssertionError("board")):
                self.assertIn("read-only", self.pre(sid, command="git commit -m x"))
                verdict = self.pre(sid, command="swarm verdict --job J --artifact a@1234567 met ok")
                self.assertEqual(verdict is None, judge)

    def test_a_seat_named_like_a_lead_but_not_a_lead_role_may_spawn_and_one_that_is_a_lead_role_but_not_named_so_too(self):
        """A lead needs the lead role (receipted) AND the driver's lead name."""
        for name, role in (("boss", "engineering_lead"), ("lead", "engineer")):
            sid, _ = self.launch_seat(name, role=role, kind="seat")
            with self.subTest(seat=name):
                self.assertIsNone(self.pre(sid, "Agent", tool_input={"prompt": "x", "description": "x"}))
        sid, _ = self.launch_seat("lead~1", role="engineering_lead", kind="lead")
        self.assertIn("lead seat", self.pre(sid, "Agent", tool_input={"prompt": "x", "description": "x"}))

    def test_P5_a_judge_row_rewritten_to_engineer_stays_read_only(self):
        """The role comes from the seat's receipt, never from the row a writer can change."""
        sid, _ = self.launch_seat("qa-1", role="judge", kind="seat")
        real = drivegate._open_board

        class Flip:
            def __init__(self, b):
                self.b = b

            def __getattr__(self, name):
                return getattr(self.b, name)

            def seat(self, job, gen, seat):
                s = self.b.seat(job, gen, seat)
                return dataclasses.replace(s, role="engineer") if s else s
        with mock.patch.object(drivegate, "_open_board", lambda cfg: Flip(real(cfg))):
            self.assertIn("could not be verified", self.pre(sid, command="git commit -m x"))
            self.assertIsNone(self.pre(sid, command="git log"))

    def test_a_seat_without_a_valid_authorization_is_held_to_the_strictest_rules(self):
        sid, _ = self.launch_seat("eng-a", authorize=False)
        self.assertIn("could not be verified", self.pre(sid, command="git commit -m x"))
        self.assertIsNone(self.pre(sid, command="git status"))

    def test_a_seat_whose_launch_cannot_be_resolved_is_strict_not_free(self):
        sid, _ = self.launch_seat("eng-a")
        self.edit(lambda l: setattr(l, "intents", []))
        self.assertIn("could not be verified", self.pre(sid, command="echo x > f"))

    def run_file(self):
        from swarm.supervisor import runner
        return runner._read_run(f"r{self.restart_id}.json", self.key)

    def rewrite_run(self, **over):
        from swarm.supervisor import runner
        run = self.run_file()
        run.update(over)
        runner.write_run(run)

    def test_a_run_file_that_is_not_this_sessions_does_not_name_its_seat(self):
        """The seat comes from the supervisor's run file, checked against the marker, the job and the session."""
        for what, over in (("another session", {"session_id": "someone-else"}), ("another job", {"job": "OTHER"}),
                           ("a malformed run", {"limit_seconds": "soon"}), ("no launch", {"launch_id": None})):
            sid, _ = self.launch_seat("eng-" + what.split()[-1][:3] + str(len(what)))
            with self.subTest(what=what):
                self.assertIsNone(self.pre(sid, command="echo x > f"))            # the run is fine: a writer
                self.rewrite_run(**over)
                self.assertIn("could not be verified", self.pre(sid, command="echo x > f"))

    def test_a_run_of_another_job_with_a_matching_marker_name_still_names_no_seat(self):
        sid, _ = self.launch_seat("eng-a")
        marker = markers.resume_binding(self.cfg, sid)
        self.assertEqual(drivegate.seat_context(self.cfg, self.key, self.payload(sid), resume=marker).seat, "eng-a")
        self.rewrite_run(job="OTHER", marker=str(Path(self.run_file()["marker"]).with_name(
            f"OTHER--resume-r{self.restart_id}.json")))
        self.assertIsNone(drivegate.seat_context(self.cfg, self.key, self.payload(sid), resume=marker).seat)

    def test_a_seat_session_is_not_the_run_of_another_session(self):
        sid, _ = self.launch_seat("eng-a")
        ctx = drivegate.seat_context(self.cfg, self.key, self.payload(sid))
        self.assertEqual((ctx.seat, ctx.intent.launch_id), ("eng-a", "L-eng-a"))
        self.rewrite_run(session_id="x", agent_key="y")
        self.assertIsNone(drivegate.seat_context(self.cfg, self.key, self.payload(sid)).seat)

    def test_requester_context_is_the_main_session_only(self):
        self.assertEqual(drivegate.requester_context(self.key, self.payload(REQ), "J").kind, "requester")
        self.assertIsNone(drivegate.requester_context(self.key, self.payload(REQ, agent_id="sub-1"), "J"))
        self.assertIsNone(drivegate.requester_context(self.key, self.payload("other"), "J"))
        self.assertIsNone(drivegate.requester_context(self.key, self.payload(REQ), "NOPE"))

    def test_classify(self):
        sid, _ = self.launch_seat("eng-a")
        self.assertEqual(drivegate.classify(self.cfg, self.payload(sid)).kind, "seat")
        self.assertEqual(drivegate.classify(self.cfg, self.payload(sid, agent_id="sub-1")).seat, "eng-a")
        self.assertEqual(drivegate.classify(self.cfg, self.payload(REQ)).kind, "requester")
        self.assertIsNone(drivegate.classify(self.cfg, self.payload(REQ, agent_id="sub-1")))
        self.assertIsNone(drivegate.classify(self.cfg, self.payload("stranger")))
        self.assertIsNone(drivegate.classify(self.cfg, {"tool_name": "Bash"}))
        enrolment.write(self.key, job="J", agent_key="sub-9", harness="claude", session_id="w", cwd=str(self.work))
        self.assertEqual(drivegate.classify(self.cfg, self.payload("w", agent_id="sub-9")).kind, "enrolled")
        with mock.patch.object(drivegate, "has_ledgers", return_value=False):
            self.assertIsNone(drivegate.classify(self.cfg, self.payload(sid)))

    def test_a_removed_marker_leaves_the_session_strict_through_its_enrolment(self):
        sid, _ = self.launch_seat("judge", role="judge", kind="judge")
        enrolment.write(self.key, job="J", agent_key=sid, harness="claude", session_id=sid, cwd=str(self.work))
        for p in Path(self.cfg["hook"]["marker_dir"]).expanduser().glob("*resume*"):
            p.unlink()
        self.assertIn("could not be verified", self.pre(sid, command="git commit -m x"))

    def test_a_session_of_no_drive_job_is_never_gated(self):
        with self.board() as b:
            b.open_job("PLAIN", None, None, None, "me")
        enrolment.write(self.key, job="PLAIN", agent_key="plain-1", harness="claude", session_id="plain-1",
                        cwd=str(self.work))
        self.assertIsNone(self.pre("plain-1", command="git push --force origin main"))
        self.assertIsNone(self.pre("stranger", command="git push --force origin main"))


class EveryCall(DriveCase):
    """M3: the gate runs on every call: the join and first calls, a seat not yet enrolled, an unbound job,
    subagents of a seat."""

    def test_first_call_of_a_launched_judge_is_gated_before_it_is_enrolled(self):
        sid, _ = self.launch_seat("judge", role="judge", kind="judge", bind=False)
        out = self.hook("turn", agent_id=None, session=sid, cwd=str(self.work), tool_name="Bash",
                        tool_input={"command": "git commit -am x && git push origin HEAD:feat/a"})
        o = out["hookSpecificOutput"]
        self.assertEqual(o["permissionDecision"], "deny")
        self.assertIn("read-only", o["permissionDecisionReason"])
        self.assertIn("swarm land", o["permissionDecisionReason"])

    def test_a_joined_judge_is_gated_on_a_later_call_too(self):
        sid, _ = self.launch_seat("judge", role="judge", kind="judge")
        out = self.hook("turn", agent_id=None, session=sid, cwd=str(self.work), tool_name="Bash",
                        tool_input={"command": "echo x > f"})
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_a_denied_call_leaves_no_tool_running(self):
        """A denied call gets no PostToolUse: it must not have been marked as running a tool either."""
        sid, _ = self.launch_seat("judge", role="judge", kind="judge")
        out = self.hook("turn", agent_id=None, session=sid, cwd=str(self.work), tool_name="Bash",
                        tool_input={"command": "git commit -am x"})
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIsNone(self.agent(sid).current_tool)
        self.hook("turn", agent_id=None, session=sid, cwd=str(self.work), tool_name="Bash",
                  tool_input={"command": "git log"})
        self.assertEqual(self.agent(sid).current_tool, "Bash")      # an allowed call is marked as usual

    def test_subagent_of_a_judge_inherits_its_restrictions(self):
        sid, _ = self.launch_seat("judge", role="judge", kind="judge")
        for cmd in ("echo x > f", "git commit -am x"):
            self.assertIn("read-only", self.pre(sid, command=cmd, agent_id="sub-1"))
        out = self.hook("turn", agent_id="sub-1", session=sid, cwd=str(self.work), tool_name="Bash",
                        tool_input={"command": "git commit -am x"})
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIsNone(self.pre(sid, command="git log", agent_id="sub-1"))

    def test_subagent_of_an_engineer_is_not_made_read_only_and_keeps_the_target_guard(self):
        sid, _ = self.launch_seat("eng-a")
        self.assertIsNone(self.pre(sid, command="git commit -m x", agent_id="sub-1"))
        self.assertIn("target branch", self.pre(sid, command="git push origin HEAD:main", agent_id="sub-1"))

    def test_a_judge_of_an_unbound_job_is_gated(self):
        """The job is not one of the session's bound markers: the old gate never ran for it."""
        sid, _ = self.launch_seat("judge", role="judge", kind="judge")
        with mock.patch.object(hooks, "_session_markers", return_value=({}, {})):
            out = self.hook("turn", agent_id=None, session=sid, cwd=str(self.work), tool_name="Bash",
                            tool_input={"command": "git commit -am x"})
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_an_enrolled_subagent_of_a_drive_job_is_guarded(self):
        enrolment.write(self.key, job="J", agent_key="sub-9", harness="claude", session_id="whoever",
                        cwd=str(self.work))
        self.assertIn("target branch", self.pre("whoever", command="git push origin HEAD:main", agent_id="sub-9"))
        self.assertIn("could not be verified", self.pre("whoever", command="echo x > f", agent_id="sub-9"))
        self.assertIsNone(self.pre("whoever", command="git status", agent_id="sub-9"))


@unittest.skipUnless(sys.platform.startswith("linux"), "the shell fast path uses Linux /proc/uptime")
class NoFastPathLease(DriveCase):
    """bin/swarm-hook serves a leased session's next calls without Python: no gate, no receipts. A session of a
    drive job (a seat, its subagents, the requester) must never hold such a lease."""

    def setUp(self):
        super().setUp()
        p = mock.patch.dict(os.environ, SWARM_CONFIG=str(self.config), SWARM_HOOK_FASTPATH="1",
                            SWARM_HOOK_EVENT="turn")
        p.start()
        self.addCleanup(p.stop)
        n = mock.patch.object(fastpath, "ensure_notifier")
        n.start()
        self.addCleanup(n.stop)

    def turn(self, sid, agent_id=None, n=3):
        for _ in range(n):
            self.hook("turn", agent_id=agent_id, session=sid, cwd=str(self.work), tool_name="Bash",
                      tool_input={"command": "ls"})

    def test_a_seat_never_holds_a_lease(self):
        sid, _ = self.launch_seat("eng-a")
        self.turn(sid)
        self.assertEqual(fastpath.read("agent-session-" + sid)[:1], ["0"])
        self.hook("done", agent_id=None, session=sid, cwd=str(self.work), tool_name="Bash",
                  tool_input={"command": "ls"})
        self.assertEqual(fastpath.read("agent-session-" + sid)[:1], ["0"])

    def test_without_the_drive_gate_the_same_seat_would_get_one(self):
        """The control: the lease the gate prevents is real."""
        sid, _ = self.launch_seat("eng-a")
        with mock.patch.object(drivegate, "classify", return_value=None):
            self.turn(sid)
        self.assertGreater(int(fastpath.read("agent-session-" + sid)[0]), 0)

    def test_a_subagent_of_a_seat_never_holds_a_lease(self):
        sid, _ = self.launch_seat("eng-a")
        self.turn(sid, agent_id="sub-1")
        self.assertEqual(fastpath.read("agent-sub-1")[:1], ["0"])

    def test_the_requester_session_never_holds_a_lease(self):
        rc, _, err = self.cli("activate", "--job", "JX", "--session", REQ)
        self.assertEqual(rc, 0, err)
        self.turn(REQ)
        self.assertEqual(fastpath.read("agent-session-" + REQ)[:1], ["0"])
        with mock.patch.object(drivegate, "classify", return_value=None):
            self.turn(REQ)
        self.assertGreater(int(fastpath.read("agent-session-" + REQ)[0]), 0)       # the control

    def test_a_stale_lease_is_ended_when_the_session_turns_out_to_be_a_seat(self):
        sid, _ = self.launch_seat("eng-a")
        fastpath.write("agent-session-" + sid, [10 ** 9, "J", "x"])
        self.turn(sid, n=1)
        self.assertEqual(fastpath.read("agent-session-" + sid)[:1], ["0"])

    def test_other_sessions_keep_their_leases(self):
        rc, _, err = self.cli("activate", "--job", "JX", "--session", "other-sess")
        self.assertEqual(rc, 0, err)
        self.turn("other-sess")
        self.assertGreater(int(fastpath.read("agent-session-other-sess")[0]), 0)


class RequesterIdentity(DriveCase):
    def test_rr9_a_planted_resume_marker_does_not_stop_the_requesters_receipts(self):
        """The requester is known from its host-private ledger before any marker is looked at."""
        self.launch_seat("eng-a")
        markers.write_resume_marker(self.cfg, "J", 999, resume_of="x", name="seat", harness="claude", session_id=REQ)
        drivegate._LEDGER_CACHE.clear()
        self.assertTrue(self.post(REQ, "swarm halt --job J --reason r", "swarm-record: kind=halt id=8 rev=1"))
        self.assertEqual(drivegate.classify(self.cfg, self.payload(REQ)).kind, "requester")
        self.assertIsNone(self.pre(REQ, command="echo x > notes.txt"))

    def test_rr14_to_16_probes(self):
        sid, _ = self.launch_seat("eng-a")
        judge, _ = self.launch_seat("judge", role="judge", kind="judge")
        self.assertIn("mirror", self.pre(sid, command="git push --m origin"))
        for cmd in ("echo 'git push origin HEAD:main' | sh /dev/stdin", "bash <<< 'git push origin HEAD:main'",
                    "echo x | sh; cat <<EOF\nx\nEOF", "source <(echo x)", "echo x | bash -c \"$(cat)\"",
                    "echo x | busybox sh"):
            self.assertIn("shell is allowed only", self.pre(sid, command=cmd))
        self.assertIn("shell is allowed only", self.pre(judge, command="bash <<< 'touch f'"))


class SpawnRefusals(DriveCase):
    SPAWN = {"prompt": "[swarm job: J]\nhelp me", "description": "helper"}

    def spawn(self, sid, **kw):
        return self.pre(sid, "Agent", tool_input=dict(self.SPAWN), **kw)

    def test_requester_cannot_spawn_subagents_in_drive_job(self):
        self.assertIn("seat request", self.spawn(REQ))
        out = self.hook("turn", agent_id=None, session=REQ, tool_name="Agent", tool_input=dict(self.SPAWN))
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("swarm seat request", out["hookSpecificOutput"]["permissionDecisionReason"])

    def test_a_stranger_may_spawn(self):
        self.assertIsNone(self.spawn("stranger"))

    def test_the_requester_is_not_refused_once_its_job_is_finished(self):
        self.edit(lambda l: setattr(l, "terminal", {"verdict": "met", "at": "2026-10-10T00:00:00+00:00"}))
        self.assertIsNone(self.spawn(REQ))

    def test_lead_cannot_spawn_subagents_in_drive_job(self):
        sid, _ = self.launch_seat("lead", role="engineering_lead", kind="lead")
        self.assertIn("lead seat", self.spawn(sid))
        self.assertIn("lead seat", self.spawn(sid, agent_id="sub-1"))
        out = self.hook("turn", agent_id=None, session=sid, tool_name="Agent", tool_input=dict(self.SPAWN))
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_read_only_seats_cannot_spawn(self):
        sid, _ = self.launch_seat("judge", role="judge", kind="judge")
        self.assertIn("read-only", self.spawn(sid))

    def test_an_unverifiable_seat_cannot_spawn(self):
        sid, _ = self.launch_seat("eng-a", authorize=False)
        self.assertIn("could not be verified", self.spawn(sid))

    def test_task_is_a_spawn_tool_too(self):
        sid, _ = self.launch_seat("lead", role="engineering_lead", kind="lead")
        self.assertIn("lead seat", self.pre(sid, "Task", tool_input=dict(self.SPAWN)))

    def test_an_engineer_spawn_is_left_to_the_ordinary_spawn_gate(self):
        sid, _ = self.launch_seat("eng-a")
        self.assertIsNone(self.spawn(sid))


class VerdictGate(DriveCase):
    def test_only_the_judge_seat_may_run_swarm_verdict(self):
        eng, _ = self.launch_seat("eng-a")
        lead, _ = self.launch_seat("lead", role="engineering_lead", kind="lead")
        rev, _ = self.launch_seat("reviewer-a-1", role="reviewer", kind="review")
        judge, _ = self.launch_seat("judge", role="judge", kind="judge")
        goal, _ = self.launch_seat("goal-judge", role="judge", kind="goal")
        cmd = "swarm verdict --job J --artifact x@1234567 met 'fine'"
        for sid in (eng, lead, rev):
            self.assertIn("only the judge seat", self.pre(sid, command=cmd))
        self.assertIsNone(self.pre(judge, command=cmd))
        self.assertIsNone(self.pre(goal, command=cmd))

    def test_verdict_show_and_other_swarm_commands_are_open_to_every_seat(self):
        eng, _ = self.launch_seat("eng-a")
        for cmd in ("swarm verdict --job J show", "swarm status --job J", "swarm post --job J hello"):
            self.assertIsNone(self.pre(eng, command=cmd))

    def test_verdict_hidden_in_a_wrapper_is_still_refused(self):
        eng, _ = self.launch_seat("eng-a")
        for cmd in ("bash -lc 'swarm verdict --job J met ok'", "cd x && ~/.local/bin/swarm verdict --job J met ok",
                    "echo $(swarm verdict --job J met ok)"):
            with self.subTest(cmd=cmd):
                self.assertIn("only the judge seat", self.pre(eng, command=cmd))

    def test_a_requester_main_session_is_not_a_seat_and_not_gated_here(self):
        self.assertIsNone(self.pre(REQ, command="swarm verdict --job J met ok"))


MCP_WRITES = ("mcp__plugin_unifi-network_unifi-network__unifi_execute",
              "mcp__plugin_unifi-network_unifi-network__unifi_reboot_device",
              "mcp__plugin_unifi-network_unifi-network__unifi_block_client",
              "mcp__plugin_unifi-network_unifi-network__unifi_toggle_wlan",
              "mcp__homeassistant__call_service", "mcp__homeassistant__restart_ha",
              "mcp__homeassistant__save_config_file", "mcp__homeassistant__fire_event",
              "mcp__claude_ai_Google_Drive__share_file", "mcp__claude_ai_Google_Drive__trash_file",
              "mcp__claude_ai_Google_Drive__copy_file", "mcp__claude_ai_MaaS_ServiceNow__change_state",
              "mcp__claude_ai_Slack__slack_schedule_message", "mcp__plugin_playwright_playwright__browser_click",
              "mcp__plugin_playwright_playwright__browser_run_code_unsafe",
              "mcp__plugin_playwright_playwright__browser_evaluate",
              "mcp__claude_ai_Microsoft_365__outlook_respond_to_event", "mcp__github__create_pull_request",
              "mcp__github__merge_pull_request", "mcp__claude_ai_Slack__slack_send_message",
              "mcp__x__run_query", "mcp__x__execute", "mcp__x__do_thing")
MCP_READS = ("mcp__x__get_comments", "mcp__claude_ai_Slack__slack_read_channel", "mcp__github__get_file_contents",
             "mcp__homeassistant__list_entities", "mcp__homeassistant__search_entities",
             "mcp__plugin_unifi-network_unifi-network__unifi_get_client_details",
             "mcp__claude_ai_Google_Drive__read_file_content", "mcp__claude_ai_MaaS_Teams__teams_list_channels",
             "mcp__claude_ai_MaaS_NVBugs__nvbugs_search_v2")


class McpAllowlist(DriveCase):
    def test_a_read_only_seat_may_call_only_read_tools(self):
        sid, _ = self.launch_seat("judge", role="judge", kind="judge")
        for tool in MCP_WRITES:
            with self.subTest(tool=tool):
                self.assertIn("read allowlist", self.pre(sid, tool, tool_input={}))
        for tool in MCP_READS:
            with self.subTest(tool=tool):
                self.assertIsNone(self.pre(sid, tool, tool_input={}))

    def test_a_writer_seat_may_call_any_mcp_tool(self):
        sid, _ = self.launch_seat("eng-a")
        for tool in MCP_WRITES + MCP_READS:
            self.assertIsNone(self.pre(sid, tool, tool_input={}))

    def test_allowlist_function_is_a_read_verb_and_no_write_verb(self):
        self.assertTrue(shellguard.mcp_read_allowed("mcp__s__get_x"))
        self.assertFalse(shellguard.mcp_read_allowed("mcp__s__get_and_delete"))
        self.assertFalse(shellguard.mcp_read_allowed("mcp__s__frobnicate"))
        self.assertTrue(shellguard.mcp_read_allowed("Bash"))     # not an MCP tool: no opinion
        self.assertTrue(shellguard.mcp_read_allowed(None))


class PushGuard(DriveCase):
    def owner(self, **details):
        rec = types.SimpleNamespace(owner=details, session_id=REQ)
        return mock.patch.object(enrolment, "find_job_owner", lambda *a, **k: rec)

    def test_push_to_target_refused(self):
        sid, _ = self.launch_seat("eng-a")
        out = self.pre(sid, command="git push origin HEAD:main")
        self.assertIn("target branch", out)

    def test_push_guard_uses_recipe_target_branch(self):
        sid, _ = self.launch_seat("eng-a")
        with self.owner(recipe="coding", target_branch="develop"):
            self.assertIn("'develop'", self.pre(sid, command="git push origin HEAD:develop"))
            self.assertIn("'main'", self.pre(sid, command="git push origin HEAD:main"))   # the coding default stays
            self.assertIsNone(self.pre(sid, command="git push origin HEAD:feat/a"))
        with self.owner(recipe="artifact"):
            self.assertIsNone(self.pre(sid, command="git push origin HEAD:main"))
            self.assertIn("force", self.pre(sid, command="git push -f origin HEAD:feat/a"))
        with self.owner(recipe="coding", target_branches=["release/1", "next"]):
            self.assertIn("release/1", self.pre(sid, command="git push origin HEAD:release/1"))
            self.assertIn("next", self.pre(sid, command="git push origin next"))

    def test_the_owner_record_is_read_only_when_a_push_needs_the_targets(self):
        sid, _ = self.launch_seat("eng-a")
        with mock.patch.object(drivegate, "drive_targets", side_effect=AssertionError("targets read")):
            self.assertIsNone(self.pre(sid, command="ls -la && git status && echo hi"))
            self.assertIn("could not decide", self.pre(sid, command="git push origin feat/a"))

    def test_branches_the_ledger_integrated_into_are_targets(self):
        sid, _ = self.launch_seat("eng-a")
        self.edit(lambda l: l.integrated.update({"feat/a": {"into": "feat/swarm-autonomy"}}))
        self.assertIn("feat/swarm-autonomy", self.pre(sid, command="git push origin HEAD:feat/swarm-autonomy"))

    def test_a_push_that_names_no_branch_pushes_the_checked_out_one(self):
        sid, _ = self.launch_seat("eng-a")
        (self.work / ".git").mkdir()
        (self.work / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
        for cmd in ("git push", "git push origin", "git push origin HEAD", "git push -u origin HEAD"):
            with self.subTest(cmd=cmd):
                self.assertIn("main", self.pre(sid, command=cmd))
        (self.work / ".git" / "HEAD").write_text("ref: refs/heads/feat/a\n")
        for cmd in ("git push", "git push origin", "git push origin HEAD", "git push -u origin HEAD"):
            with self.subTest(cmd=cmd):
                self.assertIsNone(self.pre(sid, command=cmd))
        (self.work / ".git" / "HEAD").write_text("0123456789abcdef0123456789abcdef01234567\n")   # detached
        self.assertIn("names no branch", self.pre(sid, command="git push"))

    def test_a_push_after_a_cd_is_refused_because_the_branch_cannot_be_told(self):
        sid, _ = self.launch_seat("eng-a")
        (self.work / ".git").mkdir()
        (self.work / ".git" / "HEAD").write_text("ref: refs/heads/feat/a\n")
        self.assertIn("names no branch", self.pre(sid, command="cd /elsewhere && git push origin"))
        self.assertIsNone(self.pre(sid, command="cd /elsewhere && git push origin feat/a"))

    def test_git_dash_c_directory_names_the_branch_to_look_at(self):
        sid, _ = self.launch_seat("eng-a")
        other = self.work / "other"
        (other / ".git").mkdir(parents=True)
        (other / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
        self.assertIn("main", self.pre(sid, command=f"git -C {other.as_posix()} push origin"))
        self.assertIn("main", self.pre(sid, command="git -C other push"))

    def test_head_branch_follows_worktree_gitdir_files(self):
        gitdir = self.work / "real.git"
        gitdir.mkdir()
        (gitdir / "HEAD").write_text("ref: refs/heads/feat/w\n")
        wt = self.work / "wt"
        wt.mkdir()
        (wt / ".git").write_text(f"gitdir: {gitdir}\n")
        self.assertEqual(drivegate.head_branch(str(wt)), "feat/w")
        self.assertEqual(drivegate.head_branch(str(wt / "sub")), "feat/w")
        self.assertIsNone(drivegate.head_branch("/nonexistent/x"))

    def test_refusals_hold_for_every_role_and_every_seat_kind(self):
        for name, role, kind in (("eng-a", "engineer", "seat"), ("lead", "engineering_lead", "lead"),
                                 ("judge", "judge", "judge"), ("integrator-a-1", "integrator", "integrate")):
            sid, _ = self.launch_seat(name, role=role, kind=kind)
            with self.subTest(seat=name):
                self.assertIn("target branch", self.pre(sid, command="git -c k=v push origin 'HEAD:main'"))
                self.assertIn("claude -p", self.pre(sid, command="bash -lc 'claude -p hi'"))
                self.assertIn("codex exec", self.pre(sid, command="codex e hi"))

    def test_the_hook_denies_with_a_pointer_to_swarm_land(self):
        sid, _ = self.launch_seat("eng-a")
        out = self.hook("turn", agent_id=None, session=sid, cwd=str(self.work), tool_name="Bash",
                        tool_input={"command": "git push origin HEAD:main"})
        self.assertIn("swarm land", out["hookSpecificOutput"]["permissionDecisionReason"])


class ScriptFileRule(DriveCase):
    """Round 4: a script runs in a drive seat only when it is tracked unmodified at HEAD; the gate resolves a
    relative script against the call's cwd (the payload's), the same for `./x.sh` and `bash x.sh`."""

    @unittest.skipUnless(__import__("shutil").which("git"), "needs git")
    def test_a_script_in_the_seat_cwd_is_checked_against_head(self):
        import subprocess
        git = ["git", "-C", str(self.work), "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false"]
        subprocess.run(["git", "init", "-q", str(self.work)], check=True)
        (self.work / "check.sh").write_text("echo ok\n")
        subprocess.run(git + ["add", "check.sh"], check=True)
        subprocess.run(git + ["commit", "-qm", "init"], check=True)
        (self.work / "new.sh").write_text("git push origin HEAD:main\n")
        sid, _ = self.launch_seat("eng-a")
        judge, _ = self.launch_seat("judge", role="judge", kind="judge")
        for who in (sid, judge):
            for cmd in ("./check.sh", "bash check.sh", "sh ./check.sh"):
                with self.subTest(who=who, cmd=cmd):
                    self.assertIsNone(self.pre(who, command=cmd))
            for cmd in ("./new.sh", "bash new.sh", "source new.sh"):
                with self.subTest(who=who, cmd=cmd):
                    self.assertIn("tracked unmodified at HEAD", self.pre(who, command=cmd))


class ProtectedWrites(DriveCase):
    """Round 5 (rr31): no seat writes git, gh or ssh configuration with a file tool; other files are untouched."""

    def test_write_tools_may_not_write_git_configuration(self):
        (self.work / ".git" / "hooks").mkdir(parents=True)
        sid, _ = self.launch_seat("eng-a")
        for tool, key in (("Write", "file_path"), ("Edit", "file_path"), ("MultiEdit", "file_path"),
                          ("NotebookEdit", "notebook_path")):
            for rel in (".git/config", ".git/hooks/pre-commit", ".gitmodules"):
                with self.subTest(tool=tool, rel=rel):
                    out = self.pre(sid, tool, tool_input={key: str(self.work / rel), "content": "x"})
                    self.assertIn("may not write", out)
            self.assertIn("may not write", self.pre(sid, tool, tool_input={key: ".git/config", "content": "x"}))
        self.assertIsNone(self.pre(sid, "Write", tool_input={"file_path": str(self.work / "src.py"), "content": "x"}))


class FailClosed(DriveCase):
    def test_an_error_in_a_known_seats_decision_refuses_the_call(self):
        sid, _ = self.launch_seat("eng-a")
        with mock.patch.object(shellguard, "drive_command_refusal", side_effect=RuntimeError("boom")):
            self.assertIn("could not decide", self.pre(sid, command="ls"))

    def test_an_error_before_the_session_is_known_lets_the_call_through(self):
        with mock.patch.object(drivegate, "seat_context", side_effect=RuntimeError("boom")):
            self.assertIsNone(self.pre("stranger", command="ls"))

    def test_an_unreadable_ledger_makes_the_seat_strict(self):
        sid, _ = self.launch_seat("eng-a")
        with mock.patch.object(drivegate, "load_ledger", side_effect=ledger.LedgerBadError("bad")):
            self.assertIn("could not be verified", self.pre(sid, command="echo x > f"))
            self.assertIsNone(self.pre(sid, command="git status"))

    def test_a_classify_error_does_not_stop_the_receipts(self):
        with mock.patch.object(drivegate, "classify", side_effect=RuntimeError("boom")), \
                mock.patch.object(drivegate, "post_tool") as post:
            self.hook("done", agent_id=None, session="x", tool_name="Bash", tool_input={"command": "ls"})
        post.assert_called_once()
        self.assertIn("boom", self.error_log.read_text())

    def test_a_classify_error_on_a_turn_is_denied(self):
        sid, _ = self.launch_seat("judge", role="judge", kind="judge")
        with mock.patch.object(drivegate, "classify", side_effect=RuntimeError("boom")):
            out = self.hook("turn", agent_id=None, session=sid, cwd=str(self.work), tool_name="Bash",
                            tool_input={"command": "git commit -am x"})
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_a_codex_seat_with_a_replacement_token_and_no_marker_is_still_gated(self):
        sid, _ = self.launch_seat("judge", role="judge", kind="judge", harness="codex")
        enrolment.write(self.key, job="J", agent_key=sid, harness="codex", session_id=sid, cwd=str(self.work))
        for p in Path(self.cfg["hook"]["marker_dir"]).expanduser().glob("*resume*"):
            p.unlink()
        with mock.patch.dict(os.environ, {markers.TOKEN_ENV: "tok"}):
            out = self.hook("turn", agent_id=None, session=sid, host="codex", cwd=str(self.work),
                            tool_name="Bash", tool_input={"command": "git commit -am x && git push origin HEAD:main"})
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_the_barrier_is_a_literal_barrier_hit(self):
        src = (Path(drivegate.__file__)).read_text()
        self.assertIn('barrier.hit("hook_after_cli_before_receipt")', src)
        self.assertNotIn("def _barrier", src)

    def test_the_session_is_classified_once_per_call(self):
        sid, _ = self.launch_seat("eng-a")
        with mock.patch.object(drivegate, "classify", wraps=drivegate.classify) as classify:
            self.hook("turn", agent_id=None, session=sid, cwd=str(self.work), tool_name="Bash",
                      tool_input={"command": "ls"})
        self.assertEqual(classify.call_count, 1)

    def test_the_hook_survives_a_gate_error_on_a_non_drive_session(self):
        with mock.patch.object(drivegate, "pre_tool", side_effect=RuntimeError("boom")):
            self.assertIsNone(self.hook("turn", agent_id=None, session="stranger", tool_name="Bash",
                                        tool_input={"command": "ls"}))


class Caching(DriveCase):
    def test_a_changed_ledger_invalidates_the_cached_one(self):
        sid, _ = self.launch_seat("eng-a")
        real = ledger.load
        drivegate._LEDGER_CACHE.clear()
        with mock.patch.object(ledger, "load", wraps=real) as load:
            self.pre(sid, command="ls")
            self.pre(sid, command="ls")
            self.assertEqual(load.call_count, 1)
            self.edit(lambda l: setattr(l, "errors", 3))
            n = load.call_count
            led = drivegate.load_ledger(self.key, "J")
            self.assertEqual(led.errors, 3)
            self.assertEqual(load.call_count, n + 1)
            drivegate.load_ledger(self.key, "J")
            self.assertEqual(load.call_count, n + 1)

    def test_a_ledger_that_is_gone_is_forgotten(self):
        drivegate.load_ledger(self.key, "J")
        self.assertIn((self.key, "J"), drivegate._LEDGER_CACHE)
        for f in (Path(os.environ["HOME"]) / ".local/share/swarm/supervisor/drive").glob("*.json"):
            f.unlink()
        self.assertIsNone(drivegate.load_ledger(self.key, "J"))
        self.assertNotIn((self.key, "J"), drivegate._LEDGER_CACHE)

    def test_a_host_with_no_ledger_does_no_work(self):
        with mock.patch.object(drivegate, "has_ledgers", return_value=False), \
                mock.patch.object(drivegate, "seat_context", side_effect=AssertionError("looked")):
            self.assertIsNone(self.pre("anyone", command="git push --force origin main"))

    def test_a_host_that_never_activated_a_drive_job_never_reaches_the_gate(self):
        import shutil
        shutil.rmtree(Path(os.environ["HOME"]) / ".local/share/swarm/supervisor/drive")
        with mock.patch.object(drivegate, "pre_tool", side_effect=AssertionError("gate ran")), \
                mock.patch.object(drivegate, "post_tool", side_effect=AssertionError("gate ran")):
            self.assertIsNone(self.hook("turn", agent_id=None, session="x", tool_name="Bash",
                                        tool_input={"command": "git push --force origin main"}))
            self.assertIsNone(self.hook("done", agent_id=None, session="x", tool_name="Bash",
                                        tool_input={"command": "swarm done --job J"}))
        self.assertFalse(self.error_log.exists() and "gate ran" in self.error_log.read_text())

    def test_has_ledgers_is_false_without_the_drive_directory(self):
        d = Path(os.environ["HOME"]) / ".local/share/swarm/supervisor/drive"
        self.assertTrue(drivegate.has_ledgers())
        for f in d.glob("*"):
            f.unlink()
        self.assertFalse(drivegate.has_ledgers())
        d.rmdir()
        self.assertFalse(drivegate.has_ledgers())

    def test_reads_by_a_writer_seat_need_neither_the_board_nor_the_ledger_intents(self):
        sid, _ = self.launch_seat("eng-a")
        with mock.patch.object(drivegate, "_open_board", side_effect=AssertionError("board opened")):
            self.assertIsNone(self.pre(sid, command="git log"))
            self.assertIsNone(self.pre(sid, "Read", tool_input={"file_path": "x"}))


def private_host_dir() -> Path:
    d = paths.host_dir()
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    return d


class Kick(Env):
    def test_kick_debounced(self):
        calls = []
        with mock.patch("subprocess.run", side_effect=lambda *a, **k: calls.append(a) or mock.Mock(returncode=0)):
            self.assertTrue(drivegate.kick(now=lambda: 100.0))
            self.assertFalse(drivegate.kick(now=lambda: 110.0))
            self.assertTrue(drivegate.kick(now=lambda: 121.0))
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][0][:4], ["systemctl", "--user", "start", "--no-block"])

    def test_hooks_kick_is_the_same_kick(self):
        """6a's `swarm activate` and the plan call hooks.kick(); the CLI itself calls drivegate.kick()."""
        with mock.patch("subprocess.run") as run:
            self.assertTrue(hooks.kick(now=lambda: 100.0))
            self.assertFalse(drivegate.kick(now=lambda: 105.0))      # one stamp for both
        self.assertEqual(run.call_count, 1)

    def test_kick_never_raises(self):
        with mock.patch("subprocess.run", side_effect=OSError("no systemctl")):
            self.assertFalse(drivegate.kick(now=lambda: 500.0))

    def test_P8_a_garbage_stamp_heals_itself(self):
        d = private_host_dir()
        for junk in ("not-a-number", "nan", "inf", "", "1e999999"):
            (d / "kick.stamp").write_text(junk)
            with self.subTest(stamp=junk), mock.patch("subprocess.run") as run:
                self.assertTrue(drivegate.kick(now=lambda: 10_000.0))
                self.assertFalse(drivegate.kick(now=lambda: 10_001.0))     # rewritten: debounced again
                self.assertEqual(run.call_count, 1)

    def test_a_stamp_from_the_future_does_not_suppress_kicks(self):
        d = private_host_dir()
        (d / "kick.stamp").write_text("99999999")
        with mock.patch("subprocess.run"):
            self.assertTrue(drivegate.kick(now=lambda: 100.0))


class DoctorReceipts(Env):
    def write(self, text):
        d = paths.host_dir()
        d.mkdir(parents=True, exist_ok=True)
        (d / "receipt-failures.log").write_text(text)

    def test_doctor_fails_on_receipt_write_failure(self):
        self.write(f"{time.time():.0f} J seat_request id=7 rev=1: OSError: gone\n")
        checks = [c for c in bootstrap._receipt_failures_check() if c.name == "drive receipts"]
        self.assertEqual([c.ok for c in checks], [False])

    def test_doctor_ignores_an_old_receipt_failure(self):
        self.write("1000 J seat_request id=7 rev=1: OSError: old\n")
        self.assertEqual(bootstrap._receipt_failures_check(), [])

    def test_P9_a_big_log_does_not_blind_the_doctor(self):
        old = "1000 J seat_request id=7 rev=1: OSError: old\n" * 4000     # more than 64 KiB
        self.write(old + f"{time.time():.0f} J seat_request id=8 rev=1: OSError: new\n")
        checks = bootstrap._receipt_failures_check()
        self.assertEqual([c.ok for c in checks], [False])
        self.assertIn("1 control record receipt", checks[0].detail)
        self.assertIn("id=8", checks[0].detail)

    def test_doctor_includes_the_check(self):
        with mock.patch.object(bootstrap, "_receipt_failures_check", return_value=["marker"]) as chk:
            out = bootstrap.doctor(config=self.config)
        chk.assert_called()
        self.assertIn("marker", out)

    def test_the_partial_first_line_of_a_cut_tail_is_not_a_failure(self):
        self.write("1000 " + "9" * 70000 + "\n")          # the tail starts inside a run of digits (a huge "time")
        self.assertEqual(bootstrap._receipt_failures_check(), [])

    def test_the_log_may_not_end_in_a_newline_or_hold_garbage(self):
        self.write("garbage\n\n" + f"{time.time():.0f} J person id=1 rev=1: OSError: x")
        self.assertEqual(len(bootstrap._receipt_failures_check()), 1)


class CodexDoctorLine(Env):
    def checks(self, which, features=""):
        run = mock.Mock(return_value=mock.Mock(returncode=0, stdout=features, stderr=""))
        with mock.patch("shutil.which", return_value=which), mock.patch("subprocess.run", run):
            return bootstrap._codex_drive_check()

    def test_doctor_codex_readonly_line(self):
        (c,) = self.checks("/usr/bin/codex", "rollout_budget  stable  true\n")
        self.assertEqual(c.name, "codex drive-ready")
        self.assertEqual(c.detail, "writer yes (rollout budget available); judge/reviewer: claude "
                                   "(codex read-only has no board access)")
        self.assertTrue(c.ok)

    def test_codex_writer_no_when_codex_is_missing_or_lacks_the_rollout_budget(self):
        (c,) = self.checks(None)
        self.assertEqual(c.detail, "writer no (codex is not installed); judge/reviewer: claude "
                                   "(codex read-only has no board access)")
        (c,) = self.checks("/usr/bin/codex", "other_feature stable true\n")
        self.assertTrue(c.detail.startswith("writer no (the installed codex has no rollout budget feature)"))
        self.assertIsNone(c.ok)

    def test_the_line_is_part_of_the_doctor(self):
        with mock.patch.object(bootstrap, "_codex_drive_check", return_value=["codex-marker"]):
            out = bootstrap.doctor(config=self.config)
        self.assertIn("codex-marker", out)


class RoleRules(DriveCase):
    def test_role_predicates(self):
        for role in ("judge", "reviewer", "goal_judge", "ruling_judge", "security", "security_reviewer",
                     "code_reviewer", "Judge"):
            self.assertTrue(driveroles.is_read_only_role(role), role)
        for role in ("engineer", "worker", "engineering_lead", "integrator", "qa", None, 3):
            self.assertFalse(driveroles.is_read_only_role(role), role)
        self.assertTrue(driveroles.is_judge_role("ruling_judge"))
        self.assertFalse(driveroles.is_judge_role("reviewer"))
        for name in ("lead", "lead~1", "lead~12"):
            self.assertTrue(driveroles.seat_name_is_lead(name))
        for name in ("leader", "lead-2", "eng-lead", "lead~", None):
            self.assertFalse(driveroles.seat_name_is_lead(name))
        for name in ("judge", "goal-judge", "ruling-judge", "reviewer-feat-a-12ab34cd"):
            self.assertTrue(driveroles.seat_name_is_read_only(name))
        self.assertFalse(driveroles.seat_name_is_read_only("my-judge"))
        self.assertFalse(driveroles.seat_name_is_read_only("integrator-a-1"))
        self.assertEqual(driveroles.action_kind("gen2|review|rev|feat/a|1|7"), "review")
        self.assertIsNone(driveroles.action_kind("2|review|rev|feat/a|1|7"))
        self.assertIsNone(driveroles.action_kind("driver|authorize|lead"))
        self.assertIsNone(driveroles.action_kind("requester:seat"))
        self.assertIsNone(driveroles.action_kind(None))

    def test_read_only_claude_launches_disallow_mcp_tools(self):
        from swarm.supervisor import launch
        self.assertEqual(launch.READ_ONLY_CLAUDE_DISALLOWED, "Edit,Write,NotebookEdit,MultiEdit,mcp__*")

    def test_codex_judges_and_reviewers_run_on_claude(self):
        self.assertEqual(driveroles.effective_harness("judge", "codex")[0], "claude")
        self.assertIn("Codex", driveroles.effective_harness("reviewer", "codex")[1])
        self.assertEqual(driveroles.effective_harness("engineer", "codex"), ("codex", None))
        self.assertEqual(driveroles.effective_harness("judge", "claude"), ("claude", None))


class SeatEnrolsFromMarker(Env):
    """[B1] a supervisor-launched seat enrols through its launch marker, whatever other jobs are active on the host."""

    def setUp(self):
        super().setUp()
        self.config.write_text(self.config.read_text() + "\n[supervise]\nenabled = true\n")
        from swarm import cli
        self.cfg = cli.load_config(self.config)
        for job in ("J1", "J2"):
            rc, _, err = self.cli("activate", "--job", job, "--session", "orchestrator-sess")
            self.assertEqual(rc, 0, err)

    def launch(self, job, sid):
        with self.board() as b:
            old = f"orig-{job}"
            name = b.allocate_name(old, job, "worker")
            b.close_agent(old, "stuck:dead")
            rid = b.record_restart(job, old, old, "drive", "claude", 60).id
        markers.write_resume_marker(self.cfg, job, rid, resume_of=old, name=name, harness="claude", session_id=sid)
        return name

    def test_seat_enrols_from_marker_with_several_active_jobs(self):
        name = self.launch("J2", "seat-session-2")
        out = self.hook("turn", agent_id=None, session="seat-session-2", tool_name="Bash",
                        tool_input={"command": "ls"})
        self.assertIn(name, self.context(out))
        agent = self.agent("seat-session-2", job="J2")
        self.assertEqual((agent.name, agent.status), (name, "running"))
        self.assertIsNone(self.agent("seat-session-2", job="J1"))
        self.assertEqual(enrolment.find(store_key(self.cfg), "seat-session-2").job, "J2")

    def test_a_session_with_neither_marker_nor_job_tag_joins_nothing(self):
        self.hook("turn", agent_id="stray-sub", session="orchestrator-sess", tool_name="Bash",
                  tool_input={"command": "ls"})
        self.assertIsNone(self.agent("stray-sub", job="J1"))
        self.assertIsNone(self.agent("stray-sub", job="J2"))
        self.assertIsNone(enrolment.find(store_key(self.cfg), "stray-sub"))

    def test_each_seat_joins_only_its_own_job(self):
        self.launch("J1", "seat-session-1")
        self.launch("J2", "seat-session-2")
        self.hook("turn", agent_id=None, session="seat-session-1", tool_name="Bash", tool_input={"command": "ls"})
        self.assertIsNotNone(self.agent("seat-session-1", job="J1"))
        self.assertIsNone(self.agent("seat-session-1", job="J2"))
        self.assertIsNone(self.agent("seat-session-2", job="J2"))


class DriveJudgeFlag(Env):
    """QA bug 4: a driver-launched judge seat records verdicts; the flag comes from the ledger's receipted
    seat role, never from a prompt tag; `swarm done` runs as the caller like `swarm verdict`."""

    def setUp(self):
        super().setUp()
        rc, _, err = self.cli("activate", "--job", "J", "--session", "orchestrator-sess")
        self.assertEqual(rc, 0, err)
        from swarm import cli
        self.cfg = cli.load_config(self.config)
        with self.board() as b:
            b.set_job_goal("J", "ship it")
            self.name = b.allocate_name("seat-agent", "J", "worker")

    def flag(self, role):
        ctx = drivegate.Ctx(kind="seat", job="J", led=types.SimpleNamespace(generation=1), agent_key="seat-agent")
        ctx.seat = "judge"
        from swarm.supervisor import drive_authz
        with self.board() as b, \
                mock.patch.object(drivegate, "seat_context", return_value=ctx), \
                mock.patch.object(b, "seat", return_value=types.SimpleNamespace(role=role)), \
                mock.patch.object(drive_authz, "receipted_role", return_value=role):
            hooks._drive_judge_flag(b, self.cfg, "seat-agent", "J", {"session_id": "s"}, {})
            return b.record_verdict("J", self.name, "met", "evidence", None, None)

    def test_a_driver_launched_judge_seat_records_a_verdict(self):
        self.assertTrue(self.flag("judge"))

    def test_a_non_judge_seat_does_not_become_the_judge(self):
        self.assertFalse(self.flag("engineer"))
        self.assertFalse(self.flag(None))     # a role a board writer changed after the receipt

    def test_a_judge_tag_in_a_drive_jobs_prompt_is_ignored(self):
        with self.board() as b, mock.patch.object(hooks, "_drive_job", return_value=True), \
                mock.patch.object(hooks, "_role", return_value="judge"):
            goal, judge, is_judge, note = hooks._goal_roles(b, "seat-agent", "J", {}, {"goal": True},
                                                            "[swarm role: judge]", self.cfg)
            self.assertFalse(is_judge)
            self.assertFalse(b.record_verdict("J", self.name, "met", "evidence", None, None))

    def test_done_runs_as_the_caller_like_verdict(self):
        out = hooks.own_name_verdict_command("swarm done --job J --artifact b@" + "a" * 40, "Homer", "J")
        self.assertIn("--as Homer", out)
        out = hooks.own_name_verdict_command("swarm done --job J --as Marge --artifact x", "Homer", "J")
        self.assertIn("--as Homer", out)
        self.assertNotIn("Marge", out)
        self.assertIsNone(hooks.own_name_verdict_command("swarm done --job OTHER --artifact x", "Homer", "J"))


class DriveJobDetection(unittest.TestCase):
    """hooks._drive_job, unmocked: a real ledger makes a drive job; an error is the stricter case."""

    def test_ledger_presence_and_errors(self):
        cfg = {}
        with mock.patch("swarm.board.autoinit.store_key", return_value="bk"), \
                mock.patch.object(ledger, "exists", side_effect=lambda bk, job: job == "D"):
            self.assertTrue(hooks._drive_job(cfg, "D"))
            self.assertFalse(hooks._drive_job(cfg, "N"))
        with mock.patch("swarm.board.autoinit.store_key", return_value="bk"), \
                mock.patch.object(ledger, "exists", side_effect=OSError("unreadable")):
            self.assertTrue(hooks._drive_job(cfg, "D"))   # cannot tell: fail closed

    def test_enrolment_wires_the_judge_flag_and_the_cfg(self):
        import inspect
        self.assertIn("_drive_judge_flag(board, cfg, agent_id, job, payload, resume)", inspect.getsource(hooks._enrol_resumed))
        self.assertIn("_goal_roles(board, agent_id, job, payload, marker, prompt, cfg)", inspect.getsource(hooks))
