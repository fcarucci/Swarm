"""Generic report and coding recipe transitions with a fake bounded runner."""
import datetime as dt
import json
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace as NS
from unittest import mock

from support import MemoryHarness
from swarm import review
from swarm.supervisor import pipeline, settings


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.h = MemoryHarness('pipeline-' + str(uuid.uuid4()))
        self.h.reset()
        self.b = self.h.board()
        self.addCleanup(self.b.close)
        self.b.open_job('J', 'Investigate outages', 'Produce a report', 'owner', None)
        self.b.set_job_goal('J', 'A reviewed report with learnings')
        self.cfg = self.h.cfg
        self.cfg['pipeline'] = {}
        self.sup = settings.settings(self.cfg)
        self.sup['backoff_minutes'] = [0]
        self.sup['max_concurrent_replacements'] = 10
        self.owner = NS(cwd='/work/project', harness='codex')
        self.state, self.output, self.launched = {}, [], []
        for name, value in [('swarm.supervisor.orphans.local_record', self.owner),
                            ('swarm.supervisor.orphans.human_question', False),
                            ('swarm.supervisor.command.workdir_for', '/work/project'),
                            ('swarm.supervisor.command.workdir_problem', None),
                            ('swarm.supervisor.command._workdir_hold', None),
                            ('swarm.supervisor.command._switched_off_now', None),
                            ('swarm.supervisor.settings.save_state', None),
                            ('swarm.supervisor.markers.write_resume_marker', Path('/unused-marker')),
                            ('swarm.supervisor.markers.remove_resume_marker', True),
                            ('swarm.plugins.pipeline_recipe', {})]:
            patch = mock.patch(name, return_value=value)
            patch.start(); self.addCleanup(patch.stop)
        self.recipe = mock.patch('swarm.plugins.pipeline_recipe', return_value={}).start()
        self.addCleanup(mock.patch.stopall)
        self.worker = self.b.allocate_name('worker', 'J', 'engineer')
        self.b.set_agent_runtime('worker', 'codex', 'worker-model')
        self.b.close_agent('worker', 'done')

    def done(self, ref='reports/outage.md', summary='Includes evidence and remedy'):
        return self.b.post('J', self.worker, 'DONE ' + json.dumps(ref) + ' | ' + summary)

    def tick(self, **kw):
        return pipeline.run(self.b, self.cfg, self.sup, self.state,
                            start_runner=kw.pop('start_runner', lambda cfg, run: self.launched.append(run)),
                            which=kw.pop('which', lambda binary: binary),
                            say=self.output.append, **kw)

    def enroll_latest(self):
        run = self.launched[-1]
        key = run['session_id'] or 'codex-' + str(uuid.uuid4())
        self.assertEqual(self.b.claim_resume(key, run['resume_of'], 'J'), run['name'])
        self.b.set_restart_agent(run['restart_id'], key)
        self.b.set_agent_runtime(key, run['harness'], None)
        return key, run

    def conclude(self, verdict='met', ref='reports/outage.md', next_steps=None):
        key, run = self.enroll_latest()
        self.assertTrue(self.b.record_verdict('J', run['name'], verdict, 'review evidence', next_steps, artifact=ref))
        self.b.close_agent(key, 'review complete')
        self.b.finish_restart(run['restart_id'], 'completed')

    def finish_latest(self, post=None, outcome='completed'):
        key, run = self.enroll_latest()
        if post:
            self.b.post('J', run['name'], post)
        self.b.close_agent(key, 'done')
        self.b.finish_restart(run['restart_id'], outcome)

    def test_report_flow_judge_then_finalizer_and_completion(self):
        self.done()
        self.assertEqual(self.tick(), {'J'})
        run = self.launched[-1]
        self.assertEqual(run['harness'], 'claude')
        self.assertIn('[swarm role: judge]', run['stdin'])
        self.assertIn('A reviewed report', run['stdin'])
        self.assertIn('Includes evidence', run['stdin'])
        self.assertNotIn('gh ', run['stdin'])
        self.assertIn('do not fix, merge, push', run['stdin'])
        self.conclude()
        self.tick()
        self.assertEqual(len(self.launched), 2)
        self.assertIn('[swarm role: finalizer]', self.launched[-1]['stdin'])
        self.assertIn('swarm learn', self.launched[-1]['stdin'])
        self.finish_latest('FINALIZED reports/outage.md')
        self.tick()
        self.assertEqual(len(self.launched), 2)
        self.assertTrue(self.b.close_job('J', 'completed', 'Report published; learnings recorded'))
        self.tick()
        self.assertEqual(len(self.launched), 2)

    def test_code_flow_evidence_gate_and_plugin_integrator(self):
        ref = 'feat/report@' + 'a' * 40
        green = mock.Mock(return_value=False)
        self.recipe.return_value = dict(evidence_command='fake-ci-watch exact-head',
                                        evidence_check=green, finalize='Merge exact artifact; push both remotes',
                                        finalizer_role='integrator')
        self.done(ref)
        self.tick()
        self.assertIn('fake-ci-watch exact-head', self.launched[-1]['stdin'])
        self.conclude(ref=ref)
        self.tick()
        self.assertEqual(len(self.launched), 1)
        green.return_value = True
        self.tick()
        self.assertEqual(len(self.launched), 2)
        green.assert_called_with('/work/project')
        self.assertIn('[swarm role: integrator]', self.launched[-1]['stdin'])
        self.assertIn('push both remotes', self.launched[-1]['stdin'])
        self.finish_latest('INTEGRATED ' + ref)
        self.tick()
        self.assertEqual(len(self.launched), 2)

    def test_not_met_fix_same_worker_host_and_next_brief(self):
        self.done(); self.tick()
        self.conclude('not_met', next_steps='Add outage root cause with evidence')
        self.tick()
        self.assertEqual(self.launched[-1]['harness'], 'codex')
        self.assertIn('Add outage root cause with evidence', self.launched[-1]['stdin'])
        self.assertIn('[swarm role: worker]', self.launched[-1]['stdin'])
        self.finish_latest('DONE reports/outage-v2.md')
        self.tick()
        self.assertIn('[swarm role: judge]', self.launched[-1]['stdin'])
        self.assertIn('Add outage root cause with evidence', self.launched[-1]['stdin'])
        self.conclude(ref='reports/outage-v2.md')
        self.tick()
        self.assertIn('[swarm role: finalizer]', self.launched[-1]['stdin'])

    def test_live_judge_or_pending_launch_does_not_duplicate(self):
        self.done(); self.tick(); self.tick()
        self.assertEqual(len(self.launched), 1)
        self.enroll_latest(); self.tick()
        self.assertEqual(len(self.launched), 1)

    def test_head_bound_older_met_cannot_cover_new_revision(self):
        self.recipe.side_effect = lambda cfg, board, job, artifact, **kw: {'artifact_group': artifact.split('@')[0]}
        first, second = 'branch@' + 'a' * 40, 'branch@' + 'b' * 40
        self.done(first); self.tick(); self.conclude(ref=first)
        self.done(second); self.tick()
        self.assertIn(second, self.launched[-1]['stdin'])
        self.assertIn('[swarm role: judge]', self.launched[-1]['stdin'])
        self.conclude(ref=second); self.tick()
        self.assertIn(second, self.launched[-1]['stdin'])
        self.assertIn('[swarm role: finalizer]', self.launched[-1]['stdin'])
        self.assertNotIn(first, self.launched[-1]['stdin'])
        self.assertIn(second, self.b.job_data('J').values())

    def test_multiple_artifacts_rotate_judge_then_finalize_individually(self):
        self.done('report-a'); self.done('report-b'); self.tick()
        self.conclude(ref='report-a'); self.tick()
        self.assertIn('report-b', self.launched[-1]['stdin'])
        self.assertIn('[swarm role: judge]', self.launched[-1]['stdin'])
        self.conclude(ref='report-b'); self.tick()
        self.assertIn('artifact: report-a', self.launched[-1]['stdin'])
        self.finish_latest('FINALIZED report-a'); self.tick()
        self.assertIn('artifact: report-b', self.launched[-1]['stdin'])
        self.assertIn('every current handed-off artifact', self.launched[-1]['stdin'])
        self.finish_latest('FINALIZED report-b'); self.tick()
        self.assertEqual(len(self.launched), 4)

    def test_finalizer_conflict_hands_back_to_worker(self):
        self.done(); self.tick(); self.conclude(); self.tick()
        self.finish_latest('FINALIZE_BLOCKED reports/outage.md Resolve publishing permissions')
        self.tick()
        self.assertIn('[swarm role: worker]', self.launched[-1]['stdin'])
        self.assertIn('Resolve publishing permissions', self.launched[-1]['stdin'])

    def test_finalized_opaque_ref_with_spaces(self):
        self.done('reports/my report.md'); self.tick(); self.conclude(ref='reports/my report.md'); self.tick()
        self.finish_latest('FINALIZED reports/my report.md'); self.tick()
        self.assertEqual(len(self.launched), 2)

    def test_same_ref_new_handoff_requires_new_verdict(self):
        self.done(); self.tick(); self.conclude(); self.done(summary='Changed after verdict'); self.tick()
        self.assertIn('[swarm role: judge]', self.launched[-1]['stdin'])

    def test_owner_proof_and_kill_switches_and_human_wait(self):
        self.done()
        for field, value in [('status', 'paused'), ('supervise', False), ('waiting_on', 'owner approval')]:
            with self.subTest(field=field):
                self.h.update_job('J', **{field: value})
                self.tick()
                self.assertFalse(self.launched)
                self.h.update_job('J', status='active', supervise=True, waiting_on=None)
        with mock.patch('swarm.supervisor.orphans.local_record', return_value=None):
            self.assertEqual(self.tick(), set())
        with mock.patch('swarm.supervisor.orphans.human_question', return_value=True):
            self.tick()
        self.cfg['pipeline']['enabled'] = False
        self.assertEqual(self.tick(), set())
        self.assertFalse(self.launched)

    def test_limits_gave_up_and_no_launch(self):
        self.done(); self.tick(); self.conclude('not_met', next_steps='Fix analysis')
        self.sup['max_restarts_per_job'] = 1
        self.tick(); self.tick()
        gave_up = [m for m in self.b.messages_after(0, 'J') if m.message.startswith('GAVE UP')]
        self.assertEqual(len(gave_up), 1)
        self.assertEqual(len(self.launched), 1)

    def test_backoff_before_fix_and_failed_launch_retry(self):
        self.done(); self.tick(); self.conclude('not_met', next_steps='Fix report')
        self.sup['backoff_minutes'] = [10]
        self.tick()
        self.assertEqual(len(self.launched), 1)
        self.assertTrue(any('backoff' in line for line in self.output))
        self.sup['backoff_minutes'] = [0]
        self.tick(start_runner=mock.Mock(side_effect=OSError('unavailable')))
        self.assertEqual(self.b.restarts(job='J')[-1].outcome, 'failed')
        self.tick()
        self.assertEqual(len(self.launched), 2)

    def test_dry_run_writes_nothing(self):
        self.done()
        before = len(self.b.messages_after(0, 'J'))
        self.tick(dry_run=True)
        self.assertFalse(self.launched)
        self.assertEqual(len(self.b.messages_after(0, 'J')), before)
        self.assertEqual(self.b.restarts(job='J'), [])
        self.assertEqual(self.state, {})

    def test_disabled_recipe_integrate_holds_met(self):
        self.done(); self.tick(); self.conclude()
        self.recipe.return_value = {'enabled': False}
        self.tick()
        self.assertEqual(len(self.launched), 1)

    def test_evidence_probe_failure_holds_met(self):
        self.done(); self.tick(); self.conclude()
        self.recipe.return_value = {'evidence_check': mock.Mock(side_effect=OSError('probe failed'))}
        self.tick()
        self.assertEqual(len(self.launched), 1)

    def test_recipe_failure_never_falls_back_to_completed(self):
        self.done(); self.tick(); self.conclude()
        self.recipe.side_effect = ValueError('bad team configuration')
        self.tick()
        self.assertEqual(len(self.launched), 1)
        self.assertEqual(self.b.job_status('J').status, 'active')

    def test_explicit_job_instructions_override_recipe(self):
        self.b.set_job_data('J', 'pipeline.evidence_command', 'custom health check')
        self.b.set_job_data('J', 'pipeline.finalize', 'Publish reviewed report')
        self.recipe.return_value = dict(evidence_command='other check', finalize='other task')
        self.done(); self.tick()
        self.assertIn('custom health check', self.launched[-1]['stdin'])
        self.conclude(); self.tick()
        self.assertIn('Publish reviewed report', self.launched[-1]['stdin'])

    def test_no_manager_or_missing_binary_cannot_launch(self):
        self.done()
        self.tick(scope_ok=lambda: False)
        self.tick(which=lambda binary: None)
        self.assertFalse(self.launched)

    def test_per_artifact_judge_binding_follows_resume(self):
        self.done(); self.tick()
        key, _ = self.enroll_latest()
        self.assertEqual(review.judge_artifact(self.b, 'J', key), 'reports/outage.md')
        self.assertEqual(self.b.job_status('J').judge, self.launched[-1]['name'])

    def test_unrelated_handoff_does_not_consume_completed_fix_without_done(self):
        self.done('report-a'); self.tick(); self.conclude('not_met', ref='report-a', next_steps='Fix A')
        self.tick(); self.finish_latest()
        self.done('report-b'); self.tick(); self.conclude(ref='report-b'); self.tick()
        self.assertIn('[swarm role: worker]', self.launched[-1]['stdin'])
        self.assertIn('Fix A', self.launched[-1]['stdin'])

    def test_plugin_error_on_one_job_does_not_prevent_other_job(self):
        self.done()
        self.b.open_job('Other', 'Other investigation', 'Report', 'owner', None)
        self.b.set_job_goal('Other', 'Reviewed report')
        self.b.post('Other', 'Worker', 'DONE other-report')
        def broken(cfg, board, job, artifact, **kw):
            if job == 'J':
                raise ValueError('broken recipe')
            return {}
        self.recipe.side_effect = broken
        self.tick()
        self.assertEqual(len(self.launched), 1)
        self.assertEqual(self.launched[0]['job'], 'Other')

    def test_explicit_judge_model_and_verified_workdir_used(self):
        self.cfg['pipeline']['judge_model'] = 'configured-judge'
        self.done(); self.tick()
        run = self.launched[-1]
        self.assertIn('configured-judge', run['argv'])
        self.assertEqual(run['cwd'], '/work/project')

    def test_supersession_reversal_clears_old_mapping(self):
        self.recipe.side_effect = lambda cfg, board, job, artifact, **kw: {'artifact_group': 'group'}
        self.done('S'); self.done('T'); self.tick()
        self.assertIn('T', [value for key, value in self.b.job_data('J').items() if key.startswith('pipeline.superseded.')])
        self.conclude(ref='T'); self.done('S'); self.tick()
        data = {key: value for key, value in self.b.job_data('J').items() if key.startswith('pipeline.superseded.')}
        self.assertEqual(list(data.values()), ['S'])

    def test_fix_harness_from_private_worker_enrollment(self):
        self.done(); self.tick(); self.conclude('not_met', next_steps='Fix report')
        rec = NS(cwd='/enrolled/worker', harness='claude')
        with mock.patch('swarm.supervisor.command.enrolment_of', return_value=rec):
            self.tick()
        self.assertEqual(self.launched[-1]['harness'], 'claude')


    def test_judge_role_model_wins_over_general_replacement_override(self):
        self.cfg['supervise'] = {'model_override': 'worker-override'}
        self.cfg['models'] = {'mode': 'default', 'claude': {'judge': 'judge-model', 'worker': 'worker-model'}}
        self.done(); self.tick()
        self.assertIn('judge-model', self.launched[-1]['argv'])
        self.assertNotIn('worker-override', self.launched[-1]['argv'])

    def test_explicit_empty_instructions_override_plugin_defaults(self):
        self.b.set_job_data('J', 'pipeline.evidence_command', '')
        self.b.set_job_data('J', 'pipeline.finalize', '')
        self.recipe.return_value = dict(evidence_command='plugin-ci', evidence_check=lambda cwd: True,
                                        finalize='plugin-merge')
        self.done(); self.tick()
        self.assertNotIn('plugin-ci', self.launched[-1]['stdin'])
        self.conclude(); self.tick()
        self.assertIn('[swarm role: finalizer]', self.launched[-1]['stdin'])
        self.assertNotIn('plugin-merge', self.launched[-1]['stdin'])


    def test_open_executor_without_first_hook_suppresses_new_artifact_judge(self):
        self.done('A'); self.tick(); self.conclude(ref='A'); self.tick()
        self.done('B'); self.tick()
        self.assertEqual(len(self.launched), 2)
        self.finish_latest('FINALIZED A'); self.tick()
        self.assertIn('[swarm role: judge]', self.launched[-1]['stdin'])
        self.assertIn('artifact: B', self.launched[-1]['stdin'])

    def test_rejudging_same_handoff_invalidates_old_finalization(self):
        self.done(); self.tick(); self.conclude(); self.tick()
        self.finish_latest('FINALIZED reports/outage.md')
        name = self.b.allocate_name('new-judge', 'J', 'judge')
        self.assertTrue(self.b.claim_judge('new-judge', 'J'))
        self.b.record_verdict('J', name, 'met', 'New goal accepted', artifact='reports/outage.md')
        self.b.close_agent('new-judge', 'done')
        self.tick()
        self.assertEqual(len(self.launched), 3)
        self.assertIn('[swarm role: finalizer]', self.launched[-1]['stdin'])

    def test_rejudged_artifact_uses_its_own_previous_next(self):
        self.done('A'); self.tick(); self.conclude('not_met', ref='A', next_steps='Fix A only')
        self.done('B'); self.tick(); self.conclude('not_met', ref='B', next_steps='Fix B only')
        self.done('A'); self.tick()
        self.assertIn('Fix A only', self.launched[-1]['stdin'])
        self.assertNotIn('Fix B only', self.launched[-1]['stdin'])



if __name__ == '__main__':
    unittest.main()
