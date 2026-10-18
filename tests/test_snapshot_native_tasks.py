"""Native task context remains complete in cached snapshots and after lost notifications."""
import dataclasses
import json
import os
import unittest
from support import MemoryHarness, FileHarness, SqliteHarness, PostgresHarness
from swarm import dependencies, snapshot, watchdata

class NativeTaskContract:
    def setUp(self):
        self.h = self.harness_factory()
        self.h.reset()
        self.addCleanup(self.h.close)
        self.b = self.h.board()
        self.addCleanup(self.b.close)
        self.b.open_job('j', None, None, None, 'owner', goal='release')
        self.scope = snapshot.Scope(jobs=('j',), messages=0)

    def task(self, name, depends=(), generation=1):
        return dependencies.register(self.b, 'j', generation, name, artifact='main@abc', depends=list(depends),
            role='engineer', harness='codex', model=None, workdir='/tmp', brief='Check source', queued_by='owner')

    def evidence(self, row, outcome='passed'):
        self.b.post_event('j', dependencies.KIND, dependencies._event_key(row),
            json.dumps({'outcome': outcome, 'reason': 'exact source checked'}), source='swarm-task')

    def cached(self):
        if hasattr(self.b, 'watch_snapshot'):
            return self.b.watch_snapshot('j', None, 60, 0)
        plain = dataclasses.asdict
        return watchdata.SnapshotBoard(self.b, [self.b.now(), [plain(self.b.job_status('j'))], [], [], [], {}, {},
            [plain(x) for x in self.b.blockers('j')], {'j': json.dumps(self.b.job_data('j'))}, [],
            [plain(x) for x in self.b.launches('j')], [plain(x) for x in self.b.events('j')]])

    def context(self):
        cached = self.cached()
        wire = snapshot.job_row(cached, cached.job_status('j'), cached.now())
        self.assertEqual(wire['status'], 'idle')
        return wire['status_context']

    def test_native_pending_task_is_visible_without_missing_cached_apis(self):
        self.task('qa')
        self.assertIn('task qa (main@abc): ready for bounded runner', self.context())

    def test_completed_predecessor_and_passed_evidence_release_cached_downstream(self):
        qa = self.task('qa')
        self.task('build', ['qa'])
        self.evidence(qa)
        self.b.set_launch_state(qa.id, qa.revision, 'done', job='j')
        context = self.context()
        self.assertIn('task build (main@abc): ready for bounded runner', context)
        self.assertNotIn('missing dependency', context)
        self.assertNotIn('missing exact-artifact evidence', context)

    def test_failed_predecessor_evidence_is_precisely_visible(self):
        qa = self.task('qa')
        self.task('build', ['qa'])
        self.evidence(qa, 'failed')
        self.b.set_launch_state(qa.id, qa.revision, 'done', job='j')
        self.assertIn('dependency qa failed evidence: exact source checked', self.context())

    def test_superseded_generation_is_not_a_current_ready_predecessor(self):
        qa = self.task('qa')
        self.task('build', ['qa'])
        self.evidence(qa)
        self.b.set_launch_state(qa.id, qa.revision, 'done', job='j')
        self.task('qa', generation=2)
        self.assertIn('dependency qa superseded by task generation 2', self.context())

    def test_registration_changes_snapshot_fingerprint_without_messages_or_agents(self):
        state = snapshot.ViewState(self.scope)
        state.reset(snapshot.read_view(self.b, self.scope))
        before = snapshot.fingerprint_of_state(state)
        self.task('qa')
        self.assertNotEqual(before, snapshot.remote_fingerprint(self.b, self.scope)[0])

    def test_follow_check_recovers_native_context_after_lost_notification(self):
        emitted = []
        follower = snapshot.Follower(None, self.scope, emitted.append, 60, 15, 2,
            lambda: 100.0, lambda seconds: None, lambda: True)
        follower.full(self.b, 'initial')
        self.task('qa')
        follower.check(self.b)
        self.assertEqual(len(emitted), 2)
        self.assertIn('task qa', emitted[-1]['jobs'][0]['status_context'])

    def test_evidence_and_plugin_context_changes_invalidate_cached_fingerprint(self):
        qa = self.task('qa')
        self.task('build', ['qa'])
        state = snapshot.ViewState(self.scope)
        state.reset(snapshot.read_view(self.b, self.scope))
        old = snapshot.fingerprint_of_state(state)
        self.evidence(qa, 'failed')
        self.assertNotEqual(old, snapshot.remote_fingerprint(self.b, self.scope)[0])
        state.reset(snapshot.read_view(self.b, self.scope))
        old = snapshot.fingerprint_of_state(state)
        self.b.set_job_data('j', 'task.reconciler_error', 'listener unavailable')
        self.assertNotEqual(old, snapshot.remote_fingerprint(self.b, self.scope)[0])

    def test_watch_backstop_recovers_task_context_and_then_matches_again(self):
        if not hasattr(self.b, 'watch_snapshot'):
            self.skipTest('PostgreSQL atomic snapshot source')
        source = watchdata.SnapshotSource(self.b, 'j', None, 0, clock=lambda: 100.0)
        source.read(60, 'full')
        self.task('qa')
        repaired = source.read(60, 'check')
        self.assertIsNotNone(repaired)
        self.assertIn('task qa', snapshot.job_row(repaired, repaired.job_status('j'), repaired.now())['status_context'])
        self.assertIsNone(source.read(60, 'check'))

class MemoryNativeTasks(NativeTaskContract, unittest.TestCase):
    harness_factory = MemoryHarness

class FileNativeTasks(NativeTaskContract, unittest.TestCase):
    harness_factory = FileHarness

class SqliteNativeTasks(NativeTaskContract, unittest.TestCase):
    harness_factory = SqliteHarness

@unittest.skipUnless(os.environ.get('SWARM_TEST_CONFIG'), 'throwaway PostgreSQL required')
class PostgresNativeTasks(NativeTaskContract, unittest.TestCase):
    harness_factory = staticmethod(lambda: PostgresHarness(os.environ['SWARM_TEST_CONFIG']))
