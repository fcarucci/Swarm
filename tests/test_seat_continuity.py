"""Seat continuity: a reopened job takes its orchestrator back under its name and role, posts to a
seat nobody holds are queued and delivered to its next holder, and a taken judge seat is refused
with the holder's name and age."""
from __future__ import annotations

from test_hooks_cli import Env  # noqa: E402  (sets sys.path)
from test_spawn import SpawnEnv  # noqa: E402


class SeatContinuityTests(Env):
    def activate(self, *extra):
        rc, out, err = self.cli("activate", "--job", "J", "--session", "sess-pm", "--no-drive", "--reason", "test", *extra)
        self.assertEqual(rc, 0, err)
        return out

    def messages(self):
        with self.board() as b:
            return b.messages_after(0, "J")

    def test_reopen_rejoins_the_session_in_its_previous_role_and_name(self):
        self.activate()
        rc, name, err = self.cli("join", "--job", "J", "--key", "sess-pm", "--role", "project_manager")
        self.assertEqual(rc, 0, err)
        name = name.strip()
        with self.board() as b:
            b.close_job("J", "completed", "done")
            self.assertEqual(b.job_status("J").status, "completed")
        out = self.activate()
        self.assertIn(f"rejoined J as {name}", out)
        a = self.agent("sess-pm")
        self.assertEqual((a.name, a.role, a.ended_at), (name, "project_manager", None))
        rc, _, err = self.cli("post", "--job", "J", "--as", self.peer(), "--to", "@PM", "land it")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.messages()[-1].to_agent, name)

    def test_reopen_rejoins_the_orchestrator_that_joined_with_the_documented_key(self):
        self.activate()
        rc, name, err = self.cli("join", "--job", "J", "--key", "orchestrator", "--role", "project_manager")
        self.assertEqual(rc, 0, err)
        name = name.strip()
        with self.board() as b:
            b.close_job("J", "completed", "done")
        out = self.activate()
        self.assertIn(f"rejoined J as {name}", out)
        a = self.agent("orchestrator")
        self.assertEqual((a.name, a.role, a.ended_at), (name, "project_manager", None))
        rc, _, err = self.cli("post", "--job", "J", "--as", self.peer(), "--to", "@PM", "land it")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.messages()[-1].to_agent, name)

    def test_reopen_by_another_session_rejoins_nobody(self):
        self.activate()
        self.cli("join", "--job", "J", "--key", "sess-pm", "--role", "project_manager")
        with self.board() as b:
            b.close_job("J", "completed", "done")
        rc, _, err = self.cli("activate", "--job", "J", "--session", "sess-other", "--no-drive", "--reason", "test")
        self.assertEqual(rc, 0, err)
        self.assertIsNone(self.agent("sess-other"))

    def test_role_post_with_no_holder_is_queued_then_delivered_on_join(self):
        self.activate()
        worker = self.peer()
        rc, out, err = self.cli("post", "--job", "J", "--as", worker, "--to", "@PM", "BRANCH READY b@abc")
        self.assertEqual(rc, 0, err)
        self.assertIn("queued", out)
        rc, name, err = self.cli("join", "--job", "J", "--key", "pm-1", "--role", "project_manager")
        self.assertEqual(rc, 0, err)
        mine = [m for m in self.messages() if m.to_agent == name.strip()]
        self.assertEqual(len(mine), 1)
        self.assertIn("BRANCH READY b@abc", mine[0].message)
        self.assertIn(worker, mine[0].message)
        # a second holder (or a rejoin) does not get it again
        self.cli("join", "--job", "J", "--key", "pm-1", "--role", "project_manager")
        self.assertEqual(len([m for m in self.messages() if m.to_agent == name.strip()]), 1)

    def test_queued_post_for_an_alias_reaches_the_role_it_names(self):
        self.activate()
        worker = self.peer()
        self.cli("post", "--job", "J", "--as", worker, "--to", "@EL", "need a ruling")
        rc, name, _ = self.cli("join", "--job", "J", "--key", "el-1", "--role", "engineering_lead")
        self.assertTrue([m for m in self.messages() if m.to_agent == name.strip()])

    def test_join_judge_refused_names_holder_and_age(self):
        self.assertEqual(self.cli("activate", "--job", "J", "--no-drive", "--reason", "legacy goal test",
                                  "--goal", "ship it")[0], 0)
        rc, first, err = self.cli("join", "--job", "J", "--key", "judge-1", "--judge")
        self.assertEqual(rc, 0, err)
        self.h.backdate_agent("judge-1", joined_at=125)
        rc, _, err = self.cli("join", "--job", "J", "--key", "judge-2", "--judge")
        self.assertEqual(rc, 1)
        self.assertIn(f"judge seat held by {first.strip()} (joined 2m ago)", err)

    def test_judge_refusal_is_none_for_the_holder_itself_and_for_a_free_seat(self):
        from swarm import seatcont
        self.assertEqual(self.cli("activate", "--job", "J", "--no-drive", "--reason", "legacy goal test",
                                  "--goal", "ship it")[0], 0)
        with self.board() as b:
            self.assertIsNone(seatcont.judge_refusal(b, "J"))
            self.cli("join", "--job", "J", "--key", "judge-1", "--judge")
            self.assertIsNone(seatcont.judge_refusal(b, "J", "judge-1"))
            self.assertIn("judge seat held by", seatcont.judge_refusal(b, "J", "other"))

    def test_a_finished_judge_does_not_hold_the_seat(self):
        from swarm import seatcont
        self.assertEqual(self.cli("activate", "--job", "J", "--no-drive", "--reason", "legacy goal test",
                                  "--goal", "ship it")[0], 0)
        self.cli("join", "--job", "J", "--key", "judge-1", "--judge")
        self.h.backdate_agent("judge-1", joined_at=3 * 3600, last_seen=2 * 3600)     # dead
        with self.board() as b:
            self.assertIsNone(seatcont.judge_refusal(b, "J", "other"))
        self.h.backdate_agent("judge-1", last_seen=10 * 60)                          # idle: still live
        with self.board() as b:
            self.assertIn("judge seat held by", seatcont.judge_refusal(b, "J", "other"))
        self.h.update_agent("judge-1", state="completed")
        with self.board() as b:
            self.assertIsNone(seatcont.judge_refusal(b, "J", "other"))

    def test_age_text(self):
        from swarm import seatcont
        self.assertEqual([seatcont.age_text(s) for s in (5, 125, 3700, 7200, 90000)],
                         ["5s", "2m", "1h 1m", "2h", "1d 1h"])


class SpawnJudgeRefusalTests(SpawnEnv):
    def test_spawning_a_judge_is_refused_while_the_seat_is_held(self):
        self.activate_goal()
        self.worker()
        self.spawn_judge("judge-1")
        holder = self.member("judge-1").name
        decision, reason = self.decision(self.agent_call("w1", self.child() + "\n[swarm role: judge]"))
        self.assertEqual(decision, "deny")
        self.assertIn(f"judge seat held by {holder} (joined", reason)


class RespawnBriefJudgeTests(Env):
    def test_brief_names_a_judge_still_holding_the_seat(self):
        from types import SimpleNamespace
        from swarm import respawn
        js = SimpleNamespace(verdict_by="Judge A", verdict_reason="tests missing", verdict_next="add them",
                             verdict_at=None, verdict_details=None)
        free = respawn.brief(js, "J")
        held = respawn.brief(js, "J", held="judge seat held by Bart (joined 2m ago)")
        self.assertNotIn("refused until", free)
        self.assertIn("judge seat held by Bart (joined 2m ago), so a new judge is refused until that seat is free.", held)
