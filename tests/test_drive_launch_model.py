"""Incident regressions for the binding 2026-10-15 launch model design."""
import contextlib
import datetime as dt
import io
from types import SimpleNamespace as NS
from unittest import mock

from test_drive_exec import ExecEnv
from swarm import cli, drive_cli, enrolment
from swarm.supervisor import driver, driver_exec, driver_life, driver_model, driver_snapshot, ledger


class LaunchModel(ExecEnv):
    def test_F1_two_agents_one_workdir_both_run(self):
        self.get_launch("a", workdir=str(self.work), authorize=False)
        self.get_launch("b", workdir=str(self.work), authorize=False)
        self.assertIsNotNone(self.go(self.act("a")))
        self.assertIsNotNone(self.go(self.act("b")))
        self.assertEqual(len(self.launched), 2)

    def test_B5_six_jobs_have_independent_concurrency_caps(self):
        for n in range(6):
            job = f"job-{n}"
            self.b.open_job(job, None, None, None, "me", goal="G")
            led = ledger.create(self.bk, job, budgets={"max_concurrent_sessions": 1},
                                activated_at=self.now().isoformat())
            enrolment.write_job_owner(self.bk, job=job, harness="claude", session_id="s", cwd=str(self.work))
            row = self.b.queue_launch(job, 1, "eng", role="engineer", harness="claude", model="haiku",
                                      workdir=str(self.work), brief=str(self.brief), queued_by="me")
            action = driver_model.Action(kind="launch", action_id=f"gen1|launch|eng|-|0|{row.revision}",
                                         launch="eng", prompt_kind="launch")
            run = driver_exec.execute(self.b, self.cfg, self.sup, led, action, now=self.now(),
                                      prompt="work", start_runner=lambda c, r: self.launched.append(r))
            self.assertIsNotNone(run, job)
        self.assertEqual(len(self.launched), 6)

    def test_D2_A6_reopen_first_pass_launches_lead_without_receipt(self):
        self.get_launch("lead", role="engineering_lead", authorize=False)
        self.led.terminal = {"verdict": "unreachable", "at": self.now().isoformat()}
        self.led.save()
        self.led = ledger.activate(self.bk, "J", budgets={}, activated_at=self.now().isoformat())
        self.b.set_job_data("J", "drive.enabled", "1")
        enrolment.write_job_owner(self.bk, job="J", harness="claude", session_id="s", cwd=str(self.work),
                                  roots=[str(self.work)], goal="G", brief=str(self.brief))
        driver.drive(self.b, self.cfg, self.sup, {}, now=self.now(), say=lambda s: None,
                     start_runner=lambda c, r: self.launched.append(r), scope_ok=lambda: True)
        self.assertEqual([r["launch"] for r in self.launched], ["lead"])
        self.assertEqual(self.b.get_launch("J", 2, "lead").state, "running")

    def test_B3_goal_change_does_not_halt(self):
        js = NS(status="active", supervise=True, goal="updated goal")
        owner = NS(owner={"goal": "old goal"})
        self.assertIsNone(driver.tamper(self.led, js, owner, {"drive.enabled": "1"}))

    def test_tamper_keeps_only_missing_row_pause_supervise_and_drive_flag(self):
        owner = NS(owner={"goal": "old goal"})
        flags = {"drive.enabled": "1"}
        self.assertEqual(driver.tamper(self.led, None, owner, flags), "job row missing")
        for state in ("active", "completed", "failed", "cancelled"):
            row = NS(status=state, supervise=True, goal="new goal")
            self.assertIsNone(driver.tamper(self.led, row, owner, flags))
        row = NS(status="paused", supervise=True, goal="new goal")
        self.assertEqual(driver.tamper(self.led, row, owner, flags), "job paused")
        row = NS(status="active", supervise=False, goal="new goal")
        self.assertEqual(driver.tamper(self.led, row, owner, flags), "supervise switched off")
        row.supervise = True
        self.assertEqual(driver.tamper(self.led, row, owner, {}), "drive flag cleared")

    def test_C4_dot_directory_refused_at_run(self):
        hidden = self.work / ".wt" / "eng"
        hidden.mkdir(parents=True)
        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            args = cli._parser().parse_args(["run", "--job", "J", "--name", "eng", "--role", "engineer",
                                              "--brief", str(self.brief), "--workdir", str(hidden)])
        with mock.patch.object(drive_cli, "_owner_roots", return_value=[str(self.work)]), \
                mock.patch.object(drive_cli, "TMP_ROOTS", ()), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            rc = cli.BOARD_COMMANDS["run"](self.b, self.cfg, args)
        self.assertEqual(rc, 1)
        self.assertIn("dot-directory", err.getvalue())
        self.assertIsNone(self.b.get_launch("J", 1, "eng"))

    def test_three_queued_launches_start_in_one_real_pass(self):
        for name, role in (("lead", "engineering_lead"), ("eng-a", "engineer"), ("eng-b", "engineer")):
            self.get_launch(name, role=role, workdir=str(self.work))
        self.b.set_job_data("J", "drive.enabled", "1")
        enrolment.write_job_owner(self.bk, job="J", harness="claude", session_id="s", cwd=str(self.work),
                                  roots=[str(self.work)], goal="G", brief=str(self.brief))
        driver.drive(self.b, self.cfg, self.sup, {}, now=self.now(), say=lambda s: None,
                     start_runner=lambda c, r: self.launched.append(r), scope_ok=lambda: True)
        self.assertEqual({r["launch"] for r in self.launched}, {"lead", "eng-a", "eng-b"})
        self.assertTrue(all(s.state == "running" for s in self.b.launches("J", 1)))

    def test_failed_launch_posts_once_and_requires_requeue_by_same_name(self):
        row = self.get_launch()
        run = self.go(self.act())
        self.b.finish_restart(run["restart_id"], "refused")
        rows = {r.launch_id: r for r in self.b.restarts("J")}
        for _ in range(3):
            driver_exec._settle_launches(self.b, self.cfg, self.led, rows, self.now())
        failures = [m for m in self.b.recent_messages(20, job="J") if "launch eng-a failed" in m.message]
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0].to_agent, row.queued_by)
        self.assertIsNone(self.go(self.act()))
        self.conclude_intent("refused")
        again = driver_exec._queue_existing(self.b, self.b.get_launch("J", 1, "eng-a"))
        self.assertEqual(again.id, row.id)
        self.assertIsNotNone(self.go(self.act(trigger=str(again.revision))))

    def test_worker_exit_without_handoff_fails_but_handoff_completes(self):
        for name, handoff in (("a", False), ("b", True)):
            self.get_launch(name)
            run = self.go(self.act(name))
            if handoff:
                self.b.post("J", run["name"], "DONE feature@" + "a" * 40)
            self.b.finish_restart(run["restart_id"], "completed")
        rows = {r.launch_id: r for r in self.b.restarts("J")}
        driver_exec._settle_launches(self.b, self.cfg, self.led, rows, self.now())
        self.assertEqual(self.b.get_launch("J", 1, "a").state, "failed")
        self.assertEqual(self.b.get_launch("J", 1, "b").state, "done")

    def test_requeue_checks_session_budget_then_budget_bump_allows_start(self):
        row = self.get_launch()
        run = self.go(self.act())
        self.end(run, "failed")
        again = driver_exec._queue_existing(self.b, self.b.get_launch("J", 1, row.name))
        self.led.budgets["max_sessions"] = 1
        self.led.save()
        act = self.act(trigger=str(again.revision))
        self.assertIsNone(self.go(act))
        self.led.budgets["max_sessions"] = 2
        self.led.save()
        self.assertIsNotNone(self.go(act))

    def test_each_start_checks_wall_clock_budget(self):
        self.get_launch()
        self.led.activated_at = (self.now() - dt.timedelta(hours=25)).isoformat()
        self.led.save()
        self.assertIsNone(self.go(self.act()))
        self.assertEqual(self.launched, [])

    def test_all_roles_have_writable_launches(self):
        for role in ("engineering_lead", "qa", "reviewer", "judge", "security"):
            self.assertFalse(driver_exec.read_only_role(role))

    def test_queued_without_running_for_two_passes_reports_driver_bug_once(self):
        self.get_launch("lead", role="engineering_lead")
        self.b.set_job_data("J", "drive.enabled", "1")
        enrolment.write_job_owner(self.bk, job="J", harness="claude", session_id="s", cwd=str(self.work),
                                  roots=[str(self.work)], goal="G", brief=str(self.brief))
        state = {}
        with mock.patch.object(driver_exec, "execute", return_value=None):
            for _ in range(3):
                driver.drive(self.b, self.cfg, self.sup, state, now=self.now(), say=lambda s: None,
                             scope_ok=lambda: True)
        self.assertEqual(sum("DRIVER-BUG queued launches" in m.message
                             for m in self.b.recent_messages(20, job="J")), 1)

    def test_status_exposes_queue_and_failure_reason(self):
        row = self.get_launch()
        self.assertIn("queued", self.cli("status", "--job", "J")[1])
        self.b.set_launch_state(row.id, row.revision, "failed", job="J", last_outcome="missing harness")
        rc, out, err = self.cli("status", "--job", "J")
        self.assertEqual(rc, 0, err)
        self.assertIn("failed", out)
        self.assertIn("missing harness", out)
