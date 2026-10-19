"""Internal listener reconciliation without HTTP, plugins, or orchestrator turns."""
import sys
import contextlib
import inspect
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest import mock

import support
from swarm import events_listener as el, cli


class InternalReconciliation(unittest.TestCase):
    def setUp(self):
        guard = mock.patch.object(el, '_reconciler_guard', side_effect=contextlib.nullcontext)
        guard.start()
        self.addCleanup(guard.stop)

    def listener(self, cfg=None, board=None, logs=None):
        self.assertTrue(hasattr(el.Listener, 'reconcile_due'), 'listener must reconcile durable work internally')
        return el.Listener(cfg or {'events': {'enabled': False}, 'pass': {'max_cost_usd': 7}},
                           NS(event_sources={}), lambda: contextlib.nullcontext(board),
                           log=(logs.append if logs is not None else None), reconcile=True,
                           write_reconcile_health=lambda data: None)

    def test_tick_is_periodic_coalesced_and_preserves_full_config(self):
        cfg = {'events': {'enabled': False}, 'pass': {'max_cost_usd': 7}, 'hook': {'custom': 'kept'}}
        board = object()
        listener = self.listener(cfg, board)
        with mock.patch('swarm.passcmd.run_pass', return_value=[]) as run:
            self.assertTrue(listener.reconcile_due(100))
            self.assertFalse(listener.reconcile_due(101))
            self.assertTrue(listener.reconcile_due(105))
            self.assertEqual(run.call_count, 2)
            self.assertIs(run.call_args.args[0], board)
            self.assertIs(run.call_args.args[1], cfg)
            listener._reconcile_lock.acquire()
            try:
                self.assertFalse(listener.reconcile_due(110))
            finally:
                listener._reconcile_lock.release()
            self.assertTrue(listener.reconcile_due(110))

    def test_failed_tick_retries_and_never_logs_exception_secrets(self):
        logs = []
        listener = self.listener(logs=logs)
        with mock.patch('swarm.passcmd.run_pass', side_effect=[RuntimeError('secret-token'), []]) as run:
            self.assertTrue(listener.reconcile_due(100))
            self.assertTrue(listener.reconcile_due(105))
            self.assertEqual(run.call_count, 2)
        self.assertNotIn('secret-token', '\n'.join(logs))
        self.assertTrue(any('RuntimeError' in line for line in logs))

    def test_returned_per_job_errors_report_unhealthy_then_recover(self):
        logs, health = [], []
        listener = self.listener(logs=logs)
        listener.write_reconcile_health = health.append
        with mock.patch('swarm.passcmd.run_pass', side_effect=[
                ['J: pass error RuntimeError: secret-token'], []]):
            listener.reconcile_due(100)
            self.assertEqual(health[-1]['state'], 'error')
            self.assertEqual(health[-1]['error'], 'per-job pass error')
            listener.reconcile_due(105)
            self.assertEqual(health[-1]['state'], 'ready')
        self.assertNotIn('secret-token', repr(health) + '\n'.join(logs))

    def test_internal_serve_never_binds_even_when_events_are_disabled(self):
        self.assertIn('reconcile', inspect.signature(el.serve).parameters)
        for enabled in (False, True):
            with self.subTest(enabled=enabled):
                fd = os.open(os.devnull, os.O_RDONLY)
                with mock.patch.object(el, 'take_lock', return_value=fd), \
                        mock.patch.object(el, '_write_reconcile_health'), \
                        mock.patch.object(el.Listener, '_poll_loop', autospec=True, side_effect=lambda listener: listener.reconcile_due(0)), \
                        mock.patch.object(el, 'ThreadingHTTPServer', side_effect=AssertionError('internal mode opened HTTP')), \
                        mock.patch('swarm.passcmd.run_pass', return_value=[]) as run:
                    self.assertEqual(el.serve({'events': {'enabled': enabled}}, NS(event_sources={}),
                                             lambda: contextlib.nullcontext(object()), reconcile=True), 0)
                self.assertEqual(run.call_count, 1)

    def test_cli_has_internal_mode(self):
        parser = cli._parser()
        with contextlib.redirect_stderr(io.StringIO()):
            try:
                args = parser.parse_args(['events', 'serve', '--reconcile'])
            except SystemExit:
                args = None
        self.assertIsNotNone(args, 'native events serve must accept --reconcile')
        self.assertTrue(args.reconcile)

    def test_detached_start_hands_off_the_single_listener_lock(self):
        self.assertIn('reconcile', inspect.signature(el.start_detached).parameters)
        start = mock.Mock(return_value=NS(pid=123))
        with tempfile.TemporaryDirectory() as td:
            fd = os.open(str(Path(td) / 'lock'), os.O_RDWR | os.O_CREAT, 0o600)
            try:
                self.assertEqual(el.start_detached({'_config_path': '/config.toml'}, start,
                                                   reconcile=True, lock_fd=fd), 123)
                self.assertEqual(start.call_args.args[0][-3:], ['events', 'serve', '--reconcile'])
                self.assertEqual(start.call_args.kwargs['pass_fds'], (fd,))
                self.assertEqual(start.call_args.kwargs['env']['SWARM_EVENTS_LOCK_FD'], str(fd))
            finally:
                os.close(fd)

    def test_startup_is_coalesced_and_a_crashed_listener_releases_its_lock(self):
        with tempfile.TemporaryDirectory() as td:
            held, health, starts = [], {}, []
            cfg = {'events': {'enabled': False}, '_config_path': '/config.toml'}
            def start(cfg, **kw):
                held.append(os.dup(kw['lock_fd']))  # the child's inherited flock
                starts.append(kw)
                return 123
            def write(data):
                health.clear()
                health.update(data)
            with mock.patch.object(el, '_lock_path', return_value=Path(td) / 'listener.lock'), \
                    mock.patch('swarm.supervisor.settings.ensure_private_dir'), \
                    mock.patch.object(el, '_write_reconcile_health', side_effect=write), \
                    mock.patch.object(el, '_read_reconcile_health', side_effect=lambda: dict(health)), \
                    mock.patch.object(el, 'start_detached', side_effect=start):
                try:
                    self.assertIsNone(el.ensure_reconciler(cfg))
                    self.assertIsNone(el.ensure_reconciler(cfg))
                    self.assertEqual(len(starts), 1)
                    os.close(held.pop())  # hard death: OS releases the inherited flock
                    self.assertIsNone(el.ensure_reconciler(cfg))
                    self.assertEqual(len(starts), 2)
                    with mock.patch.object(el, '_reconcile_stamp', return_value=42, create=True):
                        self.assertIn('config changed', el.ensure_reconciler(cfg))
                    health['config'] = '/another-config.toml'
                    self.assertIn('another config', el.ensure_reconciler(cfg))
                    self.assertEqual(len(starts), 2)
                finally:
                    for fd in held:
                        os.close(fd)

    def test_failed_spawn_is_reported_and_does_not_strand_the_listener_lock(self):
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(el, '_lock_path', return_value=Path(td) / 'listener.lock'), \
                mock.patch('swarm.supervisor.settings.ensure_private_dir'), \
                mock.patch.object(el, '_write_reconcile_health'), \
                mock.patch.object(el, 'start_detached', side_effect=OSError('secret argument')):
            message = el.ensure_reconciler({'events': {'enabled': False}})
            self.assertIn('OSError', message)
            self.assertNotIn('secret argument', message)
            lock = el.take_lock()
            self.assertIsNotNone(lock)
            os.close(lock)

    def test_service_adopts_inherited_lock_and_clears_environment(self):
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(el, '_lock_path', return_value=Path(td) / 'listener.lock'), \
                mock.patch('swarm.supervisor.settings.ensure_private_dir'):
            fd = el.take_lock()
            try:
                with mock.patch.dict(os.environ, {el.LOCK_FD_ENV: str(fd)}):
                    self.assertEqual(el._listener_lock(), fd)
                    self.assertNotIn(el.LOCK_FD_ENV, os.environ)
                self.assertTrue(el.lock_held())
            finally:
                os.close(fd)


