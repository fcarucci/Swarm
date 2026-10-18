"""Runtime upgrade keeps exactly one listener and never targets worker processes."""
import contextlib
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest import mock

import support
from swarm import events_listener as el


class RuntimeIdentity(unittest.TestCase):
    def setUp(self):
        guard = mock.patch.object(el, '_reconciler_guard', side_effect=contextlib.nullcontext)
        guard.start()
        self.addCleanup(guard.stop)

    def test_healthy_legacy_heartbeat_is_not_current_runtime(self):
        cfg = {'_config_path': '/cfg', 'events': {'enabled': False}}
        health = {'config': '/cfg', 'stamp': None, 'beat': 100, 'state': 'ready', 'pid': 42}
        with mock.patch.object(el, 'take_lock', return_value=None), \
                mock.patch.object(el, '_read_reconcile_health', return_value=health), \
                mock.patch.object(el.time, 'time', return_value=100), \
                mock.patch.object(el, 'start_detached') as start:
            self.assertIn('runtime', el.ensure_reconciler(cfg))
            start.assert_not_called()

    def test_startup_identity_is_not_recomputed_from_changed_source_each_tick(self):
        old = {'lib': '/old/lib', 'digest': 'old'}
        new = {'lib': '/old/lib', 'digest': 'new'}
        health = []
        with mock.patch.object(el, '_runtime_identity', return_value=old, create=True):
            listener = el.Listener({}, NS(event_sources={}), lambda: contextlib.nullcontext(object()),
                                   reconcile=True, write_reconcile_health=health.append)
        with mock.patch.object(el, '_runtime_identity', return_value=new, create=True), \
                mock.patch('swarm.passcmd.run_pass', return_value=[]):
            listener.reconcile_due(0)
        self.assertEqual(health[-1]['runtime'], old)

    def test_identity_detects_same_path_source_replacement(self):
        with tempfile.TemporaryDirectory() as td:
            lib = Path(td) / 'lib'
            (lib / 'swarm').mkdir(parents=True)
            module = lib / 'swarm' / 'a.py'
            module.write_text('old')
            before = el._runtime_identity(lib)
            module.write_text('new')
            self.assertNotEqual(before, el._runtime_identity(lib))


class Upgrade(unittest.TestCase):
    def setUp(self):
        guard = mock.patch.object(el, '_reconciler_guard', side_effect=contextlib.nullcontext)
        guard.start()
        self.addCleanup(guard.stop)

    def setup_adapters(self):
        self.cfg = {'_config_path': '/cfg'}
        self.calls = []
        self.process = mock.Mock()
        self.process.verify.side_effect = lambda *args: self.calls.append('verify-process')
        self.process.terminate.side_effect = lambda: self.calls.append('term')
        self.process.wait.return_value = True
        self.service = mock.Mock()
        self.service.verify.side_effect = lambda *args: self.calls.append('verify-service')
        self.service.start.side_effect = lambda: self.calls.append('start-service')

    def run_upgrade(self, **kw):
        return el.upgrade_reconciler(self.cfg, '/old/lib', process_factory=lambda pid: self.process,
                                    service_manager=self.service, **kw)

    def test_verified_upgrade_waits_for_exit_and_lock_before_service_start(self):
        self.setup_adapters()
        with mock.patch.object(el, '_read_reconcile_health', return_value={'pid': 42}), \
                mock.patch.object(el, 'take_lock', side_effect=[None, os.open(os.devnull, os.O_RDONLY)]):
            self.assertIsNone(self.run_upgrade())
        self.assertEqual(self.calls, ['verify-service', 'verify-process', 'term', 'start-service'])
        self.service.wait_ready.assert_called_once_with(self.cfg, 10)
        self.process.wait.assert_called_once()
        self.process.close.assert_called_once()

    def test_any_identity_mismatch_refuses_without_signaling_or_starting(self):
        for refusal in ('cmdline', 'uid', 'runtime', 'lock'):
            with self.subTest(refusal=refusal):
                self.setup_adapters()
                self.process.verify.side_effect = ValueError(refusal)
                with mock.patch.object(el, '_read_reconcile_health', return_value={'pid': 42}), \
                        mock.patch.object(el, 'take_lock', return_value=None):
                    self.assertIn(refusal, self.run_upgrade())
                self.process.terminate.assert_not_called()
                self.service.start.assert_not_called()

    def test_missing_service_refuses_before_retiring_listener(self):
        self.setup_adapters()
        self.service.verify.side_effect = ValueError('install supported service first')
        self.assertIn('install supported service', self.run_upgrade())
        self.process.terminate.assert_not_called()

    def test_nonexiting_listener_or_contended_lock_never_starts_duplicate(self):
        for exited in (False, True):
            with self.subTest(exited=exited):
                self.setup_adapters()
                self.process.wait.return_value = exited
                with mock.patch.object(el, '_read_reconcile_health', return_value={'pid': 42}), \
                        mock.patch.object(el, 'take_lock', return_value=None):
                    self.assertIsNotNone(self.run_upgrade())
                self.service.start.assert_not_called()
                self.process.close.assert_called_once()

    def test_service_start_is_not_success_until_current_listener_is_ready(self):
        self.setup_adapters()
        self.service.wait_ready.side_effect = ValueError('no verified healthy current-runtime listener')
        with mock.patch.object(el, '_read_reconcile_health', return_value={'pid': 42}), \
                mock.patch.object(el, 'take_lock', side_effect=[None, os.open(os.devnull, os.O_RDONLY)]):
            self.assertIn('no verified healthy', self.run_upgrade())


