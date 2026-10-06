"""After a not_met verdict with nobody left at work, the main session's hooks (no agent_id) tell
the orchestrator to spawn the next round (swarm.respawn): context at PreToolUse/PostToolUse, once
per verdict, and a Stop that refuses to end the turn, once. The judge's own hooks never carry it."""
from __future__ import annotations

import datetime as dt
import unittest
from types import SimpleNamespace
from unittest import mock

from test_goals import GoalEnv  # noqa: E402  (sets sys.path)

from swarm import respawn, hosts  # noqa: E402

REASON, NEXT = "no failover drill", "run the db-2 failover drill and post the log"


class BriefTests(unittest.TestCase):
    def test_codex_next_round_uses_explicit_nonempty_judge_task_name(self):
        js = SimpleNamespace(verdict_by="Judge Simpson", verdict_reason=REASON, verdict_next=NEXT)
        text = respawn.brief(js, "J", spawn_tags=False)
        self.assertIn("task name `judge__<task>`", text)
        self.assertIn("`judge__round2`", text)


class RespawnEnv(GoalEnv):
    def setUp(self):
        super().setUp()
        self.cfg["pipeline"] = {"enabled": False}
        p = mock.patch.object(respawn, "CHECK_SECONDS", 0.0)
        p.start()
        self.addCleanup(p.stop)
        self.activate_goal()
        self.spawn_judge()
        self.spawn_worker("w1")
        self.judge = self.member("judge-1").name

    def not_met(self):
        rc, _, err = self.cli("verdict", "--job", "J", "--as", self.judge, "not_met",
                              "--reason", REASON, "--next", NEXT)
        self.assertEqual(rc, 0, err)

    def finish(self, *keys):
        for k in keys:
            self.hook("stop", agent_id=k, session="sess-1", agent_type="general-purpose",
                      transcript_path=self.main_transcript())
            # These fixtures model the verdict arriving after the workers' last activity.
            # Tests for activity after a verdict add that contact explicitly.
            if k.startswith("w"):
                self.h.backdate_agent(k, joined_at=2, last_seen=1, left_at=1)

    def main(self, event="turn", host=None, **extra):
        return self.hook(event, agent_id=None, session="sess-1", host=host, tool_name="Bash", **extra)

    def expected(self):
        return (f"[swarm] job \"J\": judge {self.judge} ruled not met: {REASON}. Spawn agents now with "
                f"these instructions: {NEXT}, plus a new judge for the same goal; spawn more agents "
                f"if the work needs it. Don't leave the job idle.")


