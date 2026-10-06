"""Planner invariants for the PostgreSQL status views (no server required)."""
import unittest

import support  # noqa: F401 (sets sys.path)
from swarm.board.postgres import STATUS_VIEW


class StatusViewShapeTests(unittest.TestCase):
    def test_agent_message_count_is_restricted_to_job_and_incarnation(self):
        agent_view = STATUS_VIEW.split('CREATE VIEW job_status AS')[0]
        self.assertIn('LEFT JOIN LATERAL', agent_view)
        self.assertRegex(agent_view, r'WHERE m\.job = a\.job AND m\.agent_name = a\.name')
        self.assertIn('m.created_at >= a.joined_at', agent_view)

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
