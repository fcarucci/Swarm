"""A closed review is delegated by actual root user intent, never worker labels."""
import importlib.util
import contextlib
import concurrent.futures
import io
import inspect
import json
import os
import shlex
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from test_hooks_cli import Env
from swarm import enrolment, hosts
from swarm.board.autoinit import store_key


class LateReviewAuthority(Env):
    def setUp(self):
        super().setUp()
        self.root = '11111111-2222-3333-4444-555555555555'
        os.environ['CODEX_THREAD_ID'] = self.root
        os.environ['CODEX_SESSION_ID'] = self.root
        self.host = hosts.get('codex')
        self.native = self.tmp / ('rollout-' + self.root + '.jsonl')
        self.workdir = self.tmp / 'approved-review-work'
        self.workdir.mkdir()
        with self.config.open('a') as stream:
            stream.write('\n[supervise]\nallowed_workdirs = ' + json.dumps([str(self.workdir)]) + '\n')
        from swarm import cli
        self.cfg = cli.load_config(self.config)
        self.meta = {'id': self.root, 'source': 'cli'}
        self.write_native()
        for patcher in (mock.patch('swarm.person.host_pid', return_value=1234),
                        mock.patch('swarm.hosts.codex.find_rollout', return_value=self.native),
                        mock.patch('swarm.dependencies.wake', return_value=None)):
            patcher.start()
            self.addCleanup(patcher.stop)
        with self.board() as b:
            b.open_job('J', None, None, self.root, 'historical creator', goal='complete cleanup')
            b.set_job_supervise('J', False)
            from swarm import passcmd
            for n in range(3):
                row = b.queue_launch('J', 1, f'lead-{n}', role='lead', harness='codex', model=None,
                    workdir=str(self.tmp), brief='Earlier native lead', queued_by='swarm pass')
                b.set_launch_state(row.id, row.revision, 'failed', job='J', last_outcome='refused')
            passcmd._fail(b, 'J', '3 launches with no hand-off (DONE)', b.now())
            self.worker = b.allocate_name('cleanup', 'J', 'engineer')
            b.post('J', self.worker, 'DONE final-cleanup@abcdef012345')
            b.agent_turn_ended('cleanup')
            self.original = b.job_status('J')
        enrolment.write_job(store_key(self.cfg), job='J', harness='codex', session_id=self.root,
                            cwd=str(self.tmp))

    def write_native(self, user=True):
        items = [dict(type='session_meta', payload=self.meta)]
        if user:
            items.append(dict(type='response_item', payload=dict(id='user-item', type='message', role='user',
                content=[dict(type='input_text', text='Complete the job correctly after cleanup.')])) )
        self.native.write_text('\n'.join(json.dumps(x) for x in items) + '\n')

    def api(self):
        self.assertIsNotNone(importlib.util.find_spec('swarm.late_review'),
                             'native closed-review delegation is missing')
        from swarm import late_review
        return late_review

    def argv(self):
        from swarm import settlement
        return ['late-review', '--job', 'J', '--closure', settlement.closure(self.original)['sha'],
                '--candidate', 'final-cleanup@abcdef012345', '--req', 'b' * 64, '--request-id', 'user-item',
                '--workdir', str(self.workdir)]

    def attest(self, agent=None, root=None, argv=None):
        api = self.api()
        payload = dict(session_id=root or self.root, tool_use_id='actual-call', tool_name='Bash',
            tool_input={'command': shlex.join(['swarm', '--config', str(self.config), *(argv or self.argv())])})
        if agent:
            payload['agent_id'] = agent
        return api.attest(self.cfg, payload, self.host)

    def test_actual_root_user_request_delegates_review_without_creating_owner(self):
        self.assertTrue(self.attest())
        rc, out, err = self.cli(*self.argv())
        self.assertEqual(rc, 0, err)
        with self.board() as b:
            rows = [r for r in b.launches('J') if r.role == 'judge']
            self.assertEqual(len(rows), 1)
            self.assertEqual((rows[0].role, rows[0].model), ('judge', 'gpt-6-astra'))
            self.assertEqual(b.job_status('J').status, 'failed')
            self.assertFalse(b.job_status('J').supervise)
        self.assertIsNone(enrolment.find_job_owner(store_key(self.cfg), 'J'))
        self.assertNotEqual(self.cli(*self.argv())[0], 0, 'consumed grant must not replay')

    def test_worker_fork_wrong_source_and_missing_real_user_are_refused(self):
        with self.assertRaises(ValueError):
            self.attest(agent='worker')
        for changes in ({'forked_from_id': 'parent'}, {'source': 'exec'}):
            with self.subTest(changes=changes):
                self.meta = dict({'id': self.root, 'source': 'cli'}, **changes)
                self.write_native()
                with self.assertRaises(ValueError):
                    self.attest()
        self.meta = dict(id=self.root, source='cli')
        self.write_native(user=False)
        with self.assertRaises(ValueError):
            self.attest()

    def test_cross_session_without_grant_cannot_delegate(self):
        self.api()
        with mock.patch.dict(os.environ, {'CODEX_THREAD_ID': 'changed-root', 'CODEX_SESSION_ID': 'changed-root'}):
            self.assertNotEqual(self.cli(*self.argv())[0], 0)
        with self.assertRaises(ValueError):
            self.attest(root='unrelated-root')

    def test_unknown_legacy_failure_without_native_notice_is_not_admitted(self):
        with self.board() as b:
            b.open_job('legacy', None, None, self.root, 'owner', goal='same goal')
            b.close_job('legacy', 'failed', '3 launches with no hand-off (DONE)', closed_by='swarm pass')
            b.post('legacy', 'owner', 'DONE final-cleanup@abcdef012345')
            legacy = b.job_status('legacy')
        enrolment.write_job(store_key(self.cfg), job='legacy', harness='codex', session_id=self.root, cwd=str(self.tmp))
        from swarm import settlement
        argv = ['late-review', '--job', 'legacy', '--closure', settlement.closure(legacy)['sha'],
                '--candidate', 'final-cleanup@abcdef012345', '--req', 'b' * 64, '--request-id', 'user-item',
                '--workdir', str(self.workdir)]
        with self.assertRaises(ValueError):
            self.attest(argv=argv)

    def test_one_native_closed_review_claim_enrolls_actual_judge_without_reopening(self):
        self.attest()
        self.assertEqual(self.cli(*self.argv())[0], 0)
        from swarm import passcmd
        from swarm.supervisor import runner
        runs = []
        with mock.patch('swarm.supervisor.launch.codex_command', return_value=['codex']), \
             mock.patch('swarm.supervisor.markers.write_resume_marker', return_value=Path('/unused/marker')):
            with self.board() as b:
                passcmd.run_pass(b, self.cfg, starter=lambda cfg, run: runs.append(run))
                self.assertEqual(len(runs), 1, 'the admitted review must cross the closed-job fences')
                run = runs[0]
                self.assertIsNone(runner._kill_switch(self.cfg, run))
                runner._enrol_native_session(b, self.cfg, run, 'actual-judge-thread')
                judge = next(a for a in b.agents('J') if a.agent_key == 'actual-judge-thread')
                self.assertEqual(judge.role, 'judge')
                self.assertEqual(b.job_status('J').status, 'failed')
                self.assertFalse(b.job_status('J').supervise)
                passcmd.run_pass(b, self.cfg, starter=lambda cfg, run: runs.append(run))
        self.assertEqual(len(runs), 1, 'one immutable claim must never relaunch')

    def test_native_thread_event_cannot_enroll_or_poison_original_root_identity(self):
        self.attest()
        self.assertEqual(self.cli(*self.argv())[0], 0)
        from swarm import passcmd
        from swarm.supervisor import runner
        runs = []
        with mock.patch('swarm.supervisor.launch.codex_command', return_value=['codex']):
            with self.board() as b:
                passcmd.run_pass(b, self.cfg, starter=lambda cfg, run: runs.append(run))
                self.assertEqual(len(runs), 1)
                with self.assertRaises(ValueError):
                    runner._enrol_native_session(b, self.cfg, runs[0], self.root)
                self.assertIsNone(b.active_agent_name(self.root))
        self.assertIsNone(enrolment.find(store_key(self.cfg), self.root))

    def test_native_thread_event_retries_transient_coordinator_lock_before_enrollment(self):
        self.attest()
        self.assertEqual(self.cli(*self.argv())[0], 0)
        from swarm import passcmd
        from swarm.supervisor import runner
        runs = []
        with mock.patch('swarm.supervisor.launch.codex_command', return_value=['codex']):
            with self.board() as b:
                passcmd.run_pass(b, self.cfg, starter=lambda cfg, run: runs.append(run))
                real = enrolment.coordinator_lock
                busy = [True]
                @contextlib.contextmanager
                def transient(*args):
                    if busy.pop() if busy else False:
                        raise BlockingIOError('launch reconciler still releasing its lock')
                    with real(*args):
                        yield
                with mock.patch.object(enrolment, 'coordinator_lock', side_effect=transient):
                    runner._enrol_native_session(b, self.cfg, runs[0], 'actual-judge-thread')
                self.assertEqual(next(a.role for a in b.agents('J')
                                      if a.agent_key == 'actual-judge-thread'), 'judge')

    def test_native_stop_proof_requires_containment_absent_and_leader_gone(self):
        from swarm.supervisor import runner
        stopped = getattr(runner, 'review_executor_stopped', None)
        self.assertTrue(callable(stopped), 'native terminal labels must not stand in for stop proof')
        run = dict(child_pid=999999999, child_token='owned-token', started_epoch=1)
        for presence in ('unknown', 'present'):
            with mock.patch.object(runner, 'session_presence', return_value=presence):
                self.assertFalse(stopped(run))
        with mock.patch.object(runner, 'session_presence', return_value='absent'), \
             mock.patch('os.kill', side_effect=ProcessLookupError):
            self.assertTrue(stopped(run))
        with mock.patch.object(runner, 'session_presence', return_value='absent'), \
             mock.patch('os.kill', return_value=None), \
             mock.patch('swarm.supervisor.procwatch.start_token', return_value='owned-token'):
            self.assertFalse(stopped(run))

    def accepted_review(self):
        from swarm import passcmd, late_review
        from swarm.supervisor import runner
        from swarm.spool import deliver_verdict
        self.attest()
        self.assertEqual(self.cli(*self.argv())[0], 0)
        runs = []
        with mock.patch('swarm.supervisor.launch.codex_command', return_value=['codex']), \
             mock.patch('swarm.supervisor.markers.write_resume_marker', return_value=Path('/unused/marker')):
            with self.board() as b:
                passcmd.run_pass(b, self.cfg, starter=lambda cfg, run: runs.append(run))
                self.assertEqual(len(runs), 1)
                run = runs[0]
                runner._enrol_native_session(b, self.cfg, run, 'actual-judge-thread')
                b.set_restart_agent(run['restart_id'], 'actual-judge-thread')
                judge = next(a for a in b.agents('J') if a.agent_key == 'actual-judge-thread')
                deliver_verdict(b, 'J', judge.name, 'met', 'exact final artifact accepted',
                    artifact='final-cleanup@abcdef012345', details='Artifact report.')
                mid = deliver_verdict(b, 'J', judge.name, 'met', 'all original and approved requirements verified',
                    scope='goal', open_count=0, candidate='final-cleanup@abcdef012345', req='b' * 64,
                    details='Full independent operational acceptance report.')
                b.agent_stopped(judge.agent_key)
                b.finish_restart(run['restart_id'], 'completed')
                passcmd._settle(b, 'J', self.cfg)
        # The proof is an actual local owned process exiting, independent of the
        # model/turn status. No model binary or provider is invoked by this fixture.
        child = subprocess.Popen([sys.executable, '-c', 'pass'], start_new_session=True)
        child.wait(timeout=5)
        run.update(agent_key='actual-judge-thread', child_pid=child.pid, child_pgid=child.pid,
                   leader_reaped=True, started_epoch=1)
        self.assertTrue(late_review.record_stopped(self.cfg, run, 'completed'))
        return mid

    def finalize_argv(self, mid):
        return ['late-finalize', *self.argv()[1:], '--verdict-id', str(mid), '--evidence', 'e' * 64]

    def finalized_review(self):
        mid = self.accepted_review()
        argv = self.finalize_argv(mid)
        self.assertTrue(self.attest(argv=argv))
        rc, _, err = self.cli(*argv)
        self.assertEqual(rc, 0, err)
        return mid

    def test_event_failure_rolls_back_receipt_and_event_then_retries(self):
        self.finalized_review()
        with self.board() as b:
            original = b._insert_event
            def fail(*args, **kwargs):
                original(*args, **kwargs)
                raise RuntimeError('controlled transaction failure')
            with mock.patch.object(b, '_insert_event', side_effect=fail):
                with self.assertRaises(RuntimeError):
                    b.settle_late_review('J')
            self.assertIsNone(b.completion_receipt('J'))
            self.assertEqual([e for e in b.events('J') if e.kind == 'completion'], [])
            self.assertIsNotNone(b.finalization_receipt('J'))
            self.assertIsNotNone(b.settle_late_review('J'))

    def test_receipt_insert_failure_rolls_back_both_native_records(self):
        self.finalized_review()
        from swarm.board import settlements
        with self.board() as b:
            if hasattr(b, '_s'):
                original = settlements._memory_put
                def fail(*args, **kwargs):
                    original(*args, **kwargs)
                    if args[1] == settlements.COMPLETIONS:
                        raise RuntimeError('controlled receipt persistence failure')
                patcher = mock.patch.object(settlements, '_memory_put', side_effect=fail)
            else:
                original = b._exec
                def fail(c, sql, args=()):
                    result = original(c, sql, args)
                    if sql.startswith('INSERT INTO completion_receipts'):
                        raise RuntimeError('controlled receipt persistence failure')
                    return result
                patcher = mock.patch.object(b, '_exec', side_effect=fail)
            with patcher:
                with self.assertRaises(RuntimeError):
                    b.settle_late_review('J')
            self.assertIsNone(b.completion_receipt('J'))
            self.assertEqual([e for e in b.events('J') if e.kind == 'completion'], [])
            self.assertIsNotNone(b.settle_late_review('J'))

    def test_finalization_insert_failure_rolls_back_visible_post_then_reconciles(self):
        from swarm.board import settlements
        mid = self.accepted_review()
        argv = self.finalize_argv(mid)
        self.assertTrue(self.attest(argv=argv))
        with self.board() as b:
            cls = type(b)
            if hasattr(b, '_s'):
                original = settlements._memory_put
                def fail(*args, **kwargs):
                    original(*args, **kwargs)
                    if args[1] == settlements.FINALIZATIONS:
                        raise RuntimeError('controlled finalization receipt failure')
                patcher = mock.patch.object(settlements, '_memory_put', side_effect=fail)
            else:
                original = cls._exec
                def fail(self, c, sql, args=()):
                    result = original(self, c, sql, args)
                    if sql.startswith('INSERT INTO finalization_receipts'):
                        raise RuntimeError('controlled finalization receipt failure')
                    return result
                patcher = mock.patch.object(cls, '_exec', fail)
            with patcher:
                with self.assertRaises(RuntimeError):
                    self.cli(*argv)
            self.assertIsNone(b.finalization_receipt('J'))
            self.assertFalse(any(m.message.startswith('FINALIZED ') for m in b.pipeline_messages('J')))
            self.assertIsNotNone(b.finalize_late_review('J'), 'durable authenticated intent permits safe retry')
            self.assertIsNotNone(b.settle_late_review('J'))

    def test_concurrent_legacy_goal_change_preserves_failure_and_invalidates_completed_projection(self):
        self.finalized_review()
        ready, release, changing = threading.Event(), threading.Event(), threading.Event()
        def settle():
            with self.board() as b:
                original = b._insert_event
                def pause(*args, **kwargs):
                    ready.set()
                    if not release.wait(5):
                        raise RuntimeError('controlled barrier timeout')
                    return original(*args, **kwargs)
                with mock.patch.object(b, '_insert_event', side_effect=pause):
                    return b.settle_late_review('J')
        def change():
            changing.set()
            # Closed-job goal changes are forbidden by the public API. Model an
            # incompatible legacy/raw writer here, without weakening that policy.
            with self.board() as b:
                if hasattr(b, '_s'):
                    self.h.update_job('J', goal='changed legacy goal identity')
                else:
                    with b._launch_tx() as c:
                        b._exec(c, 'UPDATE jobs SET goal=? WHERE job=?', ('changed legacy goal identity', 'J'))
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(settle)
            self.assertTrue(ready.wait(5))
            second = pool.submit(change)
            self.assertTrue(changing.wait(5))
            release.set()
            self.assertIsNotNone(first.result(timeout=10))
            second.result(timeout=10)
        from swarm.board.base import derive_job_status
        with self.board() as b:
            js = b.job_status('J')
            self.assertEqual(js.goal, 'changed legacy goal identity')
            self.assertEqual(derive_job_status(js, 5, b.now()), 'failed')
            for field in ('status', 'outcome', 'closed_by', 'finished_at', 'activated_at'):
                self.assertEqual(getattr(js, field), getattr(self.original, field))

    def test_owner_cancel_before_start_or_after_acceptance_never_reopens_or_settles(self):
        self.attest()
        self.assertEqual(self.cli(*self.argv())[0], 0)
        from swarm import passcmd
        with self.board() as b:
            b.close_job('J', 'cancelled', 'owner cancelled after delegate', closed_by='owner')
            started = mock.Mock(side_effect=AssertionError('model started after cancellation'))
            passcmd.run_pass(b, self.cfg, starter=started)
            started.assert_not_called()
            self.assertIsNone(b.settle_late_review('J'))
            self.assertEqual(b.job_status('J').status, 'cancelled')

    def test_concurrent_cancel_serializes_with_settlement_and_remains_effective(self):
        self.finalized_review()
        ready, release, cancelling = threading.Event(), threading.Event(), threading.Event()
        def settle():
            with self.board() as b:
                original = b._insert_event
                def pause(*args, **kwargs):
                    ready.set()
                    if not release.wait(5):
                        raise RuntimeError('controlled settlement barrier timeout')
                    return original(*args, **kwargs)
                with mock.patch.object(b, '_insert_event', side_effect=pause):
                    return b.settle_late_review('J')
        def cancel():
            cancelling.set()
            with self.board() as b:
                b.close_job('J', 'cancelled', 'owner cancelled concurrently', closed_by='owner')
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(settle)
            self.assertTrue(ready.wait(5))
            second = pool.submit(cancel)
            self.assertTrue(cancelling.wait(5))
            release.set()
            self.assertIsNotNone(first.result(timeout=10))
            second.result(timeout=10)
        from swarm.board.base import derive_job_status
        with self.board() as b:
            self.assertEqual(derive_job_status(b.job_status('J'), 5, b.now()), 'cancelled')
            self.assertIsNone(b.settle_late_review('J'))
            self.assertIsNotNone(b.completion_receipt('J'), 'prior immutable audit remains in history')

    def test_concurrent_exact_root_requests_create_one_native_review(self):
        self.attest()
        from swarm import cli, late_review
        args = cli._parser().parse_args(['--config', str(self.config), *self.argv()])
        def submit():
            with self.board() as b:
                return late_review.run(b, self.cfg, args)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()), \
             concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: submit(), range(2)))
        self.assertEqual(results.count(0), 1)
        self.assertEqual(set(results), {0, 1})
        with self.board() as b:
            self.assertEqual(len([r for r in b.launches('J') if r.role == 'judge']), 1)
            self.assertEqual(b.job_status('J').status, 'failed')

    def test_expired_private_grant_does_not_block_exact_root_review(self):
        from swarm import late_review
        with mock.patch.object(late_review.time, 'time', return_value=1):
            self.attest()
        rc, _, err = self.cli(*self.argv())
        self.assertEqual(rc, 0, err)

    def test_exact_root_review_needs_no_grant_and_rejects_changed_thread(self):
        rc, _, err = self.cli(*self.argv())
        self.assertEqual(rc, 0, err)
        with mock.patch.dict(os.environ, {'CODEX_THREAD_ID': 'changed-thread'}):
            self.assertNotEqual(self.cli(*self.argv())[0], 0)

    def test_changed_owner_session_or_request_arguments_are_refused(self):
        self.assertEqual(self.cli(*self.argv())[0], 0)
        changed = self.argv()
        changed[changed.index('--req') + 1] = 'c' * 64
        self.assertNotEqual(self.cli(*changed)[0], 0)
        changed = self.argv()
        changed[changed.index('--closure') + 1] = 'd' * 64
        self.assertNotEqual(self.cli(*changed)[0], 0)
        with mock.patch.dict(os.environ, {'CODEX_THREAD_ID': 'changed-root', 'CODEX_SESSION_ID': 'changed-root'}):
            self.assertNotEqual(self.cli(*self.argv())[0], 0)

    def test_actual_work_owner_wait_and_queued_launch_prevent_review_delegation(self):
        with self.board() as b:
            b.tool_started('cleanup', 'Bash')
        with self.assertRaises(ValueError):
            self.attest()

    def test_new_handoff_after_grant_or_delegation_prevents_old_review_start(self):
        self.attest()
        self.assertEqual(self.cli(*self.argv())[0], 0)
        from swarm import passcmd
        with self.board() as b:
            b.post('J', self.worker, 'DONE changed-cleanup@abcdef999999')
            start = mock.Mock(side_effect=AssertionError('stale candidate launched'))
            passcmd.run_pass(b, self.cfg, starter=start)
            start.assert_not_called()
            self.assertIsNone(b.completion_receipt('J'))
        with self.board() as b:
            b.agent_turn_ended('cleanup')
            b.queue_launch('J', 1, 'pending', role='engineer', harness='codex', model=None,
                workdir=str(self.tmp), brief='Unfinished task', queued_by='swarm pass')
        with self.assertRaises(ValueError):
            self.attest()

    def test_manual_failure_and_newer_cancellation_are_never_eligible(self):
        for status, by in [('failed', 'owner'), ('cancelled', 'owner'), ('completed', 'owner')]:
            with self.subTest(status=status):
                with self.board() as b:
                    b.close_job('J', status, '3 launches with no hand-off (DONE)', closed_by=by)
                with self.assertRaises(ValueError):
                    self.attest()

    def test_last_actual_turn_end_and_listener_restart_settle_without_owner_polling(self):
        self.finalized_review()
        from swarm import events_listener, dependencies
        with self.board() as b:
            b.tool_started('cleanup', 'Bash')
        def listener():
            return events_listener.Listener(self.cfg, SimpleNamespace(event_sources={}), self.board,
                reconcile=True, write_reconcile_health=lambda value: None)
        with mock.patch.object(events_listener, '_reconciler_guard', side_effect=contextlib.nullcontext):
            first = listener()
            self.assertTrue(first.reconcile_due(0))
            with self.board() as b:
                self.assertIsNone(b.completion_receipt('J'))
            restarted = listener()  # no lost private state, replay, or owner turn needed
            def wake(cfg):
                restarted.reconcile_due(0)
            with mock.patch.object(dependencies, 'wake', side_effect=wake):
                self.hook('stop', agent_id='cleanup', session=self.root, host='codex')
            with self.board() as b:
                self.assertIsNotNone(b.completion_receipt('J'))
                self.assertEqual(len([e for e in b.events('J') if e.kind == 'completion']), 1)

    def test_scoped_reaper_leaves_unowned_corrupt_runtime_and_lock_directory_untouched(self):
        from swarm.supervisor import runner, privfs
        self.assertIn('job', inspect.signature(runner.reap).parameters,
                      'closed review reconciliation needs a reaper restricted to its owned job/runtime')
        with runner._runs_fd(store_key(self.cfg)) as d:
            privfs.write_atomic(d, 'r999999.json', b'null')
            before = set(os.listdir(d))
        with self.board() as b:
            runner.reap(self.cfg, b, job='J', restart_id=1)
        with runner._runs_fd(store_key(self.cfg)) as d:
            self.assertEqual(privfs.read(d, 'r999999.json'), b'null')
            self.assertEqual(set(os.listdir(d)), before)

    def test_owner_cancel_during_actual_native_process_is_observed_without_enrollment(self):
        from swarm import passcmd
        from swarm.supervisor import runner
        self.attest()
        self.assertEqual(self.cli(*self.argv())[0], 0)
        runs = []
        with mock.patch('swarm.supervisor.launch.codex_command', return_value=['codex']):
            with self.board() as b:
                passcmd.run_pass(b, self.cfg, starter=lambda cfg, run: runs.append(run))
        self.assertEqual(len(runs), 1)
        run = runs[0]
        run.update(argv=[sys.executable, '-c', 'import time; time.sleep(60)'],
                   enrol_seconds=0.2, limit_seconds=1)
        children = []
        def popen(*args, **kwargs):
            proc = subprocess.Popen(*args, **kwargs)
            children.append(proc)
            with self.board() as b:
                b.close_job('J', 'cancelled', 'owner cancelled actual native review', closed_by='owner')
            return proc
        try:
            with mock.patch.object(runner, 'scope_available', return_value=False), \
                 mock.patch.object(runner, '_launch_argv', side_effect=lambda cfg, row: (row['argv'], None)):
                outcome = runner.execute(self.cfg, run, popen=popen, poll=0.01, board_every=0.01)
            self.assertEqual(outcome, 'cancelled', run.get('refused_reason'))
            self.assertTrue(all(p.poll() is not None for p in children))
            with self.board() as b:
                self.assertEqual(b.job_status('J').status, 'cancelled')
                self.assertIsNone(b.completion_receipt('J'))
        finally:
            for child in children:
                if child.poll() is None:
                    child.kill()
                child.wait(timeout=5)

    def test_ended_background_label_does_not_prove_owned_process_stopped(self):
        self.finalized_review()
        from swarm import bg
        tag = 'late-review-bg-fixture'
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],
            start_new_session=True, env={**os.environ, bg.TAG_ENV: tag})
        try:
            with self.board() as b:
                row_id = b._insert_bg('J', 'cleanup', self.worker, 'owned local fixture',
                    bg.this_host(), bg.boot_key(), child.pid, child.pid, bg.proc_start(child.pid), tag)
                b.bg_end(row_id, 'exited', 0)
                self.assertIsNone(b.settle_late_review('J'), 'a live owned background process must block settlement')
            child.terminate()
            child.wait(timeout=5)
            with self.board() as b:
                self.assertIsNotNone(b.settle_late_review('J'))
        finally:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)

    def test_schema28_file_writer_cannot_drop_native_completion_provenance(self):
        if self.h.name != 'file':
            self.skipTest('file-format compatibility')
        self.finalized_review()
        with self.board() as b:
            self.assertIsNotNone(b.settle_late_review('J'))
        from swarm.board.file import board_dir
        path = board_dir(self.cfg) / 'state.json'
        state = json.loads(path.read_text())
        # Schema28's real FileStore serializer keeps its known fields (including
        # opaque board metadata), dropping unknown new top-level collections.
        legacy = {k: v for k, v in state.items()
                  if k not in ('completion_receipts', 'finalization_receipts')}
        path.write_text(json.dumps(legacy))
        with self.board() as b:
            self.assertIsNotNone(b.completion_receipt('J'), 'an older ordinary post must not erase native receipts')
            self.assertIsNotNone(b.finalization_receipt('J'))

    def test_plain_finalized_text_never_settles_but_typed_executor_proof_settles_once(self):
        mid = self.accepted_review()
        with self.board() as b:
            b.post('J', self.worker, 'FINALIZED final-cleanup@abcdef012345')
            self.assertIsNone(b.settle_late_review('J'), 'plain board text is not native executor proof')
        argv = self.finalize_argv(mid)
        self.assertTrue(self.attest(argv=argv))
        rc, out, err = self.cli(*argv)
        self.assertEqual(rc, 0, err)
        with self.board() as b:
            from swarm.snapshot import job_row, fingerprint_of_rows
            before_fp = fingerprint_of_rows([job_row(b, b.job_status('J'), b.now())], [], 0)
            receipt = b.settle_late_review('J')
            self.assertIsNotNone(receipt, 'all native acceptance gates must have a real completion route')
            self.assertEqual(receipt['verdict_id'], mid)
            self.assertEqual(b.settle_late_review('J'), receipt)
            self.assertEqual(len([e for e in b.events('J') if e.kind == 'completion']), 1)
            after = b.job_status('J')
            for field in ('status', 'outcome', 'closed_by', 'finished_at', 'activated_at', 'goal'):
                self.assertEqual(getattr(after, field), getattr(self.original, field), field)
            from swarm.board.base import derive_job_status
            from swarm.snapshot import job_row
            self.assertEqual(derive_job_status(after, 5, b.now()), 'completed')
            snapshot = job_row(b, after, b.now())
            self.assertEqual((snapshot['status'], snapshot['lifecycle_status']), ('completed', 'failed'))
            from swarm import snapshot as native_snapshot
            reader = native_snapshot.open_snapshot_board(self.cfg)
            try:
                actual = native_snapshot.snapshot_once(reader, native_snapshot.Scope(jobs=('J',), messages=0))
                self.assertEqual(actual['jobs'][0]['status'], 'completed')
            finally:
                reader.close()
            self.assertNotEqual(before_fp, fingerprint_of_rows([snapshot], [], 0),
                                'missed native receipt notification must be repaired by the fingerprint')
            b.tool_started('cleanup', 'Bash')
            self.assertEqual(derive_job_status(b.job_status('J'), 5, b.now()), 'active',
                             'an audited completion receipt cannot hide actual running work')
            b.agent_turn_ended('cleanup')
            self.assertEqual(derive_job_status(b.job_status('J'), 5, b.now()), 'completed')
            # A pending replacement verdict can share the exact clock timestamp.
            # Its zero binding must invalidate projection of the prior receipt.
            from swarm import review
            pending = review._chunked(b.job_data('J'), 'pipeline.goal_verdict')
            pending['message_id'] = 0
            b.set_job_data('J', 'pipeline.goal_verdict.0', json.dumps(pending))
            self.assertEqual(derive_job_status(b.job_status('J'), 5, b.now()), 'failed',
                             'unbound replacement evidence must never inherit an old completion receipt')

    def test_new_authenticated_handoff_invalidates_old_completed_projection(self):
        self.finalized_review()
        from swarm.board.base import derive_job_status
        from swarm.snapshot import job_row
        with self.board() as b:
            self.assertIsNotNone(b.settle_late_review('J'))
            b.post('J', self.worker, 'DONE revised-cleanup@012345abcdef', agent_key='cleanup')
            b.agent_turn_ended('cleanup')
            status = b.job_status('J')
            self.assertEqual(derive_job_status(status, 5, b.now()), 'failed')
            self.assertEqual(job_row(b, status, b.now())['status'], 'failed')
            from swarm import snapshot as native_snapshot
            reader = native_snapshot.open_snapshot_board(self.cfg)
            try:
                actual = native_snapshot.snapshot_once(reader, native_snapshot.Scope(jobs=('J',), messages=0))
                self.assertEqual(actual['jobs'][0]['status'], 'failed')
            finally:
                reader.close()
            if hasattr(self.h, 'shown_status'):
                self.assertEqual(self.h.shown_status('J'), 'failed')
            self.assertIsNotNone(b.completion_receipt('J'), 'the old receipt remains immutable history')

    def test_new_native_task_obligation_cannot_inherit_prior_completion(self):
        self.finalized_review()
        from swarm.board.base import derive_job_status
        with self.board() as b:
            self.assertIsNotNone(b.settle_late_review('J'))
            b.queue_launch('J', 1, 'new-unfinished-obligation', role='engineer', harness='codex',
                model=None, workdir=str(self.workdir), brief='New actual work', queued_by='swarm task')
            self.assertEqual(derive_job_status(b.job_status('J'), 5, b.now()), 'failed')

    def test_completed_new_obligation_cannot_replace_an_immutable_receipt(self):
        self.finalized_review()
        from swarm.board.base import derive_job_status
        with self.board() as b:
            original = b.settle_late_review('J')
            row = b.queue_launch('J', 1, 'later-native-work', role='engineer', harness='codex',
                model=None, workdir=str(self.workdir), brief='Later native obligation', queued_by='swarm run')
            b.set_launch_state(row.id, row.revision, 'done', job='J', last_outcome='completed')
            self.assertIsNone(b.settle_late_review('J'), 'an existing receipt key cannot be rewritten after new work')
            self.assertEqual(b.completion_receipt('J'), original)
            self.assertEqual(len([e for e in b.events('J') if e.kind == 'completion']), 1)
            self.assertEqual(derive_job_status(b.job_status('J'), 5, b.now()), 'failed')

    def test_same_native_launch_requeue_invalidates_prior_completion(self):
        self.finalized_review()
        from swarm.board.base import derive_job_status
        with self.board() as b:
            original = b.settle_late_review('J')
            row = next(r for r in b.launches('J') if r.launch_id == original['review_launch_id'])
            queued = b.queue_launch('J', row.generation, row.name, role=row.role, harness=row.harness,
                model=row.model, workdir=row.workdir, brief=row.brief, queued_by=row.queued_by)
            self.assertEqual(queued.id, row.id)
            self.assertEqual(derive_job_status(b.job_status('J'), 5, b.now()), 'failed')
            self.assertEqual(b.completion_receipt('J'), original)
