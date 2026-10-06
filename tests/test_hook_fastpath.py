"""The quiet tool hook must exit before launching any external command."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import sys
import unittest
from support import ROOT

@unittest.skipUnless(sys.platform.startswith("linux"), "the shell fast path uses Linux /proc/uptime")
class ShellFastPathTests(unittest.TestCase):
    def test_quiet_lease_is_shell_only(self):
        with tempfile.TemporaryDirectory() as td:
            home=Path(td); hd=home/'.local/share/swarm/host'; hd.mkdir(parents=True,mode=0o700)
            fp=hd/'fastpath'; fp.mkdir(mode=0o700)
            cfg=home/'config.toml'; cfg.write_text('[hook]\nhook_min_interval_s=15\n')
            # A future uptime deadline and the exact generation already consumed.
            boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip()
            cache=hd/'hook-config-claude'; cache.write_text(f'{cfg}\n{ROOT}\n{fp}\n{home}/active\n1\nuptime-v1\n{boot_id}\n')
            os.utime(cache, ns=(cfg.stat().st_atime_ns,cfg.stat().st_mtime_ns))
            (fp/'live').write_text('9999999999\n')
            (fp/'board-J').write_text('generation-1\n')
            (fp/'agent-a').write_text('9999999999\nJ\ngeneration-1\n')
            payload=json.dumps({'session_id':'s','agent_id':'a','tool_name':'Bash','tool_input':{}})
            env=dict(os.environ,HOME=td,SWARM_CONFIG=str(cfg),PATH='/nonexistent')
            r=subprocess.run([str(ROOT/'bin/swarm-hook'),'--host','claude','turn'],input=payload,text=True,capture_output=True,env=env)
            self.assertEqual(r.returncode,0,r.stderr)
            self.assertEqual(r.stdout,'')
            self.assertEqual(r.stderr,'', 'fast path launched an external command')
            # Old monotonic-clock caches cannot authorize a shell skip after upgrade.
            current_cache = cache.read_text()
            cache.write_text(current_cache.split('uptime-v1\n', 1)[0])
            os.utime(cache, ns=(cfg.stat().st_atime_ns,cfg.stat().st_mtime_ns))
            r=subprocess.run([str(ROOT/'bin/swarm-hook'),'--host','claude','turn'],input=payload,text=True,capture_output=True,env=env)
            self.assertNotEqual(r.stderr,'', 'old clock cache authorized a skipping lease')
            cache.write_text(current_cache)
            os.utime(cache, ns=(cfg.stat().st_atime_ns,cfg.stat().st_mtime_ns))
            cache.write_text(current_cache.replace(boot_id, 'previous-boot'))
            os.utime(cache, ns=(cfg.stat().st_atime_ns,cfg.stat().st_mtime_ns))
            r=subprocess.run([str(ROOT/'bin/swarm-hook'),'--host','claude','turn'],input=payload,text=True,capture_output=True,env=env)
            self.assertNotEqual(r.stderr,'', 'previous boot authorized a skipping lease')
            cache.write_text(current_cache)
            os.utime(cache, ns=(cfg.stat().st_atime_ns,cfg.stat().st_mtime_ns))
            (fp/'agent-session-s').write_text('9999999999\nJ\ngeneration-1\n')
            payload=json.dumps({'session_id':'s','tool_name':'Bash','tool_input':{},'agent_id':'unknown'})
            r=subprocess.run([str(ROOT/'bin/swarm-hook'),'--host','claude','turn'],input=payload,text=True,capture_output=True,env=env)
            self.assertNotEqual(r.stderr,'', 'agent identity after arguments borrowed the parent lease')

from unittest import mock
from test_hooks_cli import Env
from swarm import fastpath, paths, hooks
from swarm.board.base import derive_agent_status
import datetime as dt
import time

class ClockTests(unittest.TestCase):
    def test_boot_identity_invalidates_persistent_deadlines(self):
        with mock.patch.object(fastpath, 'boot_id', return_value='first-boot'):
            first=fastpath.directory()
        with mock.patch.object(fastpath, 'boot_id', return_value='second-boot'):
            self.assertNotEqual(first,fastpath.directory())

    @unittest.skipUnless(Path('/proc/uptime').exists(), 'requires /proc/uptime')
    def test_now_agrees_with_shell_uptime(self):
        uptime = float(Path('/proc/uptime').read_text().split()[0])
        self.assertLess(abs(fastpath.now() - uptime), 1)

    def test_now_falls_back_when_uptime_is_unavailable_or_invalid(self):
        for error in (OSError('unavailable'), ValueError('invalid')):
            with self.subTest(error=error), \
                 mock.patch.object(Path, 'read_text', side_effect=error), \
                 mock.patch.object(fastpath.time, 'monotonic', return_value=123.5):
                self.assertEqual(fastpath.now(), 123.5)
        for content in ('', 'invalid 100'):
            with self.subTest(content=content), \
                 mock.patch.object(Path, 'read_text', return_value=content), \
                 mock.patch.object(fastpath.time, 'monotonic', return_value=123.5):
                self.assertEqual(fastpath.now(), 123.5)

@unittest.skipUnless(sys.platform.startswith("linux"), "the shell fast path uses Linux /proc/uptime")
class LeaseIntegrationTests(Env):
    def setUp(self):
        super().setUp()
        patch=mock.patch.dict(os.environ,SWARM_CONFIG=str(self.config),SWARM_HOOK_FASTPATH='1',SWARM_HOOK_EVENT='turn')
        patch.start(); self.addCleanup(patch.stop)
        # Unit drivers are in one process. Keep the persistent notifier out of their fixtures.
        notifier=mock.patch.object(fastpath,'ensure_notifier')
        notifier.start(); self.addCleanup(notifier.stop)
        self.cli('activate','--job','J','--session','sess-1')
        self.cli('join','--job','J','--key','agent-1')
        self.author = self.peer()

    def lease(self):
        return fastpath.read('agent-agent-1')

    def test_rate_limit_and_unread_messages_without_an_extra_heartbeat(self):
        self.hook('turn',tool_name='Bash')
        first=self.agent('agent-1')
        deadline=self.lease()[0]
        self.cli('post','--job','J','--as',self.author,'--to',first.name,'addressed-message')
        result=self.hook('turn',tool_name='Bash')
        self.assertIn('addressed-message',self.context(result))
        second=self.agent('agent-1')
        self.assertEqual(second.last_contact_at,first.last_contact_at)
        self.assertEqual(second.tool_calls,first.tool_calls)
        self.assertEqual(self.lease()[0],deadline)
        # Expire only the local lease, without sleeping or changing board timestamps.
        fastpath.write('agent-agent-1',[0,'J',self.lease()[2]])
        fastpath.write('contact-agent-1',[0])
        self.hook('turn',tool_name='Bash')
        self.assertGreater(self.agent('agent-1').tool_calls,first.tool_calls)

    def test_a_post_during_a_read_is_not_acknowledged_by_the_lease(self):
        lease=fastpath.Lease(self.cfg,'claude',{'agent_id':'agent-1'})
        lease.capture('J')
        previous=lease.generations['J'][0]
        fastpath.changed('J')
        lease.allowed=True
        lease.finish()
        self.assertEqual(self.lease()[2],previous)
        self.assertNotEqual(self.lease()[2],fastpath.read('board-J')[0])

    def test_backlog_keeps_python_reads_enabled(self):
        self.cfg['board']['read_limit']=1
        for i in range(3):
            self.cli('post','--job','J','--as',self.author,f'backlog-{i}')
        result=self.hook('turn',tool_name='Bash')
        self.assertIn('more unread',self.context(result))
        self.assertEqual(self.lease()[0],'0')
        before = self.agent('agent-1').last_contact_at
        self.assertIn('backlog-',self.context(self.hook('turn',tool_name='Bash')))
        self.assertEqual(self.agent('agent-1').last_contact_at, before)

    def test_done_preserves_the_unread_stamp(self):
        self.hook('turn',tool_name='Bash')
        previous=self.lease()
        self.cli('post','--job','J','--as',self.author,'next-turn-message')
        with mock.patch.dict(os.environ,SWARM_HOOK_EVENT='done'):
            self.hook('done')
        self.assertEqual(self.lease()[1:],previous[1:])
        self.assertIn('next-turn-message',self.context(self.hook('turn',tool_name='Bash')))

    def test_verifier_cannot_receive_a_skipping_lease(self):
        with self.board() as board:
            board.claim_verifier('agent-1','J')
        self.hook('turn',tool_name='Read')
        self.assertEqual(self.lease()[0],'0')

    def test_start_and_stop_retain_original_bookkeeping(self):
        self.cli('leave','--key','agent-1')
        self.hook('start',agent_type='engineer',prompt='[swarm job: J]')
        self.assertIsNone(self.agent('agent-1').ended_at)
        self.hook('turn',tool_name='Bash')
        self.hook('stop')
        self.assertEqual(self.agent('agent-1').status,'completed')

    def test_sampled_tool_does_not_prevent_idle_or_dead_detection(self):
        self.hook('turn',tool_name='Bash')
        self.h.backdate_agent('agent-1',last_seen=6*60)
        self.assertEqual(self.agent('agent-1').status,'idle')
        self.h.backdate_agent('agent-1',last_seen=31*60)
        self.assertEqual(self.agent('agent-1').status,'dead')

    def test_large_intervals_are_capped_well_below_detection_thresholds(self):
        self.cfg['hook']['hook_min_interval_s']=600
        self.assertEqual(fastpath.interval(self.cfg),30)
        self.cfg['board']['idle_minutes']=1
        self.assertEqual(fastpath.interval(self.cfg),6)

@unittest.skipUnless(sys.platform.startswith("linux"), "the shell fast path uses Linux /proc/uptime")
class ShellDeliveryTests(Env):
    def setUp(self):
        # A process-shared board, and a synthetic always-healthy notifier for the shell test.
        patch=mock.patch('support.E2E_BACKEND','file'); patch.start()
        try: super().setUp()
        finally: patch.stop()
        patch=mock.patch.dict(os.environ,SWARM_CONFIG=str(self.config))
        patch.start(); self.addCleanup(patch.stop)
        self.cli('activate','--job','J','--session','sess-1')
        self.cli('join','--job','J','--key','agent-1')
        self.author = self.peer()
        fastpath.write('live',[9999999999])
        venv=self.tmp/'venv';(venv/'bin').mkdir(parents=True)
        (venv/'bin/python').symlink_to(__import__('sys').executable)
        self.env=dict(os.environ,SWARM_VENV=str(venv))

    def shell(self,event='turn',quiet=False):
        payload=json.dumps({'agent_id':'agent-1','session_id':'sess-1','tool_name':'Bash','tool_input':{}})
        env={**self.env,'PATH':'/nonexistent'} if quiet else self.env
        result=subprocess.run([str(ROOT/'bin/swarm-hook'),'--host','claude',event],
            input=payload,text=True,capture_output=True,env=env,timeout=15)
        self.assertEqual(result.returncode,0,result.stderr)
        return result

    def test_addressed_post_visible_next_turn_and_done_cannot_consume_it(self):
        self.shell()
        first=self.agent('agent-1')
        result=self.shell(quiet=True)
        self.assertEqual((result.stdout,result.stderr),('',''))
        self.cli('post','--job','J','--as',self.author,'--to',first.name,'shell-delivery')
        result=self.shell('done',quiet=True)
        self.assertEqual((result.stdout,result.stderr),('',''))
        result=self.shell()
        self.assertIn('shell-delivery',result.stdout)
        self.assertEqual(first.last_contact_at,self.agent('agent-1').last_contact_at)
        self.assertNotIn('shell-delivery',self.shell(quiet=True).stdout)
        self.shell('stop')
        self.assertEqual(self.agent('agent-1').status,'completed')
        self.assertEqual(fastpath.read('agent-agent-1')[0],'0')

    def test_stale_notifier_forces_python_even_with_a_fresh_lease(self):
        self.shell()
        fastpath.write('live',[0])
        # Force a visible response on this slow read; generation stays unchanged.
        with self.board() as board:
            board.post('J','Alice','fallback-read')
        result=self.shell()
        self.assertIn('fallback-read',result.stdout)
        # The hook may start the real notifier after seeing the stale one. Removing its
        # markers makes it exit, without touching any other job's process.
        for marker in self.markers.glob('*.json'): marker.unlink()

    def test_config_change_invalidates_cache_and_can_disable_sampling(self):
        self.shell()
        before=fastpath.directory()
        with self.config.open('a') as file: file.write('\n')
        self.assertNotEqual(before,fastpath.directory())
        fastpath.write('live',[9999999999])
        self.shell()
        cached=(paths.host_dir()/'hook-config-claude').read_text().splitlines()
        self.assertEqual(cached[2],str(fastpath.directory()))
        self.assertEqual((paths.host_dir()/'hook-config-claude').stat().st_mtime_ns,self.config.stat().st_mtime_ns)

@unittest.skipUnless(sys.platform.startswith("linux"), "the shell fast path uses Linux /proc/uptime")
class NotifierTests(unittest.TestCase):
    def test_runtime_failure_clears_health_and_is_silent_at_entry(self):
        from swarm.board import setup_board
        from swarm.board.file import FileBoard
        from swarm.cli import load_config
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            cfgfile = home / 'config.toml'
            markers = home / 'markers'
            markers.mkdir()
            (markers / 'J.json').write_text(json.dumps({'job': 'J'}))
            cfgfile.write_text(f'[board]\nbackend="file"\n[file]\npath="{home}/board"\n[hook]\nmarker_dir="{markers}"\n')
            observed = []
            def fail_relay(timeout):
                observed.append(fastpath.read('live'))
                # A second notifier loses the lock and must not clear the owner's health.
                fastpath.main()
                observed.append(fastpath.read('live'))
                raise RuntimeError('relay failed')
            with mock.patch.dict(os.environ, HOME=td, SWARM_CONFIG=str(cfgfile)), \
                 mock.patch.object(sys, 'argv', ['swarm.fastpath']), \
                 mock.patch.object(fastpath, 'now', return_value=100.25), \
                 mock.patch.object(FileBoard, 'wait_for_change', side_effect=fail_relay):
                setup_board(load_config(cfgfile))
                fastpath.write('live', [9999999999])
                fastpath.main()
                self.assertEqual(fastpath.read('live'), ['0'])
                self.assertEqual(observed, [['105'], ['105']])

    def test_config_change_clears_original_notifier_health(self):
        from swarm.board import setup_board
        from swarm.board.file import FileBoard
        from swarm.cli import load_config
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            cfgfile = home / 'config.toml'
            markers = home / 'markers'
            markers.mkdir()
            (markers / 'J.json').write_text(json.dumps({'job': 'J'}))
            cfgfile.write_text(f'[board]\nbackend="file"\n[file]\npath="{home}/board"\n[hook]\nmarker_dir="{markers}"\n')
            with mock.patch.dict(os.environ, HOME=td, SWARM_CONFIG=str(cfgfile)):
                setup_board(load_config(cfgfile))
                original = fastpath.directory()
                def change_config(timeout):
                    cfgfile.write_text(cfgfile.read_text() + '\n')
                    return False
                with mock.patch.object(FileBoard, 'wait_for_change', side_effect=change_config):
                    fastpath.notifier()
                self.assertEqual((original / 'live').read_text(), '0\n')

    def test_startup_invalidates_read_to_listen_gap_and_crash_expires_health(self):
        with tempfile.TemporaryDirectory() as td:
            home=Path(td); cfgfile=home/'config.toml'
            cfgfile.write_text(f'[board]\nbackend="file"\n[file]\npath="{home}/board"\n[hook]\nmarker_dir="{home}/markers"\n')
            markers=home/'markers';markers.mkdir();(markers/'J.json').write_text(json.dumps({'job':'J'}))
            with mock.patch.dict(os.environ,HOME=td,SWARM_CONFIG=str(cfgfile)):
                from swarm.cli import load_config
                cfg=load_config(cfgfile)
                from swarm.board import setup_board,open_board
                setup_board(cfg)
                with open_board(cfg) as board:
                    board.ensure_job('J')
                fastpath.changed('J');previous=fastpath.read('board-J')[0]
                env=dict(os.environ,PYTHONPATH=str(ROOT/'lib'))
                child=subprocess.Popen([__import__('sys').executable,'-B','-m','swarm.fastpath'],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
                self.addCleanup(lambda: child.poll() is None and child.terminate())
                try:
                    deadline=time.monotonic()+8
                    while time.monotonic()<deadline and not fastpath.read('live'):
                        time.sleep(.02)
                    self.assertTrue(fastpath.read('live'),'notifier did not become healthy')
                    self.assertNotEqual(previous,fastpath.read('board-J')[0],'startup gap was not invalidated')
                    previous=fastpath.read('board-J')[0]
                    with open_board(cfg) as board:
                        board.post('J','Alice','external-writer') # bypass CLI stamp
                    deadline=time.monotonic()+5
                    while time.monotonic()<deadline and previous==fastpath.read('board-J')[0]:
                        time.sleep(.02)
                    self.assertNotEqual(previous,fastpath.read('board-J')[0],'external post did not invalidate')
                    (markers/'J.json').unlink()
                    child.wait(timeout=5)
                    self.assertEqual(fastpath.read('live'),['0'])
                finally:
                    if child.poll() is None: child.terminate();child.wait(timeout=5)

import test_respawn as respawn_tests

@unittest.skipUnless(sys.platform.startswith("linux"), "the shell fast path uses Linux /proc/uptime")
class FastRespawnTests(respawn_tests.TriggerTests):
    def setUp(self):
        super().setUp()
        patch=mock.patch.dict(os.environ,SWARM_CONFIG=str(self.config),SWARM_HOOK_FASTPATH='1')
        patch.start();self.addCleanup(patch.stop)
        patch=mock.patch.object(fastpath,'ensure_notifier')
        patch.start();self.addCleanup(patch.stop)

    def hook(self,event,*args,**kwargs):
        with mock.patch.dict(os.environ,SWARM_HOOK_EVENT=event):
            return super().hook(event,*args,**kwargs)

@unittest.skipUnless(sys.platform.startswith("linux"), "the shell fast path uses Linux /proc/uptime")
class FastStopTests(respawn_tests.StopTests):
    def setUp(self):
        super().setUp()
        patch=mock.patch.dict(os.environ,SWARM_CONFIG=str(self.config),SWARM_HOOK_FASTPATH='1')
        patch.start();self.addCleanup(patch.stop)
        patch=mock.patch.object(fastpath,'ensure_notifier')
        patch.start();self.addCleanup(patch.stop)

    def hook(self,event,*args,**kwargs):
        with mock.patch.dict(os.environ,SWARM_HOOK_EVENT=event):
            return super().hook(event,*args,**kwargs)

@unittest.skipUnless(sys.platform.startswith("linux"), "the shell fast path uses Linux /proc/uptime")
class ContactBoundaryTests(Env):
    def test_notifier_health_uses_the_same_clock_as_its_deadline(self):
        with mock.patch.dict(os.environ, SWARM_CONFIG=str(self.config)), \
             mock.patch.object(fastpath, 'now', return_value=100.25), \
             mock.patch.object(fastpath.time, 'monotonic', return_value=900), \
             mock.patch.object(fastpath.subprocess, 'Popen') as spawn:
            cfg = {**self.cfg, 'board': {**self.cfg['board'], 'backend': 'file'}}
            fastpath.write('live', [105])
            fastpath.ensure_notifier(cfg)
            self.assertEqual(spawn.call_count, 0)
            fastpath.write('live', [100])
            fastpath.ensure_notifier(cfg)
            self.assertEqual(spawn.call_count, 1)

    def test_fractional_clock_and_concurrent_hooks_cannot_shorten_the_window(self):
        with mock.patch.dict(os.environ,SWARM_CONFIG=str(self.config),SWARM_HOOK_FASTPATH='1'), \
             mock.patch.object(fastpath,'now',return_value=100.9), \
             mock.patch.object(fastpath,'ensure_notifier'):
            first=fastpath.Lease(self.cfg,'claude',{'agent_id':'agent-1'})
            second=fastpath.Lease(self.cfg,'claude',{'agent_id':'agent-1'})
            self.assertTrue(first.due)
            self.assertFalse(second.due)
            self.assertEqual(first.deadline,116)
            second.finish()
            first.contacted=True
            first.finish()
            with mock.patch.object(fastpath,'now',return_value=115.8):
                third=fastpath.Lease(self.cfg,'claude',{'agent_id':'agent-1'})
                self.assertFalse(third.due)
                third.finish()