class ProcessProof(unittest.TestCase):
    def test_real_process_proofs_require_exact_native_identity_and_held_lock(self):
        import sys
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            proc = root / '42'
            (proc / 'fd').mkdir(parents=True)
            (proc / 'fdinfo').mkdir()
            lock = root / 'listener.lock'
            lock.touch()
            (proc / 'fd' / '3').symlink_to(lock)
            (proc / 'exe').symlink_to(sys.executable)
            args = [sys.executable, '-m', 'swarm.cli', '--config', '/cfg', 'events', 'serve', '--reconcile']
            def write(argv=args, env=b'PYTHONPATH=/old/lib\0', info='lock: 1: FLOCK  ADVISORY  WRITE 0 dev:inode 0 EOF\n'):
                (proc / 'cmdline').write_bytes(b'\0'.join(a.encode() for a in argv) + b'\0')
                (proc / 'environ').write_bytes(env)
                (proc / 'fdinfo' / '3').write_text(info)
            with mock.patch.object(os, 'pidfd_open', side_effect=lambda pid: os.open(os.devnull, os.O_RDONLY)), \
                    mock.patch('select.select', return_value=([], [], [])), \
                    mock.patch.object(el, '_lock_path', return_value=lock):
                process = el._ListenerProcess(42, root)
                self.addCleanup(process.close)
                write()
                process.verify({'_config_path': '/cfg'}, '/old/lib')
                for changed, why in (({'argv': args[:-1]}, 'cmdline'),
                                     ({'env': b'PYTHONPATH=/other/lib\0'}, 'runtime'),
                                     ({'info': ''}, 'exclusive listener lock')):
                    write(**changed)
                    with self.assertRaisesRegex(ValueError, why):
                        process.verify({'_config_path': '/cfg'}, '/old/lib')
                write()
                (proc / 'exe').unlink()
                (proc / 'exe').symlink_to('/bin/false')
                with self.assertRaisesRegex(ValueError, 'interpreter'):
                    process.verify({'_config_path': '/cfg'}, '/old/lib')
                (proc / 'exe').unlink()
                (proc / 'exe').symlink_to(sys.executable)
                with mock.patch.object(os, 'getuid', return_value=os.getuid() + 1):
                    with self.assertRaisesRegex(ValueError, 'uid'):
                        process.verify({'_config_path': '/cfg'}, '/old/lib')


