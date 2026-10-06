"""Generic hand-offs, artifact verdicts and mandatory judge conclusions."""
import hashlib
import datetime as dt
import subprocess
from unittest import mock
from types import SimpleNamespace

from test_goals import GoalEnv
from swarm import review, respawn


class ReviewHookTests(GoalEnv):
    def setUp(self):
        super().setUp()
        self.activate_goal()
        self.spawn_worker('w1')
        self.worker = self.member('w1').name
        self.spawn_judge()
        self.judge = self.member('judge-1').name

    def done(self, artifact='report.md'):
        rc, out, err = self.cli('done', '--job', 'J', '--as', self.worker,
                               '--artifact', artifact, '--summary', 'inspection report')
        self.assertEqual(rc, 0, err)

    def verdict(self, artifact='report.md', verdict='met'):
        extra = ['checked'] if verdict == 'met' else ['--reason', 'incomplete', '--next', 'complete the report']
        rc, out, err = self.cli('verdict', '--job', 'J', '--as', self.judge,
                               '--artifact', artifact, verdict, *extra)
        self.assertEqual(rc, 0, err)

    def test_changed_goal_invalidates_artifact_verdicts(self):
        self.done()
        self.verdict()
        with self.board() as b:
            b.set_job_goal('J', 'new goal')
            self.assertEqual(review.artifact_verdicts(b, 'J'), {})
            self.assertEqual(review.pending_artifacts(b, 'J'), ['report.md'])

    def test_reactivated_job_does_not_reuse_prior_handoffs(self):
        self.done()
        self.verdict()
        self.activate_goal()
        with self.board() as b:
            self.assertEqual(review.latest_handoffs(b, 'J'), [])

    def test_report_artifact_summary_and_verdict_roundtrip(self):
        self.done()
        self.verdict()
        with self.board() as b:
            handoff = review.latest_handoffs(b, 'J')[0]
            self.assertEqual((handoff.artifact, handoff.summary), ('report.md', 'inspection report'))
            self.assertTrue(review.covered(handoff, review.artifact_verdicts(b, 'J')['report.md']))
            self.assertEqual(b.job_status('J').verdict_artifact, 'report.md')
        rc, out, err = self.cli('status', '--job', 'J')
        self.assertIn('artifact   report.md: met', out)

    def test_two_artifact_verdicts_survive_latest_job_verdict(self):
        self.done('first.pdf')
        self.done('second.pdf')
        self.verdict('first.pdf')
        self.verdict('second.pdf', 'not_met')
        with self.board() as b:
            history = review.artifact_verdicts(b, 'J')
            self.assertEqual(history['first.pdf']['verdict'], 'met')
            self.assertEqual(history['second.pdf']['verdict'], 'not_met')
            self.assertEqual(history['second.pdf']['next_steps'], 'complete the report')

    def test_later_artifact_or_new_handoff_requires_own_verdict(self):
        self.done('deploy-a')
        self.verdict('deploy-a')
        self.done('deploy-b')
        rc, _, err = self.cli('deactivate', '--job', 'J')
        self.assertEqual(rc, 1)
        self.assertIn('deploy-b', err)
        self.done('deploy-a')
        with self.board() as b:
            h = next(h for h in review.latest_handoffs(b, 'J') if h.artifact == 'deploy-a')
            self.assertFalse(review.covered(h, review.artifact_verdicts(b, 'J')['deploy-a']))

    def test_branch_sha_compatibility_and_plain_board_convention(self):
        rc, _, err = self.cli('done', '--job', 'J', '--as', self.worker,
                             '--branch', 'feature', '--sha', 'abcdef0')
        self.assertEqual(rc, 0, err)
        self.cli('post', '--job', 'J', '--as', self.worker, 'DONE other abcdef1')
        with self.board() as b:
            self.assertEqual([h.artifact for h in review.latest_handoffs(b, 'J')],
                             ['feature@abcdef0', 'other@abcdef1'])

    def test_opaque_reference_with_spaces_and_pipe(self):
        self.done('reports/health | overview.pdf')
        with self.board() as b:
            h = review.latest_handoffs(b, 'J')[0]
            self.assertEqual(h.artifact, 'reports/health | overview.pdf')
            self.assertEqual(h.summary, 'inspection report')

    def test_summary_only_handoff_has_opaque_reference(self):
        rc, _, err = self.cli('done', '--job', 'J', '--as', self.worker, '--summary', 'health checked')
        self.assertEqual(rc, 0, err)
        with self.board() as b:
            self.assertTrue(review.latest_handoffs(b, 'J')[0].artifact.startswith('handoff-'))

    def test_stop_blocks_until_verdict_is_recorded(self):
        self.done()
        out = self.hook('stop', agent_id='judge-1')
        self.assertEqual(out['decision'], 'block')
        self.assertIsNotNone(self.job().judge)
        self.verdict()
        self.assertIsNone(self.hook('stop', agent_id='judge-1'))
        self.assertIsNone(self.job().judge)

    def test_pending_external_evidence_always_concludes_not_met(self):
        self.done()
        with self.board() as b:
            b.set_job_pipeline('J', 'check-health {artifact}', None)
        with mock.patch('subprocess.run', side_effect=subprocess.TimeoutExpired('check-health', 2)):
            self.hook('stop', agent_id='judge-1')
        self.assertEqual(self.job().verdict, 'not_met')
        self.assertEqual(self.job().verdict_reason, 'evidence pending')
        self.assertEqual(self.job().verdict_artifact, 'report.md')

    def test_expiry_sweep_does_not_cancel_met_unfinalized_handoff(self):
        from swarm.board.base import CloseGuard
        self.done()
        self.verdict()
        with self.board() as b:
            self.assertIsNone(b._expiry_action(b.job_status('J'), b.now() + dt.timedelta(days=5),
                                             4, 10, 0, None))
            self.assertFalse(b.close_job('J', 'cancelled', 'orphan', guard=CloseGuard()))
            self.assertEqual(b.job_status('J').status, 'active')

    def test_auto_close_waits_for_each_artifact_and_finalization(self):
        self.done('a')
        self.done('b')
        self.verdict('a')
        self.verdict('b')
        self.hook('stop', agent_id='judge-1')
        self.hook('stop', agent_id='w1')
        with self.board() as b:
            before = b.now() + dt.timedelta(seconds=1)
            self.assertIsNone(b.auto_close_job('J', before, 'premature'))
            b.post('J', 'executor', 'FINALIZED a')
            self.assertIsNone(b.auto_close_job('J', before, 'still premature'))
            b.post('J', 'executor', 'FINALIZED b')
            self.assertIsNotNone(b.auto_close_job('J', before, 'finalized'))

    def test_headless_judge_stop_before_first_tool_call_takes_seat_and_blocks(self):
        from swarm import hooks
        with self.board() as b:
            b.agent_stopped('judge-1')
        marker = {'job': 'J', 'resume': {'agent_key': 'new-judge', 'resume_of': 'previous-seat'}}
        def enroll(board, key, resume, payload, cfg):
            board.allocate_name(key, 'J', 'judge')
            board.claim_judge(key, 'J')
        with mock.patch.object(hooks, '_resume_binding', return_value=marker), \
                mock.patch.object(hooks, '_enrol_resumed', side_effect=enroll) as admit:
            out = self.hook('session-stop', agent_id=None)
        admit.assert_called_once()
        self.assertEqual(out['decision'], 'block')
        self.assertIn('must record swarm verdict', out['reason'])

    def test_headless_judge_session_stop_blocks_when_board_is_down(self):
        from swarm import hooks
        from swarm.board.base import BoardUnavailable
        marker = {'resume': {'agent_key': 'judge-1', 'resume_of': 'previous-seat'}}
        with mock.patch.object(hooks, '_resume_binding', return_value=marker), \
                mock.patch('swarm.board.open_board', side_effect=BoardUnavailable('offline')):
            out = self.hook('session-stop', agent_id=None)
        self.assertEqual(out['decision'], 'block')
        self.assertIn('board is unavailable', out['reason'])

    def test_recipe_failure_still_blocks_stop(self):
        self.done()
        with mock.patch('swarm.plugins.pipeline_recipe', side_effect=RuntimeError('recipe unavailable')):
            out = self.hook('stop', agent_id='judge-1')
        self.assertEqual(out['decision'], 'block')
        self.assertIsNotNone(self.job().judge)

    def test_evidence_command_never_runs_in_privileged_stop_hook(self):
        self.done()
        with self.board() as b:
            b.set_job_pipeline('J', 'rm forbidden-path', None)
        with mock.patch('subprocess.run') as run:
            self.hook('stop', agent_id='judge-1')
        run.assert_not_called()
        self.assertEqual(self.job().verdict_reason, 'evidence pending')

    def test_superseded_failed_revision_does_not_block_completion(self):
        self.done('branch@old')
        self.verdict('branch@old', 'not_met')
        self.done('branch@new')
        self.verdict('branch@new')
        self.verdict('branch@old', 'not_met')  # legacy latest field is not authoritative
        key = 'pipeline.superseded.' + hashlib.sha256(b'branch@old').hexdigest()[:32]
        with self.board() as b:
            b.set_job_data('J', key, 'branch@new')
            b.post('J', 'executor', 'FINALIZED branch@new')
        rc, _, err = self.cli('deactivate', '--job', 'J')
        self.assertEqual(rc, 0, err)

    def test_judge_read_call_never_grants_a_policy_bypass_lease(self):
        from swarm import hooks
        lease = SimpleNamespace(eligible=True)
        with self.board() as b, mock.patch.dict(hooks._CURRENT, {'lease': lease}):
            self.assertTrue(hooks._gate_judge(b, 'judge-1', 'J',
                                            {'tool_name': 'Bash', 'tool_input': {'command': 'git status'}}))
            self.assertFalse(lease.eligible)

    def test_judge_cannot_edit_push_merge_or_spawn(self):
        for tool, data in [('Write', {'file_path': 'report.md', 'content': 'fix'}),
                           ('Bash', {'command': 'git push origin main'}),
                           ('Bash', {'command': 'git merge feature'}),
                           ('Agent', {'prompt': 'fix it'})]:
            out = self.hook('turn', agent_id='judge-1', tool_name=tool, tool_input=data,
                            transcript_path=self.main_transcript())
            self.assertEqual(out['hookSpecificOutput']['permissionDecision'], 'deny', out)

    def test_assigned_judge_verdict_defaults_to_its_artifact(self):
        self.done('first.pdf')
        self.done('second.pdf')
        key = 'pipeline.judge.' + hashlib.sha256(b'judge-1').hexdigest()[:32]
        with self.board() as b:
            b.set_job_data('J', key, 'first.pdf')
        rc, _, err = self.cli('verdict', '--job', 'J', '--as', self.judge, 'met', 'checked first')
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.job().verdict_artifact, 'first.pdf')

    def test_long_fix_brief_is_preserved(self):
        self.done()
        brief = 'inspect health and remedy failures. ' * 250
        with self.board() as b:
            self.assertTrue(b.record_verdict('J', self.judge, 'not_met', 'health red', brief, 'report.md'))
            self.assertEqual(review.artifact_verdicts(b, 'J')['report.md']['next_steps'], brief)

    def test_pipeline_reminders_are_informational(self):
        self.verdict(verdict='not_met')
        self.hook('stop', agent_id='judge-1')
        self.hook('stop', agent_id='w1')
        out = self.hook('session-stop', agent_id=None)
        self.assertNotIn('decision', out or {})
        self.assertIn('supervisor review pipeline', self.context(out))
