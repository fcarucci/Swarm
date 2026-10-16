"""F03h: hand-offs cannot spend judge sessions on unchanged work."""
import datetime as dt
import time
import unittest
from dataclasses import replace

from drive_fixtures import L, T0, ho, intent, launch, snap, v
import test_drive_handoff as handoff
from test_drive_exec import ExecEnv
from swarm.supervisor import driver, driver_exec as ex, driver_model as dm, prompts, runner

HEAD = 'a' * 40
ART = 'master@' + HEAD


class SameHeadModelTests(unittest.TestCase):
    def test_fix_round_uses_open_engineer_or_lead_never_read_only_owner(self):
        for owner, role, state, target in (
                ('judge', 'judge', 'done', 'lead'),
                ('reviewer', 'reviewer', 'done', 'lead'),
                ('security', 'security', 'done', 'lead'),
                ('eng', 'engineer', 'done', 'eng'),
                ('eng', 'engineer', 'failed', 'lead'),
                ('missing', None, None, 'lead')):
            with self.subTest(owner=owner, state=state):
                rows = {'lead': launch('lead', role='engineering_lead')}
                if role:
                    rows[owner] = launch(owner, role=role, state=state)
                s = snap(ledger=L(owners={'master': owner}), launches=rows,
                         handoffs=[ho(ART, by='judge')],
                         verdicts={ART: v('not_met', artifact=ART, receipt_id=77)})
                a, = dm.next_actions(s, {'review_role': None})
                self.assertEqual((a.prompt_kind, a.launch), ('fix', target))

    def test_every_judge_action_kind_obeys_the_head_cap(self):
        for name, kind in (('judge', 'fix'), ('judge', 'verdict-retry'),
                           ('ruling-judge', 'ruling'), ('goal-judge', 'goal')):
            with self.subTest(name=name, kind=kind):
                prior = intent(launch=name, action_id=f'gen1|{kind}|{name}|master|1|70')
                s = snap(ledger=L(intents=[prior], watermarks={f'goal-head:1:{HEAD}:L0': 1}),
                         handoffs=[ho(ART, by='eng', receipt_id=80)],
                         launches={name: launch(name, role='judge')})
                a, = dm.next_actions(s, {'review_role': None})
                self.assertEqual((a.kind, a.prompt_kind), ('hold', 'goal-hold'))

    def test_recovery_of_a_legacy_fix_judge_targets_the_lead(self):
        s = snap(ledger=L(intents=[intent(launch='judge', action_id='gen1|fix|judge|master|1|77',
                                         outcome='driver-error')]),
                 launches={'lead': launch('lead', role='engineering_lead'),
                           'judge': launch('judge', 'queued', role='judge', last_outcome='driver-error')},
                 handoffs=[ho(ART, by='judge')])
        a, = dm.next_actions(s, {})
        self.assertEqual((a.kind, a.prompt_kind, a.launch, a.responsible), ('fix_lead', 'fix', 'lead', 'judge'))

    def test_one_pass_admits_only_one_judge_per_head(self):
        s = snap(launches={name: launch(name, 'queued', role='judge') for name in ('audit-a', 'audit-b')},
                 goal_ready={'id': 70, 'candidate': ART, 'req': 'r1', 'at': T0})
        acts = dm.next_actions(s, {'max_concurrent_sessions': 2})
        self.assertEqual(len([a for a in acts if a.kind == 'launch']), 1)

    def test_distinct_artifact_judgments_do_not_consume_the_generation_goal_cap(self):
        prior = [intent(launch='judge', launch_id=f'L{n}', action_id=f'gen1|judge|-|master|1|{n}')
                 for n in range(6)]
        wm = {f'goal-head:1:{n:040x}:L{n}': 1 for n in range(6)}
        s = snap(ledger=L(intents=prior, watermarks=wm),
                 goal_ready={'id': 70, 'candidate': ART, 'req': 'r1', 'at': T0})
        a, = dm.next_actions(s, {'landing_required': False})
        self.assertEqual((a.kind, a.prompt_kind), ('launch', 'goal'))

    def test_not_met_fix_then_same_sha_cannot_launch_a_second_judge(self):
        s = snap(launches={'lead': launch('lead', role='engineering_lead'),
                          'judge': launch('judge', role='judge')},
                 ledger=L(owners={'master': 'judge'}),
                 handoffs=[ho(ART, by='judge', receipt_id=70)],
                 verdicts={ART: v('not_met', artifact=ART, receipt_id=77)})
        a, = dm.next_actions(s, {'review_role': None})
        self.assertEqual((a.prompt_kind, a.launch), ('fix', 'lead'))
        s = replace(s, goal_verdict=v('not_met', scope='goal', candidate=HEAD, receipt_id=77))
        s.ledger.watermarks['goal-fix'] = 77
        s.handoffs.append(ho(ART, by='lead', receipt_id=80))
        a, = dm.next_actions(s, {'review_role': None})
        self.assertEqual(a.kind, 'no_progress')

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
        self.assertIn('waiting for', a.reason)
        self.assertNotIn('needs a person', a.reason)
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
                    if count == 1:   # a judged head waits for a new head (never a person)
                        self.assertEqual((a.kind, a.prompt_kind), ('hold', 'goal-hold'))
                        self.assertNotIn('needs a person', a.reason)
                    else:            # slice 10: no per-generation judge cap; a new head is judged
                        self.assertEqual((a.kind, a.prompt_kind), ('launch', 'goal'))

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

    def test_legacy_fix_judge_transfer_retires_the_recovery_source(self):
        self.get_launch('lead', role='engineering_lead')
        self.get_launch('judge', role='judge')
        run = self.go(self.act('judge', kind='fix', group='master', artifact=ART))
        run['refused_reason'] = "the launch's directory cannot be prepared safely: .cache has mode 0o755"
        runner.write_run(run)
        self.assertTrue(runner.finish(self.cfg, run, 'refused'))
        self.reload()
        s = snap(ledger=self.led, handoffs=[ho(ART, by='judge')],
                 launches={'lead': launch('lead', role='engineering_lead'),
                           'judge': launch('judge', 'queued', role='judge', last_outcome='driver-error')})
        a, = self.act_on(s)
        self.assertEqual(a.kind, 'fix_lead')
        # A runner can leave its file behind while finish/cleanup is being retried.
        runner.write_run(run)
        ex._settle_launches(self.b, self.cfg, self.led, {run['launch_id']: self.row_of(run)}, self.now())
        self.assertEqual(self.b.get_launch('J', 1, 'judge').state, 'done')
        self.assertEqual(self.b.get_launch('J', 1, 'lead').state, 'queued')
        self.get_launch('eng')
        s = replace(s, handoffs=[], launches={
            'judge': launch('judge', role='judge', last_outcome='fix transferred to lead'),
            'lead': launch('lead', role='engineering_lead'), 'eng': launch('eng', 'queued')})
        acts = dm.next_actions(s, {})
        self.assertEqual([a.launch for a in acts], ['eng'])

    def test_fix_lead_execution_redirects_read_only_targets_for_goal_and_ruling(self):
        self.get_launch('lead', role='engineering_lead')
        self.get_launch('judge', role='judge')
        for scope in ('goal', 'ruling'):
            s = snap(ledger=self.led, goal_verdict=v('not_met', scope=scope, receipt_id=77))
            a = dm.Action(kind='fix_lead', action_id='gen1|fix|judge|-|0|77',
                          launch='judge', prompt_kind='fix')
            driver._act(self.b, self.cfg, self.sup, self.led, s, [a], self.now(), None, '',
                        time.monotonic() + 10, driver._modules())
            self.assertEqual(self.led.watermarks[f'{scope}-fix'], 77)
        posts = [m for m in self.b.messages_after(0, 'J') if 'not_met:' in m.message]
        self.assertEqual([m.to_agent for m in posts], ['lead', 'lead'])

    def test_artifact_fix_without_owner_creates_a_lead(self):
        s = snap(ledger=self.led, handoffs=[ho(ART, by='judge')],
                 verdicts={ART: v('not_met', artifact=ART, receipt_id=77)})
        a, = self.act_on(s)
        self.assertEqual(a.kind, 'fix_lead')
        self.assertEqual(self.b.get_launch('J', 1, 'lead').role, 'engineering_lead')

    def test_fix_judge_and_ruling_bind_the_head_before_claim(self):
        for kind, name in (('fix', 'judge'), ('ruling', 'ruling-judge')):
            with self.subTest(kind=kind):
                self.get_launch(name, role='judge')
                run = self.go(self.act(name, kind=kind, group='master', artifact=ART,
                                       ruling=kind == 'ruling'))
                self.assertEqual(self.reload().watermarks[f"goal-head:1:{HEAD}:{run['launch_id']}"], 1)

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
        # slice 10: an unchanged head is a scheduled wait for the lead or the coordinator, never a person hold
        self.assertEqual(a.prompt_kind, 'goal-hold')
        self.assertIn('update the report', a.reason)
        self.assertNotIn('needs a person', a.reason)
        self.assertGreater(a.not_before, s.now)
        self.assertNotIn('goal-hold', self.led.watermarks)
        self.assertEqual(self.b.job_status('J').status, 'active')
        # a queued launch (a person's swarm run, an engineer) is never blocked by the wait
        s.live.clear()
        s.launches['r3-main'] = launch('r3-main', 'queued')
        self.assertEqual((dm.next_actions(s, {})[0].kind, dm.next_actions(s, {})[0].launch), ('launch', 'r3-main'))

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


