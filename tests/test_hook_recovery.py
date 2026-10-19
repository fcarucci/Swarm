"""Recovery restores hook routing without changing owner decisions or caller identity."""
import json
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
