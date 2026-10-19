"""Trusted spawn calls bind exact Codex children while a root owns several jobs."""
from test_codex_hooks import CodexHookTests as _CodexHookTests
import codex_fixtures as F
from test_hooks_cli import Env


class MultiJobRoutingTests(Env):
    replay = _CodexHookTests.replay
    activate_fixture_job = _CodexHookTests.activate_fixture_job
    child_payloads = _CodexHookTests.child_payloads
    rename_child = _CodexHookTests.rename_child
    denied = _CodexHookTests.denied
    def setUp(self):
        super().setUp()
        import os
        from unittest import mock
        self.codex_home = F.staged(self.tmp)
        patch = mock.patch.dict(os.environ, {'CODEX_HOME': str(self.codex_home)})
        patch.start(); self.addCleanup(patch.stop)
        self.ids = F.ids()
        self.sid = F.payloads('SubagentStart')[0]['session_id']
        self.child = self.ids['child']['thread']
        self.activate_fixture_job()
        self.activate_fixture_job('other')
        self.rename_child('/root/job__other__engineer__api')

    def spawn_payload(self, job='other', parent=None, **extra):
        return dict(session_id=self.sid, agent_id=parent, tool_name='collaborationspawn_agent',
                    tool_use_id='spawn-1', tool_input=dict(task_name=f'job__{job}__engineer__api',
                    message='gAAAAA-encrypted', fork_turns='none'), **extra)

    def call(self, event, payload):
        return self.hook(event, host='codex', **payload)

    def test_ambiguous_root_spawn_is_denied(self):
        p = self.spawn_payload()
        p['tool_input']['task_name'] = 'engineer__api'
        self.assertIsNotNone(self.denied(self.call('turn', p)))

    def test_result_binds_actual_child_and_start_before_result_stays_pending(self):
        p = self.spawn_payload()
        self.assertIsNone(self.denied(self.call('turn', p)))
        self.replay('SubagentStart', F.payloads('SubagentStart')[0])
        self.replay('PreToolUse', self.child_payloads('PreToolUse')[0])
        with self.board() as b:
            self.assertEqual(b.route(self.child).state, 'pending')
        self.call('done', {**p, 'tool_response': {'agent_id': self.child}})
        self.replay('PreToolUse', self.child_payloads('PreToolUse')[0])
        self.assertIsNotNone(self.agent(self.child, job='other'))
        self.assertEqual(self.agent(self.child, job='other').role, 'engineer')

    def test_plaintext_job_tag_can_route_root_result(self):
        p = self.spawn_payload()
        p['tool_input'] = dict(task_name='engineer__api', message='[swarm job: other]\nBuild')
        self.call('turn', p)
        self.call('done', {**p, 'tool_response': {'agent_id': self.child}})
        self.replay('SubagentStart', F.payloads('SubagentStart')[0])
        self.assertIsNotNone(self.agent(self.child, job='other'))

    def test_result_cannot_steal_existing_member(self):
        self.cli('join', '--job', 'fixture', '--key', self.child)
        p = self.spawn_payload()
        self.call('turn', p)
        self.call('done', {**p, 'tool_response': {'agent_id': self.child}})
        self.assertIsNotNone(self.agent(self.child, job='fixture'))
        with self.board() as b:
            self.assertNotEqual(b.route(self.child).job, 'other')

    def test_nested_spawn_inherits_parent_and_rejects_cross_job(self):
        root_spawn = self.spawn_payload()
        self.call('done', {**root_spawn, 'tool_response': {'agent_id': self.child}})
        self.replay('PreToolUse', self.child_payloads('PreToolUse')[0])
        p = self.spawn_payload('fixture', parent=self.child)
        p['transcript_path'] = str(next(self.codex_home.glob(f'sessions/*/*/*/*{self.child}.jsonl')))
        self.assertIsNotNone(self.denied(self.call('turn', p)))
        p['tool_input']['task_name'] = 'engineer__api'
        self.assertIsNone(self.denied(self.call('turn', p)))
        grandchild = self.ids['grandchild']['thread']
        self.call('done', {**p, 'tool_response': {'agent_id': grandchild}})
        start = next(x for x in F.payloads('SubagentStart') if x['agent_id'] == grandchild)
        self.replay('SubagentStart', start)
        self.assertIsNotNone(self.agent(grandchild, job='other'))

    def test_unbound_or_conflicting_root_tags_are_denied(self):
        for job in ('missing', 'fixture'):
            p = self.spawn_payload(job)
            p['tool_input']['message'] = '[swarm job: other]'
            self.assertIsNotNone(self.denied(self.call('turn', p)))

    def test_failed_or_malformed_result_does_not_bind_child(self):
        p = self.spawn_payload()
        for response in ('synthetic', {}, {'agent_id': '../bad'}, {'error': 'spawn failed'}):
            self.call('done', {**p, 'tool_response': response})
            with self.board() as b:
                self.assertIsNone(b.route(self.child).job)

    def test_json_result_binds_then_followup_preserves_job(self):
        import json
        p = self.spawn_payload()
        self.call('turn', p)
        self.call('done', {**p, 'tool_response': json.dumps({'agent_id': self.child})})
        self.replay('SubagentStart', F.payloads('SubagentStart')[0])
        self.replay('SubagentStop', self.child_payloads('SubagentStop')[0])
        self.call('turn', dict(session_id=self.sid, tool_name='collaborationfollowup_task',
                 tool_input={'target': self.child, 'message': '[swarm job: fixture]'}))
        self.replay('PreToolUse', self.child_payloads('PreToolUse')[0])
        self.assertIsNotNone(self.agent(self.child, job='other'))
        with self.board() as b:
            self.assertEqual(b.route(self.child).job, 'other')

    def test_actual_canonical_task_result_binds_exact_thread(self):
        p = self.spawn_payload()
        self.call('turn', p)
        self.replay('SubagentStart', F.payloads('SubagentStart')[0])
        self.call('done', {**p, 'tool_response': {'task_name': '/root/job__other__engineer__api'}})
        self.replay('PreToolUse', self.child_payloads('PreToolUse')[0])
        self.assertIsNotNone(self.agent(self.child, job='other'))

    def test_canonical_result_requires_exact_parent_and_unique_path(self):
        import json
        p = self.spawn_payload()
        roll = next(self.codex_home.glob(f'sessions/*/*/*/*{self.child}.jsonl'))
        lines = roll.read_text().splitlines()
        meta = json.loads(lines[0])
        meta['payload']['source']['subagent']['thread_spawn']['parent_thread_id'] = self.ids['grandchild']['thread']
        roll.write_text('\n'.join([json.dumps(meta), *lines[1:]]) + '\n')
        self.call('done', {**p, 'tool_response': {'task_name': '/root/job__other__engineer__api'}})
        with self.board() as b:
            self.assertIsNone(b.route(self.child).job)
        meta['payload']['source']['subagent']['thread_spawn']['parent_thread_id'] = self.sid
        roll.write_text('\n'.join([json.dumps(meta), *lines[1:]]) + '\n')
        duplicate_id = '00000000-0000-4000-8000-000000000099'
        duplicate = roll.with_name('rollout-duplicate-' + duplicate_id + '.jsonl')
        meta['payload']['id'] = duplicate_id
        duplicate.write_text('\n'.join([json.dumps(meta), *lines[1:]]) + '\n')
        self.call('done', {**p, 'tool_response': {'task_name': '/root/job__other__engineer__api'}})
        with self.board() as b:
            self.assertIsNone(b.route(self.child).job)
            self.assertIsNone(b.route(duplicate_id).job)

    def test_actual_nested_canonical_result_inherits_exact_parent_job(self):
        import json
        root_spawn = self.spawn_payload()
        self.call('done', {**root_spawn, 'tool_response': {'agent_id': self.child}})
        self.replay('PreToolUse', self.child_payloads('PreToolUse')[0])
        grandchild = self.ids['grandchild']['thread']
        roll = next(self.codex_home.glob(f'sessions/*/*/*/*{grandchild}.jsonl'))
        lines = roll.read_text().splitlines()
        meta = json.loads(lines[0])
        path = '/root/job__other__engineer__api/engineer__nested'
        meta['payload']['source']['subagent']['thread_spawn']['agent_path'] = path
        roll.write_text('\n'.join([json.dumps(meta), *lines[1:]]) + '\n')
        p = self.spawn_payload(parent=self.child)
        p['tool_input']['task_name'] = 'engineer__nested'
        self.call('done', {**p, 'tool_response': {'task_name': path}})
        start = next(x for x in F.payloads('SubagentStart') if x['agent_id'] == grandchild)
        self.replay('SubagentStart', start)
        self.assertIsNotNone(self.agent(grandchild, job='other'))

    def test_forged_board_route_has_no_private_spawn_authority(self):
        with self.board() as b:
            b.record_route(self.child, self.sid, 'unverified', 'other')
        self.replay('SubagentStart', F.payloads('SubagentStart')[0])
        self.assertIsNone(self.agent(self.child, job='other'))
        self.assertIsNone(self.agent(self.child, job='fixture'))

    def test_private_receipt_requires_same_native_child_identity(self):
        import json
        p = self.spawn_payload()
        self.call('done', {**p, 'tool_response': {'task_name': '/root/job__other__engineer__api'}})
        roll = next(self.codex_home.glob(f'sessions/*/*/*/*{self.child}.jsonl'))
        lines = roll.read_text().splitlines()
        meta = json.loads(lines[0])
        meta['payload']['source']['subagent']['thread_spawn']['parent_thread_id'] = self.ids['grandchild']['thread']
        roll.write_text('\n'.join([json.dumps(meta), *lines[1:]]) + '\n')
        self.replay('SubagentStart', F.payloads('SubagentStart')[0])
        self.assertIsNone(self.agent(self.child, job='other'))

    def test_board_only_parent_cannot_authorize_nested_job_binding(self):
        self.cli('join', '--job', 'other', '--key', self.child)
        p = self.spawn_payload(parent=self.child)
        p['tool_input']['task_name'] = 'engineer__api'
        grandchild = self.ids['grandchild']['thread']
        self.call('done', {**p, 'tool_response': {'agent_id': grandchild}})
        start = next(x for x in F.payloads('SubagentStart') if x['agent_id'] == grandchild)
        self.replay('SubagentStart', start)
        self.assertIsNone(self.agent(grandchild, job='other'))

    def test_private_spawn_receipt_scope_and_cached_job_must_match(self):
        from swarm import codex_bindings
        from unittest import mock
        p = self.spawn_payload()
        self.call('done', {**p, 'tool_response': {'agent_id': self.child}})
        self.assertIsNotNone(codex_bindings.read(self.cfg, self.sid, self.child))
        self.assertIsNone(codex_bindings.read(self.cfg, self.ids['grandchild']['thread'], self.child))
        self.assertIsNone(codex_bindings.read(self.cfg, self.sid, self.ids['grandchild']['thread']))
        with mock.patch.object(codex_bindings, 'store_key', return_value='another-board'):
            self.assertIsNone(codex_bindings.read(self.cfg, self.sid, self.child))
        with self.board() as b:
            b.record_route(self.child, self.sid, 'unverified', 'fixture')
        self.replay('SubagentStart', F.payloads('SubagentStart')[0])
        self.assertIsNone(self.agent(self.child, job='fixture'))
        self.assertIsNone(self.agent(self.child, job='other'))

# Keep helper fixture class out of unittest discovery.
del _CodexHookTests
