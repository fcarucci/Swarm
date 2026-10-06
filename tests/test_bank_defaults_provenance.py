"""General bank defaults survive queued delivery and provenance capture."""
from test_hindsight import HindsightEnv
from test_provenance_hooks import ClaudeAgents
from swarm import spool
import provenance_fixtures as PF


class GeneralBankProvenanceTests(ClaudeAgents, HindsightEnv):
    def setUp(self):
        super().setUp()
        self.enable(default_bank="engineering")
        self.fake.banks["engineering"] = []
        self.setup_agents(project=None)

    def test_queued_default_write_pins_configured_bank(self):
        doc = "swarm-spool-" + "cd" * 16
        self.post(PF.SWARM_REMEMBER_CMD, f'queued (...) [memory {doc} project ""]\n')
        [ref] = self.refs()
        self.assertEqual(ref.bank, "engineering")

    def test_spool_without_project_delivers_to_configured_default(self):
        spool.spool_memory(self.cfg, "J", self.name_of("a1"), "durable fact", None)
        with self.board() as board:
            spool.flush_spool(board, self.cfg)
        self.assertEqual([i["text"] for i in self.fake.banks["engineering"]], ["durable fact"])
        self.assertNotIn("j", self.fake.banks)

    def test_spool_only_creates_a_bank_when_explicitly_authorized(self):
        with self.board() as board:
            spool.spool_memory(self.cfg, "J", self.name_of("a1"), "first fact", "fresh")
            spool.flush_spool(board, self.cfg)
            self.assertNotIn("fresh", self.fake.banks)
            spool.spool_memory(self.cfg, "J", self.name_of("a1"), "second fact", "fresh",
                               create_bank=True)
            spool.flush_spool(board, self.cfg)
        self.assertEqual([i["text"] for i in self.fake.banks["fresh"]], ["second fact"])

    def test_spool_rejects_non_boolean_creation_flag(self):
        import json
        queued = spool.spool_memory(self.cfg, "J", self.name_of("a1"), "fact", "fresh")
        record = json.loads(queued.read_text())
        record["create_bank"] = "true"
        queued.write_text(json.dumps(record))
        with self.board() as board:
            spool.flush_spool(board, self.cfg)
        self.assertNotIn("fresh", self.fake.banks)
        self.assertEqual(len(list(self.spool_dir.glob("*.bad"))), 1)