class DurableReconciliation(unittest.TestCase):
    def test_persisted_queue_survives_listener_restart_and_launches_only_once_without_hooks(self):
        self.assertTrue(hasattr(el.Listener, 'reconcile_due'), 'listener recovery must run the native pass')
        harness = support.MemoryHarness('internal-reconciliation')
        self.addCleanup(harness.close)
        harness.reset()
        board = harness.board()
        self.addCleanup(board.close)
        board.open_job('J', None, None, None, 'owner', goal='execute persisted work')
        board.set_job_data('J', 'workdir', '/work')
        board.queue_launch('J', 1, 'build', role='engineer', harness='codex', model=None,
                           workdir='/work', brief='build exact artifact', queued_by='swarm run')
        cfg = {**harness.cfg, 'events': {'enabled': False}}
        # Only the injected starter executes: validate argv without a local Codex install.
        cfg.setdefault('harness', {}).setdefault('codex', {})['command'] = [sys.executable]
        runs = []
        def listener():
            return el.Listener(cfg, NS(event_sources={}), lambda: contextlib.nullcontext(board),
                               reconcile=True, write_reconcile_health=lambda data: None)
        with mock.patch('swarm.supervisor.command.workdir_problem', return_value=None), \
                mock.patch('swarm.supervisor.markers.write_resume_marker', return_value=Path('/no-marker')), \
                mock.patch('swarm.supervisor.runner.start', side_effect=lambda cfg, run: runs.append(run)):
            listener()  # crash after durable queue, before the first tick
            listener().reconcile_due(0)
            listener().reconcile_due(0)  # a second process restart reads the same claimed task
        self.assertEqual([r['launch'] for r in runs], ['build'])
        self.assertEqual(len(board.restarts('J')), 1)
