"""Compact panes keep live workers visible as finished workers accumulate."""
import datetime as dt
import os
from unittest import mock

from support import ROOT  # noqa: F401
from swarm import cli as swarm
from test_hooks_cli import Env

SESSION = "11111111-aaaa-bbbb-cccc-000000000001"


class CompactWorkersTests(Env):
    def setUp(self):
        super().setUp()
        self.cli("activate", "--job", "J", "--session", SESSION)
        with self.board() as b:
            self.now = b.now()
            self.names = {}
            for i in range(16):
                key = f"w{i}"
                self.names[key] = b.allocate_name(key, "J", "engineer")
                if i < 3:
                    b.post("J", self.names[key], "working")
                    b.tool_started(key, f"Bash: task-{i}")
        for i in range(3):
            self.h.update_agent(f"w{i}", last_post_at=self.now - dt.timedelta(minutes=2))
        for i in range(3, 15):
            self.h.update_agent(f"w{i}", state="completed", left_at=self.now)
        self.h.backdate_agent("w15", last_seen=2 * 3600)

    def frame(self, compact=True, all_agents=False):
        view = dict(compact=compact, session=SESSION, offset=0, wrap=False,
                    max_offset=0, all_agents=all_agents, recent_minutes=10)
        with self.board() as b, mock.patch.object(type(b), "now", return_value=self.now), mock.patch(
                "shutil.get_terminal_size", return_value=os.terminal_size((250, 80))):
            return swarm._watch_frame(b, None, 2, False, True, view)

    def agent_lines(self, lines):
        header = lines[:lines.index("MESSAGES")] if "MESSAGES" in lines else lines
        return [line for line in header if any(name in line for name in self.names.values())]

    def test_three_live_rows_and_one_finished_rollup_instead_of_sixteen_rows(self):
        for all_agents in (False, True):
            with self.subTest(all_agents=all_agents):
                lines = self.frame(all_agents=all_agents)
                self.assertEqual(len(self.agent_lines(lines)), 3)
                rollups = [line.strip() for line in lines if "WORKERS:" in line]
                self.assertEqual(rollups, ["WORKERS: 3 working, 0 waiting, 12 finished, 1 lost"])
        self.assertEqual(len(self.agent_lines(self.frame(compact=False, all_agents=True))), 16)

    def test_live_rows_show_current_tool_launch_and_post_age(self):
        self.h.update_agent("w0", title="build")
        for i, line in enumerate(self.agent_lines(self.frame())):
            self.assertIn(f"Bash: task-{i}", line)
            self.assertIn("post 2m ago", line)
            self.assertNotIn("ago ago", line)
        self.assertIn("(build)", self.agent_lines(self.frame())[0])

    def test_idle_rows_are_hidden_while_waiting_and_rollup_remain_visible(self):
        self.h.update_agent("w0", current_tool=None)
        self.h.backdate_agent("w0", last_seen=10 * 60)
        with self.board() as b:
            b.post("J", self.names["w1"], "waiting for CI")
            b.post("J", self.names["w2"], "DONE task")
        for key in ("w1", "w2"):
            self.h.update_agent(key, current_tool=None)
            self.h.backdate_agent(key, last_seen=10 * 60)
        self.h.update_agent("w3", state="left")
        lines = self.frame()
        live = self.agent_lines(lines)
        self.assertEqual(len(live), 1)
        self.assertNotIn(self.names['w0'], '\n'.join(live))
        self.assertIn(" waiting ", live[0])
        self.assertTrue(any("1 idle (no reason given)" in line for line in lines))
        self.assertTrue(any(self.names['w0'] in line and ' idle ' in line
                            for line in self.agent_lines(self.frame(compact=False, all_agents=True))))
        self.assertTrue(any("13 finished, 1 lost" in line for line in lines))

    def test_all_idle_job_and_messages_remain_without_idle_rows_or_state_changes(self):
        with self.board() as board:
            for i in range(3):
                board.agent_turn_ended(f'w{i}')
            before = board.agents('J')
        for all_agents in (False, True):
            with self.subTest(all_agents=all_agents):
                lines = self.frame(all_agents=all_agents)
                self.assertTrue(any(line.startswith('J [idle]') for line in lines))
                self.assertEqual(self.agent_lines(lines), [])
                self.assertTrue(any('0 working, 0 waiting, 3 idle (no reason given), 12 finished, 1 lost'
                                    in line for line in lines))
                self.assertIn('MESSAGES', lines)
                self.assertTrue(any('working' in line for line in lines[lines.index('MESSAGES') + 1:]))
        with self.board() as board:
            self.assertEqual(board.agents('J'), before)

    def test_started_and_running_rows_stay_visible_when_idle_row_is_hidden(self):
        with self.board() as board:
            self.names['started'] = board.allocate_name('started', 'J', 'engineer')
            board.agent_turn_ended('w0')
        lines = self.agent_lines(self.frame())
        self.assertEqual(len(lines), 3)
        self.assertTrue(any(self.names['started'] in line and ' started ' in line for line in lines))
        self.assertEqual(sum(' running ' in line for line in lines), 2)
        self.assertFalse(any(self.names['w0'] in line for line in lines))

    def test_old_finished_agents_are_counted_without_hidden_rows(self):
        for i in range(3, 15):
            self.h.backdate_agent(f"w{i}", left_at=3600, last_seen=3600)
        lines = self.frame()
        self.assertEqual(len(self.agent_lines(lines)), 3)
        self.assertTrue(any("12 finished, 1 lost" in line for line in lines))
        self.assertFalse(any("older hidden" in line for line in lines))

    def test_each_job_gets_its_own_rollup_and_no_individual_finished_rows(self):
        self.cli("activate", "--job", "K", "--session", SESSION)
        with self.board() as b:
            finished = b.allocate_name("k0", "K", "engineer")
        self.h.update_agent("k0", state="completed", left_at=self.now)
        lines = self.frame()
        k = next(i for i, line in enumerate(lines) if line.startswith("K [idle]"))
        self.assertEqual(lines[k + 1].strip(), "WORKERS: 0 working, 0 waiting, 1 finished, 0 lost")
        self.assertFalse(any(finished in line for line in lines))
        self.assertEqual(sum("WORKERS:" in line for line in lines), 2)

    def test_live_agent_without_a_post_shows_never(self):
        self.h.update_agent("w0", last_post_at=None)
        self.assertIn("post never", self.agent_lines(self.frame())[0])

    def test_compact_snapshot_fetch_does_not_discard_old_finished_agents(self):
        board, source = mock.Mock(), mock.Mock()
        view = dict(compact=True, recent_minutes=10, all_agents=False, session=SESSION)
        swarm._watch_board(board, view, None, source, "full")
        source.read.assert_called_once_with(None, "full")
        swarm._watch_board(board, view, None, None, "full")
        board.watch_snapshot.assert_called_once_with(None, SESSION, None, 60)

    def test_identical_board_state_does_not_redraw_for_wall_clock(self):
        real_datetime = dt.datetime

        class Later(real_datetime):
            @classmethod
            def now(cls, tz=None):
                return real_datetime(2026, 10, 16, 12, 0, 1, tzinfo=tz)

        first = self.frame()
        with mock.patch("datetime.datetime", Later):
            self.assertEqual(first, self.frame())
