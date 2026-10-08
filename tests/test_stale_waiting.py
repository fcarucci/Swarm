"""A job waiting with no live agents and no activity must not wait silently: one board notice at
[job] stale_waiting_minutes, then an auto-close (Board.sweep_expiry, closed_by 'auto') at
[job] stale_waiting_close_minutes: failed without a met verdict, completed with one. A live agent
keeps the job untouched."""
from __future__ import annotations

import datetime as dt

from test_hooks_cli import Env  # noqa: E402  (sets sys.path)


MIN = 60
NOTICE_AT = 121 * MIN     # past stale_waiting_minutes (120), before the close (240)
CLOSE_AT = 241 * MIN      # past stale_waiting_close_minutes (240)
OUTCOME = "auto-closed: waiting with no live agents and no verdict"


class StaleWaitingTests(Env):
    def activate(self, job="J", *extra):
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

    def waiting_goal_job(self, seconds, job="J"):
        """A goal job with no verdict whose only agent has left: `waiting (goal not met)`."""
        self.activate(job, "--goal", "ship it")
        self.finished_agent(job)
        self.quiet(seconds, job)

    def sweep(self):
        return self.cli("purge")

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
        self.waiting_goal_job(CLOSE_AT)
        _, out, _ = self.sweep()
        s = self.job()
        self.assertEqual((s.status, s.closed_by), ("failed", "auto"))
        self.assertEqual(s.outcome, OUTCOME)
        self.assertIsNotNone(s.finished_at)
        self.assertLessEqual(len(self.notices()), 1)

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
        self.activate("J", "--goal", "ship it")
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

    def test_met_verdict_with_accepted_work_awaiting_finalization_completes(self):
        """Met verdict on a pipeline artifact nobody finalized (still waiting): completed, not failed."""
        self.activate("J", "--goal", "ship it")
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
        self.assertEqual((s.status, s.closed_by), ("completed", "auto"))

    # ---- never a job with a live agent
    def test_a_live_agent_keeps_the_job_untouched(self):
        for state in ("started", "running", "idle"):
            with self.subTest(state=state):
                job = f"J-{state}"
                self.activate(job, "--goal", "ship it")
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
