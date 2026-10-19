"""Creation is a root capability, never an existing-job transition or worker privilege."""
import json
import os
from unittest import mock
from test_hooks_cli import Env
from swarm import cli, owner_create, hosts
from swarm.board.autoinit import store_key


class OwnerCreate(Env):
    def setUp(self):
        super().setUp()
        self.sid='root-session'
        self.pid=mock.patch('swarm.person.host_pid',return_value=1234);self.pid.start();self.addCleanup(self.pid.stop)
        self.audit=mock.patch('swarm.owner_create.root_request',return_value={'session':self.sid,'request_id':'msg-human','text_sha':'a'*64});self.audit.start();self.addCleanup(self.audit.stop)
        self.argv=['create','--job','new-job','--goal','Port script with parity','--workdir',str(self.tmp),'--request-id','msg-human']
        self.payload={'session_id':self.sid,'tool_use_id':'tool-1','tool_name':'Bash','tool_input':{'command':self.command()}}
        self.host=hosts.get('codex')
        os.environ['CODEX_THREAD_ID']=self.sid;os.environ['CODEX_SESSION_ID']=self.sid

    def command(self):
        import shlex
        return shlex.join(['swarm','--config',str(self.config),*self.argv])

    def test_root_attestation_creates_once_and_identical_retry_does_not_reopen(self):
        owner_create.attest(self.cfg,self.payload,self.host)
        rc,out,err=self.cli(*self.argv);self.assertEqual(rc,0,err)
        with self.board() as b:
            first=b.job_status('new-job');self.assertEqual(first.goal,'Port script with parity')
            b.close_job('new-job','completed','done',closed_by='owner')
        owner_create.attest(self.cfg,self.payload,self.host)
        rc,out,err=self.cli(*self.argv);self.assertEqual(rc,0,err)
        with self.board() as b:self.assertEqual(b.job_status('new-job').status,'completed')

    def test_no_grant_refuses_despite_claimed_root_environment(self):
        rc,out,err=self.cli(*self.argv);self.assertNotEqual(rc,0)
        with self.board() as b:self.assertIsNone(b.job_status('new-job'))

    def test_children_cannot_get_grant_even_with_shared_root_session(self):
        with self.assertRaises(ValueError):owner_create.attest(self.cfg,{**self.payload,'agent_id':'worker'},self.host)
        rc,_,_=self.cli(*self.argv);self.assertNotEqual(rc,0)

    def test_modified_goal_cannot_consume_grant(self):
        owner_create.attest(self.cfg,self.payload,self.host)
        args=self.argv.copy();args[args.index('--goal')+1]='Different goal'
        self.assertNotEqual(self.cli(*args)[0],0)

    def test_expired_grant_refused(self):
        with mock.patch('swarm.owner_create.time.time',return_value=1):owner_create.attest(self.cfg,self.payload,self.host)
        self.assertNotEqual(self.cli(*self.argv)[0],0)

    def test_existing_job_cannot_be_mutated(self):
        with self.board() as b:b.open_job('new-job','original',None,'other','owner',goal='keep')
        owner_create.attest(self.cfg,self.payload,self.host)
        self.assertNotEqual(self.cli(*self.argv)[0],0)
        with self.board() as b:self.assertEqual(b.job_status('new-job').goal,'keep')

    def test_prior_root_job_binding_is_not_replaced(self):
        cli._write_marker(cli._marker(self.cfg,'old'),{'job':'old','session_id':self.sid})
        owner_create.attest(self.cfg,self.payload,self.host)
        rc,_,err=self.cli(*self.argv);self.assertEqual(rc,0,err)
        self.assertEqual(cli._read_marker(cli._marker(self.cfg,'old'))['job'],'old')
        self.assertEqual(cli._read_marker(cli._marker(self.cfg,'new-job'))['job'],'new-job')

    def test_lifecycle_guard_still_blocks_activate_and_job(self):
        self.assertNotEqual(self.cli('activate','--job','new-job')[0],0)
        self.assertNotEqual(self.cli('job','new-job')[0],0)
