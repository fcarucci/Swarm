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
            board.open_job('J', None, None, 'root-session', 'owner', goal='Full engineering project')
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

    def test_native_registration_uses_exact_live_member_without_host_grant(self):
        with self.board() as board:
            name = board.allocate_name('task-author', 'J', 'engineering_lead')
            board.record_route('task-author', 'root-session', 'final', 'J')
        with mock.patch.dict(os.environ, {'CODEX_THREAD_ID': 'task-author', 'CODEX_SESSION_ID': 'root-session'}):
            rc, out, err = self.add()
        self.assertEqual(rc, 0, err)
        self.assertIn('qa: queued', out)
        rc, out, err = self.cli('task', 'list', '--job', 'J', '--json')
        self.assertEqual(rc, 0, err)
        self.assertEqual([(r['name'], r['state']) for r in json.loads(out)], [('qa', 'ready')])
        self.assertIsNone(enrolment.find_job_owner(store_key(self.cfg), 'J'), 'task registration cannot mint drive authority')

    def test_native_observer_cannot_attest_existing_external_agent(self):
        with self.board() as board:
            name = board.allocate_name('qa-seat', 'J', 'qa')
            board.record_route('qa-seat', 'qa-session', 'final', 'J')
        with mock.patch.dict(os.environ, {'CODEX_THREAD_ID': 'root-session', 'CODEX_SESSION_ID': 'root-session'}):
            rc, _, err = self.add('--reporter', name)
        self.assertEqual(rc, 0, err)
        args = ('task', 'report', '--job', 'J', '--name', 'qa', '--artifact', 'main@' + 'a' * 40,
                '--outcome', 'passed', '--reason', 'checked exact source', '--as', name)
        with mock.patch.dict(os.environ, {'CODEX_THREAD_ID': 'observer-thread', 'CODEX_SESSION_ID': 'observer-session'}):
            rc, _, err = self.cli(*args)
        self.assertEqual(rc, 1)
        self.assertIn('exact assigned actor session', err)
        with mock.patch.dict(os.environ, {'CODEX_THREAD_ID': 'qa-seat', 'CODEX_SESSION_ID': 'qa-session'}):
            rc, out, err = self.cli(*args)
        self.assertEqual(rc, 0, err)
        self.assertIn('evidence recorded', out)
        with self.board() as board:
            self.assertEqual(len(board.events('J')), 1)

    def child_report(self, thread, session='root-session'):
        args = ('task', 'report', '--job', 'J', '--name', 'qa', '--artifact', 'main@' + 'a' * 40,
                '--outcome', 'passed', '--reason', 'checked exact source', '--as', self.child_name)
        with mock.patch.dict(os.environ, {'CODEX_THREAD_ID': thread, 'CODEX_SESSION_ID': session}):
            return self.cli(*args)

    def trusted_child_task(self):
        with self.board() as board:
            self.child_name = board.allocate_name('child-thread', 'J', 'qa')
            board.record_route('child-thread', 'root-session', 'final', 'J')
        with mock.patch.dict(os.environ, {'CODEX_THREAD_ID': 'root-session', 'CODEX_SESSION_ID': 'root-session'}):
            rc, out, err = self.add('--reporter', self.child_name)
        self.assertEqual(rc, 0, err)

    def test_child_can_report_using_own_thread_without_root_route(self):
        self.trusted_child_task()
        with self.board() as board:
            board.record_route('child-thread', None, 'final', 'J')
        rc, out, err = self.child_report('child-thread')
        self.assertEqual(rc, 0, err)
        self.assertIn('evidence recorded', out)

    def test_assigned_actor_report_does_not_need_private_agent_grant(self):
        self.trusted_child_task()
        with self.board() as board:
            board.record_route('child-thread', None, 'final', 'J')
        with mock.patch('swarm.enrolment.owns', return_value=False):
            rc, out, err = self.child_report('child-thread')
        self.assertEqual(rc, 0, err)
        self.assertIn('evidence recorded', out)

    def test_sibling_cannot_report_as_child_sharing_root_session(self):
        self.trusted_child_task()
        rc, _, err = self.child_report('sibling-thread')
        self.assertEqual(rc, 1, 'a shared root route must not authorize sibling evidence')
        self.assertIn('exact assigned actor session', err)

    def test_parent_cannot_report_as_child_sharing_root_session(self):
        self.trusted_child_task()
        rc, _, err = self.child_report('root-session')
        self.assertEqual(rc, 1, 'a parent must not attest a child task')
        self.assertIn('exact assigned actor session', err)

    def test_native_task_startup_failure_persists_concrete_reason(self):
        with mock.patch('swarm.dependencies.wake', return_value='listener launch refused by OS'):
            with mock.patch.dict(os.environ, {'CODEX_THREAD_ID': 'root-session', 'CODEX_SESSION_ID': 'root-session'}):
                rc, out, err = self.add()
        self.assertEqual(rc, 0, err)
        self.assertIn('listener launch refused', err)
        with self.board() as board:
            self.assertEqual(board.job_data('J')['task.reconciler_error'], 'listener launch refused by OS')
            self.assertEqual(board.get_launch('J', 1, 'qa').state, 'queued')
