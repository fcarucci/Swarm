"""Late completion needs bound evidence; a historical failure is never reopened."""
import datetime as dt
import unittest
from unittest import mock

from support import drive_backends, add_backend_variants
from swarm import review
from swarm import cli, drivegate
from swarm.spool import deliver_verdict, VerdictUnbound


class LateGoalEvidence(unittest.TestCase):
    harness_factory = staticmethod(drive_backends()['memory'])

    def setUp(self):
        self.h = self.harness_factory(self.id())
        self.addCleanup(self.h.close)
        self.h.reset()
        self.b = self.h.board()
        self.addCleanup(self.b.close)
        self.time = dt.datetime(2026, 10, 19, 12, tzinfo=dt.timezone.utc)
        patcher = mock.patch.object(self.b, 'now', side_effect=lambda: self.time)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.b.open_job('J', None, None, None, 'owner', goal='complete cleanup safely')
        # The board fixture supplies an existing judge solely to exercise persistence.
        # This does not model or authorize closed-job judge admission.
        self.judge = self.b.allocate_name('native-judge', 'J', 'judge')
        self.assertTrue(self.b.claim_judge('native-judge', 'J'))

    def test_nongenerational_goal_verdict_retains_exact_posted_binding(self):
        mid = deliver_verdict(self.b, 'J', self.judge, 'met', 'all criteria verified',
            details='Full independent cleanup report.', scope='goal', open_count=0,
            candidate='cleanup@abcdef012345', req='sha256:requirements')
        data = self.b.job_data('J')
        key = 'pipeline.goal_verdict'
        self.assertIn(key, data, 'a posted full-goal verdict must retain its native message binding')
        record = review._chunked(data, key)
        self.assertEqual(record['message_id'], mid)
        self.assertEqual((record['scope'], record['candidate'], record['req'], record['open']),
                         ('goal', 'cleanup@abcdef012345', 'sha256:requirements', 0))
        self.assertEqual(record['judge'], self.judge)

    def test_failed_second_binding_write_remains_unbound(self):
        original = self.b.record_verdict
        def record(*args, **kwargs):
            if kwargs.get('message_id', 0) > 0:
                return False
            return original(*args, **kwargs)
        with mock.patch.object(self.b, 'record_verdict', side_effect=record):
            with self.assertRaises(VerdictUnbound):
                deliver_verdict(self.b, 'J', self.judge, 'met', 'checked',
                    details='Independent report.', scope='goal', open_count=0,
                    candidate='cleanup@abcdef012345', req='sha256:requirements')
        record = review._chunked(self.b.job_data('J'), 'pipeline.goal_verdict')
        self.assertEqual(record['message_id'], 0)
        self.assertIsNone(review.bound_verdict(record))

    def test_changed_goal_removes_prior_bound_full_goal_evidence(self):
        deliver_verdict(self.b, 'J', self.judge, 'met', 'checked',
            details='Independent report.', scope='goal', open_count=0,
            candidate='cleanup@abcdef012345', req='sha256:requirements')
        self.b.set_job_goal('J', 'different cleanup obligations')
        self.assertFalse(any(k.startswith('pipeline.goal_verdict') for k in self.b.job_data('J')))

    def test_late_goal_record_preserves_historical_failure_bytes(self):
        self.b.close_job('J', 'failed', '3 launches with no hand-off (DONE)', closed_by='swarm pass')
        original = self.b.job_status('J')
        self.time += dt.timedelta(hours=1)
        deliver_verdict(self.b, 'J', self.judge, 'met', 'checked',
            details='Independent report.', scope='goal', open_count=0,
            candidate='cleanup@abcdef012345', req='sha256:requirements')
        after = self.b.job_status('J')
        for field in ('status', 'outcome', 'closed_by', 'finished_at', 'activated_at'):
            self.assertEqual(getattr(after, field), getattr(original, field), field)

    def test_generic_board_data_cannot_manufacture_native_completion_receipt(self):
        self.b.close_job('J', 'failed', '3 launches with no hand-off (DONE)', closed_by='swarm pass')
        self.b.set_job_data('J', 'completion.receipt', '{"status":"completed"}')
        reader = getattr(self.b, 'completion_receipt', None)
        self.assertTrue(callable(reader), 'native completion receipts need dedicated backend storage')
        self.assertIsNone(reader('J'))
        reconcile = getattr(self.b, 'settle_late_review', None)
        self.assertTrue(callable(reconcile))
        self.assertIsNone(reconcile('J'))
        self.assertIsNone(reader('J'))

    def test_closed_review_has_explicit_owner_request_parser_and_grant_boundary(self):
        parser = cli._parser()
        # A fresh prompt-tagged judge is not an admitted native review. A distinct
        # owner request must bind the precise historical closure and evidence.
        with mock.patch.object(parser, 'error', side_effect=ValueError):
            try:
                args = parser.parse_args(['late-review', '--job', 'J', '--closure', 'a' * 64,
                    '--candidate', 'cleanup@abcdef012345', '--req', 'b' * 64, '--request-id', 'user-item',
                    '--workdir', '/approved'])
            except ValueError:
                args = None
        self.assertIsNotNone(args, 'closed review requires a narrow supported owner request')


add_backend_variants(globals(), LateGoalEvidence)
