"""F03l: terminal evidence and changed heads release holds; every wait is visible."""
from dataclasses import replace
from unittest import mock

from drive_fixtures import L, ho, launch, q, snap, v
from test_drive_exec import ExecEnv
from swarm import cli, drive_activate
from swarm.supervisor import driver, driver_exec as ex, driver_model as dm, driver_snapshot

HEAD = 'a' * 40
ART = 'main@' + HEAD


class HoldTests(ExecEnv):
    def test_met_launches_learner_then_closes_past_every_hold_and_budget(self):
        for scope in ('goal', 'ruling'):
            with self.subTest(scope=scope):
                led = L(watermarks={'goal-hold': 1, 'ruling-hold': 1, 'budget-hold': 1})
                s = snap(ledger=led, goal_verdict=v('met', scope=scope, candidate=ART), learn_pending=True)
                actions = dm.next_actions(s, {'landing_required': False, 'max_sessions': 0})
                self.assertEqual([(a.kind, a.prompt_kind) for a in actions], [('launch', 'learn')])
                self.assertEqual(dm.next_actions(replace(s, learn_pending=False),
                                                {'landing_required': False})[0].kind, 'close')

    def test_verified_met_clears_holds_even_if_progress_already_seen(self):
        for rid, scope in enumerate(('goal', 'ruling'), 77):
            ex.hold_goal(self.b, self.led, self.now(), 'unchanged head', ART)
            ex.hold_budget(self.b, self.led, ex._dcfg(self.cfg, self.led), self.now(), 'cost exhausted')
            ex.hold_ruling(self.b, self.led, self.now(), 'deadline passed')
            self.led.watermarks['p.verdict'] = rid
            self.led.save()
            s = snap(ledger=self.led, goal_verdict=v('met', scope=scope, candidate=ART, receipt_id=rid))
            with mock.patch.object(driver_snapshot, 'read', return_value=s), \
                    mock.patch.object(driver, '_with_learn', side_effect=lambda b, c, l, s: s):
                driver._plan(self.b, self.cfg, self.led, {'landing_required': False}, self.now(), driver._modules())
            for kind in ('goal-hold', 'ruling-hold', 'budget-hold'):
                self.assertNotIn(kind, self.reload().watermarks)
                self.assertFalse(self.b.job_data('J').get('drive.' + kind))

    def test_met_cleanup_retries_board_mirrors_after_a_failed_write(self):
        ex.hold_goal(self.b, self.led, self.now(), 'unchanged head', ART)
        s = snap(ledger=self.led, goal_verdict=v('met', scope='goal', candidate=ART, receipt_id=77))
        write = self.b.set_job_data
        def fail_clear(job, key, value):
            if key == 'drive.goal-hold' and value is None:
                raise RuntimeError('board unavailable')
            return write(job, key, value)
        with mock.patch.object(self.b, 'set_job_data', side_effect=fail_clear):
            with self.assertRaises(RuntimeError):
                ex.clear_met_holds(self.b, self.led, s)
        ex.clear_met_holds(self.b, self.led, s)
        self.assertFalse(self.b.job_data('J').get('drive.goal-hold'))

    def test_old_or_unreceipted_met_does_not_clear_holds(self):
        for overrides in ({'generation': 0}, {'receipt_id': None}):
            ex.hold_goal(self.b, self.led, self.now(), 'unchanged head', ART)
            s = snap(ledger=self.led, goal_verdict=v('met', scope='goal', candidate=ART, **overrides))
            ex.clear_met_holds(self.b, self.led, s)
            self.assertIn('goal-hold', self.reload().watermarks)

    def test_budget_wait_explanation_does_not_repeat_each_tick(self):
        s = snap(ledger=self.led)
        for seconds in (0, 1, 301):
            later = replace(s, now=self.now(seconds))
            action = dm.budget_hold(dm._Ctx(later, {}), 'cost exhausted')
            ex.note_waiting(self.b, self.led, later, [action])
        posts = [m for m in self.b.messages_after(0, 'J') if m.message.startswith('Waiting:')]
        self.assertEqual(len(posts), 1)

    def test_funded_met_launches_the_learner_after_stale_hold(self):
        import time
        ex.hold_goal(self.b, self.led, self.now(), 'unchanged head', ART)
        s = snap(ledger=self.led, goal_verdict=v('met', scope='goal', candidate=ART, receipt_id=77),
                 learn_pending=True)
        with mock.patch.object(driver_snapshot, 'read', return_value=s), \
                mock.patch.object(driver, '_with_learn', side_effect=lambda b, c, l, s: s):
            _, actions = driver._plan(self.b, self.cfg, self.led, {'landing_required': False},
                                     self.now(), driver._modules())
        driver._act(self.b, self.cfg, self.sup, self.led, s, actions, self.now(),
                    lambda c, r: self.launched.append(r), '', time.monotonic() + 10, driver._modules())
        self.assertEqual([r['launch'] for r in self.launched], ['learner'])

    def test_running_learner_is_shown_as_a_learnings_wait(self):
        s = snap(ledger=self.led, goal_verdict=v('met', scope='goal', candidate=ART),
                 learn_pending=True, live={'learner'})
        ex.note_waiting(self.b, self.led, s, dm.next_actions(s, {'landing_required': False}))
        lines = '\n'.join(drive_activate.drive_lines(self.cfg, 'J'))
        self.assertIn('waiting on: learner (learn.hold: learnings step still running', lines)
        self.assertIn('swarm learn', lines)

    def test_new_engineer_handoff_releases_hold_before_lead_goal_ready(self):
        ex.hold_goal(self.b, self.led, self.now(), 'unchanged head', ART)
        s = snap(ledger=self.led, handoffs=[ho('main@' + 'b' * 40, by='eng', receipt_id=80)],
                 goal_ready={'id': 70, 'candidate': ART})
        ex.refresh_goal_hold(self.b, self.led, s)
        self.assertNotIn('goal-hold', self.reload().watermarks)

    def test_goal_hold_writer_publishes_head_and_receipt(self):
        ex.hold_goal(self.b, self.led, self.now(), 'unchanged head', ART)
        s = snap(ledger=self.led, handoffs=[ho(ART, by='lead')])
        ex.refresh_goal_hold(self.b, self.led, s)
        lines = '\n'.join(drive_activate.drive_lines(self.cfg, 'J'))
        self.assertIn('goal-hold: no progress: the lead handed off the same head ' + HEAD, lines)
        self.assertIn('see #' + str(self.led.watermarks['goal-hold-msg']), lines)
        self.assertEqual(cli._job_status_word(self.b, self.b.job_status('J'), self.now()), 'waiting')
        self.assertIn('goal-hold', self.reload().watermarks)

    def test_new_handoff_head_releases_goal_hold_but_keeps_judge_cap(self):
        ex.hold_goal(self.b, self.led, self.now(), 'unchanged head', ART)
        new = 'main@' + 'b' * 40
        s = snap(ledger=self.led, handoffs=[ho(new, by='lead', receipt_id=80)],
                 goal_ready={'id': 80, 'candidate': new},
                 goal_verdict=v('not_met', scope='goal', candidate=ART, receipt_id=77),
                 launches={'lead': launch('lead', role='engineering_lead')})
        with mock.patch.object(driver_snapshot, 'read', return_value=s):
            _, actions = driver._plan(self.b, self.cfg, self.led, {'landing_required': False, 'review_role': None},
                                     self.now(), driver._modules())
        self.assertNotIn('goal-hold', self.reload().watermarks)
        self.assertEqual((actions[0].kind, actions[0].prompt_kind, actions[0].artifact), ('launch', 'judge', new))
        self.led.watermarks['goal-head:1:' + 'b' * 40 + ':L0'] = 1
        s = replace(s, ledger=self.led)
        self.assertEqual(dm.next_actions(s, {'landing_required': False, 'review_role': None})[0].prompt_kind, 'goal-hold')

    def test_every_hold_has_reason_receipt_remedy_and_waiting_status(self):
        for kind, prompt, reason, remedy in (
                ('goal-hold', 'goal-hold', 'same head', 'swarm run'),
                ('ruling-hold', 'ruling-hold', 'deadline passed', 'swarm run'),
                ('budget-hold', 'budget', 'cost exhausted', 'swarm drive budget'),
                ('verdict-retry', None, 'verdict-retry-backoff', 'retry'),
                ('question-gated', 'question', 'question-gated', 'answer'),
                ('learn.hold', 'learn', 'learnings step failed', 'swarm learn')):
            with self.subTest(kind=kind):
                s = snap(ledger=self.led, questions={'lead': [q()]})
                a = dm.Action(kind='hold', launch=None, action_id='gen1|hold|-|-|0|1',
                              prompt_kind=prompt, reason=reason)
                ex.note_waiting(self.b, self.led, s, [a])
                lines = '\n'.join(drive_activate.drive_lines(self.cfg, 'J'))
                self.assertIn('waiting on:', lines)
                self.assertIn(kind, lines)
                self.assertIn(reason, lines)
                self.assertIn('see #', lines)
                self.assertIn(remedy, lines)
                self.assertTrue(cli._job_status_word(self.b, self.b.job_status('J'), self.now()).startswith('waiting'))
        ex.note_waiting(self.b, self.led, s, [])
        self.assertFalse(self.b.job_data('J').get('drive.waiting'))
