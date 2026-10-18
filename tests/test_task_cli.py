"""Native CLI task registration authority and exact assigned evidence, with real private enrolment."""
import json
import os
from unittest import mock

from test_hooks_cli import Env
from swarm import enrolment
from swarm.board.autoinit import store_key


class TaskCLI(Env):
    def setUp(self):
        super().setUp()
        with self.board() as board:
            board.open_job('J', None, None, None, 'owner', goal='Full engineering project')
        self.wake = mock.patch('swarm.dependencies.wake', return_value=None)
        self.wake.start()
        self.addCleanup(self.wake.stop)
        self.workdir = mock.patch('swarm.supervisor.command.workdir_problem', return_value=None)
        self.workdir.start()
        self.addCleanup(self.workdir.stop)

    def add(self, *extra):
        return self.cli('task', 'add', '--job', 'J', '--name', 'qa', '--artifact', 'main@' + 'a' * 40,
                        '--role', 'qa', '--host', 'codex', '--workdir', str(self.tmp),
                        '--brief', 'Verify exact source', *extra)

    def trust_job(self):
        enrolment.write_job(store_key(self.cfg), job='J', harness='codex', session_id='owner', cwd=str(self.tmp))

    def test_native_registration_requires_existing_trusted_enrolment(self):
        rc, _, err = self.add()
        self.assertEqual(rc, 1)
        self.assertIn('trusted enrolment', err)
        with self.board() as board:
            self.assertEqual(board.launches('J'), [])
        self.trust_job()
        rc, out, err = self.add()
        self.assertEqual(rc, 0, err)
        rc, out, err = self.cli('task', 'list', '--job', 'J', '--json')
        self.assertEqual(rc, 0, err)
        self.assertEqual([(r['name'], r['state']) for r in json.loads(out)], [('qa', 'ready')])
        self.assertIsNone(enrolment.find_job_owner(store_key(self.cfg), 'J'), 'task registration cannot mint drive authority')

    def test_native_observer_cannot_attest_existing_external_agent(self):
        self.trust_job()
        with self.board() as board:
            name = board.allocate_name('qa-seat', 'J', 'qa')
            board.record_route('qa-seat', 'qa-session', 'final', 'J')
        enrolment.write(store_key(self.cfg), job='J', agent_key='qa-seat', harness='codex',
                        session_id='qa-session', cwd=str(self.tmp))
        rc, _, err = self.add('--reporter', name)
        self.assertEqual(rc, 0, err)
        args = ('task', 'report', '--job', 'J', '--name', 'qa', '--artifact', 'main@' + 'a' * 40,
                '--outcome', 'passed', '--reason', 'checked exact source', '--as', name)
        with mock.patch.dict(os.environ, {'CODEX_THREAD_ID': 'observer-thread', 'CODEX_SESSION_ID': 'observer-session'}):
            rc, _, err = self.cli(*args)
        self.assertEqual(rc, 1)
        self.assertIn('assigned enrolled agent session', err)
        with mock.patch.dict(os.environ, {'CODEX_THREAD_ID': 'qa-thread', 'CODEX_SESSION_ID': 'qa-session'}):
            rc, out, err = self.cli(*args)
        self.assertEqual(rc, 0, err)
        self.assertIn('evidence recorded', out)
        with self.board() as board:
            self.assertEqual(len(board.events('J')), 1)

    def test_native_task_startup_failure_persists_concrete_reason(self):
        self.trust_job()
        with mock.patch('swarm.dependencies.wake', return_value='listener launch refused by OS'):
            rc, out, err = self.add()
        self.assertEqual(rc, 0, err)
        self.assertIn('listener launch refused', err)
        with self.board() as board:
            self.assertEqual(board.job_data('J')['task.reconciler_error'], 'listener launch refused by OS')
            self.assertEqual(board.get_launch('J', 1, 'qa').state, 'queued')
