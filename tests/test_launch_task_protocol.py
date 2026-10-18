"""Task commands retain the bounded launch's job and assigned session through its real broker."""
import json
import os
import subprocess
import sys
import unittest
import uuid

import test_launch_isolation as isolation
from swarm import cli, dependencies, enrolment, launchproto
from swarm.board import open_board, setup_board
from swarm.board.autoinit import store_key
from swarm.supervisor import launchbroker, launchhome


class TaskProtocol(unittest.TestCase):
    def test_report_uses_task_name_and_broker_actor(self):
        req = launchproto.parse_cli(['task', 'report', '--name', 'qa', '--generation', '1',
            '--artifact', 'main@abc', '--outcome', 'passed', '--reason', 'exact source checked',
            '--as', 'forged'], '.')
        command, fields = launchproto.validate(req, 'J')
        argv = launchproto.build_argv(command, fields, key='sid', name='Lisa', file_path=str)
        self.assertEqual(argv, ['task', 'report', '--job=J', '--name=qa', '--generation=1',
            '--artifact=main@abc', '--outcome=passed', '--reason=exact source checked', '--as=Lisa'])

    def test_registration_alias_dependencies_and_inline_brief_are_typed(self):
        req = launchproto.parse_cli(['task', 'add', '--job', 'J', '--name', 'judge',
            '--generation', '2', '--artifact', 'main@abc', '--depends', 'qa', '--depends', 'review',
            '--role', 'judge', '--host', 'codex', '--model', 'gpt-6-astra', '--workdir', '/work/tree',
            '--brief', 'Verify exact source\nPreserve evidence', '--reporter', 'External QA'], '.')
        command, fields = launchproto.validate(req, 'J')
        self.assertEqual(command, 'task add')
        self.assertEqual(fields['depends'], ['qa', 'review'])
        self.assertEqual(fields['harness'], 'codex')
        self.assertEqual(fields['brief'], 'Verify exact source\nPreserve evidence')
        argv = launchproto.build_argv(command, fields, key='sid', name='Lisa', file_path=str)
        self.assertIn('--depends=qa', argv)
        self.assertIn('--depends=review', argv)
        self.assertIn('--reporter=External QA', argv)
        self.assertIn('--as=Lisa', argv)

    def test_wire_refuses_cross_job_identity_unknown_action_and_fields(self):
        fields = dict(job='J', name='qa', artifact='main@abc', outcome='passed', reason='checked')
        for extra in ({'actor': 'forged'}, {'agent_key': 'forged'}, {'reporter': 'forged'}, {'argv': []}):
            with self.subTest(extra=extra), self.assertRaises(launchproto.ProtocolError):
                launchproto.validate(dict(v=2, kind='cli', command='task report', fields={**fields, **extra}), 'J')
        with self.assertRaises(launchproto.ProtocolError):
            launchproto.validate(dict(v=2, kind='cli', command='task report', fields=fields), 'OTHER')
        with self.assertRaises(launchproto.ProtocolError):
            launchproto.parse_cli(['task', 'cancel', '--job', 'J'], '.')


@unittest.skipUnless(isolation.CAN_JAIL, 'needs Linux user namespaces')
class JailedTaskBroker(isolation.LaunchHomeCase):
    def test_add_report_list_through_jail_and_real_cli_preserve_session(self):
        board_dir = self.tmp / 'board'
        board_dir.mkdir()
        config = board_dir / 'config.toml'
        config.write_text(f'[board]\nbackend="sqlite"\nspool_dir="{board_dir}/spool"\n'
            f'[sqlite]\npath="{board_dir}/board.sqlite3"\n[hook]\nmarker_dir="{board_dir}/markers"\n'
            f'[supervise]\nallowed_workdirs=["{self.work}"]\n')
        cfg = cli.load_config(config)
        setup_board(cfg, {'qa': ['Lisa', 'Other']})
        sid = str(uuid.uuid4())
        with open_board(cfg) as board:
            board.open_job('J', None, None, None, 'owner', goal='Full project')
            name = board.allocate_name(sid, 'J', 'qa')
            other = board.allocate_name('other-sid', 'J', 'qa')
            board.record_route(sid, sid, 'final', 'J')
        wt = self.worktree()
        enrolment.write(store_key(cfg), job='J', agent_key=sid, harness='codex', session_id=sid, cwd=wt)
        script = '''import json,subprocess,sys
job,name,other,wd=sys.argv[1:]
def sw(*a):
 r=subprocess.run(['swarm','task',*a],capture_output=True,text=True)
 return dict(rc=r.returncode,out=r.stdout,err=r.stderr)
artifact='main@'+'a'*40
out={}
out['add']=sw('add','--job',job,'--name','qa','--generation','1','--artifact',artifact,'--role','qa','--harness','codex','--model','gpt-6-astra','--workdir',wd,'--brief','Verify exact source','--reporter',name,'--as',other)
out['report']=sw('report','--job',job,'--name','qa','--generation','1','--artifact',artifact,'--outcome','passed','--reason','Verified exact source','--as',other)
out['cross_job']=sw('report','--job','OTHER','--name','qa','--artifact',artifact,'--outcome','passed','--reason','forged')
out['list']=sw('list','--job',job,'--json')
print(json.dumps(out))
'''
        (self.work / 'wt' / 'task-probe.py').write_text(script)
        argv = launchhome.wrap(self.cfg, [sys.executable, '-I', 'task-probe.py', 'J', name, other, wt],
                               job='J', launch='task-wire', workdir=wt)
        sh = launchhome.home_dir('J', 'task-wire')
        env = {**os.environ, 'SWARM_AUTO_INIT': '1'}
        files = self.tmp / 'files'
        files.mkdir(mode=0o700)
        # Only service startup is replaced: the jailed client, typed protocol,
        # broker actor/session environment, CLI, enrollment and SQLite are real.
        def execute(cmd, **kwargs):
            cmd = list(cmd)
            cmd[3] = cmd[3].replace('rc = cli.main(argv)',
                'from unittest.mock import patch\nwith patch("swarm.dependencies.wake", return_value=None):\n    rc = cli.main(argv)')
            return subprocess.run(cmd, **kwargs)
        run = dict(job='J', harness='codex', agent_key=sid, name=name, cwd=wt)
        broker = launchbroker.LaunchBroker(run, str(sh / 'broker'), workdir=wt, forbid=[wt],
            launch_dir=str(sh), config=str(config), host_pid=None, base_env=env,
            file_dir=str(files), runner=execute).start()
        try:
            result = subprocess.run(argv, capture_output=True, text=True, env=env, timeout=60)
        finally:
            broker.stop()
        self.assertEqual(result.returncode, 0, result.stderr)
        out = json.loads(result.stdout)
        for action in ('add', 'report', 'list'):
            self.assertEqual(out[action]['rc'], 0, out[action])
        self.assertEqual(out['cross_job']['rc'], 2, out['cross_job'])
        self.assertEqual(json.loads(out['list']['out'])[0]['name'], 'qa')
        with open_board(cfg) as board:
            row = board.get_launch('J', 1, 'qa')
            self.assertEqual((row.model, row.harness), ('gpt-6-astra', 'codex'))
            evidence = dependencies.evidence(board, row)
            self.assertEqual((evidence['actor'], evidence['outcome']), (name, 'passed'))
            self.assertIsNone(enrolment.find_job_owner(store_key(cfg), 'J'))
