"""Question plugin contracts on every board; deadlines use a fixed clock."""
import json
import datetime as dt
import importlib.util
import os
import unittest
from pathlib import Path
from unittest import mock
from support import ROOT, MemoryHarness, FileHarness, SqliteHarness, PostgresHarness
from swarm import cli, plugins
NOW = dt.datetime(2026, 10, 5, 12, tzinfo=dt.timezone.utc)
PLUGIN = ROOT / 'skills' / 'swarm-ask' / 'swarm_plugin.py'
def load_plugin(test):
    test.assertTrue(PLUGIN.is_file(), 'ask/answer must ship as a CLI plugin')
    spec = importlib.util.spec_from_file_location('question_test_plugin', PLUGIN)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
class QuestionContract:
    def setUp(self):
        self.p = load_plugin(self)
        self.h = self.harness_factory()
        self.h.reset()
        self.addCleanup(self.h.close)
        self.b = self.h.board()
        self.addCleanup(self.b.close)
        self.b.open_job('j', None, None, None, None, goal='ship')
        self.b.allocate_name('asker', 'j', role='engineer')
        self.b.allocate_name('lead', 'j', role='engineering_lead')
        self.asker = self.b.active_agent_name('asker')
        self.lead = self.b.active_agent_name('lead')
        self.reg = plugins.Registry(self.h.cfg, Path('/work/config.toml'), core_commands=('doctor', 'blocker'))
        self.p.register(plugins.PluginAPI(self.reg, 'swarm-ask', self.reg.config_path.parent))
        self.b.plugin_registry = self.reg
        self.ctx = self.reg.context('swarm-ask', self.b)
    def ask(self, to='human', text='Choose a colour?', **kw):
        return self.p.open_question(self.ctx, self.b, 'j', self.asker, to, text, **kw)
    def test_full_payload_and_pointer_with_several_blockers(self):
        text = 'Question with substantial context. ' * 180
        q = self.ask(text=text, options=['blue', 'red'], default='blue', blocks='rendering')
        self.ask('@EL')
        self.assertEqual(self.p.question_data(self.ctx, self.b, q)['text'], text)
        self.assertEqual(self.p.question_data(self.ctx, self.b, q)['options'], ['blue', 'red'])
        msg = self.b.recent_messages(5, 'j')[0].message
        self.assertIn(f'swarm answer {q.id}', msg)
        self.assertLessEqual(len(msg), self.b.message_cap())
        self.assertEqual(len(self.b.blockers('j')), 2)
    def test_addressee_and_role_holder_can_change(self):
        q = self.ask('@EL')
        with self.assertRaises(ValueError):
            self.p.answer_question(self.ctx, self.b, q.id, self.asker, text='wrong')
        self.p.answer_question(self.ctx, self.b, q.id, self.asker, comment='a suggestion')
        self.assertEqual(self.b.blocker(q.id).state, 'open')
        self.b.set_agent_role('lead', 'engineer')
        self.b.set_agent_role('asker', 'engineering_lead')
        with self.assertRaises(ValueError):
            self.p.answer_question(self.ctx, self.b, q.id, self.lead, text='old holder')
        self.p.answer_question(self.ctx, self.b, q.id, self.asker, text='blue')
        self.assertEqual(self.b.blocker(q.id).resolved_by, self.asker)
    def test_agents_cannot_answer_human_and_human_can_answer_any(self):
        q = self.ask()
        with self.assertRaises(ValueError):
            self.p.answer_question(self.ctx, self.b, q.id, self.lead, text='blue')
        self.p.answer_question(self.ctx, self.b, q.id, 'human', text='blue')
        self.assertEqual(self.b.blocker(q.id).resolved_by, 'human')
    def test_option_default_and_reopen_replace_answer_and_notify_asker(self):
        q = self.ask(options=['blue', 'red'], default='blue')
        with self.assertRaises(ValueError):
            self.p.answer_question(self.ctx, self.b, q.id, 'human', option='green')
        self.p.answer_question(self.ctx, self.b, q.id, 'human', use_default=True)
        self.p.answer_question(self.ctx, self.b, q.id, 'human', reopen='red', comment='correction')
        self.assertEqual(self.b.blocker(q.id).resolved_how, 'red')
        self.assertIn('reopened', [e.event for e in self.b.blocker_events(q.id)])
        messages = self.b.read_new(agent_key='asker')
        self.assertTrue(any(m.to_agent == self.asker and 'red' in m.message for m in messages))
    def test_expired_default_and_overdue_notification(self):
        events = []
        with mock.patch.object(self.p, 'notify', side_effect=lambda ctx, b, event: events.append((b.id,event))):
            with mock.patch.object(self.b, 'now', return_value=NOW):
                q = self.ask(default='blue', expires='1h')
                overdue = self.ask(expires='1h')
            with mock.patch.object(self.b, 'now', return_value=NOW + dt.timedelta(hours=2)):
                self.b.sweep_expiry(0, 0)
                self.b.sweep_expiry(0, 0)
            self.assertEqual(self.b.blocker(q.id).state, 'expired')
            self.assertEqual(self.p.question_data(self.ctx, self.b, q)['answer'], 'blue')
            self.assertEqual(events.count((q.id, 'expired')), 1)
            self.assertEqual(events.count((overdue.id, 'overdue')), 1)
            self.assertTrue(any('blue' in m.message for m in self.b.read_new(agent_key='asker')))
    def test_pointer_survives_small_board_cap_and_long_role(self):
        role = 'role_' + 'a' * 40
        self.b.set_agent_role('lead', role)
        self.b.set_message_cap(50)
        q = self.ask('@' + role)
        self.assertIn(f'swarm answer {q.id}', self.b.recent_messages(1, 'j')[0].message)

    def test_to_me_means_addressee_not_human_override_permission(self):
        q = self.ask()
        self.ask('@EL')
        self.assertEqual([b.id for b in self.p.question_rows(self.ctx, self.b, 'j', to='human')], [q.id])

    def test_orchestrator_and_watch_recorded_snapshot(self):
        self.ask(default='blue', expires='1h')
        self.ask('@EL')
        self.assertIn('1 open question', '\n'.join(self.reg.orchestrator_lines('j', self.b)))
        with mock.patch.object(self.b, 'now', return_value=NOW), \
                mock.patch.object(cli.time, 'monotonic', return_value=0):
            rec = cli._Recorder(self.b)
            pane = self.reg.watch_panes('j', rec)
            self.assertEqual(pane, self.reg.watch_panes('j', cli._Replay(rec)))
        self.assertIn('QUESTIONS', '\n'.join(pane))
        self.assertIn('human', '\n'.join(pane))
