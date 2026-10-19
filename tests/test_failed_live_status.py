"""Current work must not be hidden by a historical terminal lifecycle record."""
import os
import unittest
from unittest import mock
from support import MemoryHarness, FileHarness, SqliteHarness, PostgresHarness
from swarm import cli, passcmd, snapshot
from swarm.board.base import derive_job_status

class FailedLiveContract:
    def setUp(self):
        self.h = self.harness_factory()
        self.h.reset()
        self.addCleanup(self.h.close)
        self.b = self.h.board()
        self.addCleanup(self.b.close)
        self.b.open_job('j', 'historical failure', None, 'session', 'owner', goal='ship')
        self.b.set_job_data('j', 'workdir', '/tmp')

    def terminal(self, status='failed', by='swarm pass'):
        self.b.close_job('j', status, '3 launches with no hand-off (DONE)', forced=True, closed_by=by)
        return self.b.job_status('j')

    def worker(self):
        self.b.allocate_name('current-worker', 'j', 'engineer')
        self.b.tool_started('current-worker', 'Bash')

    def assert_heading(self, state):
        raw = self.b.job_status('j')
        self.assertEqual(derive_job_status(raw, 5, self.b.now()), state)
        self.assertIn(f'[{state}]', cli.job_detail(self.b, 'j', False))
        wire = snapshot.snapshot_once(self.b, snapshot.Scope(jobs=('j',), messages=0))['jobs'][0]
        self.assertEqual(wire['status'], state)
        self.assertEqual(wire['lifecycle_status'], raw.status)
        if hasattr(self.h, 'shown_status'):
            self.assertEqual(self.h.shown_status('j'), state)
        if hasattr(self.b, 'watch_snapshot'):
            view = self.b.watch_snapshot('j', None, 60, 0)
            self.assertEqual(derive_job_status(view.job_status('j'), 5, view.now()), state)

    def test_historical_failed_record_yields_to_current_work_without_reopening(self):
        before = self.terminal()
        self.worker()
        self.assert_heading('active')
        raw = self.b.job_status('j')
        for field in ('status', 'closed_by', 'finished_at', 'outcome', 'verdict', 'activated_at'):
            self.assertEqual(getattr(raw, field), getattr(before, field), field)
        self.assertEqual(self.b.jobs(), [], 'scheduler lifecycle selection must stay closed')
        self.assertIn('prior failed', snapshot.snapshot_once(self.b, snapshot.Scope(jobs=('j',), messages=0))['jobs'][0]['status_context'])

    def test_current_workers_are_included_in_display_overview_and_default_snapshot(self):
        self.terminal()
        self.worker()
        self.assertIn('j', [j.job for j in cli.open_and_paused(self.b, False)])
        self.assertIn('j', [j['job'] for j in snapshot.snapshot_once(self.b, snapshot.Scope(messages=0))['jobs']])

    def test_session_display_does_not_replace_current_work_with_newer_finished_job(self):
        self.terminal()
        self.worker()
        self.b.open_job('newer', None, None, 'session', 'owner')
        self.b.close_job('newer', 'completed', 'done', forced=True, closed_by='owner')
        self.assertEqual([j.job for j in self.b.session_shown_jobs('session')], ['j'])
        wire = snapshot.snapshot_once(self.b, snapshot.Scope(sessions=('session',), messages=0))
        self.assertEqual([j['job'] for j in wire['jobs']], ['j'])

    def test_terminal_live_session_watch_does_not_expire_until_work_ends(self):
        from test_watch_session import Clock
        self.terminal()
        self.worker()
        clock = Clock()
        idle = cli.IdleExit(30, clock)
        view = {'session': 'session', 'idle_exit': idle, 'compact': True}
        with mock.patch('shutil.get_terminal_size', return_value=os.terminal_size((160, 40))):
            cli._watch_frame(self.b, None, 2, False, False, view)
            clock.t += 31
            cli._watch_frame(self.b, None, 2, False, False, view)
            self.assertFalse(idle.expired())
            self.b.agent_stopped('current-worker')
            cli._watch_frame(self.b, None, 2, False, False, view)
            clock.t += 30
            self.assertTrue(idle.expired())

    def test_owner_cancellation_is_preserved_while_actual_work_is_displayed(self):
        before = self.terminal('cancelled', 'owner')
        self.worker()
        self.assert_heading('active')
        self.assertEqual(self.b.job_status('j').status, 'cancelled')
        self.b.agent_stopped('current-worker')
        self.assert_heading('cancelled')
        self.assertEqual(self.b.job_status('j').finished_at, before.finished_at)

    def test_owner_failed_and_completed_records_are_preserved_until_work_ends(self):
        for state in ('failed', 'completed'):
            self.terminal(state, 'owner')
            self.worker()
            self.assert_heading('active')
            self.b.agent_stopped('current-worker')
            self.assert_heading(state)

    def test_no_workers_or_only_ended_turn_remains_failed(self):
        self.terminal()
        self.assert_heading('failed')
        self.worker()
        self.b.agent_turn_ended('current-worker')
        self.assert_heading('failed')
        self.assertEqual(self.b.shown_jobs(), [])
        self.assertEqual(snapshot.snapshot_once(self.b, snapshot.Scope(messages=0))['jobs'], [])

    def test_started_agent_is_actual_work_and_queued_launch_is_not(self):
        self.b.queue_launch('j', 1, 'queued', role='engineer', harness='codex', model=None,
                            workdir='/tmp', brief='work', queued_by='owner')
        self.terminal()
        self.assertEqual(self.b.shown_jobs(), [])
        self.b.allocate_name('current-worker', 'j', 'engineer')
        self.assert_heading('active')
        self.assertEqual([j.job for j in self.b.shown_jobs()], ['j'])

    def test_active_work_suppresses_old_failure_notice_without_acknowledging_it(self):
        self.terminal()
        self.worker()
        with mock.patch.object(self.b, 'post', wraps=self.b.post) as post:
            self.assertEqual(passcmd._notices(self.b, self.b.now()), [])
            post.assert_not_called()
        self.assertNotIn('pass_acked', self.b.job_data('j'))
        self.b.agent_stopped('current-worker')
        self.assertEqual(passcmd._notices(self.b, self.b.now()), ['j: notice repeated'])

    def test_cancelled_lifecycle_still_refuses_launch_claims(self):
        row = self.b.queue_launch('j', 1, 'queued', role='engineer', harness='codex', model=None,
                                  workdir='/tmp', brief='work', queued_by='owner')
        self.terminal('cancelled', 'owner')
        self.worker()
        runs = []
        acts, reason = passcmd._start_queued(self.b, self.h.cfg, 'j', lambda cfg, run: runs.append(run))
        self.assertEqual((acts, reason, runs), ([], None, []))
        self.assertEqual(self.b.get_launch('j', 1, 'queued').revision, row.revision)

