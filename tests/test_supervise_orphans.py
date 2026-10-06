"""Orphan job eligibility, bounded retries, and coordinator restart context."""
import datetime as dt
import unittest
from types import SimpleNamespace as NS
from unittest import mock
from support import MemoryHarness  # sets import path
from swarm.supervisor import orphans, settings

NOW = dt.datetime(2026, 10, 5, 12, tzinfo=dt.timezone.utc)


class FakeBoard:
    def __init__(self):
        self.js = NS(job="J", status="active", supervise=True, waiting_on=None,
                     last_activity_at=NOW-dt.timedelta(hours=2), activated_at=NOW-dt.timedelta(hours=3),
                     created_at=NOW-dt.timedelta(hours=3), started=0, running=0, idle=0,
                     verdict=None, task="Build parser; preserve branch", description="parser", goal="tests pass",
                     verdict_next="fix parser test")
        self.rows = []
        self.posts = []
    def now(self): return NOW
    def jobs(self, all): return [self.js]
    def job_status(self, job): return self.js
    def agents(self, job): return self.rows
    def recent_messages(self, n, job=None):
        return [NS(id=42, agent_name="Homer", message="Already pushed parser branch")][-n:]
    def post(self, job, author, message): self.posts.append((author, message))


class OrphansTest(unittest.TestCase):
    def setUp(self):
        self.b = FakeBoard()
        self.cfg = {"board": {"dead_minutes": 30}, "hook": {"marker_dir": "/nonexistent"}}
        self.sup = settings.settings({})
        self.rec = NS(cwd="/work", harness="codex", session_id="sid")
        self.state = {}
        self.out = []
        for target, result in (("swarm.cli.OrchestratorWatch.active", False),
                               ("swarm.supervisor.orphans.local_record", self.rec),
                               ("swarm.supervisor.command.workdir_for", "/work"),
                               ("swarm.supervisor.command.workdir_problem", None),
                               ("swarm.supervisor.command._workdir_hold", None),
                               ("swarm.supervisor.command._switched_off_now", None),
                               ("swarm.supervisor.settings.save_state", None)):
            patch = mock.patch(target, return_value=result)
            patch.start(); self.addCleanup(patch.stop)
        patch = mock.patch("swarm.pause.resume")
        self.resume = patch.start(); self.addCleanup(patch.stop)
    def run_pass(self, **kw):
        orphans.run(self.b, self.cfg, self.sup, self.state, now=NOW,
                    which=lambda binary: binary, say=self.out.append, **kw)
    def test_active_restarts_with_last_host_and_context(self):
        self.run_pass()
        self.assertEqual(self.resume.call_count, 1)
        args = self.resume.call_args.kwargs
        self.assertIs(args["orphan"], self.rec)
        for text in ("Build parser", "Already pushed", "#42", "no live agents", "do not redo", "fix parser test"):
            self.assertIn(text, args["restart_note"])
        self.assertEqual(len(self.state["orphan_restarts"]["J"]), 1)
    def test_paused_closed_human_wait_and_live_agents_excluded(self):
        for attr, value in (("status", "paused"), ("status", "completed"),
                            ("waiting_on", "Francesco approval / open question"),
                            ("started", 1), ("running", 1), ("idle", 1)):
            with self.subTest(attr=attr,value=value):
                old = getattr(self.b.js, attr); setattr(self.b.js, attr, value)
                self.run_pass(); setattr(self.b.js, attr, old)
        self.resume.assert_not_called()
    def test_unanswered_owner_question_is_external_wait(self):
        question = NS(agent_name="Homer", to_agent="Francesco", message="Approve this deployment?", id=50)
        with mock.patch.object(self.b,"recent_messages",return_value=[question]):
            self.run_pass(); self.resume.assert_not_called()
        answer = NS(agent_name="Francesco", to_agent="Homer", message="Yes, continue", id=51)
        with mock.patch.object(self.b,"recent_messages",return_value=[question,answer]):
            self.run_pass(); self.resume.assert_called_once()

    def test_supervisor_wait_is_recoverable(self):
        from swarm.supervisor.stuck import WAITING_PREFIX
        self.b.js.waiting_on = WAITING_PREFIX + "Homer"
        self.run_pass(); self.resume.assert_called_once()
    def test_unmet_verdict_can_restart(self):
        self.b.js.verdict = "not_met"
        self.run_pass(); self.resume.assert_called_once()
    def test_met_reminder_once_without_restart(self):
        self.b.js.verdict = "met"
        self.run_pass(); self.run_pass()
        self.resume.assert_not_called()
        self.assertEqual(len(self.b.posts), 1)
        self.assertIn("Goal is met", self.b.posts[0][1])
    def test_foreign_owner_excluded(self):
        with mock.patch.object(orphans, "local_record", return_value=None): self.run_pass()
        self.resume.assert_not_called()
    def test_recent_activity_or_coordinator_excluded(self):
        self.b.js.last_activity_at = NOW-dt.timedelta(minutes=10)
        self.run_pass()
        self.b.js.last_activity_at = NOW-dt.timedelta(hours=2)
        with mock.patch("swarm.cli.OrchestratorWatch.active", return_value=True): self.run_pass()
        self.resume.assert_not_called()
    def test_dead_transition_requires_orphan_window(self):
        self.b.rows = [NS(ended_at=None,last_contact_at=NOW-dt.timedelta(minutes=35))]
        self.run_pass(); self.resume.assert_not_called()
        self.b.rows[0].last_contact_at = NOW-dt.timedelta(minutes=46)
        self.run_pass(); self.resume.assert_called_once()
    def test_backoff_increases_and_rolling_daily_limit(self):
        for count, minutes in ((1,14),(2,29)):
            self.state = {"orphan_restarts":{"J":[NOW.timestamp()-minutes*60]*count}}
            self.run_pass(); self.resume.assert_not_called()
        self.state = {"orphan_restarts":{"J":[NOW.timestamp()-7200]*3}}
        self.run_pass(); self.run_pass()
        self.resume.assert_not_called(); self.assertEqual(len(self.b.posts),1)
        self.assertIn("GAVE UP",self.b.posts[0][1])
        self.state = {"orphan_restarts":{"J":[NOW.timestamp()-90000]*3}}
        self.run_pass(); self.resume.assert_called_once()
    def test_dry_run_changes_nothing(self):
        self.run_pass(dry_run=True)
        self.resume.assert_not_called(); self.assertEqual(self.state,{})
        self.assertEqual(self.b.posts,[])
        self.assertIn("would restart orphan J",self.out[0])
    def test_orphan_resume_uses_existing_runner_and_resume_marker(self):
        from swarm import pause
        from swarm.hosts.resume import Restored
        h = MemoryHarness("orphan-resume-test"); h.reset()
        b = h.board(); self.addCleanup(b.close)
        b.open_job("J", "desc", "build parser", None, "tester", goal="done")
        restored = Restored("codex", "briefing", ("codex", "exec", "-"), "/work", "continue", None, None)
        started = []
        with mock.patch("swarm.hosts.resume.restore",return_value=restored), \
             mock.patch("swarm.supervisor.markers.write_resume_marker",return_value="/marker"), \
             mock.patch("swarm.board.autoinit.store_key",return_value="board"):
            run = pause._resume_orphan(b,self.cfg,"J",self.rec,"context",
                               start_runner=lambda cfg,run: started.append(run))
        self.assertEqual(started,[run])
        self.assertEqual(run["argv"],["codex","exec","-"])
        self.assertTrue(b.restarts(job="J")[0].reason.startswith("orphan-coordinator:"))
        self.assertEqual(b.agents("J")[0].role,"coordinator")
    def test_owner_and_coordinator_both_required(self):
        with mock.patch("swarm.enrolment.find_job_owner",return_value=None), \
             mock.patch("swarm.enrolment.find_job",return_value=self.rec):
            # bypass setUp's local_record mock to exercise implementation
            from importlib import reload
            real = reload(orphans).local_record
            with mock.patch("swarm.board.autoinit.store_key",return_value="board"):
                self.assertIsNone(real(self.cfg,self.b.js))

if __name__ == "__main__": unittest.main()
