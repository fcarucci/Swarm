"""Completion contracts use persisted tasks and typed goal acceptance, not raw MET."""
import unittest
import datetime as dt
import hashlib
from unittest import mock

from support import MemoryHarness, SqliteHarness
from test_auto_close import AutoCloseContract
from swarm import dependencies
from swarm.board.base import AUTO_CLOSED_BY


class CompletionScopeContract:
    setUpClass = AutoCloseContract.__dict__['setUpClass']
    tearDownClass = AutoCloseContract.__dict__['tearDownClass']
    setUp = AutoCloseContract.setUp
    age = AutoCloseContract.age
    sweep = AutoCloseContract.sweep
    def prepare(self, scope='artifact', pending=False):
        self.b.open_job('j', None, None, None, 'owner', goal='Whole project including hardware')
        judge = self.b.allocate_name('judge', 'j', 'judge')
        self.b.claim_judge('judge', 'j')
        if pending:
            self.task()
        self.b.record_verdict('j', judge, 'met', 'Accepted selected scope',
                              artifact='runtime@' + 'a' * 40, scope=scope)
        self.b.agent_stopped('judge')
        self.age('j', ['judge'], [])

    def task(self, name='hardware'):
        return dependencies.register(self.b, 'j', 1, name, artifact='hardware@' + 'b' * 40,
            depends=[], role='qa', harness='codex', model=None, workdir='/work/isolated',
            brief='Hardware acceptance outstanding', queued_by='owner')

    def test_artifact_met_does_not_complete_original_goal(self):
        self.prepare()
        self.sweep()
        self.assertEqual(self.b.job_status('j').status, 'active')

    def test_goal_met_does_not_discard_pending_tasks(self):
        self.prepare(scope='goal', pending=True)
        self.sweep()
        self.assertEqual(self.b.job_status('j').status, 'active')

    def test_goal_met_without_obligations_can_complete(self):
        self.prepare(scope='goal')
        self.sweep()
        self.assertEqual(self.b.job_status('j').status, 'completed')

    def test_registration_recovers_only_erroneous_auto_close(self):
        self.prepare()
        run = self.b.job_status('j').activated_at
        self.b.close_job('j', 'completed', 'Historical erroneous auto-close', closed_by=AUTO_CLOSED_BY)
        self.task()
        self.assertEqual(self.b.job_status('j').status, 'active')
        self.assertEqual(self.b.job_status('j').activated_at, run)
        self.assertEqual(self.b.job_status('j').verdict_scope, 'artifact')
        self.task()  # idempotent admission

    def test_owner_close_is_never_recovered(self):
        self.prepare()
        self.b.close_job('j', 'completed', 'Owner decision', closed_by='owner')
        with self.assertRaisesRegex(ValueError, 'not an open job'):
            self.task()

    def test_legitimate_goal_close_is_never_recovered(self):
        self.prepare(scope='goal')
        self.b.close_job('j', 'completed', 'Goal accepted', closed_by=AUTO_CLOSED_BY)
        with self.assertRaisesRegex(ValueError, 'not an open job'):
            self.task()

    def test_new_work_after_legitimate_close_is_not_proof_of_erroneous_close(self):
        self.prepare(scope='goal', pending=True)
        row = self.b.get_launch('j', 1, 'hardware')
        self.b.close_job('j', 'completed', 'Goal accepted', closed_by=AUTO_CLOSED_BY)
        # A fresh low-level queue revision after completion cannot retroactively invalidate it.
        self.b.queue_launch('j', 1, 'hardware', role=row.role, harness=row.harness,
                            model=row.model, workdir=row.workdir, brief=row.brief, queued_by=row.queued_by)
        self.assertFalse(self.b.recover_auto_close('j'))

    def test_new_artifact_verdict_after_legitimate_goal_close_does_not_reopen(self):
        self.prepare(scope='goal')
        self.b.close_job('j', 'completed', 'Goal accepted', closed_by=AUTO_CLOSED_BY)
        judge = self.b.allocate_name('new-judge', 'j', 'judge')
        self.b.claim_judge('new-judge', 'j')
        self.b.record_verdict('j', judge, 'met', 'Later independent artifact', scope='artifact')
        self.assertFalse(self.b.recover_auto_close('j'))

    def test_running_and_failed_tasks_block_completion(self):
        for state in ('running', 'failed', 'done'):
            with self.subTest(state=state):
                self.h.reset()
                self.prepare(scope='goal', pending=True)
                row = self.b.get_launch('j', 1, 'hardware')
                if state == 'running':
                    self.assertIsNotNone(self.b.claim_drive_launch(job='j', generation=1,
                        action_id='hardware', attempt=1, launch_id='hardware-1',
                        launch_row_id=row.id, queue_version=row.revision, harness='codex',
                        reserve_usd=2, reserve_minutes=1, locks=[], lock_seconds=60))
                else:
                    self.b.set_launch_state(row.id, row.revision, state, job='j')
                # A done row without authenticated exact-artifact evidence is still incomplete.
                self.assertIsNone(self.b.auto_close_job('j', self.b.now(), 'quiet'))

    def test_explicitly_superseded_task_does_not_block(self):
        self.prepare(scope='goal', pending=True)
        key = hashlib.sha256(('hardware@' + 'b' * 40).encode()).hexdigest()[:32]
        self.b.set_job_data('j', 'pipeline.superseded.' + key, 'new artifact')
        self.sweep()
        self.assertEqual(self.b.job_status('j').status, 'completed')

    def test_goal_met_with_pending_task_auto_close_is_recovered(self):
        self.prepare(scope='goal', pending=True)
        self.b.close_job('j', 'completed', 'Old sweep omitted task', closed_by=AUTO_CLOSED_BY)
        self.assertTrue(self.b.recover_auto_close('j'))
        self.assertFalse(self.b.recover_auto_close('j'))
        self.assertEqual(self.b.get_launch('j', 1, 'hardware').state, 'queued')

    def test_stale_close_timestamp_does_not_recover(self):
        self.prepare()
        self.b.close_job('j', 'completed', 'Old sweep', closed_by=AUTO_CLOSED_BY)
        at = self.b.job_status('j').finished_at
        self.assertFalse(self.b.undo_auto_close('j', at - dt.timedelta(seconds=1), invalid_only=True))
        self.assertEqual(self.b.job_status('j').status, 'completed')

    def test_new_authoritative_verdict_wins_recovery_race(self):
        self.prepare()
        self.b.close_job('j', 'completed', 'Old sweep', closed_by=AUTO_CLOSED_BY)
        at = self.b.job_status('j').finished_at
        # Simulate another transaction accepting the whole goal after the observer's read.
        judge = self.b.allocate_name('new-judge', 'j', 'judge')
        self.b.claim_judge('new-judge', 'j')
        self.b.record_verdict('j', judge, 'met', 'Whole goal accepted', scope='goal')
        self.assertFalse(self.b.undo_auto_close('j', at, invalid_only=True))

    def test_normal_pass_recovers_before_open_job_iteration(self):
        from swarm import passcmd
        self.prepare()
        self.b.close_job('j', 'completed', 'Old sweep', closed_by=AUTO_CLOSED_BY)
        with mock.patch.object(passcmd, '_advance', return_value=[]) as advance:
            acts = passcmd.run_pass(self.b, {}, starter=lambda *a, **kw: None)
        self.assertTrue(any('restored unfinished' in a for a in acts))
        self.assertEqual(advance.call_args.args[2].status, 'active')

    def test_cancelled_job_is_not_recovered(self):
        self.prepare()
        self.b.close_job('j', 'cancelled', 'Owner cancelled', closed_by='owner')
        self.assertFalse(self.b.recover_auto_close('j'))

    def test_forced_close_is_not_recovered(self):
        self.prepare()
        self.b.close_job('j', 'completed', 'Forced completion', forced=True, closed_by=AUTO_CLOSED_BY)
        self.assertFalse(self.b.recover_auto_close('j'))

    def test_unscoped_historical_verdict_is_not_reinterpreted(self):
        self.prepare()
        # Old schema fixtures lack a typed scope; no supported command creates new legacy data.
        if isinstance(self.h, MemoryHarness):
            with self.h.store.lock:
                self.h.store.jobs['j']['verdict_scope'] = None
        else:
            self.h._set('jobs', 'job', 'j', {'verdict_scope': None})
        self.sweep()
        self.assertEqual(self.b.job_status('j').status, 'completed')
        self.assertFalse(self.b.recover_auto_close('j'))

    def test_goal_not_met_and_unreachable_are_not_success(self):
        for verdict in ('not_met', 'unreachable'):
            with self.subTest(verdict=verdict):
                self.h.reset()
                self.prepare(scope='goal')
                judge = self.b.allocate_name('judge', 'j', 'judge')
                self.b.claim_judge('judge', 'j')
                self.b.record_verdict('j', judge, verdict, 'Full goal not accepted',
                                      scope='goal', details='Independent full goal assessment')
                self.b.agent_stopped('judge')
                self.age('j', ['judge'], [])
                self.assertIsNone(self.b.auto_close_job('j', self.b.now(), 'quiet'))

    def test_task_obligation_also_blocks_goal_less_job(self):
        self.b.open_job('j', None, None, None, 'owner')
        self.task()
        self.assertIsNone(self.b.auto_close_job('j', self.b.now(), 'quiet'))

    def test_recovery_preserves_assigned_report_and_end_dependency_gate(self):
        self.prepare()
        self.b.close_job('j', 'completed', 'Old sweep', closed_by=AUTO_CLOSED_BY)
        self.assertTrue(self.b.recover_auto_close('j'))
        qa = self.b.allocate_name('qa', 'j', 'qa')
        artifact = 'hardware@' + 'b' * 40
        dependencies.register(self.b, 'j', 1, 'hardware', artifact=artifact, depends=[], role='qa',
            harness='codex', model=None, workdir='/work/isolated', brief='Hardware acceptance',
            queued_by='owner', reporter=qa)
        dependencies.register(self.b, 'j', 1, 'archive', artifact=artifact, depends=['hardware'],
            role='engineer', harness='codex', model=None, workdir='/work/isolated',
            brief='Archive accepted hardware evidence', queued_by='owner')
        foreign = self.b.allocate_name('foreign', 'j', 'qa')
        with self.assertRaisesRegex(ValueError, 'assigned live agent'):
            dependencies.report(self.b, 'j', 1, 'hardware', artifact=artifact, outcome='passed',
                                reason='Foreign identity', actor=foreign)
        dependencies.report(self.b, 'j', 1, 'hardware', artifact=artifact, outcome='passed',
                            reason='Assigned executor evidence', actor=qa)
        dependencies.reconcile_external(self.b, 'j')
        self.assertEqual(dependencies.describe(self.b, 'j')[1]['state'], 'blocked')
        self.b.agent_stopped('qa')
        dependencies.reconcile_external(self.b, 'j')
        self.assertEqual(dependencies.describe(self.b, 'j')[1]['state'], 'ready')

    def test_completed_evidenced_task_needs_current_goal_candidate(self):
        from swarm.review import completion_pending
        self.b.open_job('j', None, None, None, 'owner', goal='Whole project')
        qa = self.b.allocate_name('qa', 'j', 'qa')
        artifact = 'hardware@' + 'b' * 40
        dependencies.register(self.b, 'j', 1, 'hardware', artifact=artifact, depends=[], role='qa',
            harness='codex', model=None, workdir='/work/isolated', brief='Hardware acceptance',
            queued_by='owner', reporter=qa)
        dependencies.report(self.b, 'j', 1, 'hardware', artifact=artifact, outcome='passed',
                            reason='Actual fixture executor proof', actor=qa)
        self.b.agent_stopped('qa')
        dependencies.reconcile_external(self.b, 'j')
        self.assertEqual(dependencies.describe(self.b, 'j')[0]['state'], 'done')
        self.b.post('j', qa, 'DONE ' + artifact)
        judge = self.b.allocate_name('judge', 'j', 'judge')
        self.b.claim_judge('judge', 'j')
        self.b.record_verdict('j', judge, 'met', 'Artifact accepted', artifact=artifact)
        self.b.post('j', qa, 'FINALIZED ' + artifact)
        self.assertTrue(completion_pending(self.b, 'j', 'Whole project', 'met'))
        self.b.record_verdict('j', judge, 'met', 'Wrong goal candidate', scope='goal', candidate='other')
        self.assertTrue(completion_pending(self.b, 'j', 'Whole project', 'met'))
        self.b.record_verdict('j', judge, 'met', 'Full goal accepted', artifact=artifact,
                              scope='goal', candidate=artifact)
        # The new acceptance must be finalized separately by the executor.
        self.b.post('j', qa, 'FINALIZED ' + artifact)
        self.assertFalse(completion_pending(self.b, 'j', 'Whole project', 'met'))
        self.b.post('j', qa, 'DONE newer-artifact')
        self.assertTrue(completion_pending(self.b, 'j', 'Whole project', 'met'))


class MemoryCompletionScope(CompletionScopeContract, unittest.TestCase):
    harness_factory = MemoryHarness


class SqliteCompletionScope(CompletionScopeContract, unittest.TestCase):
    harness_factory = SqliteHarness
