"""F03h: hand-offs cannot spend judge sessions on unchanged work."""
import datetime as dt
import time
import unittest

from drive_fixtures import L, T0, ho, intent, launch, snap, v
import test_drive_handoff as handoff
from test_drive_exec import ExecEnv
from swarm.supervisor import driver, driver_exec as ex, driver_model as dm, prompts

HEAD = 'a' * 40
ART = 'master@' + HEAD


class SameHeadModelTests(unittest.TestCase):
    def snapshot(self, **kw):
        base = dict(ledger=L(watermarks={'goal-fix': 77}),
                    launches={'lead': launch('lead', role='engineering_lead')},
                    handoffs=[ho(ART, by='lead', receipt_id=80, at=T0 + dt.timedelta(minutes=1))],
                    goal_verdict=v('not_met', scope='goal', candidate=HEAD[:7], receipt_id=77,
                                   reason='report stale', next='update the report'),
                    goal_ready={'id': 81, 'candidate': ART, 'req': 'r1',
                                'at': T0 + dt.timedelta(minutes=2)})
        return snap(**{**base, **kw})

    def test_same_sha_handoff_is_recorded_as_no_progress_before_pipeline_judge(self):
        a, = dm.next_actions(self.snapshot(), {'review_role': None})
        self.assertEqual(a.kind, 'no_progress')
        self.assertIn('already ruled not_met #77', a.reason)

    def test_goal_ready_same_sha_does_not_launch_goal_judge(self):
        s = self.snapshot(handoffs=[], ledger=L(watermarks={'goal-fix': 77}), launches={})
        acts = dm.next_actions(s, {'landing_required': False})
        self.assertFalse(any(a.kind == 'launch' for a in acts))

    def test_repeated_goal_ready_records_no_progress_then_holds(self):
        led = L(watermarks={'goal-fix': 77})
        s = self.snapshot(handoffs=[], ledger=led)
        a, = dm.next_actions(s, {'landing_required': False})
        self.assertEqual(a.kind, 'no_progress')
        led.watermarks.update({'no-progress:lead': 81, 'no-progress-verdict:lead': 77})
        s.goal_ready['id'] = 83
        self.assertEqual(dm.next_actions(s, {'landing_required': False})[0].prompt_kind, 'goal-hold')

    def test_new_handoff_supersedes_earlier_unchanged_handoffs(self):
        s = self.snapshot(handoffs=[ho(ART, by='lead', receipt_id=80),
                                   ho(ART, by='lead', receipt_id=82),
                                   ho('master@' + 'b' * 40, by='lead', receipt_id=84)])
        acts = dm.next_actions(s, {'review_role': None})
        self.assertEqual([(a.kind, a.prompt_kind, a.artifact) for a in acts],
                         [('launch', 'judge', 'master@' + 'b' * 40)])

    def test_queued_goal_judge_keeps_candidate_and_prompt(self):
        s = self.snapshot(goal_verdict=None, handoffs=[],
                          launches={'goal-judge': launch('goal-judge', 'queued', role='judge')})
        a, = dm.next_actions(s, {'landing_required': False})
        self.assertEqual((a.prompt_kind, a.artifact), ('goal', ART))

    def test_not_met_requeues_the_lead_only_once_per_verdict(self):
        s = self.snapshot(ledger=L(), handoffs=[])
        self.assertEqual(dm.next_actions(s, {})[0].kind, 'fix_lead')
        s.ledger.watermarks['goal-fix'] = 77
        self.assertNotEqual(dm.next_actions(s, {})[0].kind, 'fix_lead')

    def test_goal_not_met_without_ready_record_still_delivers_fix_round(self):
        s = self.snapshot(ledger=L(), handoffs=[], goal_ready=None)
        self.assertEqual(dm.next_actions(s, {})[0].kind, 'fix_lead')

    def test_unchanged_head_without_any_worker_escalates_instead_of_waiting_silently(self):
        s = self.snapshot(ledger=L(watermarks={'goal-fix': 77, 'no-progress:lead': 81}))
        a, = dm.next_actions(s, {})
        self.assertEqual((a.kind, a.prompt_kind), ('hold', 'goal-hold'))
        self.assertIn('needs a person', a.reason)
        self.assertIn('update the report', a.reason)

    def test_goal_not_met_same_head_cannot_spend_a_ruling_session(self):
        led = L(watermarks={'goal-fix': 77}, rounds_without_progress=10)
        s = self.snapshot(ledger=led, handoffs=[], goal_ready={'id': 70, 'candidate': HEAD, 'req': 'r1'})
        a, = dm.next_actions(s, {})
        self.assertEqual((a.kind, a.prompt_kind), ('hold', 'goal-hold'))

    def test_lost_goal_claim_does_not_consume_head_allowance(self):
        led = L(intents=[intent(launch='goal-judge', action_id='gen1|goal|-|goal|0|1',
                                launch_id='lost-claim', outcome='lost', charged=0)],
                watermarks={f'goal-head:1:{HEAD}:lost-claim': 1})
        s = self.snapshot(ledger=led, goal_verdict=None, handoffs=[], launches={})
        self.assertEqual(dm.next_actions(s, {'landing_required': False})[0].prompt_kind, 'goal')

    def test_queued_and_running_engineers_precede_judging_new_lead_handoff(self):
        for state in ('queued', 'running'):
            with self.subTest(state=state):
                s = self.snapshot(goal_verdict=None, handoffs=[ho(ART, by='lead')],
                                  launches={'lead': launch('lead', role='engineering_lead'),
                                            'r3-main': launch('r3-main', state)},
                                  live={'r3-main'} if state == 'running' else set())
                acts = dm.next_actions(s, {'review_role': None})
                self.assertFalse(any(a.prompt_kind in ('judge', 'goal') for a in acts))
                self.assertFalse(any(a.kind == 'stop_launch' for a in acts))

    def test_due_ruling_does_not_cancel_engineers_after_lead_handoff(self):
        for state in ('queued', 'running'):
            s = self.snapshot(goal_verdict=None,
                              ledger=L(rounds_without_progress=10,
                                       ruling_due_at=(T0 - dt.timedelta(hours=1)).isoformat()),
                              launches={'lead': launch('lead', role='engineering_lead'),
                                        'r3-main': launch('r3-main', state)},
                              live={'r3-main'} if state == 'running' else set())
            acts = dm.next_actions(s, {})
            self.assertFalse(any(a.ruling for a in acts))
            self.assertFalse(any(a.kind == 'stop_launch' for a in acts))

    def test_judge_restarts_wait_without_starving_engineers(self):
        for path in ('retry', 'recovery', 'queued', 'wake', 'stall'):
            for state in ('queued', 'running'):
                with self.subTest(path=path, state=state):
                    led = L(intents=[intent(launch='judge', action_id='gen1|judge|-|master|1|80',
                                            outcome='interrupted by driver restart' if path == 'recovery' else 'completed')],
                            watermarks={'verdict-missing:judge': 1} if path == 'retry' else {})
                    s = self.snapshot(goal_verdict=None, ledger=led,
                                      launches={'lead': launch('lead', role='engineering_lead'),
                                                'judge': launch('judge', 'done' if path == 'wake' else
                                                    'running' if path == 'stall' else 'queued', role='judge', id=1,
                                                    last_outcome='interrupted by driver restart' if path == 'recovery' else None),
                                                'r3-main': launch('r3-main', state, id=2)},
                                      live={'r3-main'} if state == 'running' else set(),
                                      wakes={'judge': [90]} if path == 'wake' else {})
                    acts = dm.next_actions(s, {'review_role': None, 'max_concurrent_sessions': 1})
                    self.assertFalse(any(a.launch == 'judge' or a.prompt_kind in ('judge', 'goal', 'ruling') for a in acts))
                    if state == 'queued':
                        self.assertEqual([a.launch for a in acts], ['r3-main'])

    def test_queued_artifact_judge_cannot_bypass_same_head_goal_rejection(self):
        for missing in (False, True):
            wm = {'goal-fix': 77, **({'verdict-missing:judge': 1} if missing else {})}
            s = self.snapshot(ledger=L(watermarks=wm, intents=[
                                  intent(launch='judge', action_id='gen1|judge|-|master|1|70', outcome='completed')]),
                              handoffs=[ho(ART, by='lead', receipt_id=70)],
                              goal_ready={'id': 71, 'candidate': HEAD, 'req': 'r1'},
                              launches={'judge': launch('judge', 'queued', role='judge')})
            a, = dm.next_actions(s, {})
            self.assertEqual((a.kind, a.prompt_kind), ('hold', 'goal-hold'))

    def test_goal_judge_caps_head_and_generation_including_retry_and_queued_paths(self):
        for prompt, launch_name in (('goal', 'goal-judge'), ('launch', 'goal-judge')):
            for count in (1, 6):
                with self.subTest(prompt=prompt, count=count):
                    prior = [intent(launch='goal-judge', action_id=f'gen1|goal|-|goal|0|{n}',
                                    charged=0) for n in range(count)]
                    wm = {f'goal-head:1:{HEAD}:L0': 1} if count == 1 else {}
                    s = self.snapshot(goal_verdict=None, handoffs=[], ledger=L(intents=prior, watermarks=wm),
                                      launches={} if prompt == 'goal' else {
                                          launch_name: launch(launch_name, 'queued', role='judge')})
                    a, = dm.next_actions(s, {'landing_required': False})
                    self.assertEqual((a.kind, a.prompt_kind), ('hold', 'goal-hold'))

    def test_new_head_and_new_generation_are_eligible(self):
        for generation, candidate in ((1, 'b' * 40), (2, HEAD)):
            led = L(generation=generation, watermarks={f'goal-head:1:{HEAD}:L0': 1},
                    intents=[intent(launch='goal-judge', action_id='gen1|goal|-|goal|0|1', charged=0)])
            s = snap(generation=generation, ledger=led,
                     goal_ready={'id': 2, 'candidate': candidate, 'req': 'r1', 'at': T0})
            self.assertEqual(dm.next_actions(s, {'landing_required': False})[0].prompt_kind, 'goal')

    def test_session_cap_counts_current_generation_only(self):
        from swarm.drive_activate import budget_line
        led = L(generation=2, budgets={'max_sessions': 1},
                intents=[intent(action_id='gen1|launch|a|-|0|1', charged=0)])
        s = snap(generation=2, ledger=led, launches={'lead': launch('lead', 'queued', generation=2)})
        self.assertEqual(dm._Ctx(s, {}).sessions(), 0)
        self.assertIsNone(dm.reservation_reason({**dm.DEFAULT_CFG, **led.budgets}, led, False))
        self.assertEqual(dm.next_actions(s, {})[0].kind, 'launch')
        self.assertIn('sessions 0/1', budget_line(led))


