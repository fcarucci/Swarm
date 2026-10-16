"""F03f: limits ask a person without spending sessions on an unchanged head."""
import datetime as dt
import unittest

from drive_fixtures import L, T0, intent, iso, launch, snap, v
from test_drive_exec import ExecEnv
from swarm import cli, drive_activate, enrolment
from swarm.supervisor import driver, driver_exec as ex, driver_model as dm, prompts, session_driver


HEAD = 'a' * 40


class BudgetModelTests(unittest.TestCase):
    def test_cost_session_and_wall_caps_hold_and_resume_when_raised(self):
        for budgets, led_kw, raised in (
                ({'max_cost_usd': 40}, {'spent_usd': 35}, {'max_cost_usd': 45}),
                ({'max_sessions': 1}, {'intents': [intent(state='concluded', charged=0)]}, {'max_sessions': 2}),
                ({'max_wall_hours': 1}, {'activated_at': iso(T0.replace(hour=10))}, {'max_wall_hours': 3})):
            with self.subTest(budgets=budgets):
                led = L(budgets=budgets, **led_kw)
                s = snap(ledger=led, launches={'lead': launch('lead', 'queued')})
                before = len(led.intents)
                for _ in range(3):
                    (a,) = dm.next_actions(s, {})
                    self.assertEqual((a.kind, a.prompt_kind, a.ruling), ('hold', 'budget', False))
                self.assertEqual(len(led.intents), before)
                led.budgets.update(raised)
                self.assertEqual(dm.next_actions(s, {})[0].kind, 'launch')

    def test_same_head_gives_queued_lead_its_fix_round(self):
        vd = v('not_met', scope='ruling', candidate=HEAD[:7], receipt_id=77)
        for outcome in (None, 'interrupted by driver restart'):
            led = L(watermarks={'ruling-fix': 77}, rounds_without_progress=3)
            s = snap(ledger=led, goal_ready={'candidate': HEAD}, goal_verdict=vd,
                     launches={'lead': launch('lead', 'queued', last_outcome=outcome),
                               'ruling-judge': launch('ruling-judge', 'queued')})
            actions = dm.next_actions(s, {})
            self.assertEqual([a.launch for a in actions], ['lead'])
            self.assertFalse(any(a.ruling for a in actions))
            led.budgets['max_cost_usd'] = 0
            self.assertEqual(dm.next_actions(s, {})[0].prompt_kind, 'budget')

    def test_same_head_cannot_be_ruled_again_after_fix_round(self):
        led = L(watermarks={'ruling-fix': 77}, rounds_without_progress=3)
        s = snap(ledger=led, goal_ready={'candidate': HEAD},
                 goal_verdict=v('not_met', scope='ruling', candidate=HEAD, receipt_id=77))
        (a,) = dm.next_actions(s, {})
        self.assertEqual(a.kind, 'hold')
        self.assertIn(f"head {HEAD} already ruled not_met #77", a.reason)
        s = snap(ledger=led, goal_ready={"candidate": HEAD}, goal_verdict=s.goal_verdict,
                 launches={"lead": launch("lead", "done")})
        self.assertEqual(dm.next_actions(s, {})[0].prompt_kind, "ruling-hold")

    def test_head_and_generation_ruling_caps(self):
        for wm, prior in (({f'ruling-head:1:{HEAD}': 1}, []), ({}, [
                intent(launch='ruling-judge', action_id=f'gen1|ruling|-|ruling|{n}|due',
                       state='concluded', charged=0) for n in range(3)])):
            s = snap(ledger=L(watermarks=wm, intents=prior), goal_ready={'candidate': HEAD})
            (a,) = dm.next_actions(s, {})
            self.assertEqual((a.kind, a.prompt_kind), ('hold', 'goal-hold' if wm else 'ruling-hold'))
        s = snap(ledger=L(watermarks={f'ruling-head:1:{HEAD}': 1}), goal_ready={'candidate': 'b' * 40})
        (a,) = dm.next_actions(s, {})
        self.assertEqual((a.kind, a.artifact), ('launch', 'b' * 40))


class BudgetHoldTests(ExecEnv):
    def test_hold_posts_once_repeats_notifications_and_status_explains_recovery(self):
        self.get_launch('lead', role='engineering_lead')
        self.led.budgets['max_cost_usd'] = 5
        self.led.save()
        now = self.now()
        for seconds in (0, 1, 301):
            self.assertIsNone(self.go(self.act('lead'), now=now + dt.timedelta(seconds=seconds)))
        self.assertEqual(self.led.intents, [])
        self.assertEqual(self.b.get_launch('J', 1, 'lead').attempts, 0)
        posts = [m for m in self.b.messages_after(0, 'J') if m.message.startswith('DRIVER-ESCALATE budget:')]
        self.assertEqual(len(posts), 1)
        events = [e for e in self.b.pending_events('J') if e.kind == 'launch-failed']
        self.assertEqual(len(events), 2)
        self.assertEqual(cli._job_status_word(self.b, self.b.job_status('J'), now), 'waiting on a person (budget)')
        lines = '\n'.join(drive_activate.drive_lines(self.cfg, 'J'))
        self.assertIn('waiting on: person (budget: cost 0.00/5.00 USD; raise with swarm drive budget --job J max_cost_usd=N; see #', lines)
        self.assertEqual(session_driver.ledger_deadlines(self.led, {}), [
            dt.datetime.fromtimestamp(self.led.watermarks['budget-notify-at'], dt.timezone.utc)])
        self.led.budgets['max_cost_usd'] = 20
        self.led.save()
        self.b.set_job_data("J", "drive.enabled", "1")
        enrolment.write_job_owner(self.bk, job="J", harness="claude", session_id="s", cwd=str(self.work),
                                  roots=[str(self.work)], goal="G", brief=str(self.brief))
        driver.drive(self.b, self.cfg, self.sup, {}, now=self.now(), say=lambda s: None,
                     scope_ok=lambda: True, start_runner=lambda c, r: self.launched.append(r))
        self.assertEqual(len(self.launched), 1)
        self.assertNotIn('budget-hold', self.reload().watermarks)
        self.assertIsNone(self.b.job_data('J').get('drive.budget-hold'))

    def test_ruling_head_is_bound_before_the_claim(self):
        action = self.act(launch=None, kind="ruling", group="ruling", ruling=True, artifact=HEAD)
        from unittest import mock
        real = self.b.claim_drive_launch
        def claim(**kw):
            self.assertEqual(self.reload().watermarks[f"ruling-head:1:{HEAD}:{kw['launch_id']}"], 1)
            return real(**kw)
        with mock.patch.object(self.b, "claim_drive_launch", side_effect=claim):
            self.assertIsNotNone(self.go(action))

    def test_budget_ruling_prompt_names_blocker_and_effective_limits(self):
        action = self.act(launch=None, kind='ruling', group='ruling', ruling=True,
                          artifact=HEAD, reason='budget-cost')
        row = self.get_launch('ruling-judge', role='judge')
        cfg = {**self.cfg, 'drive': {**self.cfg.get('drive', {}), 'max_cost_usd': 80}}
        self.led.budgets.pop('max_cost_usd', None)
        text = prompts.render(self.b, cfg, self.led, action, row)
        self.assertIn('This ruling is due to budget exhaustion', text)
        self.assertIn('not_met with budget as the blocker', text)
        self.assertIn('80.00 USD', text)
        self.assertIn('never record unreachable for a budget or limit', text)