class NoPersonParkingTests(SameHeadModelTests):
    """Slices 9-11 (2026-10-16): a judged head never parks the job on a person."""

    def test_unknown_head_is_never_the_same_head(self):
        self.assertFalse(dm.same_head('unknown', 'unknown'))
        self.assertFalse(dm.same_head('runbook-0.22@unknown', 'runbook-0.22@unknown'))
        self.assertFalse(dm.same_head('main@abc', 'main@abc'))
        self.assertTrue(dm.same_head(ART, ART))

    def test_goal_wait_has_backoff_and_no_person(self):
        s = self.snapshot(ledger=L(watermarks={'goal-fix': 77, 'no-progress:lead': 81}, rounds_without_progress=2))
        a, = dm.next_actions(s, {})
        self.assertEqual((a.kind, a.prompt_kind), ('hold', 'goal-hold'))
        self.assertNotIn('person', a.reason)
        self.assertEqual(a.not_before, s.now + dt.timedelta(seconds=dm.GOAL_WAIT_MIN_SECONDS * 3))

    def test_queued_engineer_runs_while_the_judged_head_waits(self):
        s = self.snapshot(ledger=L(watermarks={'goal-fix': 77, 'no-progress:lead': 81}),
                          launches={'lead': launch('lead', role='engineering_lead'),
                                    'eng-fix': launch('eng-fix', 'queued', role='engineer')})
        acts = dm.next_actions(s, {})
        self.assertEqual([(a.kind, a.launch) for a in acts], [('launch', 'eng-fix')])

    def test_queued_lead_requeued_by_a_person_runs_while_the_judged_head_waits(self):
        s = self.snapshot(ledger=L(watermarks={'goal-fix': 77, 'no-progress:lead': 81}),
                          launches={'lead': launch('lead', 'queued', role='engineering_lead')})
        acts = dm.next_actions(s, {})
        self.assertTrue(any(a.kind == 'launch' and a.launch == 'lead' for a in acts), acts)

    def test_legacy_goal_hold_watermark_does_not_block_a_queued_launch(self):
        s = self.snapshot(ledger=L(watermarks={'goal-hold': 1, 'goal-fix': 77}),
                          launches={'lead': launch('lead', role='engineering_lead'),
                                    'integrate-all': launch('integrate-all', 'queued', role='engineer')})
        acts = dm.next_actions(s, {})
        self.assertIn(('launch', 'integrate-all'), [(a.kind, a.launch) for a in acts])
        self.assertFalse(any(a.kind == 'hold' for a in acts))

    def test_named_person_hold_still_waits_on_the_person(self):
        s = self.snapshot(ledger=L(watermarks={'goal-hold': 1, 'goal-hold-person': 1}),
                          launches={'lead': launch('lead', role='engineering_lead'),
                                    'eng': launch('eng', 'queued', role='engineer')})
        a, = dm.next_actions(s, {})
        self.assertEqual((a.kind, a.prompt_kind), ('hold', 'goal-hold'))
        self.assertIn('named person', a.reason)
