"""Actual work, not a recent heartbeat, determines an open job's displayed state."""
import dataclasses
import datetime as dt
import os
import unittest
from unittest import mock

from support import MemoryHarness, FileHarness, SqliteHarness, PostgresHarness
from swarm import cli
from swarm.board.base import derive_agent_status, derive_job_status

NOW = dt.datetime(2026, 10, 18, 12, tzinfo=dt.timezone.utc)


class DerivationTests(unittest.TestCase):
    def test_ended_turn_is_idle_immediately_despite_recent_running_state(self):
        self.assertEqual(derive_agent_status(
            "running", None, None, NOW, NOW, idle_minutes=5, dead_minutes=30,
            tool_timeout_minutes=60, turn_ended_at=NOW), "idle")

    def test_ended_turn_still_becomes_dead_after_contact_timeout(self):
        self.assertEqual(derive_agent_status(
            "running", None, None, NOW, NOW + dt.timedelta(minutes=31), idle_minutes=5,
            dead_minutes=30, tool_timeout_minutes=60, turn_ended_at=NOW), "dead")


class ActualStatusContract:
    def setUp(self):
        self.h = self.harness_factory()
        self.h.reset()
        self.addCleanup(self.h.close)
        self.b = self.h.board()
        self.addCleanup(self.b.close)
        self.b.open_job("j", None, None, None, "owner", goal="ship")

    def assert_status(self, expected):
        self.assertEqual(derive_job_status(self.b.job_status("j"), 5, self.b.now()), expected)
        self.assertEqual(derive_job_status(self.b.jobs()[0], 5, self.b.now()), expected)
        if hasattr(self.h, "shown_status"):
            self.assertEqual(self.h.shown_status("j"), expected)
        if hasattr(self.b, "watch_snapshot"):
            view = self.b.watch_snapshot("j", None, 60, 0)
            self.assertEqual(derive_job_status(view.job_status("j"), 5, view.now()), expected)

    def test_no_agents_is_idle_not_active(self):
        self.assert_status("idle")
        self.assertIn("no agents working; no pending work registered", cli.job_detail(self.b, "j", False))

    def test_ended_turn_remains_idle_after_post_and_sibling_resume_until_own_tool(self):
        name = self.b.allocate_name("a", "j", "engineer")
        self.b.tool_started("a", "Bash")
        self.b.agent_turn_ended("a")
        self.assertEqual(self.b.agents("j")[0].status, "idle")
        self.assert_status("idle")
        self.b.post("j", name, "waiting for CI")
        self.b.turns_resumed("j")
        self.assertEqual(self.b.agents("j")[0].status, "idle")
        self.assert_status("idle")
        self.b.tool_started("a", "Bash")
        self.assert_status("active")
        self.assertEqual(self.b.agents("j")[0].name, name)

    def test_idle_workers_with_internal_blocker_show_exact_dependency_not_human_wait(self):
        self.b.allocate_name("a", "j", "engineer")
        self.b.agent_turn_ended("a")
        self.b.open_blocker("j", "dependency", "mac-builder", "artifact release.zip is not ready")
        self.assert_status("idle")
        for text in (cli.job_detail(self.b, "j", False), cli.jobs_overview(self.b, False, False),
                     cli._job_heading(self.b, self.b.job_status("j"), self.b.now())):
            self.assertIn("dependency -> mac-builder: artifact release.zip is not ready", text)
            self.assertNotIn("waiting for human", text)

    def test_human_question_names_exact_question_and_is_overridden_by_work(self):
        q = self.b.open_blocker("j", "question", "owner", "which release?")
        self.assert_status("waiting")
        self.assertIn(f"Q{q.id} -> owner: which release?", cli.job_detail(self.b, "j", False))
        self.b.allocate_name("a", "j", "engineer")
        self.b.tool_started("a", "Bash")
        self.assert_status("active")
        self.b.agent_turn_ended("a")
        self.assert_status("waiting")

    def test_queued_launch_is_idle_with_named_queued_reason(self):
        self.b.queue_launch("j", 1, "mac-sign", role="engineer", harness="codex", model=None,
                            workdir="/tmp", brief="sign", queued_by="owner")
        self.assert_status("idle")
        self.assertIn("queued launch: mac-sign", cli.job_detail(self.b, "j", False))
        if hasattr(self.b, "watch_snapshot"):
            view = self.b.watch_snapshot("j", None, 60, 0)
            self.assertIn("queued launch: mac-sign", cli.job_detail(view, "j", False, include_agents=False))

    def test_watch_replay_ages_job_rollup_with_agents(self):
        self.b.allocate_name("a", "j", "engineer")
        self.b.tool_started("a", "Bash")
        self.b.tool_finished("a")
        rec = cli._Recorder(self.b)
        rec.board_cfg
        rec.job_status("j")
        rec.jobs()
        rec.agents("j")
        with mock.patch.object(cli.time, "monotonic", return_value=rec.taken_mono + 6 * 60):
            view = cli._Replay(rec)
            self.assertEqual(view.agents("j")[0].status, "idle")
            self.assertEqual(cli._job_status_word(view, view.job_status("j"), view.now()), "idle")
            self.assertEqual(cli._job_status_word(view, view.jobs()[0], view.now()), "idle")

    def test_reconciler_error_is_visible_as_internal_context(self):
        self.b.set_job_data('j', 'task.reconciler_error', 'listener lock held without healthy reconciliation')
        self.assert_status('idle')
        self.assertIn('reconciler: listener lock held without healthy reconciliation',
                      cli.job_detail(self.b, 'j', False))

    def test_remaining_goal_is_visible_when_no_task_is_working(self):
        self.b.set_job_data('j', 'task.blocker', 'declared tasks passed; remaining job goal: sign the release')
        self.assert_status('idle')
        self.assertIn('declared tasks passed; remaining job goal: sign the release',
                      cli.job_detail(self.b, 'j', False))