class MemoryQuestions(QuestionContract, unittest.TestCase):
    harness_factory = MemoryHarness
class FileQuestions(QuestionContract, unittest.TestCase):
    harness_factory = FileHarness
class SqliteQuestions(QuestionContract, unittest.TestCase):
    harness_factory = SqliteHarness
@unittest.skipUnless(os.environ.get('SWARM_TEST_CONFIG'), 'throwaway Postgres required')
class PostgresQuestions(QuestionContract, unittest.TestCase):
    harness_factory = staticmethod(lambda: PostgresHarness(os.environ['SWARM_TEST_CONFIG']))

class AgentInjectionApi(unittest.TestCase):
    def test_full_answer_hook_api_is_available(self):
        self.assertTrue(hasattr(plugins.PluginAPI, 'add_agent_lines'),
                        'plugin answers need an agent context hook for full text')

from test_routing import RoutingEnv
import importlib.util as _imports
import io
import subprocess
import tempfile

class QuestionCli(RoutingEnv):
    plugins_disabled = ('engineering-team',)
    def test_commands_filter_and_full_answer_injected_before_next_tool(self):
        self.activate('J')
        self.spawn('asker', '[swarm job: J]\n[swarm role: engineer]\nBuild.')
        asker = self.member('asker')
        with self.board() as b:
            b.record_route('asker', 'asker-session', 'final')
        with mock.patch.dict(os.environ, {'CLAUDE_CODE_SESSION_ID':'asker-session'}):
            rc, out, err = self.cli('ask', '--job','J','--to','human','Question?',
                                    '--options','blue,red','--default','blue','--blocks','UI')
        self.assertEqual((rc,err), (0,''))
        id = int(out.strip().removeprefix('Q'))
        with mock.patch.dict(os.environ, {'CLAUDE_CODE_SESSION_ID':'asker-session'}):
            self.assertEqual(self.cli('answer', str(id), 'blue')[0], 1)
            self.assertEqual(self.cli('blocker','resolve',str(id),'--how','blue')[0], 1)
        answer = 'Detailed decision. ' * 180
        self.assertEqual(self.cli('answer',str(id),answer)[0],0)
        hook = self.turn('asker')
        context = hook['hookSpecificOutput']['additionalContext']
        self.assertIn(answer,context)
        hook2 = self.turn('asker')
        self.assertNotIn(answer,(hook2 or {}).get('hookSpecificOutput',{}).get('additionalContext',''))
        self.assertIn(answer,self.cli('questions','--job','J','--all')[1])
        self.assertNotIn(f'Q{id}',self.cli('questions','--job','J','--open')[1])
        self.assertEqual(self.cli('answer',str(id),'--reopen','red')[0],0)
        self.assertIn('red',self.turn('asker')['hookSpecificOutput']['additionalContext'])

