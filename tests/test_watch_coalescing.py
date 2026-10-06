"""Notification bursts, snapshot misses and periodic refresh use a numeric clock."""
import datetime as dt
import re
import unittest
from unittest import mock
from support import MemoryHarness
from swarm import cli
from swarm.board.postgres import PostgresBoard
from swarm.watchdata import SnapshotBoard
from swarm.review import artifact_verdicts, verdict_data
from types import SimpleNamespace

class CoalescingTests(unittest.TestCase):
    def test_snapshot_keeps_artifact_verdicts_without_querying_the_live_board(self):
        now = dt.datetime.now(dt.timezone.utc)
        data = verdict_data(None, 'plan:1', 'met', 'reviewed', None, 'Judge', now)
        import json
        board = SimpleNamespace(cfg=cli.DEFAULTS, degraded=None)
        snap = SnapshotBoard(board, (now, [], [], [], [], {}, {}, [], {'J': data, 'empty': None}))
        self.assertEqual(artifact_verdicts(snap, 'J')['plan:1']['verdict'], 'met')
        self.assertEqual(snap.job_data('missing'), {})
        self.assertEqual(snap.job_data('empty'), {})

    def test_notify_burst_does_not_reset_the_deadline(self):
        gate=cli._RefreshGate(10,2)
        self.assertTrue(gate.due(0))
        gate.refreshed(0)
        for n in range(1,20):
            self.assertFalse(gate.due(n/10, True))
        self.assertTrue(gate.due(2))
        gate.refreshed(2)
        self.assertFalse(gate.due(11.99))
        self.assertTrue(gate.due(12))

    def test_min_redraw_bounds_short_intervals_and_snapshot_misses(self):
        gate=cli._RefreshGate(.1,2)
        gate.refreshed(0)
        self.assertFalse(gate.due(1.9,True))
        self.assertTrue(gate.due(2))

    def test_custom_window_and_quiet_interval(self):
        gate=cli._RefreshGate(20,3)
        gate.refreshed(0)
        self.assertFalse(gate.due(2,True))
        self.assertTrue(gate.due(3))
        gate.refreshed(3)
        self.assertFalse(gate.due(22.9))
        self.assertTrue(gate.due(23))

    def test_plain_session_watch_keys_and_notifications_share_refresh_gate(self):
        import io
        h = MemoryHarness("watch-frequency")
        self.addCleanup(h.close)
        h.reset()
        board = h.board()
        self.addCleanup(board.close)
        board.open_job("shown", None, None, "S", None)
        now = [0.0]
        reads = []
        real = board.session_shown_jobs
        def read(session):
            reads.append(now[0])
            return real(session)
        view = {'session': 'S', 'min_redraw': 2, 'offset': 0}
        def refresh():
            return cli._take_snapshot(board, lambda b: cli.session_jobs(b, 'S'), view, None)
        def wait(timeout):
            now[0] = round(now[0] + .1, 1)
            return True
        keys = iter(['l'] * 45 + ['q'])
        with mock.patch.object(board, 'session_shown_jobs', side_effect=read), \
             mock.patch.object(board, 'wait_for_change', side_effect=wait), \
             mock.patch.object(board, 'subscribe'), \
             mock.patch.object(cli, '_watch_loop_threaded', side_effect=AssertionError('plain watch must coalesce without selector')), \
             mock.patch.object(cli.time, 'monotonic', side_effect=lambda: now[0]), \
             mock.patch.object(cli, '_read_keys', side_effect=lambda fd: next(keys)):
            cli._watch_loop(board, io.StringIO(), None, .1, view,
                            lambda snap: [','.join(j.job for j in cli.session_jobs(snap, 'S')[0])], refresh)
        self.assertEqual(reads, [0.0, 2.0, 4.0])

    def test_threaded_session_watch_coalesces_notifications(self):
        # Drive the real refresh worker with a numeric clock, then let its error
        # terminate the selector loop. No wall-clock delay or load generator.
        import io
        h = MemoryHarness("watch-thread-frequency")
        self.addCleanup(h.close)
        h.reset()
        board = h.board()
        self.addCleanup(board.close)
        board.open_job('shown', None, None, 'S', None)
        now, reads = [0.0], []
        real = board.session_shown_jobs
        def read(session):
            reads.append(now[0])
            return real(session)
        def wait(timeout):
            now[0] = round(now[0] + .1, 1)
            if now[0] > 4.6:
                raise RuntimeError('clock finished')
            return True
        view = {'session': 'S', 'min_redraw': 2}
        def refresh():
            return cli._take_snapshot(board, lambda b: cli.session_jobs(b, 'S'), view, None)
        with mock.patch.object(board, 'session_shown_jobs', side_effect=read), \
             mock.patch.object(board, 'wait_for_change', side_effect=wait), \
             mock.patch.object(cli.time, 'monotonic', side_effect=lambda: now[0]):
            with self.assertRaisesRegex(RuntimeError, 'clock finished'):
                cli._watch_loop_threaded(board, io.StringIO(), None, .1, view,
                                         lambda snap: ['shown'], refresh)
        self.assertEqual(reads, [.1, 2.1, 4.1])

    def test_postgres_draw_has_one_round_trip(self):
        board=object.__new__(PostgresBoard)
        board.cfg=cli.DEFAULTS
        board.degraded=None
        board._conn=mock.Mock()
        board._conn.execute.return_value.fetchone.return_value=(dt.datetime.now(dt.timezone.utc),[],[],[],[],{}, {}, [], {})
        snap=board.watch_snapshot(None,'session',10,60)
        self.assertIsInstance(snap,SnapshotBoard)
        board._conn.execute.assert_called_once()
        query,params=board._conn.execute.call_args.args
        self.assertEqual(query.count('%s'),len(params))
        self.assertIn('GROUP BY a.agent_key',query)
        self.assertNotIn('(SELECT count(*) FROM messages',query)

    def test_config_defaults_and_cli_override(self):
        self.assertEqual(cli.DEFAULTS['board']['watch_interval_s'],10)
        self.assertEqual(cli.DEFAULTS['board']['watch_min_redraw_s'],2)
        self.assertIsNone(cli._parser().parse_args(['watch']).interval)
        self.assertEqual(cli._parser().parse_args(['watch','--interval','4']).interval,4)

