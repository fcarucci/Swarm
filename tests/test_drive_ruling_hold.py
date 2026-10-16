"""F03k: ruling deadlines cannot masquerade as budget exhaustion."""
import contextlib
import datetime as dt
import io
from unittest import mock

from drive_fixtures import ho, iso, launch, snap, v
from test_drive_exec import ExecEnv
from swarm import cli, drive_activate, drive_cli, enrolment, person
from swarm.supervisor import driver, driver_exec as ex, driver_model as dm, session_driver

HEAD = 'a' * 40


class RulingHoldTests(ExecEnv):
    def drive(self, now=None):
        self.b.set_job_data('J', 'drive.enabled', '1')
        enrolment.write_job_owner(self.bk, job='J', harness='claude', session_id='s', cwd=str(self.work),
                                  roots=[str(self.work)], goal='G', brief=str(self.brief))
        driver.drive(self.b, self.cfg, self.sup, {}, now=now or self.now(), say=lambda s: None,
                     scope_ok=lambda: True, start_runner=lambda c, r: self.launched.append(r))

    def test_deadline_and_attempt_exhaustion_are_ruling_holds(self):
        for due, attempts in ((self.now(-3 * 3600), 0), (self.now(), 3)):
            self.led.ruling_due_at, self.led.ruling_attempts = iso(due), attempts
            s = snap(ledger=self.led, now=self.now())
            action, = dm.next_actions(s, {})
            self.assertEqual((action.kind, action.prompt_kind), ('hold', 'ruling-hold'))

    def test_per_head_ruling_allows_engineers_after_lead_finishes(self):
        self.led.ruling_due_at = iso(self.now(-3 * 3600))
        self.led.rounds_without_progress = 3
        self.led.watermarks['ruling-fix'] = 77
        s = snap(ledger=self.led, goal_ready={'candidate': HEAD},
                 goal_verdict=v('not_met', scope='ruling', candidate=HEAD, receipt_id=77),
                 launches={'lead': launch('lead', 'done', role='engineering_lead'),
                           'eng-rebase4': launch('eng-rebase4', 'queued')})
        actions = dm.next_actions(s, {})
        self.assertEqual([(a.kind, a.launch) for a in actions], [('launch', 'eng-rebase4')])

    def test_ruling_hold_notifies_once_and_clears_on_next_pass(self):
        self.get_launch('lead', role='engineering_lead')
        self.led.ruling_due_at = iso(self.now(-3 * 3600))
        self.led.save()
        now = self.now()
        for seconds in (0, 1, 301):
            self.drive(now + dt.timedelta(seconds=seconds))
        self.assertEqual(self.launched, [])
        self.assertNotIn('budget-hold', self.reload().watermarks)
        self.assertEqual(cli._job_status_word(self.b, self.b.job_status('J'), now), 'waiting on a person (ruling)')
        lines = '\n'.join(drive_activate.drive_lines(self.cfg, 'J'))
        self.assertIn('waiting on: person (ruling: ruling deadline passed;', lines)
        self.assertIn('resume with swarm run --job J --name lead ... or swarm activate --drive', lines)
        self.assertNotIn('person (budget', lines)
        posts = [m for m in self.b.messages_after(0, 'J') if m.message.startswith('DRIVER-ESCALATE ruling:')]
        self.assertEqual(len(posts), 1)
        self.assertEqual(len([e for e in self.b.pending_events('J') if e.kind == 'launch-failed']), 2)
        self.assertEqual(session_driver.ledger_deadlines(self.led, {}), [
            dt.datetime.fromtimestamp(self.led.watermarks['ruling-notify-at'], dt.timezone.utc)])
        self.led.ruling_due_at = None
        self.led.save()
        self.drive(now + dt.timedelta(seconds=302))
        self.assertEqual(len(self.launched), 1)
        self.assertNotIn('ruling-hold', self.reload().watermarks)
        self.assertIsNone(self.b.job_data('J').get('drive.ruling-hold'))

    def test_extending_ruling_deadline_lifts_hold_with_due_time_preserved(self):
        self.get_launch('lead', role='engineering_lead')
        self.led.ruling_due_at = iso(self.now(-3 * 3600))
        self.led.save()
        self.drive()
        self.reload()
        self.led.budgets['ruling_deadline_minutes'] = 240
        self.led.save()
        self.drive()
        self.assertTrue(self.launched)
        self.assertIsNotNone(self.reload().ruling_due_at)
        self.assertNotIn('ruling-hold', self.led.watermarks)
        self.assertIsNone(self.b.job_data('J').get('drive.ruling-hold'))

    def test_ruling_status_names_last_verdict_receipt_and_head(self):
        now = self.now()
        ex.hold_ruling(self.b, self.led, now, 'ruling deadline passed')
        verdict = v('not_met', scope='ruling', candidate=HEAD, receipt_id=77)
        ex.hold_ruling(self.b, self.led, now + dt.timedelta(seconds=301), 'ruling attempts exhausted', verdict)
        lines = '\n'.join(drive_activate.drive_lines(self.cfg, 'J'))
        self.assertIn('ruling: ruling attempts exhausted; the last ruling was not_met #77 on ' + HEAD, lines)
        self.assertNotIn('person (budget', lines)
        self.assertEqual(len([m for m in self.b.messages_after(0, 'J')
                              if m.message.startswith('DRIVER-ESCALATE ruling:')]), 1)

    def test_old_deadline_after_ruling_and_lead_handoff_starts_engineer(self):
        lead = self.get_launch('lead', role='engineering_lead')
        self.b.set_launch_state(lead.id, lead.revision, 'done', job='J')
        self.get_launch('eng-rebase4')
        self.led.ruling_due_at = iso(self.now(-3 * 3600))
        self.led.rounds_without_progress = 3
        self.led.watermarks.update({'ruling-fix': 77, 'p.verdict': 77, 'budget-hold': 1,
                                    'budget-notify-at': int(self.now().timestamp()) + 300})
        self.led.save()
        self.b.set_job_data('J', 'drive.budget-hold', 'stale deadline hold')
        verdict = v('not_met', scope='ruling', candidate=HEAD, receipt_id=77)
        from swarm.supervisor import driver_snapshot
        real = driver_snapshot.read
        def read(*a, **kw):
            from dataclasses import replace
            return replace(real(*a, **kw), goal_verdict=verdict,
                           handoffs=[ho('main@' + HEAD, by='lead', receipt_id=80)])
        with mock.patch.object(driver_snapshot, 'read', side_effect=read):
            self.drive()
        self.assertEqual([r['launch'] for r in self.launched], ['eng-rebase4'])
        self.assertIsNone(self.reload().ruling_due_at)
        self.assertNotIn('person (budget', '\n'.join(drive_activate.drive_lines(self.cfg, 'J')))

    def test_ruling_verdict_and_changed_head_clear_deadline(self):
        for verdict in ('met', 'not_met', 'unreachable'):
            self.led.ruling_due_at = iso(self.now(-3 * 3600))
            self.led.watermarks.pop('p.verdict', None)
            self.led.save()
            s = snap(ledger=self.led, goal_verdict=v(verdict, scope='ruling', candidate=HEAD, receipt_id=77))
            ex.settle_progress(self.led, s, self.now())
            self.assertIsNone(self.reload().ruling_due_at)
        s = snap(ledger=self.led, handoffs=[ho('main@' + HEAD, receipt_id=80)])
        ex.settle_progress(self.led, s, self.now())
        self.led.ruling_due_at = iso(self.now(-3 * 3600))
        self.led.save()
        s.handoffs[:] = [ho('main@' + 'b' * 40, receipt_id=81)]
        ex.settle_progress(self.led, s, self.now())
        self.assertIsNone(self.reload().ruling_due_at)

    def test_old_verdict_does_not_clear_new_head_ruling_deadline(self):
        self.led.watermarks['p.verdict'] = 77
        s = snap(ledger=self.led, handoffs=[ho('main@' + 'b' * 40, by='lead', receipt_id=80)],
                 goal_verdict=v('not_met', scope='ruling', candidate=HEAD, receipt_id=77))
        ex.settle_progress(self.led, s, self.now())
        self.led.ruling_due_at = iso(self.now())
        self.led.save()
        self.assertFalse(ex.settle_progress(self.led, s, self.now()))
        self.assertIsNotNone(self.reload().ruling_due_at)

    def test_person_requeues_lead_and_budget_warns_about_ruling_hold(self):
        row = self.get_launch('lead', role='engineering_lead')
        self.led.budgets.update(drive_activate.budget_defaults(self.cfg))
        self.led.ruling_due_at = iso(self.now(-3 * 3600))
        self.led.save()
        self.drive()
        args = cli._parser().parse_args(['drive', 'budget', '--job', 'J', 'max_cost_usd=130'])
        err = io.StringIO()
        with mock.patch.object(drive_activate, '_person', return_value=None), \
                mock.patch.object(drive_activate, 'kick'), contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(err):
            self.assertEqual(drive_activate.cmd_drive(self.cfg, args), 0)
        self.assertIn('not about budget', err.getvalue())
        self.assertIn('swarm run --job J --name lead', err.getvalue())
        args = cli._parser().parse_args(['run', '--job', 'J', '--name', 'lead', '--role', row.role,
                                        '--workdir', row.workdir, '--brief', row.brief])
        for caller in (None, person.Caller(None, 'requester:run')):
            self.reload()
            self.led.ruling_due_at = iso(self.now(-3 * 3600))
            self.led.save()
            with mock.patch.object(drive_cli, '_owner_roots', return_value=[str(self.work)]), \
                    mock.patch.object(drive_cli, 'TMP_ROOTS', ()), \
                    mock.patch.object(drive_cli, '_caller', return_value=caller), \
                    mock.patch('swarm.drivegate.kick'), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(drive_cli.cmd_run(self.b, self.cfg, args), 0)
            self.assertIsNone(self.reload().ruling_due_at)
