"""F03c: hand-offs do not depend on artifact progress or harness availability."""
import time
from unittest import mock

from test_drive_exec import ExecEnv
from swarm import cli, drive_cli, receipts, review
from swarm.supervisor import driver, driver_exec as ex, driver_model as dm, driver_snapshot as ds
from swarm.supervisor import launch, prompts, driver_life, session_driver

ART = 'feature@' + 'a' * 40


class HandoffTests(ExecEnv):
    def joined(self, run, role='engineering_lead'):
        key = 'session-' + run['launch_id']
        name = self.b.allocate_name(key, 'J', role)
        self.b.set_restart_agent(run['restart_id'], key)
        if role == 'judge':
            self.b.close_agent(self.row_of(run).old_agent_key, 'replaced by judge session')
            self.b.take_judge_launch(key, 'J')
        return name

    def settle(self, run):
        self.end(run, 'completed', idle=False)
        ex._settle_launches(self.b, self.cfg, self.led, {run['launch_id']: self.row_of(run)}, self.now())
        return self.b.get_launch('J', 1, run['launch'])

    def view(self, now=None):
        return ds.read(self.b, self.led, 'J', cfg={}, now=now or self.now())

    def test_lead_done_same_sha_is_still_a_handoff(self):
        self.get_launch('lead', role='engineering_lead')
        for _ in range(2):
            run = self.go(self.act('lead'))
            name = self.joined(run)
            rc, out, err = self.cli('done', '--job', 'J', '--as', name, '--artifact', ART,
                                    '--summary', 'same artifact; judge decides progress')
            self.assertEqual(rc, 0, err)
            self.assertIn('swarm-record: kind=done', out)
            row = self.settle(run)
            self.assertEqual((row.state, row.last_outcome), ('done', 'completed'))
            self.assertNotIn('lead-recovery:lead', self.led.watermarks)
            ex._queue_existing(self.b, row)

    def test_lead_done_without_artifact_warns_and_still_hands_off(self):
        self.get_launch("lead", role="engineering_lead")
        run = self.go(self.act("lead"))
        name = self.joined(run)
        rc, out, err = self.cli("done", "--job", "J", "--as", name, "--summary", "finished")
        self.assertEqual(rc, 0, err)
        self.assertIn("--artifact <BRANCH>@<FULL SHA>", err)
        self.assertEqual(self.settle(run).state, "done")

    def test_lead_accepts_structured_done_record(self):
        self.get_launch('lead', role='engineering_lead')
        run = self.go(self.act('lead'))
        self.b.post('J', self.joined(run), 'swarm-record: kind=done id=12 rev=1')
        self.assertEqual(self.settle(run).last_outcome, 'completed')

    def test_other_agent_done_does_not_handoff(self):
        self.get_launch('lead', role='engineering_lead')
        self.b.post('J', 'someone', 'DONE ' + ART)
        run = self.go(self.act('lead'))
        self.joined(run)
        self.b.post('J', 'someone', 'DONE ' + ART)
        self.assertNotEqual(self.settle(run).state, 'done')

    def test_blocked_lead_escalates_and_retries_on_backoff_without_attempt_charge(self):
        self.get_launch('lead', role='engineering_lead')
        run = self.go(self.act('lead'))
        self.b.post('J', self.joined(run), 'BLOCKED Astra launch unavailable')
        row = self.settle(run)
        self.assertEqual((row.state, row.last_outcome, row.attempts), ('queued', 'blocked', 0))
        self.assertEqual(self.led.intents[-1].outcome, 'blocked')
        self.assertEqual(self.b.job_status('J').status, 'active')
        self.led.rounds_without_progress = 10
        self.led.ruling_due_at = self.now(-3 * 3600).isoformat()
        self.led.save()
        self.assertFalse(driver_life.enforce_deadlines(self.b, self.cfg, self.led, self.now()))
        self.assertEqual(session_driver.ledger_deadlines(self.led, {}), [])
        action, = dm.next_actions(self.view(), {})
        self.assertEqual((action.kind, action.reason), ('hold', 'blocked-retry-backoff'))
        action, = dm.next_actions(self.view(self.now(301)), {})
        self.assertEqual((action.kind, action.launch, action.ruling), ('launch', 'lead', False))
        events = [e for e in self.b.pending_events('J') if 'DRIVER-ESCALATE' in e.text]
        self.assertEqual(len(events), 1)
        self.assertIn('Astra launch unavailable', events[0].text)

    def test_ruling_not_met_exit_requeues_lead_with_reasons(self):
        self.get_launch('lead', role='engineering_lead')
        self.led.ruling_due_at = self.now().isoformat()
        self.led.rounds_without_progress = 3
        self.led.save()
        run = self.go(self.act(None, kind='ruling', group='ruling', ruling=True))
        name = self.joined(run, role='judge')
        result = self.cli('verdict', '--job', 'J', '--as', name, '--scope', 'ruling', '--candidate', 'a' * 40,
                          '--req', 'r1', 'not_met', '--open', '1', '--reason', 'Astra review missing',
                          '--next', 'staff required Astra engineer')
        self.assertEqual(result[0], 0, result[2])
        msg = next(m for m in self.b.messages_after(0, 'J') if m.message.startswith('VERDICT '))
        rec = review.drive_verdict(self.b, 'J', 1, 'ruling', 'a' * 40, 'r1')
        receipts.write(self.bk, receipts.Receipt(
            board_key=self.bk, job='J', incarnation=self.led.incarnation, generation=1,
            action_id=run['action_id'], attempt=1, launch='ruling-judge', kind='verdict',
            record_id=str(msg.id), rev=1, content_sha=receipts.content_sha('verdict', ds._verdict_row(msg.id, rec)),
            args_sha=receipts.args_sha(['swarm']), expires_at=time.time() + 3600))
        for n in range(205):
            self.b.post('J', 'other', f'later message {n}')
        self.assertEqual(self.settle(run).last_outcome, 'completed')
        self.led.ruling_due_at = self.now(-3 * 3600).isoformat()
        self.led.save()
        self.assertFalse(driver_life.enforce_deadlines(self.b, self.cfg, self.led, self.now()))
        snapshot = self.view()
        action, = dm.next_actions(snapshot, {})
        self.assertEqual(action.kind, 'fix_lead')
        driver._act(self.b, self.cfg, self.sup, self.led, snapshot, [action], self.now(), None, '',
                    time.monotonic() + 10, driver._modules())
        self.assertIsNone(self.led.terminal_intent)
        self.assertIsNone(self.led.ruling_due_at)
        self.assertEqual(self.b.job_status('J').status, 'active')
        self.assertEqual(self.b.get_launch('J', 1, 'lead').state, 'queued')
        self.assertTrue(any(m.to_agent == 'lead' and 'Astra review missing' in m.message
                            for m in self.b.messages_after(0, 'J')))
        action, = dm.next_actions(self.view(), {})
        self.assertEqual((action.kind, action.launch, action.ruling), ('launch', 'lead', False))

    def test_recorded_unreachable_escalates_before_terminal_intent_once(self):
        from drive_fixtures import snap, v
        verdict = v('unreachable', scope='ruling', reason='goal cannot be met', next='notify owner')
        snapshot = snap(ledger=self.led, goal_verdict=verdict)
        action, = dm.next_actions(snapshot, {})
        modules = driver._modules()
        for _ in range(2):
            self.assertTrue(driver._terminal_from(snapshot, self.led, self.b, self.now(), modules, action))
        notices = [m for m in self.b.messages_after(0, 'J') if 'DRIVER-ESCALATE' in m.message]
        self.assertEqual(len(notices), 1)
        self.assertIn('goal cannot be met', notices[0].message)
        self.assertEqual(self.led.terminal_intent['verdict'], 'unreachable')
        self.assertTrue(any('unreachable ruling' in e.text for e in self.b.pending_events('J')))

    def test_prompts_share_handoff_forms_and_allow_ruling_not_met(self):
        row = self.get_launch('lead', role='engineering_lead')
        text = prompts.render(self.b, self.cfg, self.led, self.act('lead'), row)
        for form in prompts.LEAD_HANDOFF_FORMS:
            self.assertIn(form.strip(), text)
        text = prompts.render(self.b, self.cfg, self.led,
                              self.act(None, kind='ruling', group='ruling', ruling=True),
                              self.get_launch('ruling-judge', role='judge'))
        self.assertIn('met|not_met|unreachable', text)


