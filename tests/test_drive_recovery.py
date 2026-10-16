"""F03/H1: driver mechanics never substitute for a judge"s verdict."""
import datetime as dt
import time
import unittest
from unittest import mock

from test_drive_exec import ExecEnv
from drive_fixtures import L, T0, intent, iso, launch, snap
from swarm.supervisor import driver_exec as ex, driver_model as dm, driver_life as life
from swarm.supervisor import prompts, runner, procwatch, session_driver as sd


class Recovery(ExecEnv):
    def test_home_preparation_refusal_is_an_uncounted_driver_error(self):
        reason = "the launch's directory cannot be prepared safely: .cache has mode 0o755, not 0o700"
        for role in ('judge', 'engineering_lead', 'engineer'):
            with self.subTest(role=role):
                name = 'home-' + role
                self.get_launch(name, role=role)
                action = self.act(name)
                for _ in range(2):
                    run = self.go(action)
                    run['refused_reason'] = reason
                    runner.write_run(run)
                    self.assertTrue(runner.finish(self.cfg, run, 'refused'))
                    self.reload()
                    row = self.b.get_launch('J', 1, name)
                    self.assertEqual((row.state, row.attempts), ('queued', 0))
                    self.assertEqual(dm._Ctx(snap(ledger=self.led), {}).attempts(action.action_id), 0)
                    self.assertFalse(dm._Ctx(snap(ledger=self.led), {}).finished(action.action_id))
                    self.assertNotIn('verdict-missing:' + name, self.led.watermarks)
                    self.assertNotIn('retry-at:' + name, self.led.watermarks)
                posts = [m.message for m in self.b.messages_after(0, 'J')
                         if m.message.startswith('DRIVER-ERROR ' + name + ':')]
                self.assertEqual(len(posts), 1)
                self.assertIn(reason, posts[0])

    def test_refused_judge_home_retries_same_candidate_on_the_next_pass(self):
        from drive_fixtures import ho
        art = 'master@' + 'a' * 40
        action = self.act(None, kind='judge', group='master', artifact=art)
        run = self.go(action)
        run['refused_reason'] = "the launch's directory cannot be prepared safely: .cache has mode 0o755"
        runner.write_run(run)
        self.assertTrue(runner.finish(self.cfg, run, 'refused'))
        self.reload()
        row = self.b.get_launch('J', 1, 'judge')
        s = snap(ledger=self.led, handoffs=[ho(art)],
                 launches={'judge': launch('judge', 'queued', role='judge', revision=row.revision,
                                           last_outcome=row.last_outcome)})
        retry, = dm.next_actions(s, {})
        self.assertEqual((retry.kind, retry.prompt_kind, retry.artifact), ('launch', 'judge', art))
        self.assertIsNotNone(self.go(retry))

    def settle(self, run, outcome):
        self.end(run, "completed", idle=False)
        if outcome == "interrupted by driver restart":
            self.led.intents[-1].outcome = outcome
            self.led.save()
        ex._idle_launch(self.b, self.led, run["launch"], outcome, self.now())

    def test_interrupted_launch_requeues_without_consuming_attempt(self):
        self.get_launch()
        run = self.go(self.act())
        self.settle(run, "interrupted by driver restart")
        row = self.b.get_launch("J", 1, "eng-a")
        self.assertEqual((row.state, row.attempts), ("queued", 0))
        self.assertEqual(dm._Ctx(snap(ledger=self.led), {}).sessions(), 0)
        self.assertFalse(dm._Ctx(snap(ledger=self.led), {}).finished(run["action_id"]))

    def test_dead_driver_run_is_requeued_once_after_reap(self):
        self.get_launch()
        run = self.go(self.act())
        # The runner and child disappeared with their driver; no recorded process outcome.
        with mock.patch.object(procwatch, "start_token", return_value="different-driver"):
            runner.reap(self.cfg, self.b)
        self.reload()
        ex.reconcile(self.b, self.cfg, self.led, self.now(), time.monotonic() + 10)
        row = self.b.get_launch("J", 1, "eng-a")
        self.assertEqual((row.state, row.attempts), ("queued", 0))
        self.assertEqual(self.led.intents[-1].outcome, "interrupted by driver restart")
        ex.reconcile(self.b, self.cfg, self.led, self.now(), time.monotonic() + 10)
        self.assertEqual(self.b.get_launch("J", 1, "eng-a").attempts, 0)

    def test_restart_exit_without_handoff_is_interruption_even_if_process_exited_zero(self):
        self.get_launch("lead", role="engineering_lead")
        run = self.go(self.act("lead"))
        self.led.watermarks["driver-restart:" + run["launch_id"]] = 1
        self.led.save()
        self.assertTrue(runner.finish(self.cfg, run, "completed"))
        self.reload()
        row = self.b.get_launch("J", 1, "lead")
        self.assertEqual((row.state, row.attempts), ("queued", 0))
        self.assertEqual(self.led.intents[-1].outcome, "interrupted by driver restart")

    def test_live_driver_run_crash_is_a_work_failure(self):
        self.get_launch()
        self.go(self.act())
        runner.reap(self.cfg, self.b)
        self.reload()
        ex.reconcile(self.b, self.cfg, self.led, self.now(), time.monotonic() + 10)
        row = self.b.get_launch("J", 1, "eng-a")
        self.assertEqual((row.state, row.attempts), ("failed", 1))

    def test_sigterm_marks_open_launches_and_exits_cleanly(self):
        self.get_launch()
        run = self.go(self.act())
        handlers = []
        def install(sig, fn):
            handlers.append(fn)
            return "previous-handler"
        def run_driver(drv):
            handlers[0](None, None)
            return drv.stop_reason()
        with mock.patch.object(sd.signal, "signal", side_effect=install), \
                mock.patch.object(sd.Driver, "run", run_driver):
            why = sd.serve(self.cfg, session=None, job="J", watch=None,
                           config_path=str(self.config), lock=sd.run_lock_name(self.bk, "J"))
        self.assertEqual(why, "driver restart")
        self.assertEqual(self.reload().watermarks["driver-restart:" + run["launch_id"]], 1)
        self.assertEqual(handlers[-1], "previous-handler")

    def test_interrupted_judge_needs_new_head_and_preserves_role_prompt(self):
        from drive_fixtures import ho
        art = "feat/a@" + "a" * 40
        action = self.act(launch=None, kind="judge", group="feat/a", artifact=art)
        run = self.go(action)
        self.settle(run, "interrupted by driver restart")
        row = self.b.get_launch("J", 1, "judge")
        s = snap(ledger=self.led, handoffs=[ho(art)],
                 launches={"judge": launch("judge", "queued", role="judge", revision=row.revision,
                                           last_outcome=row.last_outcome)})
        held, = dm.next_actions(s, {})
        self.assertEqual((held.kind, held.prompt_kind), ('hold', 'goal-hold'))
        art = 'feat/a@' + 'b' * 40
        s.handoffs.append(ho(art, receipt_id=2))
        retry, = dm.next_actions(s, {})
        self.assertEqual((retry.prompt_kind, retry.artifact), ("judge", art))

    def test_lead_recovery_precedes_no_progress_ruling(self):
        self.get_launch("lead", role="engineering_lead")
        self.settle(self.go(self.act("lead")), "no hand-off")
        self.led.rounds_without_progress = 3
        row = self.b.get_launch("J", 1, "lead")
        (action,) = dm.next_actions(snap(ledger=self.led,
            launches={"lead": launch("lead", "queued", role="engineering_lead", revision=row.revision)}), {})
        self.assertEqual(action.launch, "lead")
        self.assertFalse(action.ruling)

    def test_running_lead_recovery_is_not_quiesced_for_no_progress(self):
        led = L(rounds_without_progress=3, watermarks={"lead-recovery:lead": 1})
        (action,) = dm.next_actions(snap(ledger=led, live={"lead"},
            launches={"lead": launch("lead", "running", role="engineering_lead")}), {})
        self.assertEqual((action.kind, action.responsible), ("nothing", "lead"))

    def test_judge_backoff_ignores_expired_mechanical_deadlines(self):
        led = L(ruling_due_at=iso(T0 - dt.timedelta(hours=4)),
                activated_at=iso(T0 - dt.timedelta(days=2)),
                watermarks={"verdict-missing:ruling-judge": 3})
        self.assertEqual(sd.ledger_deadlines(led, {}), [])

    def test_old_done_judge_without_verdict_is_recovered(self):
        action = self.act(launch=None, kind="ruling", group="ruling", ruling=True)
        run = self.go(action)
        self.end(run, "completed", idle=False)
        row = self.b.get_launch("J", 1, "ruling-judge")
        self.b.set_launch_state(row.id, row.revision, "done", job="J", last_outcome="completed")
        ex.reconcile(self.b, self.cfg, self.led, self.now(), time.monotonic() + 10)
        self.assertEqual(self.b.get_launch("J", 1, "ruling-judge").state, "queued")

    def test_judge_budget_refusal_escalates_and_backs_off(self):
        action = self.act(launch=None, kind="ruling", group="ruling", ruling=True)
        self.settle(self.go(action), "no hand-off")
        row = self.b.get_launch("J", 1, "ruling-judge")
        view = launch("ruling-judge", "queued", role="judge", revision=row.revision)
        (retry,) = dm.next_actions(snap(ledger=self.led, launches={"ruling-judge": view}, now=self.now()), {})
        self.led.budgets["max_sessions"] = 1
        self.assertIsNone(self.go(retry))
        (held,) = dm.next_actions(snap(ledger=self.led, launches={"ruling-judge": view}, now=self.now()), {})
        self.assertEqual(held.kind, "hold")
        self.assertGreater(held.not_before, self.now())
        self.assertEqual(self.b.job_status("J").status, "active")

    def test_ruling_missing_verdict_cannot_repeat_the_same_head(self):
        action = self.act(launch=None, kind="ruling", group="ruling", ruling=True,
                          reason="lead-no-hand-off")
        self.settle(self.go(action), "no hand-off")
        row = self.b.get_launch("J", 1, "ruling-judge")
        s = snap(ledger=self.led, launches={"ruling-judge": launch("ruling-judge", "queued",
                 role="judge", revision=row.revision)}, now=self.now())
        (action,) = dm.next_actions(s, {})
        self.assertEqual((action.kind, action.prompt_kind), ("hold", "goal-hold"))
        self.assertEqual(self.b.job_status("J").status, "active")
        self.assertIsNone(self.led.terminal_intent)

    def test_artifact_judge_retry_requires_new_head_and_stops_after_verdict(self):
        from drive_fixtures import ho
        art = "feat/a@" + "a" * 40
        action = self.act(launch=None, kind="judge", group="feat/a", artifact=art)
        self.settle(self.go(action), "no hand-off")
        row = self.b.get_launch("J", 1, "judge")
        s = snap(ledger=self.led, handoffs=[ho(art)],
                 launches={"judge": launch("judge", "queued", role="judge", revision=row.revision)})
        held, = dm.next_actions(s, {})
        self.assertEqual((held.kind, held.prompt_kind), ('hold', 'goal-hold'))
        art = "feat/a@" + "b" * 40
        s.handoffs.append(ho(art, receipt_id=2))
        retry, = dm.next_actions(s, {})
        self.assertEqual((retry.prompt_kind, retry.artifact), ("judge", art))
        run = self.go(retry)
        key = "judge-session-001"
        name = self.b.allocate_name(key, "J", "judge")
        self.b.set_restart_agent(run["restart_id"], key)
        self.b.post("J", name, "VERDICT " + art + " not_met")
        self.end(run, "completed", idle=False)
        ex._settle_launches(self.b, self.cfg, self.led, {run["launch_id"]: self.row_of(run)}, self.now())
        self.assertEqual(self.b.get_launch("J", 1, "judge").state, "done")
        self.assertNotIn("verdict-missing:judge", self.led.watermarks)
        for n in range(205):
            self.b.post("J", name, f"later activity {n}")
        ex._settle_launches(self.b, self.cfg, self.led, {run["launch_id"]: self.row_of(run)}, self.now())
        self.assertEqual(self.b.get_launch("J", 1, "judge").state, "done")

    def test_lead_gets_one_recovery_round_before_ruling(self):
        self.get_launch("lead", role="engineering_lead")
        run = self.go(self.act("lead"))
        self.settle(run, "no hand-off")
        row = self.b.get_launch("J", 1, "lead")
        self.assertEqual(row.state, "queued")
        action = self.act("lead", trigger=str(row.revision))
        text = prompts.render(self.b, self.cfg, self.led, action, row)
        self.assertIn("no hand-off", text)
        self.assertIn("recovery round", text)
        run = self.go(action)
        self.settle(run, "no hand-off")
        row = self.b.get_launch("J", 1, "lead")
        self.assertEqual(row.state, "failed")
        self.led.rounds_without_progress = 3
        (action,) = dm.next_actions(snap(ledger=self.led, launches={"lead": launch("lead", "failed",
                                     role="engineering_lead", last_outcome="no hand-off")}), {})
        self.assertTrue(action.ruling)
        self.assertIn("no hand-off", action.reason)
        judge = ex.materialize_launch(self.b, self.led, action, self.cfg, self.now())
        text = prompts.render(self.b, self.cfg, self.led, action, judge)
        self.assertIn("Launch history", text)
        self.assertIn(run["launch_id"], text)


class MissingRuling(unittest.TestCase):
    def test_finished_ruling_is_retried_even_without_settlement(self):
        aid = "gen1|ruling|-|ruling|0|unrecorded"
        led = L(intents=[intent(action_id=aid, launch="ruling-judge", state="concluded", outcome="completed")],
                rounds_without_progress=3)
        (action,) = dm.next_actions(snap(ledger=led), {})
        self.assertEqual((action.kind, action.prompt_kind), ("launch", "ruling"))
        self.assertNotEqual(action.action_id, aid)

