"""Orphan expiry cannot discard persisted goal/task obligations."""
import datetime as dt
import os
import unittest
from unittest import mock

from support import FileHarness, MemoryHarness, PostgresHarness, SqliteHarness
from test_completion_scope import CompletionScopeContract
from swarm.board.base import AUTO_CLOSED_BY


class OrphanScopeContract:
    setUpClass = CompletionScopeContract.__dict__['setUpClass']
    tearDownClass = CompletionScopeContract.__dict__['tearDownClass']
    setUp = CompletionScopeContract.setUp
    prepare = CompletionScopeContract.prepare
    task = CompletionScopeContract.task
    age = CompletionScopeContract.age

    def expiry(self):
        return self.b.sweep_expiry(0, 30, goal_stall_hours=0)

    def cancel(self, outcome='auto-closed: no live agents for 30 min', **kw):
        return self.b.close_job('j', 'cancelled', outcome, closed_by=AUTO_CLOSED_BY, **kw)

    def test_artifact_acceptance_does_not_allow_orphan_cancel(self):
        self.prepare()
        self.assertEqual(self.expiry(), [])
        self.assertEqual(self.b.job_status('j').status, 'active')

    def test_tasks_protect_goal_less_or_goal_accepted_orphans(self):
        for goal in (None, 'Whole project'):
            with self.subTest(goal=goal):
                self.h.reset()
                if goal is None:
                    self.b.open_job('j', None, None, None, 'owner')
                    self.task()
                    self.age('j', [], [])
                else:
                    self.prepare(scope='goal', pending=True)
                self.assertEqual(self.expiry(), [])
                self.assertEqual(self.b.job_status('j').status, 'active')

    def test_orphan_close_rechecks_obligation_under_backend_lock(self):
        self.prepare(scope='goal')
        action = self.b._expiry_action(self.b.job_status('j'), self.b.now(), 0, 30, 0, None)
        self.assertIsNotNone(action)
        self.task()
        self.assertFalse(self.b.close_job('j', action.status, action.outcome,
                                          closed_by=AUTO_CLOSED_BY, guard=action.guard))

    def test_actual_pass_recovers_only_proven_orphan_cancel_and_preserves_run(self):
        from swarm import passcmd
        self.prepare(pending=True)
        original = self.b.job_status('j')
        task = self.b.get_launch('j', 1, 'hardware')
        self.cancel()
        agents = self.b.agents('j')
        with mock.patch.object(passcmd, '_advance', return_value=[]):
            acts = passcmd.run_pass(self.b, {}, starter=lambda *a, **kw: None)
        restored = self.b.job_status('j')
        self.assertTrue(any('restored unfinished' in a for a in acts))
        self.assertEqual(restored.status, 'active')
        self.assertEqual(restored.activated_at, original.activated_at)
        self.assertEqual(restored.verdict_at, original.verdict_at)
        self.assertEqual(self.b.get_launch('j', 1, 'hardware'), task)
        self.assertEqual(self.b.agents('j'), agents)
        self.assertEqual(self.expiry(), [])
        self.assertFalse(self.b.recover_auto_close('j'))

    def test_normal_task_admission_recovers_proven_orphan_cancel(self):
        self.prepare()
        self.cancel()
        self.task()
        self.assertEqual(self.b.job_status('j').status, 'active')
        self.assertEqual(self.b.get_launch('j', 1, 'hardware').state, 'queued')

    def test_unconditional_auto_close_undo_cannot_reopen_cancellation(self):
        self.prepare()
        self.cancel()
        self.assertFalse(self.b.undo_auto_close('j', self.b.job_status('j').finished_at))
        self.assertEqual(self.b.job_status('j').status, 'cancelled')

    def test_legitimate_auto_orphan_cancel_stays_closed(self):
        self.prepare(scope='goal')
        self.cancel()
        self.assertFalse(self.b.recover_auto_close('j'))
        self.assertEqual(self.b.job_status('j').status, 'cancelled')

    def test_other_auto_cancel_reason_stays_closed(self):
        self.prepare(pending=True)
        self.cancel('auto-closed: waiting with no live agents; verdict met')
        self.assertFalse(self.b.recover_auto_close('j'))

    def test_owner_and_forced_orphan_cancellations_stay_closed(self):
        for owner, forced in (('owner', False), (AUTO_CLOSED_BY, True)):
            with self.subTest(owner=owner, forced=forced):
                self.h.reset()
                self.prepare(pending=True)
                self.b.close_job('j', 'cancelled', 'auto-closed: no live agents for 30 min',
                                  closed_by=owner, forced=forced)
                self.assertFalse(self.b.recover_auto_close('j'))

    def test_stale_timestamp_cannot_recover_orphan_cancel(self):
        self.prepare()
        self.cancel()
        closed = self.b.job_status('j').finished_at
        self.assertFalse(self.b.undo_auto_close('j', closed - dt.timedelta(seconds=1), invalid_only=True))
        self.assertEqual(self.b.job_status('j').status, 'cancelled')

    def test_new_goal_acceptance_wins_orphan_recovery_race(self):
        self.prepare()
        self.cancel()
        closed = self.b.job_status('j').finished_at
        judge = self.b.allocate_name('replacement-judge', 'j', 'judge')
        self.b.claim_judge('replacement-judge', 'j')
        self.b.record_verdict('j', judge, 'met', 'Full goal accepted', scope='goal')
        self.assertFalse(self.b.undo_auto_close('j', closed, invalid_only=True))

    def test_authoritative_unreachable_without_tasks_retains_orphan_semantics(self):
        self.prepare(scope='goal')
        judge = self.b.allocate_name('judge2', 'j', 'judge')
        self.b.claim_judge('judge2', 'j')
        self.b.record_verdict('j', judge, 'unreachable', 'Cannot finish', scope='goal', details='Required hardware is permanently unavailable')
        self.b.agent_stopped('judge2')
        self.age('j', ['judge', 'judge2'], [])
        self.assertEqual(len(self.expiry()), 1)
        self.assertEqual(self.b.job_status('j').status, 'cancelled')
        self.assertFalse(self.b.recover_auto_close('j'))

    def test_new_work_after_legitimate_orphan_cancel_is_not_old_obligation(self):
        self.prepare(scope='goal')
        self.cancel()
        # Low-level queue admission is not proof that the earlier cancellation was wrong.
        self.b.queue_launch('j', 1, 'new', role='qa', harness='codex', model=None,
                            workdir='/work/isolated', brief='New work', queued_by='owner')
        self.assertFalse(self.b.recover_auto_close('j'))


class MemoryOrphanScope(OrphanScopeContract, unittest.TestCase):
    harness_factory = MemoryHarness


class SqliteOrphanScope(OrphanScopeContract, unittest.TestCase):
    harness_factory = SqliteHarness


class FileOrphanScope(OrphanScopeContract, unittest.TestCase):
    harness_factory = FileHarness


@unittest.skipUnless(os.environ.get('SWARM_TEST_CONFIG'), 'needs throwaway PostgreSQL config')
class PostgresOrphanScope(OrphanScopeContract, unittest.TestCase):
    @staticmethod
    def harness_factory():
        return PostgresHarness(os.environ['SWARM_TEST_CONFIG'])