class MemoryActualStatus(ActualStatusContract, unittest.TestCase):
    harness_factory = MemoryHarness


class FileActualStatus(ActualStatusContract, unittest.TestCase):
    harness_factory = FileHarness


class SqliteActualStatus(ActualStatusContract, unittest.TestCase):
    harness_factory = SqliteHarness


@unittest.skipUnless(os.environ.get("SWARM_TEST_CONFIG"), "throwaway Postgres required")
class PostgresActualStatus(ActualStatusContract, unittest.TestCase):
    harness_factory = staticmethod(lambda: PostgresHarness(os.environ["SWARM_TEST_CONFIG"]))

    def test_existing_schema26_views_upgrade_without_disrupting_old_clients(self):
        import contextlib
        from swarm.board import autoinit, base
        from swarm.board.postgres import STATUS_VIEW, _AGENT_STATUS_COLS
        from support import SMALL_POOL
        self.b.allocate_name('a', 'j', 'engineer')
        self.b.tool_started('a', 'Bash')
        self.b.agent_turn_ended('a')
        old_view = STATUS_VIEW.replace("         WHEN a.turn_ended_at IS NOT NULL THEN 'idle'\n", '')
        old_view = old_view.replace(', a.title,\n       a.turn_ended_at', ', a.title')
        old_view = old_view.replace("ELSE 'idle'\n       END AS shown_status", "ELSE 'active'\n       END AS shown_status")
        self.h.conn.execute(old_view.format(idle=5, dead=30, tool_timeout=60))
        self.h.conn.execute("UPDATE board_meta SET value='26' WHERE key='schema_version'")
        old_columns = _AGENT_STATUS_COLS.removesuffix(', turn_ended_at')
        self.assertEqual(self.h.conn.execute(f'SELECT {old_columns} FROM agent_status').fetchone()[3], 'running')
        with mock.patch.object(autoinit, 'enabled', return_value=True), \
             mock.patch.object(autoinit, 'stamp_path', return_value=None), \
             mock.patch.object(autoinit, 'setup_lock', return_value=contextlib.nullcontext()):
            result = autoinit.ensure_initialized(self.h.cfg, names=SMALL_POOL)
        self.assertEqual(result.action, 'initialized')
        self.assertGreater(base.SCHEMA_VERSION, 26)
        self.assert_status('idle')
        # The old explicit column contract still reads and writes ordinary work after upgrade.
        self.assertEqual(self.h.conn.execute(f'SELECT {old_columns} FROM agent_status').fetchone()[3], 'idle')
        self.b.post('j', self.b.active_agent_name('a'), 'old-client post')
        self.b.tool_started('a', 'Bash')
        self.assert_status('active')
