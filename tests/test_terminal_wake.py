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

    def test_runner_finish_wakes_after_restart_is_settled(self):
        run = self.run_dict(['true'])
        seen = []
        def wake(cfg):
            seen.append(self.restart().outcome)
        with mock.patch.object(dependencies, 'wake', side_effect=wake):
            self.assertTrue(runner.finish(self.cfg, run, 'completed'))
        self.assertEqual(seen, ['completed'])
