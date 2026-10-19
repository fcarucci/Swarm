"""Only actual trusted root/EL hook context can authorize task supersession."""
import json
import os
import shlex
from pathlib import Path
from unittest import mock

from test_hooks_cli import Env
from swarm import dependencies, enrolment, hosts
from swarm.board.autoinit import store_key


class TaskSupersedeCLI(Env):
    def setUp(self):
        super().setUp()
        self.root = 'root-session'
        os.environ['CODEX_THREAD_ID'] = self.root
        os.environ['CODEX_SESSION_ID'] = self.root
        self.host = hosts.get('codex')
        self.argv = ['task', 'supersede', '--job', 'J', '--name', 'old', '--generation', '2',
            '--artifact', 'old@abc', '--replacement', 'replacement', '--replacement-generation', '4',
            '--replacement-artifact', 'new@def', '--reason', 'Independent corrected review accepted']
        for patcher in (mock.patch('swarm.person.host_pid', return_value=1234),
                        mock.patch('swarm.hosts.codex.find_rollout', return_value=Path('/native')),
                        mock.patch('swarm.hosts.codex._first_meta', return_value={'id':self.root,'source':'cli'}),
                        mock.patch('swarm.dependencies.wake', return_value=None)):
            patcher.start()
            self.addCleanup(patcher.stop)
        with self.board() as board:
            board.open_job('J', None, None, None, 'owner', goal='Whole project')
            old = dependencies.register(board, 'J', 2, 'old', artifact='old@abc', depends=[],
                role='qa', harness='codex', model=None, workdir=str(self.tmp), brief='Old review', queued_by='owner')
            board.set_launch_state(old.id, old.revision, 'failed', job='J')
            actor = board.allocate_name('review-seat', 'J', 'qa')
            dependencies.register(board, 'J', 4, 'replacement', artifact='new@def', depends=[],
                role='qa', harness='codex', model=None, workdir=str(self.tmp), brief='Corrected review', queued_by='owner', reporter=actor)
            dependencies.report(board, 'J', 4, 'replacement', artifact='new@def', outcome='passed', reason='Checked exact document', actor=actor)
            board.agent_turn_ended('review-seat')
            dependencies.reconcile_external(board, 'J')
        enrolment.write_job(store_key(self.cfg), job='J', harness='codex', session_id=self.root, cwd=str(self.tmp))

    def attest(self, agent=None):
        from swarm import task_authority
        payload = dict(session_id=self.root, tool_use_id='tool-supersede', tool_name='Bash',
            tool_input={'command': shlex.join(['swarm', '--config', str(self.config), *self.argv])})
        if agent:
            payload['agent_id'] = agent
        return task_authority.attest(self.cfg, payload, self.host)

    def test_actual_root_grant_creates_audit_and_list_keeps_history(self):
        self.assertTrue(self.attest())
        rc, out, err = self.cli(*self.argv)
        self.assertEqual(rc, 0, err)
        self.assertIn('supersession recorded', out)
        rc, out, err = self.cli('task', 'list', '--job', 'J', '--json')
        self.assertEqual(rc, 0, err)
        self.assertIn(('old','superseded'), [(r['name'],r['state']) for r in json.loads(out)])
        self.assertTrue(self.attest())
        rc, out, err = self.cli(*self.argv)
        self.assertEqual(rc, 0, err)
        self.assertIn('already recorded', out)

    def test_real_pretool_hook_mints_exact_grant_before_marker_fastpath(self):
        command = shlex.join(['swarm', '--config', str(self.config), *self.argv])
        self.hook('turn', agent_id=None, session=self.root, host='codex',
                  tool_use_id='actual-hook-call', tool_name='Bash', tool_input={'command':command})
        rc, out, err = self.cli(*self.argv)
        self.assertEqual(rc, 0, err)
        self.assertIn('supersession recorded', out)

    def test_expired_or_new_run_grant_cannot_mutate_task_obligations(self):
        from swarm import task_authority
        with mock.patch.object(task_authority.time, 'time', return_value=1):
            self.attest()
        self.assertNotEqual(self.cli(*self.argv)[0], 0)
        self.attest()
        with self.board() as board:
            board.open_job('J', None, None, None, 'owner', goal='New run')
        self.assertNotEqual(self.cli(*self.argv)[0], 0)

    def test_changed_private_coordinator_proof_revokes_unconsumed_grant(self):
        self.attest()
        enrolment.write_job(store_key(self.cfg), job='J', harness='codex', session_id='different-root', cwd=str(self.tmp))
        self.assertNotEqual(self.cli(*self.argv)[0], 0)
        with self.board() as board:
            self.assertFalse(any(e.kind=='TASK-SUPERSEDE' for e in board.events('J')))

    def test_claimed_root_environment_alone_has_no_authority(self):
        self.assertNotEqual(self.cli(*self.argv)[0], 0)

    def test_worker_cannot_inherit_or_spoof_root_authority(self):
        with mock.patch('swarm.codex_bindings.read', return_value=None):
            with self.assertRaises(ValueError):
                self.attest(agent='worker-thread')
        # Even lying about root environment cannot mint the missing private hook grant.
        self.assertNotEqual(self.cli(*self.argv)[0], 0)

    def test_forked_root_and_changed_arguments_are_refused(self):
        with mock.patch('swarm.hosts.codex._first_meta', return_value={'id':self.root,'source':'cli','forked_from_id':'parent'}):
            with self.assertRaises(ValueError):
                self.attest()
        self.attest()
        self.assertNotEqual(self.cli(*self.argv[:-1], 'Unattested different reason')[0], 0)

    def test_el_requires_native_role_and_exact_private_same_job_enrollment(self):
        with self.board() as board:
            name = board.allocate_name('el-thread', 'J', 'engineering_lead')
        enrolment.write(store_key(self.cfg), job='J', agent_key='el-thread', harness='codex',
            session_id=self.root, cwd=str(self.tmp))
        os.environ['CODEX_THREAD_ID'] = 'el-thread'
        proof = dict(job='J', native={'path':'/root/engineering_lead__project'})
        with mock.patch('swarm.codex_bindings.read', return_value=proof):
            self.assertTrue(self.attest(agent='el-thread'))
            self.assertEqual(self.cli(*self.argv)[0], 0)

    def test_replaced_el_private_session_revokes_existing_grant(self):
        with self.board() as board:
            board.allocate_name('el-thread', 'J', 'engineering_lead')
        enrolment.write(store_key(self.cfg), job='J', agent_key='el-thread', harness='codex',
            session_id=self.root, cwd=str(self.tmp))
        os.environ['CODEX_THREAD_ID'] = 'el-thread'
        proof = dict(job='J', native={'path':'/root/engineering_lead__project'})
        with mock.patch('swarm.codex_bindings.read', return_value=proof):
            self.attest(agent='el-thread')
            enrolment.write(store_key(self.cfg), job='J', agent_key='el-thread', harness='codex',
                session_id='different-root', cwd=str(self.tmp))
            self.assertNotEqual(self.cli(*self.argv)[0], 0)

    def test_mutable_el_board_role_does_not_grant_native_worker_authority(self):
        with self.board() as board:
            board.allocate_name('worker-thread', 'J', 'engineering_lead')
        enrolment.write(store_key(self.cfg), job='J', agent_key='worker-thread', harness='codex',
            session_id=self.root, cwd=str(self.tmp))
        proof = dict(job='J', native={'path':'/root/engineer__worker'})
        with mock.patch('swarm.codex_bindings.read', return_value=proof):
            with self.assertRaises(ValueError):
                self.attest(agent='worker-thread')
