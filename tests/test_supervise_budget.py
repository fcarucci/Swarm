"""budget.decide: every rail, in order."""
from __future__ import annotations

import datetime as dt
import unittest

from support import base_config

from swarm.board import AgentStatus, Restart
from swarm.supervisor import budget
from swarm.supervisor.settings import settings

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 9, 27, 12, 0, tzinfo=UTC)
M = dt.timedelta(minutes=1)
SUP = settings(base_config())
_ids = iter(range(1, 10_000))


def r(root="root", old=None, at_min=100, ran=10, cap=60.0, reason="stuck:dead", ended=True,
      outcome="completed", job="J"):
    at = NOW - at_min * M
    return Restart(next(_ids), job, root, 1, at, reason, old or f"old{next(_ids)}", "new",
                   "claude", "h", "u", cap, at + ran * M if ended else None,
                   outcome if ended else None)


def decide(**kw):
    base = dict(lineage="root", job_restarts=[], host_restarts=[], closed_at=NOW - 60 * M,
                now=NOW, running=0)
    base.update(kw)
    return budget.decide(SUP, **base)


class DayRowsTests(unittest.TestCase):
    """The daily cap counts every run that overlaps today."""

    def test_rows_overlapping_the_day(self):
        day = NOW - 30 * M
        before = r(at_min=100, ran=10)                      # ended before the day began
        across = r(at_min=40, ran=20)                       # began before, ended after
        running = r(at_min=40, ended=False)                 # began before, still running
        today = r(at_min=10, ran=5)
        self.assertEqual(budget.day_rows([before, across, running, today], day), [across, running, today])

    def test_a_run_across_midnight_counts_at_its_reserved_cap(self):
        day = NOW - 5 * M
        running = r(at_min=10, ended=False, cap=60.0)
        d = decide(host_restarts=budget.day_rows([running], day))
        self.assertEqual(d.minutes, 60.0)
        sup = dict(SUP, daily_restart_minutes=100)
        d = budget.decide(sup, lineage="root", job_restarts=[], host_restarts=budget.day_rows([running], day),
                          closed_at=NOW - 60 * M, now=NOW, running=0)
        self.assertEqual(d.minutes, 40.0)


