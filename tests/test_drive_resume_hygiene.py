"""F03i: pause holds the ledger; resume handles and judge retries stay local."""
import contextlib
import io
from types import SimpleNamespace as NS
from unittest import mock

from test_drive_exec import ExecEnv, SESS9
from swarm import cli, pause
from swarm.supervisor import driver_exec as ex, driver_life as life, driver_model as dm
from swarm.supervisor import driver_snapshot as ds


class ResumeHygiene(ExecEnv):
    def test_pause_resume_drive_unhalts_and_never_starts_generic_sessions(self):
        self.get_launch()
        run = self.go(self.act())
        with mock.patch.object(life.Hooks, "stop_rows"), mock.patch.object(life.Hooks, "rows_live", return_value=False):
            rep = pause.pause(self.b, self.cfg, "J", "stop ruling loop", "Francesco", wait=0)
        led = self.reload()
        self.assertEqual(led.halted["by"], "person: pause")
        self.assertEqual(led.halted["reason"], "stop ruling loop")
        self.assertEqual(led.intents[-1].outcome, "interrupted by pause")
        self.assertEqual(self.b.get_launch("J", 1, "eng-a").state, "queued")
        with mock.patch("swarm.hosts.resume.start") as start, mock.patch(
                "swarm.supervisor.markers.write_resume_marker") as marker:
            report = pause.resume(self.b, self.cfg, "J", enrol_wait=0)
        self.assertTrue(report.reopened)
        self.assertIsNone(self.reload().halted)
        self.assertEqual(self.b.job_status("J").status, "active")
        start.assert_not_called()
        marker.assert_not_called()

    def test_pause_interruption_survives_resume_before_runner_finish(self):
        self.get_launch()
        run = self.go(self.act())
        with mock.patch.object(life.Hooks, "stop_rows"), mock.patch.object(
                life.Hooks, "rows_live", return_value=True):
            pause.pause(self.b, self.cfg, "J", "stop", "Francesco", wait=0)
            pause.resume(self.b, self.cfg, "J", enrol_wait=0)
        self.assertTrue(ex.conclude_run(self.cfg, run, "failed", 0.0))
        self.assertEqual(self.reload().intents[-1].outcome, "interrupted by pause")
        self.assertEqual(dm.session_count(self.led), 0)
        self.b.finish_restart(run["restart_id"], "failed")
        ex._settle_launches(self.b, self.cfg, self.led, {run["launch_id"]: self.row_of(run)}, self.now())
        row = self.b.get_launch("J", 1, "eng-a")
        self.assertEqual((row.state, row.last_outcome), ("queued", "interrupted by pause"))

    def test_named_requeue_clears_session_identity(self):
        self.get_launch()
        self.tamper_launch("eng-a", session_id=SESS9, agent_key=SESS9)
        row = self.get_launch()
        self.assertIsNone(row.session_id)
        self.assertIsNone(row.agent_key)

    def handle(self, status="running", outcome="completed", foreign=False):
        row = self.get_launch()
        self.tamper_launch("eng-a", session_id=SESS9, agent_key=SESS9, launch_id="old")
        row = self.b.get_launch("J", 1, "eng-a")
        board = mock.Mock()
        board.agents.return_value = [NS(agent_key=SESS9, status=status)]
        board.restarts.return_value = [NS(new_agent_key=SESS9, launch_id="other" if foreign else "old", outcome=outcome)]
        led = NS(intents=[NS(launch="eng-a", outcome=outcome)])
        return row, board, led

    def test_only_a_live_session_of_this_launch_can_resume(self):
        row, board, led = self.handle()
        self.assertEqual(ex._check_resumable(row, board, led), SESS9)
        board.set_launch_state.assert_not_called()
        for options in ({"status": "dead"}, {"status": "completed"}, {"status": "left"},
                        {"outcome": "no hand-off"}, {"outcome": "refused"}, {"foreign": True}):
            with self.subTest(options=options):
                row, board, led = self.handle(**options)
                self.assertIsNone(ex._check_resumable(row, board, led))
                board.set_launch_state.assert_called_once_with(
                    row.id, row.revision, row.state, job="J", session_id=None, agent_key=None)

    def test_one_failed_resume_drops_the_handle(self):
        self.get_launch()
        run = self.go(self.act())
        self.end(run, "failed", idle=False)
        self.tamper_launch("eng-a", session_id=SESS9, agent_key=SESS9)
        self.led.watermarks[f"resuming:{run['launch_id']}"] = 1
        ex._idle_launch(self.b, self.led, "eng-a", "failed", self.now(),
                        session_id=SESS9, agent_key=SESS9)
        row = self.b.get_launch("J", 1, "eng-a")
        self.assertIsNone(row.session_id)
        self.assertIsNone(row.agent_key)

    def test_judge_missing_handoff_clears_handle_on_first_retry(self):
        self.get_launch("judge", role="judge")
        run = self.go(self.act("judge"))
        self.end(run, "completed", idle=False)
        self.tamper_launch("judge", session_id=SESS9, agent_key=SESS9)
        ex._idle_launch(self.b, self.led, "judge", "no hand-off", self.now())
        row = self.b.get_launch("J", 1, "judge")
        self.assertEqual(row.state, "queued")
        self.assertIsNone(row.session_id)
        self.assertIsNone(row.agent_key)

    def test_judge_backoff_allows_queued_lead_and_engineer(self):
        self.get_launch("judge", role="judge")
        self.get_launch("lead", role="engineering_lead")
        self.get_launch("eng-a")
        self.led.watermarks["verdict-missing:judge"] = 3
        self.led.watermarks["retry-at:judge"] = int(self.now().timestamp()) + 300
        snapshot = ds.read(self.b, self.led, "J", cfg={}, now=self.now())
        actions = dm.next_actions(snapshot, {})
        self.assertTrue(any(a.launch == "judge" and a.reason == "verdict-retry-backoff" for a in actions))
        self.assertEqual({a.launch for a in actions if a.kind == "launch"}, {"lead", "eng-a"})

    def test_zero_cost_escalation_names_start_failure(self):
        row = self.get_launch("judge", role="judge")
        run = self.go(self.act("judge"))
        self.end(run, "completed")
        ex._escalate_verdict(self.b, self.led, row, self.now(), "missing verdict")
        text = self.b.recent_messages(1, "J")[0].message
        self.assertIn("stale resume handle or start failure; see driver.log", text)

    def test_status_exposes_halted_ledger(self):
        self.led.halted = {"by": "person: pause", "reason": "inspect", "at": self.now().isoformat()}
        self.led.save()
        args = cli._parser().parse_args(["status", "--job", "J"])
        with contextlib.redirect_stdout(io.StringIO()) as out:
            cli._board_status(self.b, self.cfg, args)
        self.assertIn("halted: person: pause (inspect) - unhalt with swarm unhalt --job J", out.getvalue())
