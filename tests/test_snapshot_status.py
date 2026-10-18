"""JSON snapshots expose the same actual work state and context as status/watch."""
import os
import unittest

from support import MemoryHarness, FileHarness, SqliteHarness, PostgresHarness
from swarm import cli, snapshot
from swarm.board.base import derive_job_status


class SnapshotStatusContract:
    def setUp(self):
        self.h = self.harness_factory()
        self.h.reset()
        self.addCleanup(self.h.close)
        self.b = self.h.board()
        self.addCleanup(self.b.close)
        self.b.open_job('j', None, None, None, 'owner', goal='ship release')
        self.scope = snapshot.Scope(jobs=('j',), messages=0)

    def assert_view(self, expected, reason):
        raw = self.b.job_status('j')
        self.assertEqual(derive_job_status(raw, 5, self.b.now()), expected)
        wire = snapshot.snapshot_once(self.b, self.scope)['jobs'][0]
        self.assertEqual(wire['status'], expected)
        self.assertEqual(wire['lifecycle_status'], raw.status)
        self.assertIn(reason, wire['status_context'])
        self.assertIn(f'[{expected}]', cli.job_detail(self.b, 'j', False))
        self.assertIn(reason, cli._job_heading(self.b, raw, self.b.now()))
        state = snapshot.ViewState(self.scope)
        state.reset(snapshot.read_view(self.b, self.scope))
        self.assertEqual(snapshot.fingerprint_of_state(state), snapshot.remote_fingerprint(self.b, self.scope)[0])

    def test_zero_work_is_idle_even_though_lifecycle_is_active(self):
        self.assert_view('idle', 'no agents working; no pending work registered')

    def test_ended_turn_is_idle_immediately_without_a_clock_timeout(self):
        self.b.allocate_name('worker', 'j', 'engineer')
        self.b.tool_started('worker', 'Bash')
        self.b.agent_turn_ended('worker')
        self.assert_view('idle', 'no agents working; no pending work registered')

    def test_internal_dependency_is_named_without_human_wait(self):
        self.b.open_blocker('j', 'dependency', 'mac-sign', 'waiting for signed release.zip')
        self.assert_view('idle', 'dependency -> mac-sign: waiting for signed release.zip')

    def test_human_question_names_the_specific_question(self):
        question = self.b.open_blocker('j', 'question', 'owner', 'which release?')
        self.assert_view('waiting', f'Q{question.id} -> owner: which release?')

    def test_queued_work_is_named_but_does_not_claim_it_is_running(self):
        self.b.queue_launch('j', 1, 'mac-sign', role='engineer', harness='codex', model=None,
                            workdir='/tmp', brief='sign', queued_by='owner')
        self.assert_view('idle', 'queued launch: mac-sign')

    def test_real_work_overrides_an_unanswered_question(self):
        self.b.open_blocker('j', 'question', 'owner', 'which release?')
        self.b.allocate_name('worker', 'j', 'engineer')
        self.b.tool_started('worker', 'Bash')
        self.assert_view('active', 'which release?')

    def test_stream_delta_tracks_actual_work_then_turn_end(self):
        self.b.allocate_name('worker', 'j', 'engineer')
        self.b.agent_turn_ended('worker')
        state = snapshot.ViewState(self.scope)
        before = state.reset(snapshot.read_view(self.b, self.scope))
        self.assertEqual(before['jobs'][0]['status'], 'idle')
        self.b.tool_started('worker', 'Bash')
        delta = state.diff(snapshot.read_view(self.b, self.scope))
        self.assertEqual(snapshot.apply_delta(before, delta)['jobs'][0]['status'], 'active')
        self.b.agent_turn_ended('worker')
        delta = state.diff(snapshot.read_view(self.b, self.scope))
        self.assertEqual(delta['jobs']['upsert'][0]['status'], 'idle')
        self.assertEqual(delta['jobs']['upsert'][0]['lifecycle_status'], 'active')


class MemorySnapshotStatus(SnapshotStatusContract, unittest.TestCase):
    harness_factory = MemoryHarness


class FileSnapshotStatus(SnapshotStatusContract, unittest.TestCase):
    harness_factory = FileHarness


class SqliteSnapshotStatus(SnapshotStatusContract, unittest.TestCase):
    harness_factory = SqliteHarness


@unittest.skipUnless(os.environ.get('SWARM_TEST_CONFIG'), 'throwaway Postgres required')
class PostgresSnapshotStatus(SnapshotStatusContract, unittest.TestCase):
    harness_factory = staticmethod(lambda: PostgresHarness(os.environ['SWARM_TEST_CONFIG']))