class DecideTests(unittest.TestCase):
    def test_first_restart_goes_after_two_minutes(self):
        d = decide(closed_at=NOW - 1 * M)
        self.assertEqual((d.go, d.cap, d.not_before), (False, None, NOW + 1 * M))
        d = decide(closed_at=NOW - 2 * M)
        self.assertEqual((d.go, d.attempt, d.minutes), (True, 1, 60.0))

    def test_backoff_grows_2_10_30(self):
        one = [r(at_min=15, ran=5)]
        self.assertTrue(decide(job_restarts=one, host_restarts=one).go)
        one = [r(at_min=12, ran=5)]
        self.assertFalse(decide(job_restarts=one, host_restarts=one).go)
        sup = dict(SUP, max_restarts_per_agent=5)
        two = [r(at_min=60, ran=5), r(at_min=40, ran=5)]
        self.assertTrue(budget.decide(sup, lineage="root", job_restarts=two,
                                      host_restarts=two, closed_at=NOW - 60 * M,
                                      now=NOW, running=0).go)
        three = two + [r(at_min=30, ran=5)]
        self.assertFalse(budget.decide(sup, lineage="root", job_restarts=three,
                                       host_restarts=three, closed_at=NOW - 60 * M,
                                       now=NOW, running=0).go)

    def test_agent_budget(self):
        two = [r(), r(), r()]
        d = decide(job_restarts=two, host_restarts=two)
        self.assertEqual((d.go, d.cap), (False, "agent"))
        self.assertIn("3/3", d.why)

    def test_agent_cap_resets_after_rolling_24_hours(self):
        old = [r(at_min=25 * 60), r(at_min=26 * 60), r(at_min=27 * 60)]
        self.assertTrue(decide(job_restarts=old, host_restarts=[]).go)

    def test_job_budget(self):
        six = [r(root=f"x{i}") for i in range(6)]
        d = decide(job_restarts=six, host_restarts=six)
        self.assertEqual((d.go, d.cap), (False, "job"))

    def test_job_minutes(self):
        used = [r(root="a", ran=60), r(root="b", ran=60), r(root="c", ran=58)]
        d = decide(job_restarts=used, host_restarts=used)
        self.assertEqual((d.go, d.cap), (False, "job_minutes"))

    def test_minutes_cut_to_what_is_left(self):
        used = [r(root="a", ran=60), r(root="b", ran=90, cap=90)]
        d = decide(job_restarts=used, host_restarts=used)
        self.assertEqual((d.go, d.minutes), (True, 30.0))

    def test_daily_minutes_across_jobs(self):
        day = [r(root=f"d{i}", job=f"K{i}", ran=60) for i in range(8)]
        d = decide(host_restarts=day)
        self.assertEqual((d.go, d.cap), (False, "daily_minutes"))

    def test_running_restart_from_previous_local_date_counts_toward_daily_cap(self):
        previous_date = NOW.astimezone().date() - dt.timedelta(days=1)
        at = dt.datetime.combine(previous_date, dt.time.min,
                                 tzinfo=NOW.astimezone().tzinfo)
        running = r(at_min=(NOW - at).total_seconds() / 60, cap=120, ended=False)
        sup = dict(SUP, daily_restart_minutes=120)
        d = budget.decide(sup, lineage="root", job_restarts=[],
                          host_restarts=[running], closed_at=NOW - 60 * M,
                          now=NOW, running=0)
        self.assertEqual((d.go, d.cap), (False, "daily_minutes"))

    def test_running_charged_by_elapsed_and_refused_free(self):
        running = r(at_min=20, ended=False)
        refused = r(ran=0, outcome="refused")
        self.assertAlmostEqual(budget.charged_minutes(running, NOW), 20.0)
        self.assertEqual(budget.charged_minutes(refused, NOW), 0.0)

    def test_running_restart_reserves_its_whole_cap_in_decisions(self):
        just_started = r(at_min=1, cap=60, ended=False)
        sup = dict(SUP, daily_restart_minutes=100)
        d = budget.decide(sup, lineage="root", job_restarts=[], host_restarts=[just_started],
                          closed_at=NOW - 60 * M, now=NOW, running=0)
        self.assertEqual((d.go, d.minutes), (True, 40.0))   # not 99: the running one may use 60
        self.assertEqual(budget.reserved_minutes(just_started, NOW), 60.0)
        self.assertEqual(budget.reserved_minutes(r(ran=10), NOW), 10.0)

    def test_concurrency(self):
        d = decide(closed_at=NOW - 10 * M, running=2)
        self.assertEqual((d.go, d.cap), (False, None))
        self.assertIn("2/2", d.why)

    def test_outage_death_restarted_once_without_backoff(self):
        start = NOW - 50 * M
        d = decide(closed_at=NOW - 0.5 * M, outage_started=start)
        self.assertTrue(d.go)
        again = [r(at_min=5, ran=1, reason="outage")]
        d = decide(job_restarts=again, host_restarts=again,
                   closed_at=NOW - 0.5 * M, outage_started=start)
        self.assertEqual((d.go, d.cap), (False, "outage"))

    def test_backoff_after_failed_attempt(self):
        failed = [r(at_min=3, ran=0.1, outcome="failed")]
        d = decide(job_restarts=failed, host_restarts=failed, closed_at=NOW - 3 * M)
        self.assertEqual((d.go, d.cap), (False, None))


class LineageTests(unittest.TestCase):
    def _a(self, key, resume_of=None):
        t = NOW
        return AgentStatus("J", "N", None, "left", None, 0, 0, t, t, None, t,
                           "h", key, "claude", None, "u", "stuck:dead", resume_of)

    def test_follows_resume_of_to_the_root(self):
        by = {k: self._a(k, p) for k, p in (("a", None), ("b", "a"), ("c", "b"))}
        self.assertEqual(budget.lineage_root("c", by), "a")

    def test_purged_predecessor_stops_at_the_recorded_key(self):
        by = {"c": self._a("c", "gone")}
        self.assertEqual(budget.lineage_root("c", by), "gone")

    def test_cycle_does_not_hang(self):
        by = {"a": self._a("a", "b"), "b": self._a("b", "a")}
        self.assertIn(budget.lineage_root("a", by), ("a", "b"))