class NotificationTests(unittest.TestCase):
    def setUp(self):
        self.p = load_plugin(self)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        self.ctx = plugins.Registry({'notify':{'on_question':'notify {event} {job} {id} {to} {summary}'}},
                                    self.path / 'config.toml').context('swarm-ask')
        from swarm.board.base import Blocker
        self.b = Blocker(12,'j','question','human','$(touch forbidden); hello',None,None,'open',
                         'Alice',NOW,None,None,None)
    def test_template_values_are_single_arguments_and_env_no_shell(self):
        with mock.patch.object(self.p.subprocess,'Popen') as launch:
            self.p.notify(self.ctx,self.b,'opened')
        args,kw = launch.call_args
        self.assertNotIn('shell',kw)
        self.assertEqual(json.loads(kw['env']['SWARM_QUESTION_NOTIFY_PAYLOAD'])['argv'][-1],self.b.reason)
        self.assertEqual(kw['env']['SWARM_TO'],'human')
        self.assertEqual(kw['env']['SWARM_EVENT'],'opened')
        self.assertEqual(kw['stdin'],subprocess.DEVNULL)
        self.assertTrue(kw.get('start_new_session') or kw.get('creationflags'))
    def test_windows_quoted_executable_keeps_spaces_and_literal_backslashes(self):
        command = '\"C:\\Program Files\\Notify\\notify.exe\" {summary}'
        values = {'summary': 'a; $(literal)'}
        with mock.patch.object(self.p, 'os', __import__('types').SimpleNamespace(name='nt')):
            self.assertEqual(self.p.notification_argv(command, values),
                             ['C:\\Program Files\\Notify\\notify.exe', 'a; $(literal)'])

    def test_launch_failure_is_isolated_and_doctor_shows_failure(self):
        with mock.patch.object(self.p.subprocess,'Popen',side_effect=OSError('private details')):
            self.p.notify(self.ctx,self.b,'opened')
        out=io.StringIO()
        with __import__('contextlib').redirect_stdout(out):
            self.p.doctor(self.ctx,None)
        self.assertIn('notification launch failed',out.getvalue())
        self.assertNotIn('private details',out.getvalue())
    def test_worker_timeout_and_nonzero_are_logged_without_command_output(self):
        spec=_imports.spec_from_file_location('test_notify_worker',PLUGIN.with_name('notify_runner.py'))
        worker=_imports.module_from_spec(spec);spec.loader.exec_module(worker)
        env={'SWARM_ID':'12','SWARM_EVENT':'opened'}
        log=self.path/'errors.log'
        for problem in (subprocess.TimeoutExpired('private-command',5), OSError('private-error')):
            with mock.patch.object(worker.subprocess,'run',side_effect=problem) as run:
                worker.run({'argv':['notify','literal ; $(unsafe)'],'timeout':5},env,log)
                self.assertEqual(run.call_args.kwargs['timeout'],5)
                self.assertFalse(run.call_args.kwargs['shell'])
        with mock.patch.object(worker.subprocess,'run',return_value=__import__('types').SimpleNamespace(returncode=9)):
            worker.run({'argv':['notify'],'timeout':5},env,log)
        text=log.read_text()
        self.assertIn('TimeoutExpired',text)
        self.assertIn('exit 9',text)
        self.assertNotIn('private-',text)