import os
from support import PostgresHarness

@unittest.skipUnless(os.environ.get('SWARM_TEST_CONFIG'),'needs throwaway PostgreSQL')
class PostgresSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.h=PostgresHarness(os.environ['SWARM_TEST_CONFIG'])
        self.addCleanup(self.h.close)
        self.h.reset()
        self.board=self.h.board(); self.addCleanup(self.board.close)
        self.board.ensure_job('J')
        self.board.bind_job_session('J','snapshot-session')
        self.name=self.board.allocate_name('agent-a','J')
        self.board.post('J',self.name,'hello')

    def test_job_detail_keeps_artifact_verdicts_in_the_single_statement_snapshot(self):
        import json
        data = verdict_data(None, 'plan:1', 'met', 'reviewed', None, 'Judge', self.board.now())
        for key, value in json.loads(data).items():
            self.board.set_job_data('J', key, value)
        expected = cli.job_detail(self.board, 'J', False, include_agents=False)
        with mock.patch.object(self.board._conn, 'execute', wraps=self.board._conn.execute) as execute:
            snap = self.board.watch_snapshot('J', None, 10, 1)
            actual = cli.job_detail(snap, 'J', False, include_agents=False)
            self.assertEqual(execute.call_count, 1)
        self.assertIn('artifact   plan:1: met by Judge', actual)
        self.assertEqual(actual, expected)

    def test_full_and_compact_frames_match_and_one_statement_per_snapshot(self):
        for compact in (False,True):
            view={'offset':0,'max_offset':0,'wrap':False,'all_agents':False,'anchor':None,
                  'scroll':0,'mark':None,'page':1,'recent_minutes':10,'db_label':None,
                  'session':'snapshot-session','compact':compact,'idle_exit':None}
            expected=cli._watch_frame(self.board,None,10,False,False,dict(view))
            with mock.patch.object(self.board._conn,'execute',wraps=self.board._conn.execute) as execute:
                rec=cli._take_snapshot(self.board,lambda b: cli._watch_frame(b,None,10,False,False,dict(view)),view,None)
                self.assertEqual(execute.call_count,1)
            actual=cli._watch_frame(cli._Replay(rec),None,10,False,False,dict(view))
            # The title's wall clock may cross a second while the snapshot query runs.
            clock = r"\d{2}:\d{2}:\d{2}"
            self.assertEqual(re.sub(clock, '', actual[0]), re.sub(clock, '', expected[0]))
            self.assertEqual(actual[1:], expected[1:])

    def test_session_shown_rollups_are_bounded_by_displayed_jobs(self):
        # A long-lived session has much more history than the dashboard displays.
        c = self.board._conn
        c.execute("INSERT INTO jobs (job, session_id, status, finished_at) "
                  "SELECT 'history-' || n, 'snapshot-session', 'completed', now() - interval '1 day' "
                  "FROM generate_series(1, 143) n")
        c.execute("INSERT INTO messages (job, agent_name, message) "
                  "SELECT job, 'X', 'history' FROM jobs WHERE job LIKE 'history-%'")
        self.board.open_job('K', None, None, 'snapshot-session', None)
        self.board.post('K', 'X', 'live')
        for expected in (['J', 'K'], ['K']):
            with mock.patch.object(self.board._conn, 'execute', wraps=c.execute) as execute:
                shown = self.board.session_shown_jobs('snapshot-session')
                query, params = execute.call_args.args
            self.assertEqual([j.job for j in shown], expected)
            plan = c.execute('EXPLAIN (ANALYZE, FORMAT JSON) ' + query, params).fetchone()[0][0]['Plan']
            self.assertEqual(plan['Actual Rows'], len(expected))
            def scans(node):
                if node.get('Relation Name') == 'messages':
                    yield node
                for child in node.get('Plans', []):
                    yield from scans(child)
            message_scans = list(scans(plan))
            self.assertTrue(message_scans)
            for scan in message_scans:
                self.assertEqual(scan['Actual Loops'], len(expected))
            self.board.close_job('J', 'completed', None)
            self.board.close_job('K', 'completed', None)
            c.execute("UPDATE jobs SET finished_at = now() + interval '1 second' WHERE job = 'K'")

    def test_hidden_rows_do_not_receive_message_counts(self):
        self.board.allocate_name('old','J'); self.board.agent_stopped('old')
        self.h.backdate_agent('old',left_at=7200,last_seen=7200)
        snap=self.board.watch_snapshot(None,'snapshot-session',10,60)
        rows,hidden=snap.watch_agents('J',10)
        self.assertEqual(hidden,1)
        self.assertEqual([a.agent_key for a in rows],['agent-a'])
        self.assertEqual(rows[0].messages,1)
        self.assertEqual(snap.jobs()[0].agents,2)

    def test_show_all_and_final_session_state(self):
        self.board.agent_stopped('agent-a')
        self.h.backdate_agent('agent-a',left_at=7200,last_seen=7200)
        self.board.close_job('J','completed','test')
        snap=self.board.watch_snapshot(None,'snapshot-session',None,60)
        self.assertEqual(snap.session_jobs('snapshot-session')[0].status,'completed')
        self.assertEqual(len(snap.watch_agents('J',None)[0]),1)