class MemoryFailedLive(FailedLiveContract, unittest.TestCase):
    harness_factory = MemoryHarness
class FileFailedLive(FailedLiveContract, unittest.TestCase):
    harness_factory = FileHarness
class SqliteFailedLive(FailedLiveContract, unittest.TestCase):
    harness_factory = SqliteHarness
@unittest.skipUnless(os.environ.get('SWARM_TEST_CONFIG'), 'throwaway Postgres required')
class PostgresFailedLive(FailedLiveContract, unittest.TestCase):
    harness_factory = staticmethod(lambda: PostgresHarness(os.environ['SWARM_TEST_CONFIG']))

    def test_schema27_priority_upgrade_preserves_raw_lifecycle_and_old_client_reads(self):
        import contextlib
        from swarm.board import autoinit, base
        from swarm.board.postgres import STATUS_VIEW, _AGENT_STATUS_COLS
        from support import SMALL_POOL
        before = self.terminal()
        self.worker()
        old_priority = STATUS_VIEW.replace(
            "CASE WHEN COALESCE(s.started, 0) + COALESCE(s.running, 0) > 0 THEN 'active'\n"
            "            WHEN j.status <> 'active' THEN j.status",
            "CASE WHEN j.status <> 'active' THEN j.status\n"
            "            WHEN COALESCE(s.started, 0) + COALESCE(s.running, 0) > 0 THEN 'active'")
        self.assertNotEqual(old_priority, STATUS_VIEW)
        self.h.conn.execute(old_priority.format(idle=5, dead=30, tool_timeout=60))
        self.h.conn.execute("UPDATE board_meta SET value='27' WHERE key='schema_version'")
        self.assertEqual(self.h.shown_status('j'), 'failed')
        with mock.patch.object(autoinit, 'enabled', return_value=True), \
             mock.patch.object(autoinit, 'stamp_path', return_value=None), \
             mock.patch.object(autoinit, 'setup_lock', return_value=contextlib.nullcontext()):
            result = autoinit.ensure_initialized(self.h.cfg, names=SMALL_POOL)
        self.assertEqual(result.action, 'initialized')
        self.assertEqual(base.SCHEMA_VERSION, 28)
        self.assert_heading('active')
        after = self.b.job_status('j')
        self.assertEqual((after.status, after.finished_at, after.closed_by),
                         (before.status, before.finished_at, before.closed_by))
        old_columns = _AGENT_STATUS_COLS.removesuffix(', turn_ended_at')
        self.assertEqual(self.h.conn.execute(f'SELECT {old_columns} FROM agent_status').fetchone()[3], 'running')
        self.b.post('j', self.b.active_agent_name('current-worker'), 'old-client post')
        self.assertEqual(self.b.jobs(), [])