class TriggerTests(RespawnEnv):
    def test_not_met_and_last_agent_done_injects_the_brief(self):
        self.not_met()
        self.finish("w1")
        self.assertIsNone(self.main("turn"))          # the judge is still running
        self.finish("judge-1")
        ctx = self.context(self.main("turn"))
        self.assertTrue(ctx.startswith(self.expected()), ctx)
        self.assertIn("[swarm job: J]", ctx)
        self.assertIn("[swarm role: judge]", ctx)

    def test_post_tool_use_delivers_it_too(self):
        self.not_met()
        self.finish("w1", "judge-1")
        out = self.main("done")
        self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "PostToolUse")
        self.assertIn(self.expected(), out["hookSpecificOutput"]["additionalContext"])

    def test_not_met_with_an_agent_still_running_says_nothing(self):
        self.not_met()
        self.finish("judge-1")                         # w1 still works
        self.assertIsNone(self.main("turn"))
        self.assertIsNone(self.main("done"))
        self.assertIsNone(self.main("session-stop"))

    def test_a_closed_job_never_nags(self):
        # closed by hand, or by the expiry sweep, while its marker is still here
        self.not_met()
        self.finish("w1", "judge-1")
        self.assertIsNotNone(self.main("turn"))    # open: it does
        with self.board() as b:
            b.close_job("J", "cancelled", "auto-closed: no live agents for 30 min", closed_by="auto")
        self.assertTrue((self.markers / "J.json").exists())
        for ev in ("turn", "done", "session-stop"):
            self.assertIsNone(self.main(ev))

    def test_a_waiting_job_never_nags_until_it_resumes(self):
        # `swarm wait`: the next round needs the user, so spawning agents can't move it
        self.not_met()
        self.finish("w1", "judge-1")
        rc, _, err = self.cli("wait", "--job", "J", "--on", "the user's re-link")
        self.assertEqual(rc, 0, err)
        for ev in ("turn", "done", "session-stop"):
            self.assertIsNone(self.main(ev))
        rc, _, err = self.cli("resume", "--job", "J")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.main("session-stop")["decision"], "block")

    def test_met_says_nothing(self):
        rc, _, _ = self.cli("verdict", "--job", "J", "--as", self.judge, "met", "all", "good")
        self.assertEqual(rc, 0)
        self.finish("w1", "judge-1")
        for ev in ("turn", "done", "session-stop"):
            self.assertIsNone(self.main(ev))

    def test_no_verdict_yet_says_nothing(self):
        self.finish("w1", "judge-1")
        for ev in ("turn", "done", "session-stop"):
            self.assertIsNone(self.main(ev))

    def test_said_once_per_verdict_then_again_for_a_new_one(self):
        self.not_met()
        self.finish("w1", "judge-1")
        self.assertIsNotNone(self.main("turn"))
        self.assertIsNone(self.main("turn"))
        self.assertIsNone(self.main("done"))
        # a fresh round: new agents, a new judge's second not_met verdict
        self.spawn_worker("w2")
        self.assertIsNone(self.main("turn"))
        self.spawn("judge-2", self.judge_prompt())
        judge2 = self.member("judge-2").name
        rc, _, err = self.cli("verdict", "--job", "J", "--as", judge2, "not_met", "--reason", "still no",
                              "--next", "fix the drill")
        self.assertEqual(rc, 0, err)
        self.finish("w2", "judge-2")
        ctx = self.context(self.main("turn"))
        self.assertIn(f"judge {judge2} ruled not met: still no.", ctx)

    def test_reminded_after_a_while_if_the_job_still_sits_idle(self):
        self.not_met()
        self.finish("w1", "judge-1")
        self.assertIsNotNone(self.main("turn"))
        self.assertIsNone(self.main("turn"))
        self.cfg["supervise"] = {"orphan_minutes": 1}
        with mock.patch.object(respawn.time, "time", return_value=respawn.time.time() + 61):
            self.assertIsNotNone(self.main("turn"))

    def test_checks_are_throttled(self):
        self.not_met()
        self.finish("w1", "judge-1")
        with mock.patch.object(respawn, "CHECK_SECONDS", 3600.0):
            first = self.main("turn")
            self.assertIsNotNone(first)
            self.finish()        # nothing
            self.assertIsNone(self.main("turn"))
        # the throttled look is skipped, but Stop always looks
        with mock.patch.object(respawn, "CHECK_SECONDS", 3600.0):
            self.assertIsNone(self.main("session-stop"))

    def test_a_job_without_a_goal_never_opens_the_board(self):
        self.activate("K", session="sess-2")
        self.h.set_available(False)
        self.assertIsNone(self.hook("turn", agent_id=None, session="sess-2", tool_name="Bash"))
        self.assertIsNone(self.hook("session-stop", agent_id=None, session="sess-2"))
        self.assertFalse(self.error_log.exists())

    def test_another_session_gets_nothing(self):
        self.not_met()
        self.finish("w1", "judge-1")
        self.assertIsNone(self.hook("turn", agent_id=None, session="sess-9", tool_name="Bash"))

    def test_board_trouble_is_logged_not_raised(self):
        self.not_met()
        self.finish("w1", "judge-1")
        self.h.set_available(False)
        self.assertIsNone(self.main("turn"))


