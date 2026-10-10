"""`swarm watch` without a polling timer: client-side ageing, incremental refresh on a wake, a cheap
fingerprint check instead of a periodic full snapshot, and a visible degraded mark.

The Postgres cases need $SWARM_TEST_CONFIG (a throwaway database); they count the statements a
watch issues, on a fake clock, so the measured rates are deterministic.
"""
from __future__ import annotations

import datetime as dt
import os
import unittest
from dataclasses import replace
from unittest import mock

from support import MemoryHarness, PostgresHarness, SMALL_POOL  # noqa: F401  (sets sys.path)

from swarm import cli  # noqa: E402
from swarm import snapshot as snap  # noqa: E402
from swarm import watchdata  # noqa: E402
from swarm.board import AgentStatus  # noqa: E402

MIN = dt.timedelta(minutes=1)


def agent(**kw) -> AgentStatus:
    now = dt.datetime(2026, 1, 1, 12, 0, tzinfo=dt.timezone.utc)
    base = dict(job="J", name="N", role=None, status="running", current_tool=None, tool_calls=1, messages=0,
                joined_at=now - 60 * MIN, last_contact_at=now, last_post_at=None, ended_at=None, host=None,
                agent_key="k")
    return AgentStatus(**{**base, **kw})


CFG = {"idle_minutes": 5, "dead_minutes": 30, "tool_timeout_minutes": 60}


class AgeingTests(unittest.TestCase):
    def test_a_quiet_agent_goes_idle_then_dead_as_the_clock_moves(self):
        a = agent()
        t0 = a.last_contact_at
        self.assertEqual(watchdata.age_agent(a, t0 + 4 * MIN, CFG).status, "running")
        self.assertEqual(watchdata.age_agent(a, t0 + 6 * MIN, CFG).status, "idle")
        self.assertEqual(watchdata.age_agent(a, t0 + 31 * MIN, CFG).status, "dead")

    def test_strict_comparison_like_the_view(self):
        a = agent()
        self.assertEqual(watchdata.age_agent(a, a.last_contact_at + 5 * MIN, CFG).status, "running")

    def test_an_agent_in_a_tool_call_is_not_aged_and_finished_ones_stay(self):
        self.assertEqual(watchdata.age_agent(agent(current_tool="Bash"), dt.datetime(2026, 1, 1, 14, tzinfo=dt.timezone.utc),
                                             CFG).status, "running")
        for status in ("completed", "left", "dead"):
            a = agent(status=status)
            self.assertEqual(watchdata.age_agent(a, a.last_contact_at + 90 * MIN, CFG).status, status)

    def test_idle_becomes_dead_and_never_the_other_way(self):
        a = agent(status="idle")
        self.assertEqual(watchdata.age_agent(a, a.last_contact_at + 2 * MIN, CFG).status, "idle")
        self.assertEqual(watchdata.age_agent(a, a.last_contact_at + 31 * MIN, CFG).status, "dead")

    def test_replay_ages_a_snapshot_between_refreshes(self):
        h = MemoryHarness("watch-age")
        h.reset()
        b = h.board()
        b.open_job("J", "d", None, None, "t")
        b.allocate_name("k", "J")
        rec = cli._Recorder(b)
        self.assertEqual(rec.watch_agents("J", 10)[0][0].status, "started")
        replay = cli._Replay(rec)
        with mock.patch.object(cli.time, "monotonic", return_value=rec.taken_mono + 6 * 60):
            self.assertEqual(replay.watch_agents("J", 10)[0][0].status, "idle")
        with mock.patch.object(cli.time, "monotonic", return_value=rec.taken_mono + 31 * 60):
            self.assertEqual(replay.watch_agents("J", 10)[0][0].status, "dead")
        self.assertEqual(rec.watch_agents("J", 10)[0][0].status, "started")    # the snapshot itself is untouched


class FakeSnapshot:
    """A snapshot with job/agent/message rows, for the merge test (no database)."""


