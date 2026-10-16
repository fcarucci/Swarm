"""swarm answer works for the session that owns the job, with no join and no board identity."""
import os
import unittest
from unittest import mock
from test_routing import RoutingEnv
from test_ask_answer import QuestionCli


class AnswerNoJoin(RoutingEnv):
    plugins_disabled = QuestionCli.plugins_disabled
    def env(self, session):
        return mock.patch.dict(os.environ, {'CLAUDE_CODE_SESSION_ID': session,
                                           'CLAUDE_CODE_CHILD_SESSION': '1'}, clear=True)

    def test_owner_answers_as_orchestrator_without_join(self):
        self.activate('J', session='root-session')
        with self.board() as b:
            q = b.open_blocker('J', 'question', 'human', 'Choose?')
        with self.env('root-session'):
            rc, out, err = self.cli('answer', str(q.id), 'blue')
        self.assertEqual((rc, err), (0, ''), out)
        with self.board() as b:
            self.assertEqual(b.blocker(q.id).state, 'resolved')
            self.assertEqual(b.blocker(q.id).resolved_by, 'orchestrator')

    def test_owner_answers_under_previous_board_name(self):
        self.activate('J', session='root-session')
        with self.board() as b:
            b.allocate_name('lead-key', 'J', role='orchestrator')
            name = b.active_agent_name('lead-key')
            b.record_route('lead-key', 'root-session', 'final')
            b.leave('lead-key') if hasattr(b, 'leave') else None
            q = b.open_blocker('J', 'question', '@orchestrator', 'Choose?')
        with self.env('root-session'):
            rc, _, err = self.cli('answer', str(q.id), 'blue')
        self.assertEqual((rc, err), (0, ''))
        with self.board() as b:
            self.assertEqual(b.blocker(q.id).resolved_by, name)

    def test_other_session_still_refused(self):
        self.activate('J', session='root-session')
        with self.board() as b:
            q = b.open_blocker('J', 'question', 'human', 'Choose?')
        with self.env('stranger'):
            rc, _, err = self.cli('answer', str(q.id), 'blue')
        self.assertEqual(rc, 1)
        self.assertIn('no active board identity', err)


if __name__ == '__main__':
    unittest.main()
