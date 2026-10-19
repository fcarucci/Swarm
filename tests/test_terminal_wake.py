"""Terminal lifecycle events wake durable reconciliation after their state is visible."""
from unittest import mock

from test_hooks_cli import Env
from test_supervise_runner import RunnerBase
from swarm import dependencies
from swarm.supervisor import runner


class HookWakeTests(Env):
    def test_codex_turn_end_wakes_after_recording_idle(self):
        self.cli('activate', '--job', 'J', '--session', 'sess-1')
        self.hook('start', agent_id='a', host='codex')
        self.hook('turn', agent_id='a', host='codex', tool_name='Bash')
        seen = []
        def wake(cfg):
            with self.board() as board:
                row = next(a for a in board.agents('J') if a.agent_key == 'a')
                seen.append((row.status, row.turn_ended_at is not None))
        with mock.patch.object(dependencies, 'wake', side_effect=wake):
            self.hook('stop', agent_id='a', host='codex')
        self.assertEqual(seen, [('idle', True)])

    def test_terminal_wake_failure_does_not_break_hook(self):
        with mock.patch.object(dependencies, 'wake', side_effect=OSError('unavailable')) as wake:
            self.hook('session-end', agent_id=None, host='codex')
        self.assertEqual(wake.call_count, 1)


class RunnerWakeTests(RunnerBase):
    def test_cancel_during_launch_preparation_prevents_model_spawn(self):
        run = self.run_dict(['true'])
        def prepare(cfg, row):
            with self.board() as board:
                board.close_job('J', 'cancelled', 'owner cancelled')
            return row['argv'], None
        with mock.patch.object(runner, '_launch_argv', side_effect=prepare), \
             mock.patch.object(dependencies, 'wake'):
            spawned = mock.Mock(side_effect=AssertionError('model spawned after owner cancellation'))
            outcome = runner.execute(self.cfg, run, popen=spawned)
        self.assertEqual(outcome, 'cancelled')
        spawned.assert_not_called()

    def test_runner_preserves_terminal_outcome_and_records_returned_wake_error(self):
        run = self.run_dict(['true'])
        reason = 'events listener runtime changed; restart it'
        with self.board() as board:
            board.open_job('unrelated', None, None, None, 'owner')
            board.set_job_data('unrelated', 'task.reconciler_error', 'keep')
        with mock.patch.object(dependencies, 'wake', return_value=reason), \
                mock.patch('swarm.supervisor.settings.log') as log:
            self.assertTrue(runner.finish(self.cfg, run, 'completed'))
        self.assertEqual(self.restart().outcome, 'completed')
        with self.board() as board:
            self.assertEqual(board.job_data('J').get('task.reconciler_error'), reason)
            self.assertEqual(board.job_data('unrelated')['task.reconciler_error'], 'keep')
        self.assertTrue(any(reason in str(call) for call in log.call_args_list))

    def test_runner_retains_raised_startup_error_after_successful_completion(self):
        run = self.run_dict(['true'])
        with mock.patch.object(dependencies, 'wake', side_effect=OSError('listener unavailable')):
            self.assertTrue(runner.finish(self.cfg, run, 'completed'))
        self.assertEqual(self.restart().outcome, 'completed')
        with self.board() as board:
            self.assertIn('OSError', board.job_data('J')['task.reconciler_error'])

    def test_runner_finish_wakes_after_restart_is_settled(self):
        run = self.run_dict(['true'])
        seen = []
        def wake(cfg):
            seen.append(self.restart().outcome)
        with mock.patch.object(dependencies, 'wake', side_effect=wake):
            self.assertTrue(runner.finish(self.cfg, run, 'completed'))
        self.assertEqual(seen, ['completed'])


class TerminalReconciliationErrors(Env):
    def test_returned_startup_error_is_visible_for_only_the_stopping_job(self):
        self.cli('activate', '--job', 'J', '--session', 'sess-1')
        self.hook('start', agent_id='a', host='codex')
        self.hook('turn', agent_id='a', host='codex', tool_name='Bash')
        with self.board() as board:
            board.open_job('unrelated', None, None, None, 'owner')
            board.set_job_data('unrelated', 'task.reconciler_error', 'unrelated failure')
        reason = 'events listener runtime changed; restart the existing listener'
        with mock.patch.object(dependencies, 'wake', return_value=reason):
            self.hook('stop', agent_id='a', host='codex', turn_id='last-proof-turn')
        with self.board() as board:
            row = next(a for a in board.agents('J') if a.agent_key == 'a')
            self.assertEqual(row.status, 'idle')
            self.assertIsNotNone(row.turn_ended_at)
            self.assertEqual(board.job_status('J').status, 'active')
            self.assertEqual(board.job_data('J').get('task.reconciler_error'), reason)
            self.assertEqual(board.job_data('unrelated')['task.reconciler_error'], 'unrelated failure')
            from swarm import cli
            self.assertIn(reason, cli.job_detail(board, 'J', False))
        self.assertIn(reason, self.error_log.read_text())
        with mock.patch.object(dependencies, 'wake', return_value=None):
            self.hook('stop', agent_id='a', host='codex')
        with self.board() as board:
            self.assertIsNone(board.job_data('J').get('task.reconciler_error'))
            self.assertEqual(board.job_data('unrelated')['task.reconciler_error'], 'unrelated failure')

    def test_raised_startup_error_is_logged_and_retained_without_blocking_stop(self):
        self.cli('activate', '--job', 'J', '--session', 'sess-1')
        self.hook('start', agent_id='a', host='codex')
        with mock.patch.object(dependencies, 'wake', side_effect=OSError('listener unavailable')):
            self.hook('stop', agent_id='a', host='codex')
        with self.board() as board:
            self.assertIn('OSError', board.job_data('J')['task.reconciler_error'])
            self.assertEqual(next(a for a in board.agents('J') if a.agent_key == 'a').status, 'idle')
        self.assertIn('listener unavailable', self.error_log.read_text())

    def test_unmapped_terminal_error_is_logged_without_creating_or_changing_a_job(self):
        with mock.patch.object(dependencies, 'wake', return_value='listener startup refused'):
            self.hook('session-end', agent_id=None, session='unknown-session', host='codex')
        self.assertTrue(self.error_log.exists(), 'returned startup failures must leave a trace even without a job')
        self.assertIn('listener startup refused', self.error_log.read_text())
        with self.board() as board:
            self.assertEqual(board.jobs(include_closed=True), [])


class NativeRootReconciliationErrors(Env):
    def test_no_goal_native_worker_root_retains_then_clears_own_startup_error(self):
        from swarm import enrolment
        from swarm.board.autoinit import store_key
        self.cli('activate', '--job', 'J', '--session', 'coordinator')
        worker = 'native-worker-root'
        with self.board() as board:
            board.allocate_name(worker, 'J')
            board.record_route(worker, worker, 'final', 'J')
        enrolment.write(store_key(self.cfg), job='J', agent_key=worker, harness='codex',
                        session_id=worker, cwd=str(self.tmp))
        reason = 'native root listener startup failed'
        with mock.patch.object(dependencies, 'wake', return_value=reason):
            self.hook('session-stop', agent_id=None, session=worker, host='codex')
        with self.board() as board:
            self.assertEqual(board.job_data('J').get('task.reconciler_error'), reason)
        with mock.patch.object(dependencies, 'wake', return_value=None):
            self.hook('session-stop', agent_id=None, session=worker, host='codex')
        with self.board() as board:
            self.assertIsNone(board.job_data('J').get('task.reconciler_error'))