class MergeTests(unittest.TestCase):
    def snap(self, jobs, messages, board=None):
        row = [dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc), jobs, [], messages, [], {}, {}, [], {}, []]
        class B:
            cfg = {"board": {}}
            degraded = None
            change_mode = "push"
        return watchdata.SnapshotBoard(board or B(), row)

    def job(self, name):
        return {"job": name, "status": "active", "description": None, "task": None, "outcome": None,
                "created_by": None, "session_id": None, "created_at": "2026-01-01T00:00:00+00:00",
                "activated_at": None, "finished_at": None, "agents": 0, "started": 0, "running": 0, "idle": 0,
                "completed": 0, "dead_or_left": 0, "messages": 0, "last_activity_at": None}

    def msg(self, i, job="A"):
        return {"id": i, "created_at": "2026-01-01T00:00:00+00:00", "job": job, "agent_name": "x",
                "to_agent": None, "message": f"m{i}"}

    def test_incremental_messages_merge_into_the_base_and_trim_per_job(self):
        base = self.snap([self.job("A"), self.job("B")], [self.msg(1), self.msg(2), self.msg(3, "B")])
        fresh = self.snap([self.job("A"), self.job("B")], [self.msg(4), self.msg(5)])
        merged = fresh.merged(base, 3)
        self.assertEqual([m.id for m in merged.message_rows], [2, 3, 4, 5][-4:])   # A: 2,4,5 trimmed to 3; B: 3
        self.assertEqual(merged.max_message_id(), 5)

    def test_a_job_that_left_the_scope_drops_its_messages(self):
        base = self.snap([self.job("A"), self.job("B")], [self.msg(1), self.msg(2, "B")])
        fresh = self.snap([self.job("A")], [])
        self.assertEqual([m.id for m in fresh.merged(base, 5).message_rows], [1])

    def test_new_jobs_are_reported(self):
        base = self.snap([self.job("A")], [])
        fresh = self.snap([self.job("A"), self.job("B")], [])
        self.assertEqual(fresh.new_jobs(base), {"B"})


class FakeClock:
    t = 0.0

    def __call__(self):
        return self.t


