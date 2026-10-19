"""Task supersession is an audited obligation change, never fabricated task evidence."""
import concurrent.futures
import datetime as dt
import threading
import os
import unittest
import uuid

from support import MemoryHarness, FileHarness, SqliteHarness, PostgresHarness
from swarm import dependencies, review, snapshot


class SupersedeContract:
    factory = None

    def setUp(self):
        self.h = self.factory()
        self.h.reset()
        self.addCleanup(self.h.close)
        self.b = self.h.board()
        self.addCleanup(self.b.close)
        self.b.open_job('J', None, None, None, 'owner', goal='Whole project')
        self.old = self.register('old-review', 2, 'old@abc')
        self.b.set_launch_state(self.old.id, self.old.revision, 'failed', job='J')
        actor = self.b.allocate_name('replacement-seat', 'J', 'qa')
        self.register('corrected-review', 4, 'new@def', reporter=actor)
        dependencies.report(self.b, 'J', 4, 'corrected-review', artifact='new@def',
                            outcome='passed', reason='Independent exact replacement checked', actor=actor)
        self.b.agent_turn_ended('replacement-seat')
        dependencies.reconcile_external(self.b, 'J')

    def register(self, name, generation, artifact, role='qa', reporter=None, depends=()):
        return dependencies.register(self.b, 'J', generation, name, artifact=artifact,
            depends=list(depends), role=role, harness='codex', model=None, workdir='/work',
            brief='Verify '+artifact, queued_by='owner', reporter=reporter)

    def supersede(self, board=None, **kw):
        args = dict(job='J', generation=2, name='old-review', artifact='old@abc',
                    replacement='corrected-review', replacement_generation=4,
                    replacement_artifact='new@def', reason='Corrected document independently reviewed', actor='EL')
        args.update(kw)
        return (board or self.b).supersede_task(**args)

    def test_supersession_preserves_rows_and_evidence_and_does_not_accept_goal(self):
        before = self.b.launches('J')
        evidence = dependencies.evidence(self.b, self.b.get_launch('J', 4, 'corrected-review'))
        revision = snapshot.context_revision(self.b, 'J')
        event_id, created = self.supersede()
        self.assertTrue(created)
        self.assertEqual(self.b.launches('J'), before)
        self.assertEqual(dependencies.evidence(self.b, self.b.get_launch('J', 4, 'corrected-review')), evidence)
        self.assertNotEqual(snapshot.context_revision(self.b, 'J'), revision)
        self.assertEqual([(r['name'], r['state']) for r in dependencies.describe(self.b, 'J')], [('corrected-review', 'done')])
        history = dependencies.describe(self.b, 'J', include_superseded=True)
        self.assertEqual(next(r['state'] for r in history if r['name']=='old-review'), 'superseded')
        self.assertTrue(review.completion_pending(self.b, 'J', 'Whole project', None))
        self.assertEqual(self.b.job_status('J').status, 'active')
        self.assertEqual(dependencies.advance(self.b, 'J'), [])
        self.assertEqual(self.b.get_launch('J', 2, 'old-review').state, 'failed')

    def test_retries_are_immutable_and_later_replacement_generation_still_required(self):
        first = self.supersede()
        self.assertEqual(self.supersede(), (first[0], False))
        with self.assertRaises(ValueError):
            self.supersede(reason='different reason')
        self.register('corrected-review', 5, 'newer@ghi')
        self.assertEqual(self.supersede(), (first[0], False))
        self.assertEqual(len([e for e in self.b.events('J') if e.kind=='TASK-SUPERSEDE']), 1)
        self.assertTrue(review.completion_pending(self.b, 'J', 'Whole project', 'met'))
        self.assertEqual([(r['generation'], r['state']) for r in dependencies.describe(self.b, 'J')], [(5, 'ready')])

    def test_historical_passed_turn_remains_accepted_after_reporter_resumes_other_work(self):
        self.b.tool_started('replacement-seat', 'Unrelated new task')
        self.assertEqual(self.b.get_launch('J', 4, 'corrected-review').state, 'done')
        self.assertTrue(self.supersede()[1])

    def test_passed_evidence_without_terminal_settlement_is_refused(self):
        actor = self.b.allocate_name('still-reporting', 'J', 'qa')
        self.register('unsettled', 4, 'new@def', reporter=actor)
        dependencies.report(self.b, 'J', 4, 'unsettled', artifact='new@def',
                            outcome='passed', reason='Reported but turn still active', actor=actor)
        with self.assertRaises(ValueError):
            self.supersede(replacement='unsettled')

    def test_identity_role_run_and_self_guards(self):
        self.register('wrong-role', 4, 'new@def', role='engineer')
        for kw in ({'artifact':'wrong'}, {'replacement_artifact':'wrong'}, {'job':'other'},
                   {'replacement':'missing'}, {'replacement':'wrong-role'},
                   {'replacement':'old-review','replacement_generation':2,'replacement_artifact':'old@abc'},
                   {'reason':''}):
            with self.subTest(kw=kw), self.assertRaises(ValueError):
                self.supersede(**kw)
        self.b.open_job('J', None, None, None, 'owner', goal='Fresh run')
        with self.assertRaises(ValueError):
            self.supersede()

    def test_active_old_and_incomplete_or_newer_replacement_are_refused(self):
        actor = self.b.allocate_name('active-old', 'J', 'qa')
        self.register('active-review', 2, 'old@abc', reporter=actor)
        with self.assertRaises(ValueError):
            self.supersede(name='active-review')
        self.register('corrected-review', 5, 'newer@ghi')
        with self.assertRaises(ValueError):
            self.supersede()
        with self.assertRaises(ValueError):
            self.supersede(replacement_generation=5, replacement_artifact='newer@ghi')

    def test_downstream_dependencies_keep_original_artifact_contract(self):
        self.register('old-build', 2, 'old@abc', depends=['old-review'])
        self.supersede()
        child = self.b.get_launch('J', 2, 'old-build')
        self.assertIn('superseded', dependencies.block_reason(self.b, child))
        self.assertEqual(dependencies.spec(child)['depends'], ['old-review'])
        self.assertTrue(review.completion_pending(self.b, 'J', 'Whole project', 'met'))

    def test_incomplete_or_damaged_legacy_events_cannot_retire_tasks_or_crash_reads(self):
        import json
        row = self.b.get_launch('J', 2, 'old-review')
        key = dependencies._supersede_key(row)
        self.b._insert_event('J', 'TASK-SUPERSEDE', key,
            json.dumps({'old':dependencies._identity(row)}), None, 'swarm-task')
        self.assertTrue(any(r['name']=='old-review' for r in dependencies.describe(self.b, 'J')))
        with self.assertRaises(ValueError):
            self.supersede()

    def test_generic_event_cannot_forge_supersession(self):
        with self.assertRaises(ValueError):
            self.b.post_event('J', 'TASK-SUPERSEDE', 'forged', '{}', source='swarm-task')
        self.assertEqual(len(dependencies.describe(self.b, 'J')), 2)

    def test_ack_and_retention_cannot_resurrect_superseded_obligation(self):
        event_id, _ = self.supersede()
        self.b.ack_events('J', [event_id], 'EL')
        # Other retained activity keeps the job present while aged acked events are purged.
        from unittest import mock
        with mock.patch.object(self.b, 'now', return_value=self.b.now()+dt.timedelta(days=10)):
            self.b.post('J', 'EL', 'job still active')
            self.b.purge()
        self.assertTrue(any(e.id==event_id for e in self.b.events('J')))
        self.assertFalse(any(r['name']=='old-review' for r in dependencies.describe(self.b, 'J')))

    def test_queued_superseded_task_does_not_block_goal_judge_or_pollute_cached_context(self):
        import dataclasses
        import json
        from swarm import passcmd, statusview, watchdata
        self.register('queued-old', 2, 'old@abc')
        self.supersede(name='queued-old')
        # Supersede the other obsolete failed review too: only the accepted replacement remains.
        self.supersede()
        self.b.post('J', 'EL', 'DONE new@def')
        actions = passcmd.run_pass(self.b, self.h.cfg, starter=lambda *_: None)
        self.assertTrue(any('judge queued' in action for action in actions), actions)
        self.assertTrue(any(r.name.startswith('goal-judge-') for r in self.b.launches('J')))
        self.assertEqual(self.b.get_launch('J', 2, 'queued-old').state, 'queued')
        plain = dataclasses.asdict
        cached = watchdata.SnapshotBoard(self.b, [self.b.now(), [plain(self.b.job_status('J'))],
            [], [], [], {}, {}, [], {'J':json.dumps(self.b.job_data('J'))}, [],
            [plain(r) for r in self.b.launches('J')], [plain(e) for e in self.b.events('J')]])
        self.assertFalse(any(r['name']=='queued-old' for r in dependencies.describe(cached, 'J')))
        self.assertNotIn('queued launch: queued-old', statusview.job_context(cached, cached.job_status('J'), cached.now()))

    def test_external_old_terminal_evidence_does_not_mutate_retired_row_on_advance(self):
        actor = self.b.allocate_name('old-external', 'J', 'qa')
        row = self.register('external-old', 2, 'old@abc', reporter=actor)
        dependencies.report(self.b, 'J', 2, 'external-old', artifact='old@abc',
                            outcome='failed', reason='Original document failed', actor=actor)
        self.b.agent_turn_ended('old-external')
        self.supersede(name='external-old')
        before = self.b.get_launch('J', 2, 'external-old')
        with self.assertRaisesRegex(ValueError, 'superseded'):
            dependencies.report(self.b, 'J', 2, 'external-old', artifact='old@abc',
                                outcome='passed', reason='Late obsolete report', actor=actor)
        dependencies.advance(self.b, 'J')
        self.assertEqual(self.b.get_launch('J', 2, 'external-old'), before)
        self.assertFalse(any(r['name']=='external-old' for r in dependencies.describe(self.b, 'J')))

    def test_parallel_audit_retries_and_conflicts_preserve_one_first_record(self):
        for conflicting in (False, True):
            name = 'parallel-' + str(conflicting).lower()
            old = self.register(name, 2, 'old@abc')
            self.b.set_launch_state(old.id, old.revision, 'failed', job='J')
            barrier = threading.Barrier(4)
            def write(i):
                with self.h.board() as board:
                    barrier.wait(10)
                    try:
                        return self.supersede(board, name=name,
                            reason=('reason '+str(i) if conflicting else 'same reason'))
                    except ValueError:
                        return None
            with concurrent.futures.ThreadPoolExecutor(4) as pool:
                receipts = list(pool.map(write, range(4)))
            accepted = [r for r in receipts if r is not None]
            self.assertEqual(len(accepted), 1 if conflicting else 4)
            self.assertEqual(sum(created for _, created in accepted), 1)

    def test_claim_race_has_exactly_one_winner(self):
        old = self.register('queued-review', 2, 'old@abc')
        barrier = threading.Barrier(2)
        def supersede():
            with self.h.board() as board:
                barrier.wait(10)
                try:
                    return self.supersede(board, name='queued-review')
                except ValueError:
                    return None
        def claim():
            with self.h.board() as board:
                barrier.wait(10)
                return board.claim_drive_launch(job='J', generation=2, action_id='race', attempt=1,
                    launch_id='race-old', launch_row_id=old.id, queue_version=old.revision,
                    harness='codex', reserve_usd=1, reserve_minutes=1, locks=[], lock_seconds=60)
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            a, b = pool.submit(supersede), pool.submit(claim)
            retired, started = a.result(), b.result()
        self.assertNotEqual(retired is None, started is None)


class MemorySupersede(SupersedeContract, unittest.TestCase):
    factory = staticmethod(lambda: MemoryHarness('supersede-'+str(uuid.uuid4())))
class FileSupersede(SupersedeContract, unittest.TestCase):
    factory = FileHarness
class SqliteSupersede(SupersedeContract, unittest.TestCase):
    factory = SqliteHarness


@unittest.skipUnless(os.environ.get('SWARM_TEST_CONFIG'), 'throwaway Postgres required')
class PostgresSupersede(SupersedeContract, unittest.TestCase):
    factory = staticmethod(lambda: PostgresHarness(os.environ['SWARM_TEST_CONFIG']))