class StopTests(RespawnEnv):
    def setUp(self):
        super().setUp()
        self.not_met()
        self.finish("w1", "judge-1")

    def test_stop_blocks_with_the_brief_as_the_reason(self):
        out = self.main("session-stop")
        self.assertEqual(out["decision"], "block")
        self.assertTrue(out["reason"].startswith(self.expected()), out["reason"])

    def test_stop_does_not_repeat_the_tool_hooks_reminder(self):
        self.assertIsNotNone(self.main("turn"))
        self.assertIsNone(self.main("session-stop"))

    def test_repeated_stops_say_it_once_per_verdict(self):
        self.assertIsNotNone(self.main("session-stop"))
        self.assertIsNone(self.main("session-stop"))

    def test_worker_post_after_verdict_suppresses_reminder(self):
        with self.board() as b:
            b.post("J", self.agent("w1").name, "fix round started")
        self.assertIsNone(self.main("session-stop"))

    def worker_contact_after_verdict(self, name):
        with self.board() as b:
            # A fix worker can finish before the orchestrator's next Stop.
            b.allocate_name("fix", "J")
            b.tool_started("fix", "Bash")
            b.agent_stopped("fix")
        contact = self.job().verdict_at + dt.timedelta(seconds=1)
        self.h.update_agent("fix", name=name, joined_at=contact, last_seen=contact,
                            left_at=contact)
        return contact.timestamp()

    def test_worker_contact_after_verdict_suppresses_reminder(self):
        now = self.worker_contact_after_verdict("Fix Worker")
        with mock.patch.object(respawn.time, "time", return_value=now):
            self.assertIsNone(self.main("session-stop"))

    def test_worker_reusing_judge_name_suppresses_reminder(self):
        now = self.worker_contact_after_verdict(self.judge)
        with mock.patch.object(respawn.time, "time", return_value=now):
            self.assertIsNone(self.main("session-stop"))

    def test_idle_window_restarts_after_worker_activity(self):
        self.cfg["supervise"] = {"orphan_minutes": 10}
        self.assertIsNotNone(self.main("session-stop"))
        with mock.patch.object(respawn.time, "time", return_value=respawn.time.time() + 601):
            with self.board() as b:
                post = b.post("J", self.agent("w1").name, "fix round started")
            self.h.backdate_message(post.id, -601)
            self.assertIsNone(self.main("session-stop"))
            with mock.patch.object(respawn.time, "time", return_value=respawn.time.time() + 601):
                self.assertIsNotNone(self.main("session-stop"))
                self.assertIsNone(self.main("session-stop"))

    def test_judge_activity_after_verdict_does_not_suppress_reminder(self):
        with self.board() as b:
            b.post("J", self.judge, "verdict details")
        self.assertIsNotNone(self.main("session-stop"))

    def test_repeated_stop_waits_for_configured_orphan_window(self):
        self.cfg["supervise"] = {"orphan_minutes": 10}
        self.assertIsNotNone(self.main("session-stop"))
        now = respawn.time.time()
        with mock.patch.object(respawn.time, "time", return_value=now + 301):
            self.assertIsNone(self.main("session-stop"))
        with mock.patch.object(respawn.time, "time", return_value=now + 601):
            self.assertIsNotNone(self.main("session-stop"))
            self.assertIsNone(self.main("session-stop"))

    def test_new_session_can_receive_same_verdict(self):
        self.assertIsNotNone(self.main("session-stop"))
        marker = self.markers / "J.json"
        with self.board() as b:
            text = respawn.check(marker, "J", force=True,
                                 host=hosts.get("claude"),
                                 open_board=self.board, cfg=self.cfg, session_id="new-session")
        self.assertIsNotNone(text)

    def test_live_started_running_and_idle_workers_suppress_reminder(self):
        self.spawn_worker("w2")
        self.assertIsNone(self.main("session-stop"))
        with self.board() as b:
            b.tool_started("w2", "Bash")
        self.assertIsNone(self.main("session-stop"))
        with self.board() as b:
            b.tool_finished("w2")
        self.h.backdate_agent("w2", last_seen=6 * 60)
        self.assertEqual(self.agent("w2").status, "idle")
        self.assertIsNone(self.main("session-stop"))

    def test_a_continued_stop_is_let_through_so_the_user_can_be_told(self):
        self.assertIsNone(self.main("session-stop", stop_hook_active=True))

    def test_a_subagents_stop_is_not_the_orchestrators(self):
        self.assertIsNone(self.hook("session-stop", agent_id="w1", session="sess-1"))

    def test_stop_says_nothing_once_agents_are_at_work(self):
        self.spawn_worker("w2")
        self.assertIsNone(self.main("session-stop"))


class CodexTests(RespawnEnv):
    def test_the_same_brief_without_claude_tag_lines(self):
        self.not_met()
        self.finish("w1", "judge-1")
        ctx = self.context(self.main("turn", host="codex"))
        self.assertTrue(ctx.startswith(self.expected()), ctx)
        self.assertNotIn("[swarm job: J]", ctx)
        self.assertIn("task name `judge__<task>`", ctx)
        self.assertIn("`judge__round2`", ctx)


class TextTests(RespawnEnv):
    def test_long_fields_are_clipped_and_controls_shown_safely(self):
        rc, _, err = self.cli("verdict", "--job", "J", "--as", self.judge, "not_met", "--reason", "r",
                              "--next", "x" * 5000)
        self.assertEqual(rc, 0, err)
        self.finish("w1", "judge-1")
        ctx = self.context(self.main("turn"))
        self.assertIn("x" * 100 + "…", ctx)
        self.assertNotIn("x" * 3001, ctx)
        self.assertEqual(respawn._clip("a\x1b[31mb"), "a\\x1b[31mb")

    def test_state_file_goes_with_the_marker(self):
        self.not_met()
        self.finish("w1", "judge-1")
        self.main("turn")
        self.assertTrue((self.markers / "J.respawn").exists())
        rc, _, err = self.cli("deactivate", "--job", "J", "--force")
        self.assertEqual(rc, 0, err)
        self.assertFalse((self.markers / "J.respawn").exists())
