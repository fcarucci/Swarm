"""General-bank memory routing against the fake Hindsight API."""
from test_hindsight import HindsightEnv
from swarm import hindsight


class GeneralBanksTests(HindsightEnv):
    def setUp(self):
        super().setUp()
        self.enable()

    def test_missing_bank_requires_explicit_creation(self):
        client = hindsight.Client(self.cfg)
        with self.assertRaisesRegex(hindsight.HindsightError, '--create-bank'):
            client.retain('missing', 'fact', [], {})
        self.assertEqual(self.fake.calls('PUT'), [])
        client.retain('missing', 'fact', [], {}, create_bank=True)
        self.assertEqual(client.recall('missing', 'q')[0]['text'], 'fact')

    def test_general_hook_recalls_both_banks_and_dedupes_facts(self):
        self.fake.add_memory('coding', 'shared fact')
        self.fake.add_memory('hermes', 'shared fact')
        self.fake.add_memory('hermes', 'hermes fact')
        self.cli('activate', '--job', 'J')
        ctx = self.start()
        self.assertEqual(ctx.count('- shared fact'), 1)
        self.assertIn('- hermes fact', ctx)
        self.assertEqual([r['path'] for r in self.fake.calls('POST', '/memories/recall')],
                         ['/v1/default/banks/coding/memories/recall', '/v1/default/banks/hermes/memories/recall'])

    def test_one_bank_error_does_not_hide_other_bank(self):
        self.fake.fail(500, 'corrupt', bank='coding')
        self.fake.add_memory('hermes', 'healthy fact')
        self.cli('activate', '--job', 'J')
        self.assertIn('healthy fact', self.start())

    def test_bank_list(self):
        self.fake.banks.update(coding=[], hermes=[])
        self.assertEqual(hindsight.Client(self.cfg).list_banks(), ['coding', 'hermes'])

    def test_configured_recall_banks_include_explicit_project_once(self):
        self.enable(default_bank='general', recall_banks=['coding', 'coding', 'hermes'])
        self.fake.add_memory('coding', 'general fact')
        self.fake.add_memory('pg-ha', 'project fact')
        self.cli('activate', '--job', 'J', '--project', 'PG HA')
        ctx = self.start()
        self.assertIn('general fact', ctx)
        self.assertIn('project fact', ctx)
        self.assertEqual(len(self.fake.calls('POST', '/memories/recall')), 3)

    def test_cache_bound_drops_whole_trailing_facts(self):
        for i in range(10):
            self.fake.add_memory('coding', str(i) + '\\"' * 900)
        items = hindsight.Client(self.cfg).recall_many(['coding'], 'q')
        import json
        self.assertLessEqual(len(json.dumps(items)), 6000)
        self.assertGreater(len(items), 0)
        self.assertLess(len(items), 10)
        self.assertEqual(items[-1]['text'], str(len(items) - 1) + '\\"' * 900)

    def test_periodic_recall_uses_all_banks_and_output_bounds(self):
        self.enable(recall_max_items=2, recall_max_chars=35)
        self.fake.add_memory('coding', 'old fact')
        self.cli('activate', '--job', 'J')
        self.assertIn('old fact', self.start())
        self.fake.add_memory('hermes', 'new shared bank fact')
        self.fake.add_memory('coding', 'new coding fact')
        self.backdate('agent-1', memory_recalled_at=16)
        ctx = self.turn()
        self.assertIn('new coding fact', ctx)
        self.assertNotIn('- old fact', ctx)
        memory = ctx[ctx.index('[swarm memory]'):].splitlines()[1:]
        self.assertLessEqual(len(memory), 2)
        self.assertLessEqual(sum(len(line) for line in memory), 35)
