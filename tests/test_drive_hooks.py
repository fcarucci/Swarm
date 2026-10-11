"""Drive-job gates of the hooks (spec 3.4, 4.0, 4.4; Task 5 and 5b): who is calling comes from host-private state,
read-only seats stay read-only on every path (the join, the first call, an unbound job, a subagent), seats stay off
the target branch, the MCP allowlist, spawn refusals, the kick, the doctor lines, [B1] marker enrolment."""
from __future__ import annotations

import dataclasses
import os
import time
import types
from pathlib import Path
from unittest import mock

from drive_hooks_support import DriveCase, REQ
from test_hooks_cli import Env

from swarm import bootstrap, drive_hooks, driveroles, enrolment, hooks, paths, shellguard
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
            with mock.patch.object(drive_hooks, "_open_board", side_effect=AssertionError("board opened")):
                with self.subTest(seat=name):
                    self.assertIn("read-only", self.pre(sid, command="git commit -m x"))

    def test_P5_a_judge_row_rewritten_to_engineer_stays_read_only(self):
        """The role comes from the seat's receipt, never from the row a writer can change."""
        sid, _ = self.launch_seat("qa-1", role="judge", kind="seat")
        real = drive_hooks._open_board

        class Flip:
            def __init__(self, b):
                self.b = b

            def __getattr__(self, name):
                return getattr(self.b, name)

            def seat(self, job, gen, seat):
                s = self.b.seat(job, gen, seat)
                return dataclasses.replace(s, role="engineer") if s else s
        with mock.patch.object(drive_hooks, "_open_board", lambda cfg: Flip(real(cfg))):
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
        self.assertIn("main", self.pre(sid, command=f"git -C {other} push origin"))
        self.assertIn("main", self.pre(sid, command="git -C other push"))

    def test_head_branch_follows_worktree_gitdir_files(self):
        gitdir = self.work / "real.git"
        gitdir.mkdir()
        (gitdir / "HEAD").write_text("ref: refs/heads/feat/w\n")
        wt = self.work / "wt"
        wt.mkdir()
        (wt / ".git").write_text(f"gitdir: {gitdir}\n")
        self.assertEqual(drive_hooks.head_branch(str(wt)), "feat/w")
        self.assertEqual(drive_hooks.head_branch(str(wt / "sub")), "feat/w")
        self.assertIsNone(drive_hooks.head_branch("/nonexistent/x"))

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


class FailClosed(DriveCase):
    def test_an_error_in_a_known_seats_decision_refuses_the_call(self):
        sid, _ = self.launch_seat("eng-a")
        with mock.patch.object(shellguard, "drive_command_refusal", side_effect=RuntimeError("boom")):
            self.assertIn("could not decide", self.pre(sid, command="ls"))

    def test_an_error_before_the_session_is_known_lets_the_call_through(self):
        with mock.patch.object(drive_hooks, "seat_context", side_effect=RuntimeError("boom")):
            self.assertIsNone(self.pre("stranger", command="ls"))

    def test_an_unreadable_ledger_makes_the_seat_strict(self):
        sid, _ = self.launch_seat("eng-a")
        with mock.patch.object(drive_hooks, "load_ledger", side_effect=ledger.LedgerBadError("bad")):
            self.assertIn("could not be verified", self.pre(sid, command="echo x > f"))
            self.assertIsNone(self.pre(sid, command="git status"))

    def test_the_hook_survives_a_gate_error_on_a_non_drive_session(self):
        with mock.patch.object(drive_hooks, "pre_tool", side_effect=RuntimeError("boom")):
            self.assertIsNone(self.hook("turn", agent_id=None, session="stranger", tool_name="Bash",
                                        tool_input={"command": "ls"}))


class Caching(DriveCase):
    def test_a_changed_ledger_invalidates_the_cached_one(self):
        sid, _ = self.launch_seat("eng-a")
        real = ledger.load
        drive_hooks._LEDGER_CACHE.clear()
        with mock.patch.object(ledger, "load", wraps=real) as load:
            self.pre(sid, command="ls")
            self.pre(sid, command="ls")
            self.assertEqual(load.call_count, 1)
            self.edit(lambda l: setattr(l, "errors", 3))
            n = load.call_count
            led = drive_hooks.load_ledger(self.key, "J")
            self.assertEqual(led.errors, 3)
            self.assertEqual(load.call_count, n + 1)
            drive_hooks.load_ledger(self.key, "J")
            self.assertEqual(load.call_count, n + 1)

    def test_a_ledger_that_is_gone_is_forgotten(self):
        drive_hooks.load_ledger(self.key, "J")
        self.assertIn((self.key, "J"), drive_hooks._LEDGER_CACHE)
        self.assertIsNone(drive_hooks.load_ledger(self.key, "NOPE"))
        self.assertNotIn((self.key, "NOPE"), drive_hooks._LEDGER_CACHE)

    def test_a_host_with_no_ledger_does_no_work(self):
        with mock.patch.object(drive_hooks, "has_ledgers", return_value=False), \
                mock.patch.object(drive_hooks, "seat_context", side_effect=AssertionError("looked")):
            self.assertIsNone(self.pre("anyone", command="git push --force origin main"))

    def test_has_ledgers_is_false_without_the_drive_directory(self):
        d = Path(os.environ["HOME"]) / ".local/share/swarm/supervisor/drive"
        self.assertTrue(drive_hooks.has_ledgers())
        for f in d.glob("*"):
            f.unlink()
        self.assertFalse(drive_hooks.has_ledgers())
        d.rmdir()
        self.assertFalse(drive_hooks.has_ledgers())

    def test_reads_by_a_writer_seat_need_neither_the_board_nor_the_ledger_intents(self):
        sid, _ = self.launch_seat("eng-a")
        with mock.patch.object(drive_hooks, "_open_board", side_effect=AssertionError("board opened")):
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
            self.assertTrue(drive_hooks.kick(now=lambda: 100.0))
            self.assertFalse(drive_hooks.kick(now=lambda: 110.0))
            self.assertTrue(drive_hooks.kick(now=lambda: 121.0))
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][0][:4], ["systemctl", "--user", "start", "--no-block"])

    def test_kick_never_raises(self):
        with mock.patch("subprocess.run", side_effect=OSError("no systemctl")):
            self.assertFalse(drive_hooks.kick(now=lambda: 500.0))

    def test_P8_a_garbage_stamp_heals_itself(self):
        d = private_host_dir()
        for junk in ("not-a-number", "nan", "inf", "", "1e999999"):
            (d / "kick.stamp").write_text(junk)
            with self.subTest(stamp=junk), mock.patch("subprocess.run") as run:
                self.assertTrue(drive_hooks.kick(now=lambda: 10_000.0))
                self.assertFalse(drive_hooks.kick(now=lambda: 10_001.0))     # rewritten: debounced again
                self.assertEqual(run.call_count, 1)

    def test_a_stamp_from_the_future_does_not_suppress_kicks(self):
        d = private_host_dir()
        (d / "kick.stamp").write_text("99999999")
        with mock.patch("subprocess.run"):
            self.assertTrue(drive_hooks.kick(now=lambda: 100.0))


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
        self.assertIsNone(driveroles.action_kind("requester:seat"))
        self.assertIsNone(driveroles.action_kind(None))

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