class HarnessTests(ExecEnv):
    def test_missing_queued_harness_parks_once_without_attempts_then_recovers(self):
        self.get_launch(harness='codex')
        with mock.patch.object(launch.shutil, 'which', return_value=None):
            for _ in range(3):
                self.assertIsNone(self.go(self.act()))
        self.assertEqual(self.b.get_launch('J', 1, 'eng-a').attempts, 0)
        self.assertEqual(self.led.intents, [])
        self.assertEqual(self.b.job_status('J').status, 'active')
        notices = [m for m in self.b.messages_after(0, 'J') if 'DRIVER-ESCALATE' in m.message]
        self.assertEqual(len(notices), 1)
        self.assertIn('set [harness.codex] command', notices[0].message)
        with mock.patch.object(launch.shutil, 'which', return_value='/opt/bin/codex'):
            run = self.go(self.act())
        self.assertIsNotNone(run)
        self.assertEqual(run['argv'][0], '/opt/bin/codex')
        self.assertNotIn('harness-unavailable:eng-a', self.led.watermarks)

    def test_run_refuses_missing_harness_before_queueing(self):
        args = cli._parser().parse_args(['run', '--job', 'J', '--name', 'astra', '--role', 'engineer',
                '--brief', str(self.brief), '--workdir', str(self.work), '--harness', 'codex'])
        with mock.patch.object(launch.shutil, 'which', return_value=None), self.assertRaisesRegex(
                drive_cli.Refuse, 'codex harness not available on host .*set .*harness.codex'):
            drive_cli.cmd_run(self.b, self.cfg, args)
        self.assertEqual(self.b.launches('J'), [])