class ServiceProof(unittest.TestCase):
    def test_ready_means_current_runtime_and_exact_service_pid_with_lock(self):
        runtime = {'lib': '/current/lib', 'digest': 'current'}
        good = {'runtime': runtime, 'config': '/cfg', 'stamp': None, 'state': 'ready', 'beat': 100, 'pid': 42}
        service = el._ReconcilerService()
        for change, accepted in (({}, True), ({'runtime': {'lib': '/old/lib'}}, False),
                                 ({'runtime': {**runtime, 'digest': 'old'}}, False),
                                 ({'pid': 43}, False), ({'state': 'starting'}, False),
                                 ({'beat': 0}, False)):
            with self.subTest(change=change), \
                    mock.patch.object(el, '_runtime_identity', return_value=runtime), \
                    mock.patch.object(el, '_read_reconcile_health', return_value={**good, **change}), \
                    mock.patch.object(el, '_ListenerProcess') as process, \
                    mock.patch.object(service, 'command', return_value='42'), \
                    mock.patch.object(el.time, 'time', return_value=200), \
                    mock.patch.object(el.time, 'monotonic', return_value=0):
                if accepted:
                    service.wait_ready({'_config_path': '/cfg'}, 0)
                    process.assert_called_once_with(42)
                    process.return_value.verify.assert_called_once()
                    process.return_value.close.assert_called_once()
                else:
                    with self.assertRaisesRegex(ValueError, 'no verified healthy'):
                        service.wait_ready({'_config_path': '/cfg'}, 0)
                    process.assert_not_called()

    def test_loaded_unit_must_pin_native_current_runtime_and_preserve_workers(self):
        import sys
        from swarm import paths
        service = el._ReconcilerService()
        unit = ('LoadState=loaded\nActiveState=inactive\nKillMode=process\nRestart=on-failure\n'
                f'Environment=PYTHONPATH={paths.LIB_DIR}\n'
                f'ExecStart={{ path={sys.executable} ; argv[]={sys.executable} -m swarm.cli --config /cfg events serve --reconcile ; }}\n')
        with mock.patch.object(service, 'command', return_value=unit):
            service.verify({'_config_path': '/cfg'})
        for old, new in (('KillMode=process', 'KillMode=control-group'),
                         (str(paths.LIB_DIR), '/obsolete/lib'),
                         ('--config /cfg', '--config /other'),
                         ('LoadState=loaded', 'LoadState=loaded\nEnvironmentFiles=/override.env'),
                         ('LoadState=loaded', 'LoadState=loaded\nUnsetEnvironment=PYTHONPATH'),
                         ('LoadState=loaded', 'LoadState=loaded\nPassEnvironment=PYTHONPATH'),
                         (f'path={sys.executable}', 'path=/bin/false'),
                         (' ; }', ' ; } { path=/bin/false ; argv[]=/bin/false ; }'),
                         ('ActiveState=inactive', 'ActiveState=active')):
            with self.subTest(old=old), mock.patch.object(service, 'command', return_value=unit.replace(old, new)):
                with self.assertRaisesRegex(ValueError, 'supported reconciler service'):
                    service.verify({'_config_path': '/cfg'})


class StartupSerialization(unittest.TestCase):
    def test_held_upgrade_guard_refuses_hook_start_without_touching_listener_lock(self):
        from swarm.supervisor import privfs
        with tempfile.TemporaryDirectory() as td:
            directory = os.open(td, os.O_RDONLY)
            @contextlib.contextmanager
            def open_directory():
                yield directory
            try:
                with mock.patch.object(privfs, 'dir_fd', side_effect=open_directory):
                    with el._reconciler_guard(), mock.patch.object(el, 'take_lock') as lock:
                        self.assertIn('upgrade/startup in progress', el.ensure_reconciler({}))
                        lock.assert_not_called()
                        start = mock.Mock()
                        self.assertEqual(el.ensure_running({'events': {'enabled': True}}, lambda text: None,
                                                          probe_fn=lambda cfg: False, lock_fn=lambda: False,
                                                          start=start), 'stuck')
                        start.assert_not_called()
            finally:
                os.close(directory)