@unittest.skipUnless(os.environ.get("SWARM_TEST_CONFIG"), "set SWARM_TEST_CONFIG to a throwaway database")
class PostgresWatchSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.h = PostgresHarness(os.environ["SWARM_TEST_CONFIG"])

    @classmethod
    def tearDownClass(cls):
        cls.h.close()

    def setUp(self):
        self.h.reset()
        self.writer = self.h.board()
        self.addCleanup(self.writer.close)
        self.writer.open_job("J", "d", None, "S", "t")
        self.name = self.writer.allocate_name("k", "J")
        self.writer.post("J", self.name, "first", agent_key="k")
        self.board = self.h.board()
        self.addCleanup(self.board.close)
        self.stmts: list[str] = []
        real = self.board._conn.execute

        def spy(query, *a, **k):
            self.stmts.append(str(query).strip().split(None, 1)[0].upper() + (" fp" if "md5(" in str(query) else ""))
            return real(query, *a, **k)
        self.board._conn.execute = spy
        self.clock = FakeClock()
        self.src = watchdata.SnapshotSource(self.board, None, None, 60, clock=self.clock, check_s=60.0)

    def post(self, text):
        self.writer.post("J", self.name, text, agent_key="k")

    def test_incremental_equals_a_full_read(self):
        self.src.read(10, "full")
        for i in range(5):
            self.post(f"more {i}")
            inc = self.src.read(10, "incremental")
        full = self.board.watch_snapshot(None, None, 10, 60)
        self.assertEqual([m.id for m in inc.message_rows], [m.id for m in full.message_rows])
        self.assertEqual(inc.job_rows, full.job_rows)
        self.assertEqual(inc.agent_rows, full.agent_rows)

    def test_a_matching_check_reads_nothing_more_and_a_mismatch_reads_once(self):
        self.src.read(10, "full")
        del self.stmts[:]
        self.assertIsNone(self.src.read(10, "check"))
        self.assertEqual(self.stmts, ["WITH fp"])        # the one cheap statement
        self.src.base.message_rows.clear()             # a simulated bug: the held state lost its messages
        del self.stmts[:]
        repaired = self.src.read(10, "check")
        self.assertEqual(self.stmts, ["WITH fp", "WITH"])  # fingerprint, then exactly one full read
        self.assertEqual([m.message for m in repaired.message_rows], ["first"])
        self.assertEqual(self.src.counts["full"], 2)

    def test_a_silent_change_is_repaired_within_one_check(self):
        self.src.read(10, "full")
        self.post("no notification reached us")
        self.clock.t = 61
        got = self.src.read(10, "check")
        self.assertIn("no notification reached us", [m.message for m in got.message_rows])

    def test_a_new_job_in_scope_forces_a_full_read(self):
        self.src.read(10, "full")
        self.writer.open_job("J2", "d", None, "S", "t")
        k2 = self.writer.allocate_name("k2", "J2")
        self.writer.post("J2", k2, "second job", agent_key="k2")
        got = self.src.read(10, "incremental")
        self.assertEqual({j.job for j in got.job_rows}, {"J", "J2"})
        self.assertEqual([m.message for m in got.message_rows if m.job == "J2"], ["second job"])

    def test_measured_statement_rates_idle_and_busy(self):
        """The watch's refresh plan on a fake clock: 10 quiet minutes, then 4 busy ones. Statements
        per second are asserted and printed (the numbers in the CHANGELOG / report)."""
        gate = cli._RefreshPlan(interval=10, check_s=60, minimum=2, clock=self.clock)
        def run(seconds, notify_every=None):
            n0, t0 = len(self.stmts), self.clock.t
            last_notify = t0
            while self.clock.t < t0 + seconds:
                self.clock.t += 0.5
                changed = False
                if notify_every and self.clock.t - last_notify >= notify_every:
                    self.post(f"busy {self.clock.t}")
                    last_notify, changed = self.clock.t, True
                kind = gate.next(self.clock.t, changed, False, True)
                if kind:
                    self.src.read(10, kind)
                    gate.refreshed(self.clock.t)
            return (len(self.stmts) - n0) / seconds
        idle = run(600)
        busy = run(240, notify_every=2.4)
        print(f"\nwatch statements/s: idle {idle:.4f} (was 0.10 with the 10 s timer), "
              f"busy {busy:.3f} with one notification per 2.4 s (same order as before, but each is an "
              f"incremental read of the new rows only: {self.src.counts})")
        self.assertLessEqual(idle, 1 / 55)            # one fingerprint a minute
        self.assertLessEqual(busy, 0.5)
        self.assertEqual(self.src.counts["full"], 1)     # never a full snapshot while it all matched


class PlanTests(unittest.TestCase):
    def test_plan_kinds(self):
        c = FakeClock()
        g = cli._RefreshPlan(interval=10, check_s=60, minimum=2, clock=c)
        self.assertEqual(g.next(0, False, False, True), "full")            # the first draw
        g.refreshed(0)
        self.assertIsNone(g.next(30, False, False, True))                  # quiet: nothing
        self.assertEqual(g.next(61, False, False, True), "check")          # the periodic backstop
        g.refreshed(61)
        self.assertEqual(g.next(62, True, False, True), None)              # throttled to min_redraw
        self.assertEqual(g.next(63.5, True, False, True), "incremental")
        g.refreshed(63.5)
        self.assertEqual(g.next(70, False, True, True), "full")            # the view lacks data (SnapshotMiss)

    def test_polling_modes_keep_the_interval_timer(self):
        g = cli._RefreshPlan(interval=10, check_s=60, minimum=2, clock=FakeClock())
        g.next(0, False, False, False)
        g.refreshed(0)
        self.assertEqual(g.next(11, False, False, False), "full")          # no push: refresh as before

    def test_leaving_degraded_mode_is_a_full_read(self):
        g = cli._RefreshPlan(interval=10, check_s=60, minimum=2, clock=FakeClock())
        g.next(0, False, False, False)
        g.refreshed(0)
        self.assertEqual(g.next(1, False, False, True), "full")            # push is back


class DegradedMarkTests(unittest.TestCase):
    def test_polling_is_shown(self):
        self.assertIn("polling", cli.polling_notice(2.0))


if __name__ == "__main__":
    unittest.main()
