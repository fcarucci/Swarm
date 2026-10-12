"""A job waiting with no live agents and no activity must not wait silently: one board notice at
[job] stale_waiting_minutes, then an auto-close (Board.sweep_expiry, closed_by 'auto') at
[job] stale_waiting_close_minutes: failed without a met verdict, completed with one. A live agent
keeps the job untouched."""
from __future__ import annotations

import datetime as dt

from test_hooks_cli import Env  # noqa: E402  (sets sys.path)
from swarm.board.base import STALE_NOTICE  # noqa: E402


MIN = 60
NOTICE_AT = 121 * MIN     # past stale_waiting_minutes (120), before the close (240)
CLOSE_AT = 241 * MIN      # past stale_waiting_close_minutes (240)
OUTCOME = "auto-closed: waiting with no live agents and no verdict"


class StaleWaitingTests(Env):
    def activate(self, job="J", *extra):
        if "--goal" in extra:      # a goal job nothing drives (activation would otherwise need a supervisor pass)
            extra = (*extra, "--no-drive", "--reason", "stale-waiting test")
        rc, _, err = self.cli("activate", "--job", job, *extra)
        self.assertEqual(rc, 0, err)

    def job(self, job="J"):
        with self.board() as b:
            return b.job_status(job)

    def notices(self, job="J"):
        with self.board() as b:
            return [m for m in b.messages_after(0, job)
                    if m.agent_name == "swarm" and "stale" in m.message.lower() and "waiting" in m.message.lower()]

    def quiet(self, seconds, job="J", agents=("agent-1",), live=()):
        """Everything the job did is `seconds` old: its agents `agents` left, `live` still running."""
        with self.board() as b:
            ids = [m.id for m in b.messages_after(0, job)]
        self.h.backdate_job(job, created_at=seconds + 120, activated_at=seconds + 120)
        for a in agents:
            self.h.backdate_agent(a, joined_at=seconds + 60, last_seen=seconds, left_at=seconds)
        for a in live:
            self.h.backdate_agent(a, joined_at=seconds + 60, last_seen=seconds)
        for i in ids:
            self.h.backdate_message(i, seconds)

    def finished_agent(self, job="J", key="agent-1"):
        with self.board() as b:
            b.allocate_name(key, job, "worker")
            b.agent_stopped(key)

    def waiting_job(self, seconds, job="J"):
        """A goal-less job waiting on a wait, its only agent gone: the stale-waiting rule may close it."""
        self.activate(job)
        self.finished_agent(job)
        rc, _, err = self.cli("wait", "--job", job, "--on", "the human")
        self.assertEqual(rc, 0, err)
        self.quiet(seconds, job)

    def waiting_goal_job(self, seconds, job="J"):
        """A goal job with no verdict whose only agent has left: `waiting (goal not met)`."""
        self.activate(job, "--no-drive", "--reason", "legacy goal test", "--goal", "ship it")
        self.finished_agent(job)
        self.quiet(seconds, job)

    def sweep(self):
        return self.cli("purge")

    # ---- a reopened job's quiet time starts at the reopening, not at the old silence
    def test_a_reactivated_job_is_not_closed_by_its_old_silence(self):
        self.waiting_job(10 * 3600)            # idle for hours, past the close threshold
        self.activate("J")     # reopened now
        self.sweep()
        self.assertEqual(self.job().status, "active")
        self.assertEqual(self.notices(), [])
        self.h.backdate_job("J", created_at=11 * 3600, activated_at=60)
        self.sweep()                                # swept shortly after the reopening
        self.assertEqual(self.job().status, "active")
        self.assertEqual(self.notices(), [])

    def test_a_reactivated_job_still_closes_once_quiet_again(self):
        self.waiting_job(10 * 3600)
        self.activate("J")
        self.h.backdate_job("J", activated_at=CLOSE_AT)
        self.sweep()
        self.assertEqual(self.job().status, "failed")

    def test_the_notice_carries_the_reopening_as_quiet_start(self):
        self.waiting_goal_job(10 * 3600)
        self.activate("J", "--goal", "ship it")
        self.h.backdate_job("J", activated_at=NOTICE_AT)
        self.sweep()
        self.assertEqual(self.job().status, "active")
        self.assertEqual(len(self.notices()), 1)

    def test_the_notices_quiet_start_decides_the_close(self):
        self.waiting_job(250 * MIN)
        since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=250)).isoformat(timespec="seconds")
        with self.board() as b:
            b.post("J", "agent-1", "still thinking")
            last = b.messages_after(0, "J")[-1].id
            b.post("J", "swarm", f"{STALE_NOTICE}{since}; auto-closes in 5 min")
            notice = b.messages_after(0, "J")[-1].id
        self.h.backdate_message(last, 100 * MIN)       # the newest activity: 100 min old
        self.h.backdate_message(notice, 90 * MIN)      # the notice after it, carrying a quiet start 250 min ago
        self.sweep()
        self.assertEqual(self.job().status, "failed")  # the notice's quiet start decides, not the 100 min

    def test_a_notice_from_before_the_reopening_does_not_count_the_old_quiet(self):
        self.waiting_job(300 * MIN)
        since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=300)).isoformat(timespec="seconds")
        with self.board() as b:
            b.post("J", "swarm", f"{STALE_NOTICE}{since}; auto-closes in 5 min")
            notice = b.messages_after(0, "J")[-1].id
        self.h.backdate_message(notice, 200 * MIN)
        self.h.backdate_job("J", activated_at=150 * MIN)      # reopened 150 min ago: quiet for 150, not 300
        self.sweep()
        self.assertEqual(self.job().status, "active")

    # ---- the notice
    def test_one_notice_when_quiet_past_stale_waiting_minutes(self):
        self.waiting_goal_job(NOTICE_AT)
        self.sweep()
        self.assertEqual(self.job().status, "active")
        self.assertEqual(len(self.notices()), 1, self.notices())
        for _ in range(3):
            self.sweep()
        self.assertEqual(len(self.notices()), 1)      # exactly one, however often it is swept
        self.assertEqual(self.job().status, "active")

    def test_no_notice_before_the_threshold(self):
        self.waiting_goal_job(100 * MIN)
        self.sweep()
        self.assertEqual(self.notices(), [])
        self.assertEqual(self.job().status, "active")

    def test_zero_turns_it_off(self):
        self.config.write_text(self.config.read_text() + "[job]\nstale_waiting_minutes = 0\n")
        self.waiting_goal_job(CLOSE_AT)
        self.sweep()
        self.assertEqual(self.notices(), [])
        self.assertEqual(self.job().status, "active")

    # ---- the close
    def test_auto_closes_failed_without_a_verdict(self):
        self.waiting_job(CLOSE_AT)
        self.sweep()
        s = self.job()
        self.assertEqual((s.status, s.closed_by), ("failed", "auto"))
        self.assertEqual(s.outcome, OUTCOME)
        self.assertIsNotNone(s.finished_at)
        self.assertLessEqual(len(self.notices()), 1)

    def test_the_orphan_rule_leaves_a_job_waiting_on_a_person_alone(self):
        for ask, expect in ((False, "cancelled"), (True, "active")):
            with self.subTest(question=ask):
                self.setUp()
                self.activate("J")
                with self.board() as b:
                    b.allocate_name("agent-1", "J", "worker")      # a dead agent: no heartbeat, never left
                    if ask:
                        b.post("J", "agent-1", "Francesco, which one?", to="Francesco")
                    ids = [m.id for m in b.messages_after(0, "J")]
                self.h.backdate_job("J", created_at=3 * 3600, activated_at=3 * 3600)
                self.h.backdate_agent("agent-1", joined_at=2 * 3600 + 60, last_seen=2 * 3600)
                for i in ids:
                    self.h.backdate_message(i, 2 * 3600)
                self.sweep()
                self.assertEqual(self.job().status, expect)

    def test_the_stall_rule_leaves_a_job_waiting_on_a_person_alone_with_the_default_limit(self):
        for ask, expect in ((False, "failed"), (True, "active")):
            with self.subTest(question=ask):
                self.setUp()
                self.waiting_job(CLOSE_AT + 3600)
                if ask:
                    with self.board() as b:
                        b.post("J", "agent-1", "Francesco, shall I merge it?", to="Francesco")
                        mid = b.messages_after(0, "J")[-1].id
                    self.h.backdate_message(mid, CLOSE_AT + 3000)
                self.sweep()
                self.assertEqual(self.job().status, expect)

    def test_an_answered_question_no_longer_protects(self):
        self.waiting_job(CLOSE_AT + 3600)
        with self.board() as b:
            b.post("J", "agent-1", "Francesco, shall I merge it?", to="Francesco")
            q = b.messages_after(0, "J")[-1].id
            b.post("J", "Francesco", "yes")
            a = b.messages_after(0, "J")[-1].id
        self.h.backdate_message(q, CLOSE_AT + 3200)
        self.h.backdate_message(a, CLOSE_AT + 3100)
        self.sweep()
        self.assertEqual(self.job().status, "failed")

    def test_a_goal_job_without_a_met_verdict_is_never_closed_but_gets_its_notice(self):
        self.waiting_goal_job(CLOSE_AT + 10 * 3600)
        for _ in range(3):
            self.sweep()
        self.assertEqual(self.job().status, "active")
        self.assertEqual(len(self.notices()), 1)
        self.assertNotIn("auto-closes in", self.notices()[0].message)

    def test_a_reactivated_long_idle_goal_job_with_stall_hours_zero_stays_open(self):
        self.waiting_goal_job(10 * 3600)
        self.activate("J", "--goal", "ship it", "--stall-hours", "0")
        self.h.backdate_job("J", activated_at=CLOSE_AT + 3600)
        self.finished_agent("J", "agent-2")
        for _ in range(3):
            self.sweep()
        self.assertEqual(self.job().status, "active")

    def test_a_job_with_its_own_limit_of_zero_is_never_closed_by_quiet_time(self):
        self.activate("J", "--stall-hours", "0")
        self.finished_agent()
        rc, _, err = self.cli("wait", "--job", "J", "--on", "the human")
        self.assertEqual(rc, 0, err)
        self.quiet(CLOSE_AT + 3600)
        self.sweep()
        self.assertEqual(self.job().status, "active")

    def test_a_job_with_a_question_for_a_person_is_never_closed(self):
        self.activate("J")
        self.finished_agent()
        rc, _, err = self.cli("wait", "--job", "J", "--on", "the human")
        self.assertEqual(rc, 0, err)
        with self.board() as b:
            b.post("J", "agent-1", "Francesco, shall I merge it?", to="Francesco")
        self.quiet(CLOSE_AT + 3600)
        self.sweep()
        self.assertEqual(self.job().status, "active")

    def test_a_wait_job_without_a_goal_also_closes(self):
        self.activate("J")
        self.finished_agent()
        rc, _, err = self.cli("wait", "--job", "J", "--on", "the human")
        self.assertEqual(rc, 0, err)
        self.quiet(CLOSE_AT)
        self.sweep()
        s = self.job()
        self.assertEqual((s.status, s.closed_by), ("failed", "auto"))

    def test_auto_closes_completed_with_a_met_verdict(self):
        """Goal met, but the orchestrator is parked in a wait: completed, not failed."""
        self.activate("J", "--no-drive", "--reason", "legacy goal test", "--goal", "ship it")
        with self.board() as b:
            judge = b.allocate_name("judge-1", "J", "judge")
            self.assertTrue(b.claim_judge("judge-1", "J"))
            self.assertTrue(b.record_verdict("J", judge, "met", "accepted"))
            b.close_agent("judge-1", "done")
        rc, _, err = self.cli("wait", "--job", "J", "--on", "the human")
        self.assertEqual(rc, 0, err)
        self.quiet(CLOSE_AT, agents=("judge-1",))
        self.h.backdate_job("J", verdict_at=CLOSE_AT)
        self.sweep()
        s = self.job()
        self.assertEqual((s.status, s.closed_by), ("completed", "auto"))

    def test_unreachable_verdict_is_named_in_the_outcome_not_no_verdict(self):
        """A stale job whose judge recorded `unreachable` is closed failed, and the outcome says so."""
        self.activate("J", "--goal", "ship it")
        with self.board() as b:
            judge = b.allocate_name("judge-1", "J", "judge")
            self.assertTrue(b.claim_judge("judge-1", "J"))
            self.assertTrue(b.record_verdict("J", judge, "unreachable", "cannot be built", details="tried x", scope="goal"))
            b.close_agent("judge-1", "done")
        rc, _, err = self.cli("wait", "--job", "J", "--on", "the human")
        self.assertEqual(rc, 0, err)
        self.quiet(CLOSE_AT, agents=("judge-1",))
        self.h.backdate_job("J", verdict_at=CLOSE_AT)
        self.sweep()
        s = self.job()
        self.assertEqual((s.status, s.closed_by), ("failed", "auto"))
        self.assertEqual(s.outcome, "auto-closed: waiting with no live agents; verdict unreachable")
        self.assertNotIn("no verdict", s.outcome)

    def test_met_verdict_with_accepted_work_awaiting_finalization_closes_failed(self):
        """Met verdict on a pipeline artifact nobody finalized (still waiting): failed, not completed."""
        self.activate("J", "--no-drive", "--reason", "legacy goal test", "--goal", "ship it")
        with self.board() as b:
            b.set_job_data("J", "pipeline.started_at", (b.now() - dt.timedelta(hours=9)).isoformat())
            worker = b.allocate_name("agent-1", "J", "worker")
            b.post("J", worker, "DONE ref-1")
            b.close_agent("agent-1", "done")
            judge = b.allocate_name("judge-1", "J", "judge")
            self.assertTrue(b.claim_judge("judge-1", "J"))
            self.assertTrue(b.record_verdict("J", judge, "met", "accepted", artifact="ref-1"))
            b.close_agent("judge-1", "done")
        self.quiet(CLOSE_AT, agents=("agent-1", "judge-1"))
        self.h.backdate_job("J", verdict_at=CLOSE_AT)
        self.sweep()
        s = self.job()
        self.assertEqual((s.status, s.closed_by), ("failed", "auto"))
        self.assertEqual(s.outcome, "auto-closed: goal met but accepted work never finalized")

    def test_a_bounded_wait_keeps_the_job_open(self):
        self.activate("J")
        self.finished_agent()
        rc, _, err = self.cli("wait", "--job", "J", "--for", "600m", "--on", "the build")
        self.assertEqual(rc, 0, err)
        self.quiet(CLOSE_AT)
        self.sweep()
        self.assertEqual(self.job().status, "active")
        self.assertEqual(self.notices(), [])

    def test_an_open_human_question_keeps_the_job_open(self):
        self.activate("J")
        self.finished_agent()
        with self.board() as b:
            b.open_blocker("J", "question", "human", "which colour?")
        self.quiet(CLOSE_AT)
        self.sweep()
        self.assertEqual(self.job().status, "active")
        self.assertEqual(self.notices(), [])

    # ---- never a job with a live agent
    def test_a_live_agent_keeps_the_job_untouched(self):
        for state in ("started", "running", "idle"):
            with self.subTest(state=state):
                job = f"J-{state}"
                self.activate(job, "--no-drive", "--reason", "legacy goal test", "--goal", "ship it")
                key = f"a-{state}"
                self.finished_agent(job, f"done-{state}")
                with self.board() as b:
                    b.allocate_name(key, job, "worker")
                    if state == "running":   # a long tool call: no contact for ages, still running
                        b.tool_started(key, "Bash")
                self.quiet(CLOSE_AT, job=job, agents=(f"done-{state}",))
                if state == "running":   # (no contact for 40 min, a tool call still open)
                    self.h.backdate_agent(key, joined_at=CLOSE_AT, last_seen=40 * MIN, tool_started_at=40 * MIN)
                else:
                    self.h.backdate_agent(key, joined_at=CLOSE_AT, last_seen=(10 if state == "idle" else 2) * MIN)
                with self.board() as b:
                    self.assertEqual(next(a.status for a in b.agents(job) if a.agent_key == key), state)
                self.sweep()
                self.assertEqual(self.job(job).status, "active", state)
                self.assertEqual(self.notices(job), [], state)

    def test_activity_resets_the_clock(self):
        self.waiting_goal_job(CLOSE_AT)
        with self.board() as b:
            b.post("J", b.allocate_name("agent-2", "J", "worker"), "still thinking")
            b.agent_stopped("agent-2")
        self.sweep()
        self.assertEqual(self.job().status, "active")
