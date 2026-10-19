"""Recovery restores hook routing without changing owner decisions or caller identity."""
import json
import errno
import threading
from unittest import mock
from test_hooks_cli import Env
from swarm import cli, dependencies, enrolment, passcmd
from swarm.board.autoinit import store_key
from swarm.board.base import AUTO_CLOSED_BY


class HookRecovery(Env):
    def setUp(self):
        super().setUp()
        self.sid = 'original-session'
        with self.board() as board:
            board.open_job('J', None, None, 'display-session', 'owner', goal='Full task')
            dependencies.register(board, 'J', 1, 'pending', artifact='source@abc', depends=[],
                role='engineer', harness='codex', model=None, workdir=str(self.tmp),
                brief='Finish work', queued_by='owner')
            board.close_job('J', 'cancelled', 'auto-closed: no live agents for 30 min',
                            closed_by=AUTO_CLOSED_BY)
        enrolment.write_job(store_key(self.cfg), job='J', harness='codex',
                            session_id=self.sid, cwd=str(self.tmp))

    def bindings(self):
        return [cli._read_marker(p) for p in cli._job_markers(self.cfg, 'J')]

    def contended_pass(self, already_recovered):
        entered, release = threading.Event(), threading.Event()
        def hold_coordinator_lock():
            with enrolment.coordinator_lock(store_key(self.cfg), 'J'):
                entered.set()
                release.wait(10)
        with self.board() as board:
            if already_recovered:
                self.assertTrue(board.recover_auto_close('J'))
                task_job = 'J'
            else:
                task_job = 'independent'
                board.open_job(task_job, None, None, None, 'owner', goal='Independent task')
                dependencies.register(board, task_job, 1, 'independent-stage', artifact='other@abc',
                    depends=[], role='engineer', harness='codex', model=None, workdir=str(self.tmp),
                    brief='Independent ready work', queued_by='owner')
            runs = []
            worker = threading.Thread(target=hold_coordinator_lock)
            worker.start()
            try:
                self.assertTrue(entered.wait(3))
                with mock.patch('swarm.supervisor.command.workdir_problem', return_value=None), \
                        mock.patch('swarm.supervisor.markers.write_resume_marker', return_value=self.tmp / 'resume'):
                    actions = passcmd.run_pass(board, self.cfg, starter=lambda cfg, run: runs.append(run))
                self.assertFalse(any('pass error' in action for action in actions), actions)
                self.assertEqual([run['job'] for run in runs], [task_job])
                self.assertEqual(board.job_status('J').status, 'active' if already_recovered else 'cancelled')
                self.assertEqual(self.bindings(), [], 'routing repair must wait for the lock holder')
            finally:
                release.set()
                worker.join(3)
            self.assertFalse(worker.is_alive())
            with mock.patch('swarm.supervisor.command.workdir_problem', return_value=None), \
                    mock.patch('swarm.supervisor.markers.write_resume_marker', return_value=self.tmp / 'resume'):
                actions = passcmd.run_pass(board, self.cfg, starter=lambda cfg, run: runs.append(run))
            self.assertFalse(any('pass error' in action for action in actions), actions)
            self.assertEqual(board.job_status('J').status, 'active')
            self.assertTrue(any(m.get('session_id') == self.sid for m in self.bindings()))
            self.assertEqual(len(runs), 1 if already_recovered else 2)

    def test_recovery_lock_contention_defers_until_next_pass_without_starving_ready_task(self):
        self.contended_pass(already_recovered=False)

    def test_routing_lock_contention_defers_without_blocking_native_ready_task(self):
        self.contended_pass(already_recovered=True)

    def test_non_contention_blocking_io_errors_remain_visible(self):
        for operation, phase in (('recover_auto_close', 'Recovery'), ('repair_open_routing', 'Routing')):
            with self.subTest(operation=operation), self.board() as board:
                with mock.patch.object(passcmd, operation, side_effect=BlockingIOError(errno.EIO, 'disk failure')):
                    actions = passcmd.run_pass(board, self.cfg, starter=lambda *_: None)
                self.assertTrue(any(f'pass error {phase} BlockingIOError' in action for action in actions), actions)

    def test_failed_marker_creation_can_retry_without_stranding_open_job(self):
        with self.board() as board:
            with mock.patch('swarm.safefs.create', side_effect=OSError('disk full')):
                with self.assertRaises(OSError):
                    passcmd.recover_auto_close(board, self.cfg, 'J')
            self.assertEqual(board.job_status('J').status, 'cancelled')
            self.assertTrue(passcmd.recover_auto_close(board, self.cfg, 'J'))
            self.assertEqual(board.job_status('J').status, 'active')
        self.assertTrue(any(m['session_id'] == self.sid for m in self.bindings()))

    def test_worker_resume_marker_does_not_replace_original_binding(self):
        path = self.markers_path('J--worker.json')
        cli._write_marker(path, {'job': 'J', 'session_id': 'worker-session', 'resume': {'agent_key': 'worker'}})
        with self.board() as board:
            self.assertTrue(passcmd.recover_auto_close(board, self.cfg, 'J'))
        self.assertTrue(any(m.get('session_id') == self.sid and 'resume' not in m for m in self.bindings()))
        self.assertEqual(cli._read_marker(path)['session_id'], 'worker-session')

    def markers_path(self, name):
        return self.markers / name

    def test_owner_close_is_not_recovered(self):
        with self.board() as board:
            board.close_job('J', 'cancelled', 'owner decision', closed_by='owner')
            self.assertFalse(passcmd.recover_auto_close(board, self.cfg, 'J'))
        self.assertEqual(self.bindings(), [])

    def test_owner_deactivation_cannot_race_routing_recovery(self):
        entered, release = threading.Event(), threading.Event()
        def recovery_holds_lock():
            with enrolment.coordinator_lock(store_key(self.cfg), 'J'):
                entered.set()
                release.wait(3)
        worker = threading.Thread(target=recovery_holds_lock)
        worker.start()
        try:
            self.assertTrue(entered.wait(2))
            rc, _, err = self.cli('deactivate', '--job', 'J', '--force')
            self.assertEqual(rc, 1)
            self.assertIn('coordinator operation', err)
        finally:
            release.set()
            worker.join(3)

    def test_missing_private_activation_does_not_mint_hook_authority(self):
        enrolment.remove_job(store_key(self.cfg), 'J')
        with self.board() as board:
            self.assertTrue(passcmd.recover_auto_close(board, self.cfg, 'J'))
        self.assertEqual(self.bindings(), [])

    def test_conflicting_marker_is_not_overwritten(self):
        path = self.markers / ('J--' + self.sid + '.json')
        cli._write_marker(path, {'job': 'J', 'session_id': 'different-session'})
        before = path.read_bytes()
        with self.board() as board:
            with self.assertRaises(FileExistsError):
                passcmd.recover_auto_close(board, self.cfg, 'J')
            self.assertEqual(board.job_status('J').status, 'cancelled')
        self.assertEqual(path.read_bytes(), before)

    def test_one_recovery_failure_does_not_starve_other_jobs(self):
        with self.board() as board:
            board.open_job('other', None, None, None, 'owner')
            def repair(_board, _cfg, job):
                if job == 'J':
                    raise OSError('disk full')
                return False
            with mock.patch.object(passcmd, 'recover_auto_close', side_effect=repair), \
                    mock.patch.object(passcmd, '_advance', return_value=[]) as advance:
                acts = passcmd.run_pass(board, self.cfg)
            self.assertTrue(any('J: pass error Recovery' in a for a in acts))
            self.assertTrue(any(call.args[2].job == 'other' for call in advance.call_args_list))

    def test_stale_activation_proof_does_not_bind_a_new_run(self):
        enrolment.write_job(store_key(self.cfg), job='J', harness='codex',
                            session_id='old-session', cwd=str(self.tmp), now=1)
        with self.board() as board:
            self.assertTrue(passcmd.recover_auto_close(board, self.cfg, 'J'))
        self.assertEqual(self.bindings(), [])

    def test_pass_repairs_routing_of_already_recovered_open_run(self):
        with self.board() as board:
            self.assertTrue(board.recover_auto_close('J'))  # historical version reopened without routing
            with mock.patch.object(passcmd, '_advance', return_value=[]):
                passcmd.run_pass(board, self.cfg)
        self.assertTrue(any(m['session_id'] == self.sid for m in self.bindings()))

    def test_pass_does_not_undo_explicit_local_routing_stop(self):
        enrolment.disable_job_routing(store_key(self.cfg), 'J')
        with self.board() as board:
            self.assertTrue(board.recover_auto_close('J'))
            with mock.patch.object(passcmd, '_advance', return_value=[]):
                passcmd.run_pass(board, self.cfg)
        self.assertEqual(self.bindings(), [])

    def test_new_trusted_activation_supersedes_local_routing_stop(self):
        enrolment.disable_job_routing(store_key(self.cfg), 'J')
        old = enrolment.find_job(store_key(self.cfg), 'J')
        enrolment.write_job(store_key(self.cfg), job='J', harness='codex', session_id=self.sid,
                            cwd=str(self.tmp), now=old.created_at + 1)
        with self.board() as board:
            self.assertTrue(board.recover_auto_close('J'))
            with mock.patch.object(passcmd, '_advance', return_value=[]):
                passcmd.run_pass(board, self.cfg)
        self.assertTrue(any(m['session_id'] == self.sid for m in self.bindings()))

    def test_explicit_deactivate_persists_stop_before_removing_marker(self):
        with self.board() as board:
            self.assertTrue(board.recover_auto_close('J'))
        rec = enrolment.find_job(store_key(self.cfg), 'J')
        rc, _, err = self.cli('deactivate', '--job', 'J', '--force')
        self.assertEqual(rc, 0, err)
        self.assertTrue(enrolment.job_routing_disabled(rec))

    def test_routing_failure_does_not_block_native_tasks_in_same_job(self):
        with self.board() as board:
            self.assertTrue(board.recover_auto_close('J'))
            with mock.patch.object(passcmd, 'repair_open_routing', side_effect=OSError('disk full')), \
                    mock.patch.object(passcmd, '_advance', return_value=[]) as advance:
                acts = passcmd.run_pass(board, self.cfg)
            self.assertTrue(any(': pass error Routing' in a for a in acts))
            self.assertTrue(any(call.args[2].job == 'J' for call in advance.call_args_list))

    def test_remote_close_during_restore_removes_only_created_binding(self):
        with self.board() as board:
            board.recover_auto_close('J')
            original = passcmd._restore_hook_binding
            def close_during_restore(*args):
                result = original(*args)
                board.close_job('J', 'cancelled', 'remote owner', closed_by='owner')
                return result
            with mock.patch.object(passcmd, '_restore_hook_binding', side_effect=close_during_restore):
                self.assertFalse(passcmd.repair_open_routing(board, self.cfg, 'J'))
        self.assertEqual(self.bindings(), [])

    def test_new_run_during_restore_cannot_inherit_old_binding(self):
        with self.board() as board:
            board.recover_auto_close('J')
            original = passcmd._restore_hook_binding
            def new_run_during_restore(*args):
                result = original(*args)
                board.close_job('J', 'cancelled', 'remote owner', closed_by='owner')
                board.open_job('J', None, None, 'new-session', 'owner')
                return result
            with mock.patch.object(passcmd, '_restore_hook_binding', side_effect=new_run_during_restore):
                self.assertFalse(passcmd.repair_open_routing(board, self.cfg, 'J'))
        self.assertEqual(self.bindings(), [])

    def test_pass_retires_recovered_binding_after_later_remote_close(self):
        with self.board() as board:
            board.recover_auto_close('J')
            self.assertTrue(passcmd.repair_open_routing(board, self.cfg, 'J'))
            board.close_job('J', 'cancelled', 'remote owner', closed_by='owner')
            with mock.patch.object(passcmd, '_advance', return_value=[]):
                passcmd.run_pass(board, self.cfg)
        self.assertEqual(self.bindings(), [])

    def test_routing_stop_is_durable_before_marker_removal(self):
        from swarm import safefs
        with mock.patch.object(safefs, 'write_atomic', wraps=safefs.write_atomic) as write:
            enrolment.disable_job_routing(store_key(self.cfg), 'J')
        calls = [c for c in write.call_args_list if c.args[1].startswith('routing-stop-')]
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0].kwargs.get('durable'))
