"""Planner invariants for the PostgreSQL status views (no server required)."""
import unittest

import support  # noqa: F401 (sets sys.path)
from swarm.board.postgres import SCHEMA, STATUS_VIEW


class StatusViewShapeTests(unittest.TestCase):
    def test_agent_message_count_is_read_from_the_agent_row(self):
        agent_view = STATUS_VIEW.split('CREATE VIEW job_status AS')[0]
        self.assertIn('a.message_count AS messages', agent_view)
        self.assertNotIn('LATERAL', agent_view)           # no per-agent count of messages on a read
        self.assertNotIn('FROM messages', agent_view)

    def test_message_count_is_kept_by_triggers_that_match_the_old_count(self):
        # an insert bumps every row of the job and name that joined before it; a reset, move or rename recounts
        self.assertRegex(SCHEMA, r'UPDATE agents SET message_count = message_count \+ 1\s+'
                                 r'WHERE job = NEW\.job AND name = NEW\.agent_name AND joined_at <= NEW\.created_at')
        self.assertIn('BEFORE UPDATE OF job, name, joined_at ON agents', SCHEMA)
        self.assertIn('m.created_at >= NEW.joined_at', SCHEMA)

    def test_hot_tables_vacuum_early(self):
        for table in ('messages', 'agents'):
            self.assertRegex(SCHEMA, rf'ALTER TABLE {table} SET \(autovacuum_vacuum_insert_scale_factor = 0\.02, '
                                     r'autovacuum_vacuum_scale_factor = 0\.05,\s+autovacuum_analyze_scale_factor = 0\.05\)')

    def test_job_rollup_does_not_compute_per_agent_message_counts(self):
        job_view = STATUS_VIEW.split('CREATE VIEW job_status AS')[1]
        self.assertNotIn('JOIN agent_status', job_view)
        self.assertRegex(job_view, r'FROM agents a')
        self.assertRegex(job_view, r'GROUP BY a\.job')

    def test_job_totals_are_correlated_to_each_selected_job(self):
        job_view = STATUS_VIEW.split('CREATE VIEW job_status AS')[1]
        self.assertEqual(job_view.count('LEFT JOIN LATERAL'), 2)
        self.assertIn('WHERE a.job = j.job', job_view)
        self.assertIn('WHERE m.job = j.job', job_view)

    def test_job_rollups_keep_blocker_columns_and_expiry_activity_filter(self):
        job_view = STATUS_VIEW.split('CREATE VIEW job_status AS')[1]
        for column in ('AS blockers', 'AS open_blockers', 'AS protected_blockers'):
            self.assertIn(column, job_view)
        self.assertIn("message LIKE 'Blocker % expired:%'", job_view)
        self.assertIn("WHEN EXISTS (SELECT 1 FROM blockers", job_view)
