"""CLI bank lifecycle coverage against the isolated fake Hindsight API."""
from unittest import mock

from test_hindsight import HindsightEnv


class LearnBanksTests(HindsightEnv):
    def setUp(self):
        super().setUp()
        self.enable()
        self.fake.banks['coding'] = []
        self.fake.banks['hermes'] = []

    def learn(self, *args, facts='A self-contained fact.\n'):
        return self.cli('learn', '--job', 'J', *args, '-', stdin=facts)

    def test_activation_uses_general_banks_and_resets_previous_project(self):
        self.cli('activate', '--job', 'J', '--project', 'old-bank')
        self.cli('activate', '--job', 'J')
        with self.board() as board:
            self.assertFalse(board.job_status('J').project)
        self.assertEqual(self.fake.calls('PUT'), [])

    def test_remember_missing_bank_requires_explicit_create(self):
        self.cli('activate', '--job', 'J', '--project', 'missing')
        self.cli('join', '--job', 'J', '--key', 'k')
        name = self.agent('k').name
        rc, _, err = self.cli('remember', '--job', 'J', '--as', name, 'A fact')
        self.assertEqual(rc, 1, err)
        self.assertIn('--create-bank', err)
        self.assertNotIn('missing', self.fake.banks)
        rc, _, err = self.cli('remember', '--job', 'J', '--as', name, '--create-bank', 'A fact')
        self.assertEqual(rc, 0, err)
        self.assertIn('missing', self.fake.banks)

    def test_learn_default_existing_bank_and_provenance(self):
        self.cli('activate', '--job', 'J', '--project', 'job-bank')
        rc, out, err = self.learn(facts='Fact one.\n\nFact two.\n')
        self.assertEqual(rc, 0, err)
        self.assertEqual(len(self.fake.banks['coding']), 2)
        self.assertIn('[memory ', out)
        self.assertFalse(self.fake.calls('POST', '/memories')[0]['body']['async'])
        with self.board() as board:
            refs = board.memory_refs(job='J')
        self.assertEqual(len(refs), 2)
        self.assertEqual({r.writer for r in refs}, {'swarm-learn'})
        self.assertEqual({r.bank for r in refs}, {'coding'})
        for fact in self.fake.banks['coding']:
            self.assertEqual(fact['metadata']['job'], 'J')
            self.assertEqual(fact['metadata']['learning'], 'true')

    def test_learn_lists_existing_banks_without_job(self):
        rc, out, err = self.cli('learn', '--list-banks')
        self.assertEqual(rc, 0, err)
        self.assertEqual(set(out.splitlines()), {'coding', 'hermes'})
        self.assertEqual(self.fake.calls('PUT'), [])

    def test_learn_timeout_allows_extraction_without_changing_normal_memory_calls(self):
        from swarm import hindsight
        self.cli('activate', '--job', 'J')
        self.cli('join', '--job', 'J', '--key', 'k')
        name = self.agent('k').name
        for configured, expected in ((1, 120), (180, 180)):
            self.enable(timeout_seconds=configured)
            with mock.patch.object(hindsight, 'remember', wraps=hindsight.remember) as retain:
                self.assertEqual(self.learn()[0], 0)
                learn_call = retain.call_args
                self.assertEqual(learn_call.args[1]['hindsight']['timeout_seconds'], expected)
                self.assertTrue(learn_call.kwargs['synchronous'])
                self.assertEqual(self.cli('remember', '--job', 'J', '--as', name, 'Normal fact')[0], 0)
                normal_call = retain.call_args
                self.assertEqual(normal_call.args[1]['hindsight']['timeout_seconds'], configured)
                self.assertNotIn('synchronous', normal_call.kwargs)
            self.assertEqual(self.cfg['hindsight']['timeout_seconds'], configured)

    def test_learn_missing_bank_explicit_flag_and_configured_default(self):
        self.enable(default_bank='hermes')
        self.cli('activate', '--job', 'J')
        rc, _, err = self.learn()
        self.assertEqual(rc, 0, err)
        self.assertEqual(len(self.fake.banks['hermes']), 1)
        rc, _, err = self.learn('--bank', 'new-bank')
        self.assertEqual(rc, 1, err)
        self.assertIn('--create-bank', err)
        rc, _, err = self.learn('--bank', 'new-bank', '--create-bank')
        self.assertEqual(rc, 0, err)
        self.assertEqual(len(self.fake.banks['new-bank']), 1)

    def test_learn_refuses_overlong_facts_before_any_write(self):
        self.enable(remember_max_chars=15)
        self.cli('activate', '--job', 'J')
        rc, _, err = self.learn(facts='Short fact.\n' + 'X' * 16)
        self.assertEqual(rc, 1, err)
        self.assertEqual(self.fake.calls('POST', '/memories'), [])

    def test_delete_bank_refuses_until_learnings_elsewhere_and_survives_close(self):
        self.fake.banks['job-bank'] = []
        self.cli('activate', '--job', 'J', '--project', 'job-bank')
        rc, _, err = self.cli('deactivate', '--job', 'J', '--delete-bank')
        self.assertEqual(rc, 1, err)
        self.assertIn('retain learnings elsewhere first', err)
        self.assertTrue((self.markers / 'J.json').exists())
        self.assertEqual(self.learn()[0], 0)
        self.cli('deactivate', '--job', 'J')
        self.assertFalse((self.markers / 'J.json').exists())
        rc, out, err = self.cli('deactivate', '--job', 'J', '--delete-bank')
        self.assertEqual(rc, 0, err)
        self.assertIn('deleted explicit project bank', out)
        self.assertNotIn('job-bank', self.fake.banks)
        self.assertIn('coding', self.fake.banks)
        self.assertEqual(len(self.fake.calls('DELETE')), 1)

    def test_failed_or_partial_learn_cannot_enable_deletion(self):
        self.fake.banks['job-bank'] = []
        self.cli('activate', '--job', 'J', '--project', 'job-bank')
        self.fake.fail(422, 'refused', bank='coding', content='second', method='POST')
        rc, _, err = self.learn(facts='first fact\nsecond fact\n')
        self.assertEqual(rc, 1, err)
        with self.board() as board:
            self.assertEqual(board.memory_refs(job='J'), [])
        self.assertEqual(self.cli('deactivate', '--job', 'J', '--delete-bank')[0], 1)
        self.assertEqual(self.fake.calls('DELETE'), [])

    def test_same_bank_or_removed_export_cannot_enable_deletion(self):
        self.fake.banks['job-bank'] = []
        self.cli('activate', '--job', 'J', '--project', 'job-bank')
        self.assertEqual(self.learn('--bank', 'job-bank')[0], 0)
        self.assertEqual(self.cli('deactivate', '--job', 'J', '--delete-bank')[0], 1)
        self.assertEqual(self.learn()[0], 0)
        self.fake.documents['coding'].clear()
        self.assertEqual(self.cli('deactivate', '--job', 'J', '--delete-bank')[0], 1)
        self.assertEqual(self.fake.calls('DELETE'), [])

    def test_reopening_requires_fresh_export_and_general_bank_is_protected(self):
        self.fake.banks['job-bank'] = []
        self.cli('activate', '--job', 'J', '--project', 'job-bank')
        self.assertEqual(self.learn()[0], 0)
        self.cli('deactivate', '--job', 'J')
        self.cli('activate', '--job', 'J', '--project', 'job-bank')
        self.assertEqual(self.cli('deactivate', '--job', 'J', '--delete-bank')[0], 1)
        self.cli('activate', '--job', 'J', '--project', 'coding')
        self.assertEqual(self.learn('--bank', 'hermes')[0], 0)
        rc, _, err = self.cli('deactivate', '--job', 'J', '--delete-bank')
        self.assertEqual(rc, 1, err)
        self.assertIn('general memory bank', err)

    def test_no_explicit_project_bank_cannot_be_deleted(self):
        self.cli('activate', '--job', 'J')
        self.assertEqual(self.learn()[0], 0)
        rc, _, err = self.cli('deactivate', '--job', 'J', '--delete-bank')
        self.assertEqual(rc, 1, err)
        self.assertIn('explicit --project', err)

    def test_default_bank_is_protected_when_not_a_recall_bank(self):
        self.enable(default_bank='coding', recall_banks=['hermes'])
        self.cli('activate', '--job', 'J', '--project', 'coding')
        self.assertEqual(self.learn('--bank', 'hermes')[0], 0)
        rc, _, err = self.cli('deactivate', '--job', 'J', '--delete-bank')
        self.assertEqual(rc, 1, err)
        self.assertIn('general memory bank', err)
        self.assertEqual(self.fake.calls('DELETE'), [])

    def test_close_and_met_verdict_require_learnings(self):
        self.cli('activate', '--job', 'J', '--goal', 'done')
        self.cli('join', '--job', 'J', '--key', 'judge', '--judge')
        name = self.agent('judge').name
        rc, out, err = self.cli('verdict', '--job', 'J', '--as', name, 'met', 'Done')
        self.assertEqual(rc, 0, err)
        self.assertIn('Required learnings step', out)
        rc, out, err = self.cli('deactivate', '--job', 'J')
        self.assertEqual(rc, 0, err)
        self.assertIn('best-matching EXISTING bank', out)

    def test_cli_recall_general_and_project_with_bank_error(self):
        self.fake.add_memory('coding', 'General fact')
        self.fake.add_memory('job-bank', 'Project fact')
        self.fake.fail(500, 'broken', bank='hermes', suffix='/memories/recall')
        self.cli('activate', '--job', 'J', '--project', 'job-bank', '--task', 'Job task')
        rc, out, err = self.cli('recall', '--job', 'J')
        self.assertEqual(rc, 0, err)
        self.assertIn('General fact', out)
        self.assertIn('Project fact', out)
        self.assertIn('hermes', err)
        self.assertEqual({r['body']['query'] for r in self.fake.calls('POST', '/memories/recall')}, {'Job task'})

    def test_queued_met_verdict_still_instructs_learning(self):
        self.cli('activate', '--job', 'J', '--goal', 'done')
        self.cli('join', '--job', 'J', '--key', 'judge', '--judge')
        name = self.agent('judge').name
        self.h.set_available(False)
        rc, out, err = self.cli('verdict', '--job', 'J', '--as', name, 'met', 'Done')
        self.assertEqual(rc, 0, err)
        self.assertIn('queued', out)
        self.assertIn('Required learnings step', out)
