"""Task supersession derives its actor from the recorded live session."""
import json
import os
from unittest import mock

from test_hooks_cli import Env
from swarm import dependencies


class TaskSupersedeCLI(Env):
    def setUp(self):
        super().setUp()
        self.root = 'root-session'
        os.environ['CODEX_THREAD_ID'] = self.root
        os.environ['CODEX_SESSION_ID'] = self.root
        self.argv = ['task', 'supersede', '--job', 'J', '--name', 'old', '--generation', '2',
            '--artifact', 'old@abc', '--replacement', 'replacement', '--replacement-generation', '4',
            '--replacement-artifact', 'new@def', '--reason', 'Independent corrected review accepted']
        wake = mock.patch('swarm.dependencies.wake', return_value=None)
        wake.start()
        self.addCleanup(wake.stop)
        with self.board() as board:
            board.open_job('J', None, None, self.root, 'owner', goal='Whole project')
            old = dependencies.register(board, 'J', 2, 'old', artifact='old@abc', depends=[],
                role='qa', harness='codex', model=None, workdir=str(self.tmp), brief='Old review', queued_by='owner')
            board.set_launch_state(old.id, old.revision, 'failed', job='J')
            actor = board.allocate_name('review-seat', 'J', 'qa')
            dependencies.register(board, 'J', 4, 'replacement', artifact='new@def', depends=[],
                role='qa', harness='codex', model=None, workdir=str(self.tmp), brief='Corrected review', queued_by='owner', reporter=actor)
            dependencies.report(board, 'J', 4, 'replacement', artifact='new@def', outcome='passed', reason='Checked exact document', actor=actor)
            board.agent_turn_ended('review-seat')
            dependencies.reconcile_external(board, 'J')

    def test_actual_root_supersedes_without_private_grant_and_preserves_history(self):
        rc, out, err = self.cli(*self.argv)
        self.assertEqual(rc, 0, err)
        self.assertIn('supersession recorded', out)
        with self.board() as board:
            events = [e for e in board.events('J') if e.kind == 'TASK-SUPERSEDE']
        self.assertEqual(len(events), 1)
        self.assertEqual(json.loads(events[0].text)['actor'], self.root)
        rc, out, err = self.cli('task', 'list', '--job', 'J', '--json')
        self.assertEqual(rc, 0, err)
        self.assertIn(('old','superseded'), [(r['name'],r['state']) for r in json.loads(out)])
        rc, out, err = self.cli(*self.argv)
        self.assertEqual(rc, 0, err)
        self.assertIn('already recorded', out)

    def test_live_member_supersedes_with_exact_actor_audit_and_no_role_gate(self):
        member_key = 'qa-thread'
        with self.board() as board:
            member = board.allocate_name(member_key, 'J', 'qa')
            board.record_route(member_key, self.root, 'final', 'J')
        os.environ['CODEX_THREAD_ID'] = member_key
        rc, out, err = self.cli(*self.argv)
        self.assertEqual(rc, 0, err)
        self.assertIn('supersession recorded', out)
        with self.board() as board:
            events = [e for e in board.events('J') if e.kind == 'TASK-SUPERSEDE']
        self.assertEqual(len(events), 1)
        self.assertEqual(json.loads(events[0].text)['actor'], member)

    def test_exact_live_member_supersedes_without_private_host_grants(self):
        member_key = 'qa-thread'
        with self.board() as board:
            member = board.allocate_name(member_key, 'J', 'qa')
            board.record_route(member_key, self.root, 'final', 'J')
        os.environ['CODEX_THREAD_ID'] = member_key
        rc, out, err = self.cli(*self.argv)
        self.assertEqual(rc, 0, err)
        self.assertIn('supersession recorded', out)
        with self.board() as board:
            events = [e for e in board.events('J') if e.kind == 'TASK-SUPERSEDE']
        self.assertEqual(len(events), 1)
        self.assertEqual(json.loads(events[0].text)['actor'], member)

    def test_unenrolled_thread_cannot_spoof_root_or_supersede(self):
        os.environ['CODEX_THREAD_ID'] = 'worker-thread'
        self.assertNotEqual(self.cli(*self.argv)[0], 0)

    def test_forked_root_remains_untrusted(self):
        os.environ['CODEX_THREAD_ID'] = 'forked-thread'
        self.assertNotEqual(self.cli(*self.argv)[0], 0)

    def test_actor_override_cannot_impersonate_another_member(self):
        self.assertNotEqual(self.cli(*self.argv, '--as', 'Bart Simpson')[0], 0)
