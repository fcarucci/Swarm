"""`swarm pass`: the stateless sweep that keeps jobs from halting (lib/swarm/passcmd.py)."""
import argparse
import contextlib
import datetime as _dt
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import support
from support import drive_backends, add_backend_variants
from swarm import cli, hooks, passcmd


def run(fn, *a):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = fn(*a)
    return code, out.getvalue(), err.getvalue()


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

    def queue(self, name, role="lead", queued_by="me"):
        return self.b.queue_launch("J", 1, name, role=role, harness="claude", model=None,
                                   workdir="/work/shared", brief="B", queued_by=queued_by)

    def test_native_explicit_codex_model_survives_enforced_worker_default(self):
        self.cfg["models"] = {"mode": "enforce", "codex": {"worker": "gpt-6.1-sol"}}
        self.b.queue_launch("J", 1, "explicit", role="engineer", harness="codex", model="gpt-6-astra",
                            workdir="/work/shared", brief="B", queued_by="me")
        with mock.patch("swarm.supervisor.launch.codex_command", return_value=["codex"]):
            self.pass_()
        self.assertEqual(len(self.runs), 1)
        argv = self.runs[0]["argv"]
        self.assertEqual(argv[argv.index("-m") + 1], "gpt-6-astra")

    def test_native_omitted_codex_model_uses_worker_default(self):
        self.cfg["models"] = {"mode": "enforce", "codex": {"worker": "gpt-6.1-sol"}}
        self.b.queue_launch("J", 1, "default", role="engineer", harness="codex", model=None,
                            workdir="/work/shared", brief="B", queued_by="me")
        with mock.patch("swarm.supervisor.launch.codex_command", return_value=["codex"]):
            self.pass_()
        self.assertEqual(len(self.runs), 1)
        argv = self.runs[0]["argv"]
        self.assertEqual(argv[argv.index("-m") + 1], "gpt-6.1-sol")

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
        self.assertEqual(self.runs[0]["launch"], "lead-1")
        self.assertTrue(self.runs[0]["name"] and self.runs[0]["name"] != "lead-1")   # the session's own agent name
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

    def test_a_plain_job_without_launches_or_work_dir_is_left_alone(self):
        self.b.open_job("P", None, None, None, "me", goal="plain")
        self.b.open_blocker("P", "question", "alice", "ship now?", until=self.b.now() - _dt.timedelta(minutes=1),
                            created_by="me")
        self.pass_(minutes=passcmd.NOTICE_MINUTES + 1)
        self.assertEqual(self.b.job_status("P").status, "active")
        self.assertEqual(self.b.launches("P"), [])

    def test_hook_pass_is_throttled_per_host(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(passcmd.paths, "state_dir", return_value=Path(tmp)):
            self.assertTrue(passcmd.due())
            self.assertIsNotNone(passcmd.throttled(self.b, self.cfg))
            self.assertFalse(passcmd.due())
            self.assertIsNone(passcmd.throttled(self.b, self.cfg))

    def test_cli_parses_pass_and_ack(self):
        parser = cli._parser()
        self.assertEqual(parser.parse_args(["pass"]).cmd, "pass")
        self.assertEqual(parser.parse_args(["ack", "--job", "J"]).job, "J")
        self.assertIs(cli.BOARD_COMMANDS["pass"], cli._board_pass)
        self.assertIs(cli.BOARD_COMMANDS["ack"], cli._board_ack)

    def test_cli_pass_starts_the_queued_launch_and_prints_it(self):
        self.queue("lead-1")
        with mock.patch("swarm.supervisor.runner.start", side_effect=self.starter):
            code, out, _ = run(cli._board_pass, self.b, self.cfg, argparse.Namespace())
        self.assertIsNone(code)
        self.assertIn("J: started lead-1 as restart", out)
        self.assertEqual(len(self.runs), 1)

    def test_cli_pass_says_nothing_to_do_for_an_unmanaged_job(self):
        self.b.set_job_data("J", "workdir", None)   # a plain job: no work dir, no launches
        code, out, _ = run(cli._board_pass, self.b, self.cfg, argparse.Namespace())
        self.assertEqual(out, "nothing to do\n")
        self.assertEqual(self.b.launches("J"), [])

    def test_cli_ack_stops_the_notices_of_a_failed_job(self):
        self.b.open_blocker("J", "question", "alice", "ship now?", until=self.b.now() - _dt.timedelta(minutes=1),
                            created_by="me")
        self.pass_()
        self.assertEqual(self.job().status, "failed")
        code, out, _ = run(cli._board_ack, self.b, self.cfg, argparse.Namespace(job="J"))
        self.assertEqual((code, out), (None, "J: acked\n"))
        notices = lambda: sum("NOTICE J failed" in m.message for m in self.b.recent_messages(50, "J"))
        before = notices()
        self.pass_(minutes=10 * passcmd.NOTICE_MINUTES)
        self.assertEqual(notices(), before)

    def test_cli_ack_refuses_an_unknown_job(self):
        code, out, err = run(cli._board_ack, self.b, self.cfg, argparse.Namespace(job="NOPE"))
        self.assertEqual(code, 1)
        self.assertIn("no job NOPE", err)

    def test_hook_sweep_opens_the_board_once_per_throttle_window(self):
        self.queue("lead-1")
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(passcmd.paths, "state_dir", return_value=Path(tmp)), \
                mock.patch("swarm.board.open_board", return_value=contextlib.nullcontext(self.b)) as opened, \
                mock.patch("swarm.supervisor.runner.start", side_effect=self.starter):
            hooks._pass_sweep(self.cfg)
            hooks._pass_sweep(self.cfg)
        self.assertEqual(opened.call_count, 1)
        self.assertEqual(len(self.runs), 1)

    def test_hook_sweep_never_raises(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(passcmd.paths, "state_dir", return_value=Path(tmp)), \
                mock.patch("swarm.board.open_board", side_effect=RuntimeError("no board")), \
                mock.patch.object(hooks, "_log_error") as log:
            hooks._pass_sweep(self.cfg)
        log.assert_called_once()


class JudgeStartsNextRun(PassCmd):
    def test_not_met_queues_and_starts_the_lead_while_the_judge_is_still_running(self):
        artifact = self.hand_off()
        self.pass_()          # judge queued
        self.pass_()          # judge started (its restart stays open: the judge is still running)
        judge = self.b.allocate_name("judge", "J", "judge")
        self.assertTrue(self.b.claim_judge("judge", "J"))
        self.assertTrue(self.b.record_verdict("J", judge, "not_met", "tests missing", "add tests", artifact=artifact))
        before = len(self.runs)
        out = passcmd.on_not_met(self.b, self.cfg, "J", starter=self.starter)
        self.assertIn("relead-", out)
        self.assertEqual(len(self.runs), before + 1)          # started now, not after the judge exits
        relead = self.b.get_launch("J", 1, "relead-" + passcmd._key(artifact))
        self.assertEqual(relead.state, "running")
        self.assertIn("add tests", relead.brief)
        passcmd.on_not_met(self.b, self.cfg, "J", starter=self.starter)
        self.assertEqual(len(self.runs), before + 1)          # never twice for the same artifact


class EmptyJobs(PassCmd):
    def test_an_unlaunched_job_nobody_works_on_fails_after_the_quiet_limit(self):
        self.b.open_job("O", None, None, None, "me", goal="g")   # an orchestrator's job: no workdir, no launches
        self.pass_()
        self.assertEqual(self.b.job_status("O").status, "active")
        self.pass_(minutes=passcmd.EMPTY_MINUTES + 5)
        self.assertEqual(self.b.job_status("O").status, "failed")

    def test_an_open_question_keeps_it_open(self):
        self.b.open_job("Q", None, None, None, "me", goal="g")
        self.b.open_blocker("Q", "question", "alice", "go?", until=self.b.now() + _dt.timedelta(days=1), created_by="me")
        self.pass_(minutes=passcmd.EMPTY_MINUTES + 5)
        self.assertEqual(self.b.job_status("Q").status, "active")


class FailedRestart(PassCmd):
    def test_late_settlement_of_old_judge_does_not_consume_new_same_artifact(self):
        artifact = self.hand_off()
        self.pass_()
        self.pass_()
        for restart in self.open_restarts():
            self.b.finish_restart(restart.id, "failed")
        self.b.close_job("J", "failed", "old judge failed")
        self.queue("fresh", queued_by="swarm run")
        self.pass_()  # old judge row settles only after the new activation
        for restart in self.open_restarts():
            self.b.finish_restart(restart.id, "completed")
        self.hand_off(artifact)
        self.pass_()
        self.assertEqual(self.job().status, "active")
        judge = self.b.get_launch("J", 1, "judge-" + passcmd._key(artifact))
        self.assertEqual(judge.state, "queued")

    def test_three_old_attempts_do_not_exhaust_the_new_run(self):
        self.test_no_hand_off_relaunches_the_lead_then_fails_after_three()
        self.queue("fresh", queued_by="swarm run")
        self.pass_()
        for restart in self.open_restarts():
            self.b.finish_restart(restart.id, "failed")
        self.pass_()
        self.assertEqual(self.job().status, "active")
        self.assertTrue(any(r.state == "queued" and r.role == "lead" for r in self.b.launches("J")))

    def test_fresh_same_artifact_handoff_gets_a_new_judge_attempt(self):
        artifact = self.hand_off()
        self.pass_()
        previous = self.b.get_launch("J", 1, "judge-" + passcmd._key(artifact))
        self.b.set_launch_state(previous.id, previous.revision, "failed", job="J")
        self.b.close_job("J", "failed", "old judge failed")
        self.queue("fresh", queued_by="swarm run")
        self.pass_()
        for restart in self.open_restarts():
            self.b.finish_restart(restart.id, "completed")
        self.hand_off(artifact)
        self.pass_()
        current = self.b.get_launch("J", 1, "judge-" + passcmd._key(artifact))
        self.assertEqual(current.state, "queued")
        self.assertGreater(current.revision, previous.revision)
        self.assertEqual(self.job().status, "active")

    def test_fresh_request_reopens_before_launch_and_starts_once(self):
        self.b.set_job_max_hours("J", 9)
        self.b.close_job("J", "failed", "old attempts exhausted")
        failed_at = self.job().finished_at
        self.queue("fresh", queued_by="swarm run")
        self.assertEqual(self.job().status, "failed")  # queuing records facts only
        self.pass_()
        self.assertEqual(self.job().status, "active")
        self.assertGreater(self.job().activated_at, failed_at)
        self.assertEqual(self.job().max_hours, 9)
        self.assertEqual([r["launch"] for r in self.runs], ["fresh"])
        self.pass_()
        self.assertEqual(len(self.runs), 1)

    def test_historical_queue_and_system_requeues_do_not_reopen_failure(self):
        self.queue("historical", queued_by="swarm run")
        self.b.close_job("J", "failed", "stopped")
        self.queue("automatic", queued_by=passcmd.PASS_NAME)
        self.pass_()
        self.assertEqual(self.job().status, "failed")
        self.assertEqual(self.runs, [])

    def test_fresh_retry_does_not_start_historical_queued_work(self):
        self.queue("historical", queued_by="swarm run")
        self.b.close_job("J", "failed", "stopped")
        self.queue("fresh", queued_by="swarm run")
        self.pass_()
        self.assertEqual([r["launch"] for r in self.runs], ["fresh"])

    def test_restart_ignores_old_handoff_judge_and_exhausted_attempts(self):
        old = self.hand_off()
        self.pass_()
        judge = self.b.get_launch("J", 1, "judge-" + passcmd._key(old))
        self.b.set_launch_state(judge.id, judge.revision, "failed", job="J")
        self.b.close_job("J", "failed", "judge ended with no verdict")
        self.queue("fresh", queued_by="swarm run")
        self.pass_()
        for restart in self.open_restarts():
            self.b.finish_restart(restart.id, "failed")
        self.pass_()
        self.assertEqual(self.job().status, "active")
        self.assertTrue(any(r.state == "queued" and r.role == "lead" for r in self.b.launches("J")))

    def test_retry_preserves_lifetime_cost_cap(self):
        self.b.set_job_data("J", "max_cost_usd", "1")
        self.queue("spent")
        self.pass_()
        self.b.finish_restart(self.open_restarts()[0].id, "failed", cost_usd=1)
        self.b.close_job("J", "failed", "cost reached")
        self.queue("fresh", queued_by="swarm run")
        self.pass_()
        self.assertEqual(len(self.runs), 1)
        self.assertEqual(self.job().status, "failed")
        self.assertIn("cost cap reached", self.job().outcome)

    def test_engine_never_launches_on_completed_cancelled_or_paused_jobs(self):
        for state in ("completed", "cancelled", "paused"):
            with self.subTest(state=state):
                self.b.close_job("J", state, "owner stopped")
                self.queue("fresh", queued_by="swarm run")
                self.pass_()
                self.assertEqual(self.job().status, state)
                self.assertEqual(self.runs, [])

    def test_reopen_compare_and_swap_does_not_override_owner_cancel(self):
        self.b.close_job("J", "failed", "stopped")
        failed_at = self.job().finished_at
        row = self.queue("fresh", queued_by="swarm run")
        self.b.close_job("J", "cancelled", "owner cancelled")
        self.assertFalse(self.b.reopen_failed_job("J", failed_at, row.id, row.revision))
        self.assertEqual(self.job().status, "cancelled")


add_backend_variants(globals(), FailedRestart)


if __name__ == "__main__":
    unittest.main()
