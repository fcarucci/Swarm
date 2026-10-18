"""Isolated real terminal hooks and listener passes with a simulated bounded launcher.

No production config, real agent/model process, user service, or live job is touched.
"""
import json
from pathlib import Path
from types import SimpleNamespace as NS
from unittest import mock

from test_hooks_cli import Env
from swarm import dependencies, events_listener

ARTIFACT = 'reviewed/swarm@' + 'a' * 40


class TerminalDependencyHandoff(Env):
    def setUp(self):
        super().setUp()
        self.assertEqual(self.cli('activate', '--job', 'J', '--session', 'sess-1')[0], 0)
        self.cfg['models'] = {'mode': 'enforce', 'codex': {'engineer': 'gpt-6.1-sol'}}
        self.runs, self.wakes = [], []
        for patcher in (
            mock.patch('swarm.supervisor.command.workdir_problem', return_value=None),
            mock.patch('swarm.supervisor.markers.write_resume_marker', return_value=Path('/simulated-marker')),
            mock.patch('swarm.supervisor.runner.start', side_effect=self.simulated_start),
            mock.patch.object(dependencies, 'wake', side_effect=self.wake),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        for key in ('qa-seat', 'review-seat'):
            self.hook('start', agent_id=key, host='codex')
            with self.board() as board:
                board.set_agent_role(key, 'engineer')
        with self.board() as board:
            names = {a.agent_key: a.name for a in board.agents('J')}
            self.names = names
            # The successor is durable before either predecessor can report or end.
            self.register(board, 'build', ['qa', 'source-review'])
            self.register(board, 'qa', [], reporter=names['qa-seat'])
            self.register(board, 'source-review', [], reporter=names['review-seat'])

    def register(self, board, name, depends, reporter=None):
        return dependencies.register(board, 'J', 1, name, artifact=ARTIFACT, depends=depends,
            role='engineer', harness='codex', model='gpt-6.1-sol', workdir=str(self.tmp),
            brief='Isolated proof stage ' + name, queued_by='fixture coordinator', reporter=reporter)

    def listener(self):
        return events_listener.Listener(self.cfg, NS(event_sources={}), self.board,
            reconcile=True, write_reconcile_health=lambda data: None)

    def wake(self, cfg):
        with self.board() as board:
            self.wakes.append({a.agent_key: (a.status, a.turn_ended_at is not None)
                               for a in board.agents('J', include_departed=False)})
        return None

    def simulated_start(self, cfg, run):
        self.runs.append(run)
        with self.board() as board:
            board.claim_resume('simulated-bounded-child', run['resume_of'], 'J')
            board.set_restart_agent(run['restart_id'], 'simulated-bounded-child')
            board.set_agent_runtime('simulated-bounded-child', 'codex', 'gpt-6.1-sol')
            board.tool_started('simulated-bounded-child', 'simulated bounded runner')

    def report(self, stage, key, artifact=ARTIFACT):
        with self.board() as board:
            return dependencies.report(board, 'J', 1, stage, artifact=artifact, outcome='passed',
                reason='Isolated exact revision check', actor=self.names[key])

    def test_last_real_terminal_hook_releases_one_claim_without_any_later_turn(self):
        self.report('qa', 'qa-seat')
        self.hook('stop', agent_id='qa-seat', host='codex', turn_id='proof-qa-terminal')
        listener = self.listener()
        listener.reconcile_due(0)
        self.assertEqual(self.runs, [])
        self.report('source-review', 'review-seat')
        self.hook('stop', agent_id='review-seat', host='codex', turn_id='proof-review-terminal')
        self.assertEqual(self.wakes[-1], {'qa-seat': ('idle', True), 'review-seat': ('idle', True)})
        # No orchestrator/agent turn follows the final hook: only existing listener ticks.
        listener.reconcile_due(5)
        restarted = self.listener()
        restarted.reconcile_due(0)
        restarted.reconcile_due(5)
        self.hook('stop', agent_id='review-seat', host='codex', turn_id='proof-review-terminal')
        restarted.reconcile_due(10)
        self.assertEqual([r['launch'] for r in self.runs], ['build'])
        with self.board() as board:
            task = board.get_launch('J', 1, 'build')
            restarts = board.restarts('J')
            self.assertEqual(len(restarts), 1)
            self.assertEqual(task.state, 'running')
            self.assertEqual((restarts[0].launch_id, restarts[0].new_agent_key),
                             (task.launch_id, 'simulated-bounded-child'))
            self.proof = {'proof_kind': 'isolated simulated launcher; real hooks/listener/claim',
                'terminal_event_id': 'proof-review-terminal', 'artifact': ARTIFACT,
                'task': task.name, 'generation': task.generation, 'launch_id': task.launch_id,
                'restart_id': restarts[0].id, 'action_id': restarts[0].action_id,
                'simulated_agent_key': restarts[0].new_agent_key, 'model': 'gpt-6.1-sol',
                'effective_launch_count': len(restarts)}

    def test_missing_and_wrong_artifact_evidence_hold_after_all_terminal_hooks(self):
        self.report('qa', 'qa-seat')
        with self.assertRaises(ValueError):
            self.report('source-review', 'review-seat', 'wrong@' + 'b' * 40)
        for key in ('qa-seat', 'review-seat'):
            self.hook('stop', agent_id=key, host='codex', turn_id='proof-missing-' + key)
        listener = self.listener()
        for tick in (0, 5, 10):
            listener.reconcile_due(tick)
        self.assertEqual(self.runs, [])
        with self.board() as board:
            self.assertEqual(board.restarts('J'), [])
            self.assertEqual(board.get_launch('J', 1, 'build').state, 'queued')


class FilteredWaiterHeartbeat(Env):
    def test_real_filtered_wait_records_exact_coverage_and_receives_its_event(self):
        with self.board() as board:
            board.open_job('J', None, None, None, 'fixture owner')
            seen = []
            def deliver(_seconds):
                seen.append(board.job_data('J'))
                board.post_event('J', 'READY', 'proof', 'Fixture event', to='@orchestrator')
            with mock.patch.object(board, '_events_sleep', side_effect=deliver):
                events = board.wait_event('J', to='@orchestrator', timeout=1)
            self.assertEqual([e.kind for e in events], ['READY'])
            self.assertIn('events.waiter.orchestrator', seen[0])
            scopes = [json.loads(value) for key, value in seen[0].items() if key.startswith('events.waiter_scope.')]
            self.assertEqual([scope['targets'] for scope in scopes], [[None, '@orchestrator']])
            self.assertFalse(any(key.startswith('events.waiter_scope.') for key in board.job_data('J')))


    def test_real_global_and_repeated_target_waits_cover_matching_pm_event(self):
        from swarm import events_safety
        for selector in (None, ('@pm', '@el')):
            with self.subTest(selector=selector), self.board() as board:
                board.open_job('J', None, None, None, 'fixture owner')
                codes = []
                def deliver(_seconds):
                    board.post_event('J', 'READY', 'pm-proof-' + ('all' if selector is None else 'multi'),
                                     'Fixture event', to='@pm')
                    codes.extend(p.code for p in events_safety.check(board, self.cfg, board.now()))
                with mock.patch.object(board, '_events_sleep', side_effect=deliver):
                    events = board.wait_event('J', to=selector, timeout=1)
                self.assertEqual([event.kind for event in events], ['READY'])
                self.assertNotIn('waiter-stale', codes)
                board.ack_events('J', [event.id for event in events], 'fixture owner')
