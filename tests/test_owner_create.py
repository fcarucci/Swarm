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

    def test_retry_respects_explicit_routing_stop_on_still_active_job(self):
        owner_create.attest(self.cfg,self.payload,self.host);self.assertEqual(self.cli(*self.argv)[0],0)
        from swarm import enrolment
        enrolment.disable_job_routing(store_key(self.cfg),'new-job')
        cli.remove_marker(cli._marker(self.cfg,'new-job'))
        owner_create.attest(self.cfg,self.payload,self.host);self.assertEqual(self.cli(*self.argv)[0],0)
        self.assertFalse(cli._marker(self.cfg,'new-job').exists())
        self.assertTrue(enrolment.job_routing_disabled(enrolment.find_job(store_key(self.cfg),'new-job')))

    def test_retry_preserves_reassigned_board_and_private_proof(self):
        owner_create.attest(self.cfg,self.payload,self.host);self.assertEqual(self.cli(*self.argv)[0],0)
        from swarm import enrolment
        bk=store_key(self.cfg)
        with self.board() as b:b.reassign_job_session('new-job','new-owner',expected_session=self.sid)
        enrolment.reassign_coordinator_records(enrolment.coordinator_records(bk,'new-job'),'new-owner')
        data={'job':'new-job','session_id':'new-owner'};cli._write_marker(cli._marker(self.cfg,'new-job'),data)
        owner_create.attest(self.cfg,self.payload,self.host);self.assertEqual(self.cli(*self.argv)[0],0)
        self.assertEqual(enrolment.find_job(bk,'new-job').session_id,'new-owner')
        self.assertEqual(cli._read_marker(cli._marker(self.cfg,'new-job')),data)

    def test_display_session_is_distinct_from_hook_authority(self):
        self.argv+=['--display-session','pane-session'];self.payload['tool_input']['command']=self.command()
        owner_create.attest(self.cfg,self.payload,self.host);self.assertEqual(self.cli(*self.argv)[0],0)
        with self.board() as b:self.assertEqual(b.job_status('new-job').session_id,'pane-session')
        from swarm import enrolment
        self.assertEqual(enrolment.find_job(store_key(self.cfg),'new-job').session_id,self.sid)

    def test_grant_is_single_use(self):
        owner_create.attest(self.cfg,self.payload,self.host)
        self.assertEqual(self.cli(*self.argv)[0],0)
        self.assertNotEqual(self.cli(*self.argv)[0],0)

    def test_root_provenance_rejects_assistant_and_inherited_user_items(self):
        self.audit.stop()
        from swarm.hosts import codex
        path=self.tmp/'rollout.jsonl'
        def write(source='cli',role='user',sid=None):
            path.write_text('\n'.join(json.dumps(x) for x in [
                {'type':'session_meta','payload':{'id':sid or self.sid,'source':source}},
                {'type':'response_item','payload':{'type':'message','id':'msg-human','role':role,
                 'content':[{'type':'input_text','text':'Start a new swarm for the port.'}]}}])+'\n')
        with mock.patch.object(codex,'find_rollout',return_value=path):
            write();self.assertEqual(owner_create.root_request(self.cfg,self.payload,self.host,'msg-human')['session'],self.sid)
            for values in [dict(role='assistant'),dict(source={'subagent':{'thread_spawn':{'depth':1}}}),dict(sid='other')]:
                write(**values)
                with self.assertRaises(ValueError):owner_create.root_request(self.cfg,self.payload,self.host,'msg-human')

    def test_root_hook_grants_but_worker_hook_denies(self):
        got=self.hook('turn',agent_id=None,session=self.sid,host='codex',tool_name='Bash',
            tool_use_id='actual-tool',tool_input={'command':self.command()})
        self.assertNotEqual((got or {}).get('hookSpecificOutput',{}).get('permissionDecision'),'deny')
        self.assertEqual(self.cli(*self.argv)[0],0)
        got=self.hook('turn',agent_id='child',session=self.sid,host='codex',tool_name='Bash',
            tool_use_id='child-tool',tool_input={'command':self.command()})
        self.assertEqual(got['hookSpecificOutput']['permissionDecision'],'deny')

    def test_incomplete_creation_never_bootstraps_reopened_run(self):
        owner_create.attest(self.cfg,self.payload,self.host)
        with mock.patch.object(owner_create,'_bootstrap'):
            self.assertEqual(self.cli(*self.argv)[0],0)
        with self.board() as b:
            b.close_job('new-job','completed','done',closed_by='owner')
            b.open_job('new-job',None,None,self.sid,'owner',goal='later run')
        owner_create.attest(self.cfg,self.payload,self.host)
        self.assertEqual(self.cli(*self.argv)[0],0)
        self.assertFalse(cli._marker(self.cfg,'new-job').exists())

    def test_remote_reopen_during_bootstrap_removes_new_marker(self):
        owner_create.attest(self.cfg,self.payload,self.host)
        original=cli._write_marker
        def race(path,data):
            original(path,data)
            with self.board() as b:
                b.close_job('new-job','completed','done',closed_by='owner')
                b.open_job('new-job',None,None,self.sid,'owner',goal='later run')
        with mock.patch.object(cli,'_write_marker',side_effect=race):
            self.assertEqual(self.cli(*self.argv)[0],0)
        self.assertFalse(cli._marker(self.cfg,'new-job').exists())

    def test_forged_board_receipt_cannot_mint_private_creation_authority(self):
        import hashlib
        audit={'session':self.sid,'request_id':'msg-human','text_sha':'a'*64}
        args=owner_create._command(self.payload,self.host)
        fields=owner_create._fields(args)
        request=owner_create._digest([store_key(self.cfg),self.sid,'msg-human'])
        with self.board() as b:
            b.open_job('new-job',None,None,self.sid,'other',goal='existing')
            now=b.job_status('new-job').created_at
            fake={'job':'new-job','session_id':self.sid,'request_id':request,
                  'request_hash':owner_create._digest([fields,audit]),'created_at':now.isoformat()}
            b._write_meta('create-job-once:'+hashlib.sha256(request.encode()).hexdigest(),json.dumps(fake))
        owner_create.attest(self.cfg,self.payload,self.host)
        self.assertNotEqual(self.cli(*self.argv)[0],0)
        from swarm import enrolment
        self.assertIsNone(enrolment.find_job_owner(store_key(self.cfg),'new-job'))

    def test_interrupted_bootstrap_retry_reuses_private_intent(self):
        owner_create.attest(self.cfg,self.payload,self.host)
        with mock.patch.object(owner_create,'_bootstrap',side_effect=OSError('interrupted')):
            self.assertNotEqual(self.cli(*self.argv)[0],0)
        owner_create.attest(self.cfg,self.payload,self.host)
        self.assertEqual(self.cli(*self.argv)[0],0)
        self.assertTrue(cli._marker(self.cfg,'new-job').exists())

    def test_reopen_before_private_write_cannot_recover_stale_authority(self):
        from swarm import enrolment,passcmd
        owner_create.attest(self.cfg,self.payload,self.host)
        original=enrolment.write_job
        def race(*args,**kwargs):
            with self.board() as b:
                b.close_job('new-job','completed','done',closed_by='owner')
                b.open_job('new-job',None,None,self.sid,'owner',goal='later run')
            return original(*args,**kwargs)
        with mock.patch.object(enrolment,'write_job',side_effect=race):
            self.assertEqual(self.cli(*self.argv)[0],0)
        with self.board() as b:self.assertFalse(passcmd.repair_open_routing(b,self.cfg,'new-job'))
        self.assertFalse(cli._marker(self.cfg,'new-job').exists())
