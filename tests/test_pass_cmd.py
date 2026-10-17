"""`swarm pass`: the stateless sweep that keeps jobs from halting (lib/swarm/passcmd.py)."""
import datetime as _dt
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import support
from support import drive_backends
from swarm import passcmd


class PassCmd(unittest.TestCase):
    harness_factory = staticmethod(drive_backends()["memory"])

    def setUp(self):
        self.h = self.harness_factory(self.id())
        self.addCleanup(self.h.close)
        self.h.reset()
        self.b = self.h.board()
        self.addCleanup(self.b.close)
        self.cfg = self.h.cfg
        self.runs = []
        self.b.open_job("J", None, None, None, "me", goal="ship it")
        self.b.set_job_data("J", "workdir", "/work/shared")
        for patcher in (mock.patch("swarm.supervisor.command.workdir_problem", return_value=None),
                        mock.patch("swarm.supervisor.markers.write_resume_marker",
                                   return_value=Path("/nonexistent/marker"))):
            patcher.start()
            self.addCleanup(patcher.stop)

    def starter(self, cfg, run):
        self.runs.append(run)
        return 1

    def pass_(self, minutes=0):
        now = self.b.now() + _dt.timedelta(minutes=minutes)
        return passcmd.run_pass(self.b, self.cfg, now=now, starter=self.starter)

    def queue(self, name, role="lead"):
        return self.b.queue_launch("J", 1, name, role=role, harness="claude", model=None,
                                   workdir="/work/shared", brief="B", queued_by="me")

    def open_restarts(self):
        return [r for r in self.b.restarts("J") if r.ended_at is None]

    def hand_off(self, artifact="br@abc123"):
        self.b.post("J", "me", "DONE " + json.dumps(artifact) + " | did it")
        return artifact

    def verdict(self, verdict, reason, next_steps=None, artifact=None):
        """A judge agent records the verdict, then leaves (as the judge does after `swarm verdict`)."""
        judge = self.b.allocate_name("judge", "J", "judge")
        self.assertTrue(self.b.claim_judge("judge", "J"))
        self.assertTrue(self.b.record_verdict("J", judge, verdict, reason, next_steps, artifact=artifact))
        self.b.close_agent("judge", "done")
        for r in self.open_restarts():   # the judge's session ends with its verdict
            self.b.finish_restart(r.id, "completed")

    def job(self):
        return self.b.job_status("J")

    def test_queued_launch_starts_once_with_its_restart(self):
        self.queue("lead-1")
        self.pass_()
        self.assertEqual(self.b.get_launch("J", 1, "lead-1").state, "running")
        self.assertEqual(len(self.runs), 1)
        self.assertTrue(self.runs[0]["launch_id"].startswith("lead-1-"))
        self.assertEqual(self.runs[0]["name"], "lead-1")
        self.pass_()
        self.assertEqual(len(self.runs), 1)   # running and not re-started

    def test_concurrency_cap_holds_the_rest_queued(self):
        self.queue("lead-1")
        self.queue("lead-2")
        with mock.patch.object(passcmd, "MAX_JOB_RUNNING", 1):
            self.pass_()
        self.assertEqual(self.b.get_launch("J", 1, "lead-1").state, "running")
        self.assertEqual(self.b.get_launch("J", 1, "lead-2").state, "queued")

    def test_cost_cap_fails_the_job_with_a_notice(self):
        self.b.set_job_data("J", "max_cost_usd", "1")
        self.queue("lead-1")
        self.pass_()
        self.b.finish_restart(self.open_restarts()[0].id, "completed", cost_usd=1.0)
        self.queue("lead-2")
        self.pass_()
        self.assertEqual(self.job().status, "failed")
        self.assertIn("cost cap reached", self.job().outcome)
        self.assertTrue(any("NOTICE J failed" in m.message for m in self.b.recent_messages(5, "J")))

    def test_open_question_waits_and_nothing_else_starts(self):
        self.b.open_blocker("J", "question", "alice", "ship now?", until=self.b.now() + _dt.timedelta(hours=1),
                            created_by="me")
        self.pass_()
        self.assertEqual(self.job().status, "active")
        self.assertEqual(self.b.launches("J"), [])

    def test_expired_question_fails_the_job(self):
        self.b.open_blocker("J", "question", "alice", "ship now?", until=self.b.now() - _dt.timedelta(minutes=1),
                            created_by="me")
        self.pass_()
        self.assertEqual(self.job().status, "failed")
        self.assertIn("expired unanswered", self.job().outcome)

    def test_no_hand_off_relaunches_the_lead_then_fails_after_three(self):
        for _ in range(12):
            self.pass_()
            for r in self.open_restarts():
                self.b.finish_restart(r.id, "failed")
            if self.job().status != "active":
                break
        self.assertEqual(self.job().status, "failed")
        self.assertEqual(len(self.runs), passcmd.LEAD_ATTEMPTS)
        self.assertEqual(sorted(r.name for r in self.b.launches("J")), ["lead-1", "lead-2", "lead-3"])

    def test_a_hand_off_is_judged_once_and_met_completes_the_job(self):
        artifact = self.hand_off()
        self.pass_()
        judge = self.b.get_launch("J", 1, "judge-" + passcmd._key(artifact))
        self.assertEqual((judge.role, judge.state), ("judge", "queued"))
        self.pass_()
        self.assertEqual(len([r for r in self.b.launches("J") if r.role == "judge"]), 1)
        self.verdict("met", "all there", artifact=artifact)
        self.pass_()
        self.assertEqual(self.job().status, "completed")

    def test_not_met_requeues_the_lead_once_for_that_artifact(self):
        artifact = self.hand_off()
        self.pass_()
        self.pass_()   # the judge is started
        self.verdict("not_met", "tests missing", next_steps="add tests", artifact=artifact)
        self.pass_()
        self.pass_()
        relead = [r for r in self.b.launches("J") if r.name.startswith("relead-")]
        self.assertEqual([r.name for r in relead], ["relead-" + passcmd._key(artifact)])
        self.assertIn("add tests", relead[0].brief)
        self.assertEqual(self.job().status, "active")

    def test_failed_job_notice_repeats_until_acked(self):
        self.b.open_blocker("J", "question", "alice", "ship now?", until=self.b.now() - _dt.timedelta(minutes=1),
                            created_by="me")
        self.pass_()
        self.assertEqual(self.job().status, "failed")
        notices = lambda: sum("NOTICE J failed" in m.message for m in self.b.recent_messages(50, "J"))
        before = notices()
        self.pass_(minutes=5)
        self.assertEqual(notices(), before)   # not yet due
        self.pass_(minutes=passcmd.NOTICE_MINUTES + 1)
        self.assertEqual(notices(), before + 1)
        self.assertTrue(passcmd.ack(self.b, "J"))
        self.pass_(minutes=10 * passcmd.NOTICE_MINUTES)
        self.assertEqual(notices(), before + 1)

    def test_hook_pass_is_throttled_per_host(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(passcmd.paths, "state_dir", return_value=Path(tmp)):
            self.assertIsNotNone(passcmd.throttled(self.b, self.cfg))
            self.assertIsNone(passcmd.throttled(self.b, self.cfg))


if __name__ == "__main__":
    unittest.main()
