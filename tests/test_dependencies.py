"""Durable exact-artifact stage gates through the real launch queue and pass."""
import datetime as dt
import json
from pathlib import Path
from unittest import mock

import support
from test_pass_cmd import PassCmd
from swarm import passcmd


class DependentTasks(PassCmd):
    def register(self, name, deps=(), artifact='main@' + 'a' * 40, **kw):
        from swarm import dependencies
        return dependencies.register(self.b, 'J', 1, name, artifact=artifact, depends=list(deps),
                                     role='engineer', harness='codex', model=None,
                                     workdir='/work/shared', brief='Execute ' + name,
                                     queued_by='me', **kw)

    def evidence(self, name, outcome='passed', artifact='main@' + 'a' * 40):
        from swarm import dependencies
        row = self.b.get_launch('J', 1, name)
        restart = next(r for r in self.b.restarts('J') if r.launch_id == row.launch_id)
        actor = self.b.allocate_name('worker-' + name, 'J', 'engineer')
        self.b.set_restart_agent(restart.id, 'worker-' + name)
        dependencies.report(self.b, 'J', 1, name, artifact=artifact, outcome=outcome,
                            reason='verified exact revision', actor=actor)

    def finish(self, name):
        row = self.b.get_launch('J', 1, name)
        restart = next(r for r in self.b.restarts('J') if r.launch_id == row.launch_id)
        self.b.finish_restart(restart.id, 'completed')
        self.b.close_agent('worker-' + name, 'done')

    def test_tasks_long_blocker_summary_does_not_interrupt_normal_pass(self):
        from swarm import dependencies
        from swarm.board.base import JOB_DATA_VALUE_MAX
        names = [f'blocked-{i:02}-' + 'x' * 40 for i in range(40)]
        for name in names:
            self.register(name, ['missing-predecessor'])
        original = {row.name: row.brief for row in self.b.launches('J')}
        blocked = dependencies.describe(self.b, 'J')
        summary = '; '.join(f'{row["name"]}: {row["reason"]}' for row in blocked)
        self.assertGreater(len(summary), JOB_DATA_VALUE_MAX)
        self.register('ready-stage')
        actions = self.pass_()
        self.assertFalse(any('pass error' in action for action in actions), actions)
        self.assertEqual([run['launch'] for run in self.runs], ['ready-stage'])
        self.assertEqual(self.b.job_data('J')['task.blocker'], summary[:JOB_DATA_VALUE_MAX])
        self.assertEqual({row.name: row.brief for row in self.b.launches('J') if row.name in original}, original)
        self.assertEqual([row for row in dependencies.describe(self.b, 'J') if row['name'] in names], blocked)
        # A subsequent sweep must still dispatch new independent ready work.
        self.register('next-ready-stage')
        actions = self.pass_()
        self.assertFalse(any('pass error' in action for action in actions), actions)
        self.assertEqual([run['launch'] for run in self.runs], ['ready-stage', 'next-ready-stage'])

    def test_tasks_completed_summary_bounds_long_remaining_goal(self):
        from swarm import dependencies
        from swarm.board.base import JOB_DATA_VALUE_MAX
        self.register('qa')
        self.pass_()
        self.evidence('qa')
        self.finish('qa')
        reason = 'remaining work ' + 'x' * JOB_DATA_VALUE_MAX
        self.verdict('not_met', reason, artifact='other@' + 'b' * 40)
        actions = self.pass_()
        self.assertFalse(any('pass error' in action for action in actions), actions)
        expected = 'declared tasks passed; remaining job goal: ' + reason
        self.assertEqual(self.b.job_data('J')['task.blocker'], expected[:JOB_DATA_VALUE_MAX])
        self.assertEqual(self.job().verdict_reason, reason)
        self.assertEqual(dependencies.describe(self.b, 'J')[0]['state'], 'done')

    def test_tasks_queued_gate_prevents_starting_missing_dependency(self):
        spec = {'version': 1, 'artifact': 'main@' + 'a' * 40, 'depends': ['qa'],
                'brief': 'Build approved source', 'run': self.job().activated_at.isoformat(),
                'reporter': None}
        self.b.queue_launch('J', 1, 'build', role='engineer', harness='codex', model=None,
                            workdir='/work/shared', brief='SWARM_TASK_V1\n' + json.dumps(spec),
                            queued_by='swarm task')
        self.pass_()
        self.assertEqual(self.runs, [], 'a missing dependency must hold a durable queued task')

    def test_tasks_release_build_once_after_both_exact_artifact_stages_end(self):
        self.register('qa')
        self.register('source-review')
        self.register('build', ['qa', 'source-review'])
        self.pass_()
        self.assertEqual([r['launch'] for r in self.runs], ['qa', 'source-review'])
        self.evidence('source-review')
        self.evidence('qa')
        self.pass_()
        self.assertEqual(len(self.runs), 2, 'evidence alone cannot bypass running predecessors')
        self.finish('qa')
        self.pass_()
        self.assertEqual(len(self.runs), 2)
        self.finish('source-review')
        for _ in range(5):
            self.pass_()
        self.assertEqual([r['launch'] for r in self.runs], ['qa', 'source-review', 'build'])

    def test_tasks_registration_is_idempotent_and_never_requeues_running_or_done(self):
        first = self.register('qa')
        self.pass_()
        again = self.register('qa')
        self.assertEqual(again.state, 'running')
        self.assertEqual(again.attempts, 1)
        self.evidence('qa')
        self.finish('qa')
        self.pass_()
        self.assertEqual(self.register('qa').state, 'done')
        self.assertEqual(len(self.runs), 1)

    def test_tasks_failed_evidence_holds_downstream_and_reports_reason(self):
        from swarm import dependencies
        self.register('qa')
        self.register('build', ['qa'])
        self.pass_()
        self.evidence('qa', 'failed')
        self.finish('qa')
        self.pass_()
        self.assertEqual(len(self.runs), 1)
        rows = {r['name']: r for r in dependencies.describe(self.b, 'J')}
        self.assertIn('failed', rows['build']['reason'])
        self.assertEqual(rows['build']['state'], 'blocked')

    def test_tasks_wrong_artifact_and_unassigned_reporter_are_rejected(self):
        from swarm import dependencies
        self.register('qa')
        self.pass_()
        with self.assertRaises(ValueError):
            dependencies.report(self.b, 'J', 1, 'qa', artifact='other@' + 'b' * 40,
                                outcome='passed', reason='bad', actor='me')
        with self.assertRaises(ValueError):
            dependencies.report(self.b, 'J', 1, 'qa', artifact='main@' + 'a' * 40,
                                outcome='passed', reason='bad', actor='unassigned')

    def test_tasks_met_verdict_does_not_close_pending_stage(self):
        self.register('qa')
        self.register('build', ['qa'])
        artifact = self.hand_off('main@' + 'a' * 40)
        self.verdict('met', 'source checked', artifact=artifact)
        self.pass_()
        self.assertEqual(self.job().status, 'active')
        self.assertEqual(len(self.runs), 1)

    def test_tasks_done_does_not_accept_broader_unmet_job_goal(self):
        self.register('archive-qa')
        self.pass_()
        self.evidence('archive-qa')
        self.finish('archive-qa')
        self.verdict('not_met', 'Phase 1 has more engineering work', artifact='other@' + 'b' * 40)
        self.pass_()
        self.assertEqual(self.job().status, 'active', 'finishing a subset must not accept the whole job goal')


    def test_tasks_external_report_waits_for_new_turn_end_and_never_launches(self):
        from swarm import dependencies
        actor = self.b.allocate_name('external-qa', 'J', 'engineer')
        base = self.b.now()
        with mock.patch.object(self.b, 'now', return_value=base):
            self.b.agent_turn_ended('external-qa')
            with self.assertRaisesRegex(ValueError, 'active agent turn'):
                self.register('qa', reporter=actor)
            # An explicit new tool call resumes the same agent turn and clears the old end.
            self.b.tool_started('external-qa', 'Read')
            self.register('qa', reporter=actor)
            self.register('build', ['qa'])
        with mock.patch.object(self.b, 'now', return_value=base + dt.timedelta(seconds=10)):
            dependencies.report(self.b, 'J', 1, 'qa', artifact='main@' + 'a' * 40,
                                outcome='passed', reason='exact source checked', actor=actor)
            self.pass_()
            self.assertEqual(self.runs, [], 'an old idle marker cannot stand in for the reporting turn end')
            self.b.agent_turn_ended('external-qa')
            # PostgreSQL's hook timestamp uses SQL now(), independent of Board.now(). Plant
            # the same controlled clock value in the fixture; no wall-clock delay is involved.
            ended = base + dt.timedelta(seconds=10)
            if self.h.name == 'postgres':
                self.h.conn.execute('UPDATE agents SET turn_ended_at=%s WHERE agent_key=%s', (ended, 'external-qa'))
            elif self.h.name == 'sqlite':
                self.h._db().execute('UPDATE agents SET turn_ended_at=? WHERE agent_key=?', (ended.isoformat(), 'external-qa'))
            else:
                with self.h.store.lock:
                    self.h.store.agents['external-qa']['turn_ended_at'] = ended
                    self.h.store.touch()
            self.pass_()
            self.pass_()
        self.assertEqual([r['launch'] for r in self.runs], ['build'])

    def test_tasks_first_failed_report_cannot_be_overwritten(self):
        self.register('qa')
        self.register('build', ['qa'])
        self.pass_()
        self.evidence('qa', 'failed')
        self.evidence('qa', 'passed')
        self.finish('qa')
        self.pass_()
        self.assertEqual([r['launch'] for r in self.runs], ['qa'])

    def test_tasks_missing_evidence_and_artifact_mismatch_hold_successful_runner(self):
        from swarm import dependencies
        self.register('qa', artifact='other@' + 'b' * 40)
        self.register('build', ['qa'])
        self.pass_()
        self.finish('qa')
        self.pass_()
        row = next(r for r in dependencies.describe(self.b, 'J') if r['name'] == 'build')
        self.assertIn('artifact/run mismatch', row['reason'])
        self.assertEqual(len(self.runs), 1)

    def test_tasks_cancel_between_selection_and_claim_prevents_start(self):
        self.register('qa')
        claim = self.b.claim_drive_launch
        def cancel(**kwargs):
            self.b.close_job('J', 'cancelled', 'owner cancelled')
            return claim(**kwargs)
        with mock.patch.object(self.b, 'claim_drive_launch', side_effect=cancel):
            self.pass_()
        self.assertEqual(self.runs, [])
        self.assertEqual(self.b.restarts('J'), [])

    def test_tasks_atomic_claim_rechecks_dependency_and_reserved_cost(self):
        from swarm import dependencies
        row = self.register('build', ['qa'])
        params = dict(job='J', generation=1, action_id='task-build', attempt=1, launch_id='build-1',
                      launch_row_id=row.id, queue_version=row.revision, harness='codex', reserve_usd=2,
                      reserve_minutes=1, locks=[], lock_seconds=60, max_cost_usd=200)
        self.assertIsNone(self.b.claim_drive_launch(**params))
        ready = self.register('qa')
        params.update(action_id='task-qa', launch_id='qa-1', launch_row_id=ready.id,
                      queue_version=ready.revision, max_cost_usd=1)
        self.assertIsNone(self.b.claim_drive_launch(**params), 'claim must enforce cost under the job lock')
        self.assertEqual(self.b.restarts('J'), [])

    def test_tasks_restart_after_claim_before_runner_start_settles_and_holds_children(self):
        from swarm import dependencies
        row = self.register('qa')
        self.register('build', ['qa'])
        restart = self.b.claim_drive_launch(job='J', generation=1, action_id='crash-qa', attempt=1,
            launch_id='qa-1', launch_row_id=row.id, queue_version=row.revision, harness='codex',
            reserve_usd=2, reserve_minutes=1, locks=[], lock_seconds=60)
        with mock.patch.object(self.b, 'now', return_value=restart.at + dt.timedelta(minutes=32)):
            self.pass_()
            self.pass_()
        self.assertEqual(self.runs, [])
        self.assertEqual(self.b.get_launch('J', 1, 'qa').state, 'failed')
        self.assertIn('failed', next(r['reason'] for r in dependencies.describe(self.b, 'J') if r['name'] == 'build'))

    def test_tasks_repeated_concurrent_passes_launch_one_effective_task(self):
        import concurrent.futures
        import threading
        self.register('qa')
        barrier = threading.Barrier(4)
        def tick():
            with self.h.board() as board:
                barrier.wait()
                return passcmd.run_pass(board, self.cfg, starter=self.starter)
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: tick(), range(4)))
        self.assertFalse(any('pass error' in action for result in results for action in result), results)
        self.assertEqual([r['launch'] for r in self.runs], ['qa'])
        self.assertEqual(len(self.b.restarts('J')), 1)


    def test_tasks_all_passed_handoff_dispatches_full_goal_judge(self):
        self.register('qa')
        self.pass_()
        self.evidence('qa')
        self.finish('qa')
        self.hand_off('main@' + 'a' * 40)
        self.pass_()
        self.pass_()
        self.assertEqual(len(self.runs), 2, 'all stage gates must allow full-goal acceptance to progress')
        self.assertTrue(self.runs[-1]['launch'].startswith('goal-judge-'))
        self.assertIn('--scope goal', self.runs[-1]['stdin'])



    def test_tasks_new_generation_retries_once_and_supersedes_failed_old_stage(self):
        from swarm import dependencies
        self.register('qa')
        self.pass_()
        self.evidence('qa', 'failed')
        self.finish('qa')
        self.pass_()
        dependencies.register(self.b, 'J', 2, 'qa', artifact='main@' + 'a' * 40,
            depends=[], role='engineer', harness='codex', model=None, workdir='/work/shared',
            brief='Retry QA with corrected checks', queued_by='me')
        self.pass_()
        self.pass_()
        self.assertEqual([r['launch'] for r in self.runs], ['qa', 'qa'])
        self.assertEqual([(r['name'], r['generation']) for r in dependencies.describe(self.b, 'J')], [('qa', 2)])

    def test_tasks_obsolete_queued_generation_is_never_started(self):
        from swarm import dependencies
        self.register('qa')
        dependencies.register(self.b, 'J', 2, 'qa', artifact='main@' + 'a' * 40,
            depends=[], role='engineer', harness='codex', model=None, workdir='/work/shared',
            brief='Superseding QA', queued_by='me')
        self.pass_()
        self.assertEqual(len(self.runs), 1)
        self.assertEqual(self.b.get_launch('J', 1, 'qa').state, 'failed')
        self.assertEqual(self.b.get_launch('J', 2, 'qa').state, 'running')



    def test_tasks_cancel_after_successful_claim_prevents_runner_start(self):
        self.register('qa')
        claim = self.b.claim_drive_launch
        def cancel(**kwargs):
            result = claim(**kwargs)
            self.b.close_job('J', 'cancelled', 'owner cancelled after claim')
            return result
        with mock.patch.object(self.b, 'claim_drive_launch', side_effect=cancel):
            self.pass_()
        self.assertEqual(self.runs, [])
        self.assertTrue(all(r.ended_at is not None for r in self.b.restarts('J')))



    def test_tasks_build_cannot_consume_superseded_dependency_generation(self):
        from swarm import dependencies
        self.register('qa')
        self.pass_()
        self.evidence('qa')
        self.finish('qa')
        self.pass_()
        dependencies.register(self.b, 'J', 2, 'qa', artifact='main@' + 'a' * 40,
            depends=[], role='engineer', harness='codex', model=None, workdir='/work/shared',
            brief='Superseding QA', queued_by='me')
        self.register('build', ['qa'])
        self.pass_()
        self.assertEqual([r['launch'] for r in self.runs], ['qa', 'qa'])
        self.assertIn('superseded', next(r['reason'] for r in dependencies.describe(self.b, 'J') if r['name'] == 'build'))

    def test_tasks_goal_judge_preserves_codex_role_model_policy(self):
        self.cfg['models'] = {'mode': 'enforce', 'codex': {'judge': 'gpt-6-astra', 'engineering_lead': 'gpt-6-astra'}}
        self.register('qa')
        self.pass_()
        self.evidence('qa')
        self.finish('qa')
        self.hand_off('main@' + 'a' * 40)
        self.pass_()
        self.pass_()
        row = next(r for r in self.b.launches('J') if r.name.startswith('goal-judge-'))
        self.assertEqual((row.harness, row.model), ('codex', 'gpt-6-astra'))



    def test_tasks_stale_unrelated_goal_verdict_cannot_close_current_handoff(self):
        self.register('qa')
        self.pass_()
        self.evidence('qa')
        self.finish('qa')
        judge = self.b.allocate_name('goal-seat', 'J', 'judge')
        self.b.claim_judge('goal-seat', 'J')
        self.b.record_verdict('J', judge, 'met', 'old goal accepted', scope='goal', candidate='old@' + 'b' * 40)
        self.b.close_agent('goal-seat', 'done')
        self.hand_off('main@' + 'a' * 40)
        self.pass_()
        self.assertEqual(self.job().status, 'active')



    def test_tasks_superseded_artifact_stage_cannot_block_current_goal_judge(self):
        import hashlib
        old = 'old@' + 'b' * 40
        self.register('obsolete-stage', artifact=old)
        self.b.set_job_data('J', 'pipeline.superseded.' + hashlib.sha256(old.encode()).hexdigest()[:32], 'main@' + 'a' * 40)
        self.register('qa')
        self.pass_()
        self.evidence('qa')
        self.finish('qa')
        self.hand_off('main@' + 'a' * 40)
        self.pass_()
        self.pass_()
        self.assertEqual([r['launch'] for r in self.runs][:1], ['qa'])
        self.assertTrue(self.runs[-1]['launch'].startswith('goal-judge-'))



    def test_tasks_external_reporter_replacement_cannot_inherit_old_agent_authority(self):
        from swarm import dependencies
        actor = self.b.allocate_name('old-external', 'J', 'engineer')
        self.register('qa', reporter=actor)
        self.b.close_agent('old-external', 'replaced')
        self.assertEqual(self.b.claim_resume('replacement-external', 'old-external', 'J'), actor)
        with self.assertRaises(ValueError):
            dependencies.report(self.b, 'J', 1, 'qa', artifact='main@' + 'a' * 40,
                                outcome='passed', reason='same display name is a new identity', actor=actor)
        self.assertEqual(self.b.events('J'), [])
        self.assertEqual(self.register('qa', reporter=actor).state, 'queued', 'repeated registration preserves the original assigned agent')



from support import add_backend_variants
add_backend_variants(globals(), DependentTasks)
