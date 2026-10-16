"""F03b: explicit retries get a new allowance and queued work explains its holds."""
import contextlib
import io
from unittest import mock

from drive_fixtures import snap
from test_drive_exec import ExecEnv
from swarm import cli, drive_cli
from swarm.supervisor import driver, driver_exec, driver_life, driver_model as dm, ledger


class RequeueTests(ExecEnv):
    def test_run_reauthorizes_exhausted_action_without_losing_history_or_cost(self):
        row = self.get_launch()
        action = self.act()
        for attempt in range(1, 4):
            run = self.go(action)
            self.assertIsNotNone(run)
            self.end(run)
            self.led.intents[-1].charged_usd = 1
            self.led.save()
            driver_exec._queue_existing(self.b, self.b.get_launch("J", 1, row.name))
        self.led.save()
        self.assertIsNone(self.go(action))
        args = cli._parser().parse_args([
            "run", "--job", "J", "--name", row.name, "--role", row.role,
            "--workdir", row.workdir, "--brief", row.brief])
        with mock.patch.object(drive_cli, "_owner_roots", return_value=[str(self.work)]), \
                mock.patch.object(drive_cli, "TMP_ROOTS", ()), \
                mock.patch.object(drive_cli, "_caller", return_value=None), \
                mock.patch("swarm.drivegate.kick"), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(drive_cli.cmd_run(self.b, self.cfg, args), 0)
        self.reload()
        self.assertIn("queued (engineer, attempts 3, requeued 1)", self.cli("status", "--job", "J")[1])
        run = self.go(action)
        self.assertIsNotNone(run)
        self.assertEqual(self.reload().intents[-1].attempt, 1)
        self.assertEqual(len(self.led.intents), 4)
        self.assertEqual(self.led.charged_usd(), 3 + self.led.intents[-1].reserved_usd)
        self.assertIn("requeued 1", self.cli("status", "--job", "J")[1])
        # Pure planning and execution must use the same retry identity.
        ctx = dm._Ctx(snap(ledger=self.led), {})
        self.assertEqual(ctx.aid("launch", row.name, None, 0, "1"), run["action_id"])

    def test_status_shows_last_driver_error(self):
        self.b.set_job_data("J", "drive.error.1", "Traceback (most recent call last):\nRuntimeError: broken step\n")
        rc, out, err = self.cli("status", "--job", "J")
        self.assertEqual(rc, 0, err)
        self.assertIn("last driver error: RuntimeError: broken step (see driver.log)", out)

    def test_new_generation_queues_lead_from_owner_when_no_previous_launch_exists(self):
        driver_life.default_request_lead(self.b, self.cfg, self.led, 2)
        lead = self.b.get_launch("J", 2, "lead")
        self.assertEqual((lead.state, lead.role, lead.workdir),
                         ("queued", "engineering_lead", str(self.work)))
        from pathlib import Path
        self.assertIn("G", Path(lead.brief).read_text())

    def test_claim_skip_identifies_job_concurrency_cap(self):
        self.get_launch("busy")
        self.assertIsNotNone(self.go(self.act("busy")))
        self.get_launch()
        self.led.budgets["max_concurrent_sessions"] = 1
        self.led.save()
        self.assertIsNone(self.go(self.act()))
        self.assertIn("job concurrency limit", self.b.job_data("J").get("drive.skip.1.eng-a", ""))

    def test_skip_reason_is_visible_and_notice_posts_once_across_driver_restarts(self):
        self.get_launch("lead", role="engineering_lead")
        self.b.set_job_data("J", "drive.enabled", "1")
        from swarm import enrolment
        enrolment.write_job_owner(self.bk, job="J", harness="claude", session_id="s", cwd=str(self.work),
                                  roots=[str(self.work)], goal="G", brief=str(self.brief))
        self.led.budgets["max_sessions"] = 0
        self.led.save()
        for _ in range(3):
            driver.drive(self.b, self.cfg, self.sup, {}, now=self.now(), say=lambda s: None,
                         scope_ok=lambda: True, start_runner=lambda c, r: self.launched.append(r))
        notices = [m.message for m in self.b.recent_messages(50, job="J")
                   if m.message.startswith("DRIVER-SKIP lead:")]
        self.assertEqual(len(notices), 1)
        self.assertIn("session", notices[0])
        out = self.cli("status", "--job", "J")[1]
        self.assertRegex(out, r"launch lead gen 1: queued.*session")

    def test_executor_records_each_skip_reason(self):
        self.get_launch()
        for why, change in (
                ("attempt", lambda: self.led.intents.extend(ledger.Intent(
                    f"old{n}", self.act().action_id, n, "eng-a", 1, 1, [], "concluded", "failed", False, 0)
                    for n in range(1, 4))),
                ("open", lambda: self.led.intents.append(ledger.Intent(
                    "open", self.act().action_id, 1, "eng-a", 1, 1, [], "claiming"))),
                ("active", lambda: setattr(self.led, "state", "activating"))):
            with self.subTest(why=why):
                self.led.intents.clear()
                self.led.state = "active"
                change()
                self.assertIsNone(self.go(self.act()))
                self.assertIn(why, self.b.job_data("J").get("drive.skip.1.eng-a", ""))
