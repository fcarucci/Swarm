"""Rulings wait for productive launches and disclose partial work before stopping."""
import datetime as dt
import subprocess
import unittest
from types import SimpleNamespace as NS
from dataclasses import replace
from unittest import mock

from drive_fixtures import L, T0, iso, launch, snap
from test_drive_exec import ExecEnv
from swarm.supervisor import driver, driver_exec as ex, driver_model as dm, driver_snapshot as ds


class RulingWorkModelTests(unittest.TestCase):
    def snapshot(self, **kw):
        return snap(**dict(ledger=L(ruling_due_at=iso(T0 - dt.timedelta(hours=1)),
                                   rounds_without_progress=3),
                           launches={'eng': launch('eng', 'running')}, live={'eng'}, **kw))

    def test_recent_post_or_commit_keeps_engineer_and_lead_working(self):
        for role in ('engineer', 'engineering_lead', 'reviewer'):
            for age in (0, 15):
                s = self.snapshot()
                s.launches['eng'] = launch('eng', 'running', role=role,
                                           updated_at=T0 - dt.timedelta(hours=1))
                s = replace(s, launch_progress={'eng': T0 - dt.timedelta(minutes=age)})
                actions = dm.next_actions(s, {})
                self.assertEqual([(a.kind, a.prompt_kind) for a in actions], [('hold', 'ruling-pending')])
                self.assertIn('ruling pending: 1 launches still working', actions[0].reason)

    def test_silent_launch_can_stop_only_after_grace_and_progress_window(self):
        s = self.snapshot()
        s.launches['eng'] = launch('eng', 'running', updated_at=T0 - dt.timedelta(hours=1))
        s = replace(s, launch_progress={'eng': T0 - dt.timedelta(minutes=16)})
        self.assertEqual(dm.next_actions(s, {'ruling_progress_minutes': 20})[0].kind, 'hold')
        action, = dm.next_actions(s, {})
        self.assertEqual(action.kind, 'stop_launch')
        self.assertIn('silent', action.reason)
        s.ledger.ruling_due_at = iso(T0 - dt.timedelta(minutes=1))
        self.assertEqual(dm.next_actions(s, {})[0].kind, 'hold')

    def test_ledger_only_trigger_cancels_with_queued_or_running_engineer(self):
        for state in ('queued', 'running'):
            s = self.snapshot()
            s.ledger.rounds_without_progress = 0
            s.launches['eng'] = launch('eng', state)
            action, = dm.next_actions(s, {})
            self.assertEqual(action.kind, 'cancel_ruling')

    def test_finished_work_allows_ruling(self):
        s = self.snapshot()
        s.live.clear()
        s.launches['eng'] = launch('eng', 'done')
        self.assertTrue(dm.next_actions(s, {})[0].ruling)

    def test_budget_exhaustion_still_shows_productive_ruling_hold(self):
        s = self.snapshot()
        s.ledger.budgets['max_sessions'] = 0
        action, = dm.next_actions(s, {})
        self.assertEqual((action.kind, action.prompt_kind), ('hold', 'ruling-pending'))


class RulingWorkExecTests(ExecEnv):
    def test_pending_hold_is_visible_without_a_person_remedy(self):
        s = snap(ledger=self.led)
        a = dm.Action('hold', 'pending', None, 'ruling-pending',
                      reason='ruling pending: 2 launches still working')
        ex.note_waiting(self.b, self.led, s, [a])
        self.assertIn('waiting on: ruling pending: 2 launches still working',
                      self.b.job_data('J')['drive.waiting'])
        self.assertNotIn('person', self.b.job_data('J')['drive.waiting'])

    def test_snapshot_observes_posts_by_launch_agent(self):
        self.get_launch('eng')
        name = self.agent_of('eng')
        self.b.post('J', name, 'still implementing')
        s = ds.read(self.b, self.led, 'J', cfg={}, now=self.now())
        self.assertGreaterEqual(s.launch_progress['eng'], self.now(-5))

    def test_snapshot_observes_commit_in_launch_workdir(self):
        row = self.get_launch('eng')
        self.tamper_launch('eng', state='running')
        subprocess.run(['git', 'init', row.workdir], check=True, capture_output=True)
        subprocess.run(['git', '-C', row.workdir, '-c', 'user.name=Test', '-c',
                        'user.email=test@example.org', 'commit', '--allow-empty', '-m', 'progress'],
                       check=True, capture_output=True)
        s = ds.read(self.b, self.led, 'J', cfg={}, now=self.now())
        self.assertGreaterEqual(s.launch_progress['eng'], self.now(-5))

    def test_cancel_clears_due_time_and_posts_once(self):
        self.led.ruling_due_at = iso(self.now())
        self.led.save()
        s = snap(ledger=self.led)
        action = dm.Action('cancel_ruling', 'cancel', None, None, reason='ledger-only ruling cancelled')
        driver._act(self.b, self.cfg, self.sup, self.led, s, [action], self.now(), None, '', float('inf'),
                    driver._modules())
        self.assertIsNone(self.reload().ruling_due_at)
        driver._act(self.b, self.cfg, self.sup, self.led, s, [action], self.now(), None, '', float('inf'),
                    driver._modules())
        self.assertEqual(sum('ledger-only ruling cancelled' in m.message
                             for m in self.b.messages_after(0, 'J')), 1)

    def test_stop_discloses_dirty_files_before_stopping(self):
        row = self.get_launch('eng')
        self.tamper_launch('eng', state='running')
        subprocess.run(['git', 'init', row.workdir], check=True, capture_output=True)
        from pathlib import Path
        (Path(row.workdir) / 'unfinished.py').write_text('partial work')
        observed = []
        hooks = NS(stop_rows=lambda *a: observed.extend(m.message for m in self.b.messages_after(0, 'J')))
        life = NS(_open_rows=lambda *a: [NS(launch_id='L0')], Hooks=lambda: hooks)
        from swarm.supervisor.ledger import Intent
        self.led.intents.append(Intent(launch_id='L0', action_id='gen1|launch|eng|-|0|1', attempt=1,
                                       launch='eng', reserved_usd=0, reserved_minutes=1, locks=[], state='claimed'))
        driver._stop_launch(self.b, self.cfg, self.led, 'eng', NS(life=life))
        self.assertTrue(any('unfinished.py' in text for text in observed))
        self.assertTrue(any('wip:' in text for text in observed))
        self.assertTrue((Path(row.workdir) / 'unfinished.py').exists())

    def test_failed_dirty_file_scan_holds_stop_instead_of_losing_work(self):
        self.get_launch('eng')
        from swarm.supervisor.ledger import Intent
        self.led.intents.append(Intent(launch_id='L0', action_id='gen1|launch|eng|-|0|1', attempt=1,
                                       launch='eng', reserved_usd=0, reserved_minutes=1, locks=[], state='claimed'))
        stopped = []
        life = NS(_open_rows=lambda *a: [NS(launch_id='L0')],
                  Hooks=lambda: NS(stop_rows=lambda *a: stopped.append(True)))
        with mock.patch.object(driver.subprocess, 'run', side_effect=OSError('unreadable workdir')):
            driver._stop_launch(self.b, self.cfg, self.led, 'eng', NS(life=life))
        self.assertEqual(stopped, [])
        self.assertTrue(any('dirty-file scan failed' in m.message for m in self.b.messages_after(0, 'J')))