class SameHeadExecTests(ExecEnv):
    joined = handoff.HandoffTests.joined
    settle = handoff.HandoffTests.settle

    def act_on(self, s):
        acts = dm.next_actions(s, {'review_role': None})
        driver._act(self.b, self.cfg, self.sup, self.led, s, acts, self.now(), None, '',
                    time.monotonic() + 10, driver._modules())
        return acts

    def test_two_unchanged_handoffs_notify_and_hold_until_person_or_requeue(self):
        self.get_launch('lead', role='engineering_lead')
        for n in range(85):
            self.b.post('J', 'engineer', f'work evidence {n}')
        self.led.watermarks['goal-fix'] = 77
        self.led.save()
        s = SameHeadModelTests().snapshot(ledger=self.led)
        s.launches['r3-main'] = launch('r3-main', 'running')
        s.live.add('r3-main')
        self.act_on(s)
        self.act_on(s)
        posts = [m for m in self.b.messages_after(0, 'J') if m.message.startswith('DRIVER-NO-PROGRESS')]
        self.assertEqual(len(posts), 1)
        self.assertEqual(self.led.watermarks['no-progress:lead'], 81)
        s.handoffs.append(ho(ART, by='lead', receipt_id=82))
        a, = self.act_on(s)
        self.assertEqual(a.prompt_kind, 'goal-hold')
        self.assertTrue(any('lead hands off an unchanged head; needs a person: update the report' in e.text
                            for e in self.b.pending_events('J')))
        self.assertEqual(self.b.job_status('J').status, 'active')
        s.live.clear()
        s.launches['r3-main'] = launch('r3-main', 'queued')
        self.assertEqual(dm.next_actions(s, {})[0].kind, 'hold')
        self.b.post('J', 'engineer', 'try again')
        ex.refresh_goal_hold(self.b, self.led)
        self.assertIn('goal-hold', self.led.watermarks)
        self.b.post('J', 'owner', 'I will supply the missing decision')
        ex.refresh_goal_hold(self.b, self.led)
        self.assertNotIn('goal-hold', self.led.watermarks)
        self.assertEqual(dm.next_actions(s, {})[0].launch, 'r3-main')

    def test_goal_head_is_bound_before_claim(self):
        run = self.go(self.act(None, kind='goal', group='goal', artifact=ART))
        self.assertEqual(self.reload().watermarks[f"goal-head:1:{HEAD}:{run['launch_id']}"], 1)

    def test_explicit_requeue_releases_hold_and_notifications_have_deadline(self):
        from swarm.supervisor import session_driver
        self.get_launch('lead', role='engineering_lead')
        ex.hold_goal(self.b, self.led, self.now(), 'unchanged head; needs a person')
        ex.hold_goal(self.b, self.led, self.now(301), 'unchanged head; needs a person')
        self.assertEqual(len([e for e in self.b.pending_events('J') if 'DRIVER-ESCALATE' in e.text]), 2)
        self.assertEqual(len(session_driver.ledger_deadlines(self.led, {})), 1)
        self.led.watermarks['requeue:1:lead'] = 1
        self.led.save()
        ex.refresh_goal_hold(self.b, self.led)
        self.assertNotIn('goal-hold', self.led.watermarks)

    def test_unchanged_handoff_does_not_move_progress_clock(self):
        self.led.last_progress_at = T0.isoformat()
        self.led.watermarks['p.verdict'] = 77
        self.led.save()
        s = SameHeadModelTests().snapshot(ledger=self.led)
        ex.settle_progress(self.led, s, T0 + dt.timedelta(minutes=5))
        self.assertEqual(self.led.last_progress_at, T0.isoformat())

    def test_lead_handoff_keeps_own_running_and_queued_engineers(self):
        self.get_launch('lead', role='engineering_lead')
        lead = self.go(self.act('lead'))
        name = self.joined(lead)
        self.get_launch('r3-main')
        eng = self.go(self.act('r3-main'))
        self.get_launch('skill-r6')
        self.cli('done', '--job', 'J', '--as', name, '--artifact', ART)
        self.assertEqual(self.settle(lead).state, 'done')
        self.assertEqual(self.b.get_launch('J', 1, 'r3-main').state, 'running')
        self.assertIsNone(self.row_of(eng).ended_at)
        self.assertEqual(self.b.get_launch('J', 1, 'skill-r6').state, 'queued')

    def test_lead_prompt_requires_new_sha_or_named_blocker(self):
        row = self.get_launch('lead', role='engineering_lead')
        text = prompts.render(self.b, self.cfg, self.led, self.act('lead'), row)
        self.assertIn("Hand off a NEW sha only when the judge's open items are addressed.", text)
        self.assertIn('post BLOCKED naming what you need and end your run', text)

    def test_goal_prompt_accepts_a_branch_qualified_candidate(self):
        row = self.get_launch('goal-judge', role='judge')
        text = prompts.render(self.b, self.cfg, self.led,
                              self.act(None, kind='goal', group='goal', artifact=ART), row)
        self.assertIn('--scope goal --candidate ' + HEAD, text)

    def test_named_person_blocker_escalates_without_retrying_the_lead(self):
        self.get_launch('lead', role='engineering_lead')
        run = self.go(self.act('lead'))
        self.b.post('J', self.joined(run), 'BLOCKED owner must approve the missing launch')
        self.settle(run)
        self.assertIn('goal-hold', self.led.watermarks)
        from test_drive_handoff import HandoffTests
        s = HandoffTests.view(self, self.now(301))
        self.assertEqual(dm.next_actions(s, {})[0].kind, 'hold')
