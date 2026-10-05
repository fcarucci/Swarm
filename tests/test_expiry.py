"""Jobs don't stay open forever: [job] stall_hours closes a job with no progress (failed), [job] orphan_minutes
closes one nobody works on (cancelled), and `swarm wait --for` bounds a wait
(Board.sweep_expiry, driven by swarm.sweep_jobs).

ExpiryContract runs on every backend (memory, sqlite, file; Postgres with SWARM_TEST_CONFIG).
ExpiryCliTests drive the CLI and the hooks end to end on SWARM_TEST_BACKEND.
"""
from __future__ import annotations

import os
import unittest

from support import FileHarness, MemoryHarness, PostgresHarness, SqliteHarness  # noqa: F401  (sets sys.path)
from test_hooks_cli import Env  # noqa: E402

from swarm import cli as swarm  # noqa: E402
from swarm.board import base  # noqa: E402

MIN = 60
HOUR = 3600
CAP, ORPHAN = 4, 30   # stall_hours, orphan_minutes   # the defaults


class ExpiryContract:
    harness_factory = None

    @classmethod
    def setUpClass(cls):
        cls.h = cls.harness_factory()

    @classmethod
    def tearDownClass(cls):
        cls.h.close()

    def setUp(self):
        self.h.reset()
        self.b = self.h.board()
        self.addCleanup(self.b.close)

    def job(self, name="j", age=0, agents=(), goal=None):
        """An open job whose run started `age` seconds ago, with agents (key -> seconds of
        silence; still active, never stopped)."""
        self.b.open_job(name, "d", None, None, "me", goal=goal)
        self.h.backdate_job(name, created_at=age + 60, activated_at=age)
        for key, quiet in dict(agents).items():
            self.b.allocate_name(key, name)
            self.h.backdate_agent(key, joined_at=max(age, quiet) + 1, last_seen=quiet)

    def sweep(self, cap=CAP, orphan=ORPHAN, watch=None):
        return self.b.sweep_expiry(cap, orphan, watch)

    def status(self, name="j"):
        return self.b.job_status(name)

    # ---- stall limit: no progress for N hours
    def test_stalled_job_is_closed_failed_even_with_live_agents(self):
        self.job(age=5 * HOUR, agents={"a": 0})   # heartbeats only: last_seen just now
        closed = self.sweep()
        self.assertEqual([c.job for c in closed], ["j"])
        s = self.status()
        self.assertEqual((s.status, s.closed_by, s.outcome),
                         ("failed", base.AUTO_CLOSED_BY, "auto-closed: no progress for 4 h"))
        self.assertIsNotNone(s.finished_at)
        self.assertEqual(self.b.agents("j", include_departed=False), [])   # left, as deactivate does
        self.assertEqual(self.sweep(), [])   # closed once

    def test_outcome_carries_the_last_verdict(self):
        # a met goal: the default limit applies
        self.job(age=5 * HOUR, agents={"j1": 0}, goal="ship it")
        self.assertTrue(self.b.claim_judge("j1", "j"))
        self.assertTrue(self.b.record_verdict("j", self.b.active_agent_name("j1"), "met", "tests pass", None))
        self.h.backdate_job("j", verdict_at=4 * HOUR + MIN)
        self.sweep()
        self.assertEqual(self.status().outcome, "auto-closed: no progress for 4 h; last verdict met: tests pass")

    def test_outcome_of_an_unmet_goal_carries_goal_not_met_and_the_last_verdict(self):
        # an unmet goal is only ever stalled by its own limit (here the job's own 4 h)
        self.job(age=5 * HOUR, agents={"j1": 0}, goal="ship it")
        self.b.set_job_max_hours("j", 4)
        self.assertTrue(self.b.claim_judge("j1", "j"))
        self.assertTrue(self.b.record_verdict("j", self.b.active_agent_name("j1"), "not_met",
                                              "tests fail", "fix them"))
        self.h.backdate_job("j", verdict_at=4 * HOUR + MIN)
        self.sweep()
        self.assertEqual((self.status().status, self.status().outcome),
                         ("failed", "auto-closed: no progress for 4 h; goal not met; last verdict not_met: tests fail"))

    def test_a_job_within_its_limit_stays_open(self):
        self.job(age=3 * HOUR, agents={"a": 0})
        self.assertEqual(self.sweep(), [])
        self.assertEqual(self.status().status, "active")

    def test_progress_keeps_an_old_job_open(self):
        # a message posted by an agent, a verdict, an agent joining: each restarts the clock
        self.job("posted", age=9 * HOUR, agents={"a": 0})
        self.h.backdate_message(self.b.post("posted", self.b.active_agent_name("a"), "found it").id, 2 * HOUR)
        self.job("judged", age=9 * HOUR, agents={"j1": 0}, goal="g")
        self.b.set_job_max_hours("judged", 4)   # an unmet goal is stalled by its own limit only
        self.b.claim_judge("j1", "judged")
        self.b.record_verdict("judged", self.b.active_agent_name("j1"), "not_met", "no", "fix")
        self.h.backdate_job("judged", verdict_at=3 * HOUR)
        self.job("joined", age=9 * HOUR, agents={"b": 0})
        self.h.backdate_agent("b", joined_at=HOUR)
        self.assertEqual(self.sweep(), [])
        for j in ("posted", "judged", "joined"):
            self.assertEqual(self.status(j).status, "active", j)
        # ...for the limit: 4 h after the progress it is stalled again
        self.h.backdate_job("judged", verdict_at=4 * HOUR + MIN)
        self.h.backdate_agent("b", joined_at=5 * HOUR)
        self.assertEqual(sorted(c.job for c in self.sweep()), ["joined", "judged"])

    def test_heartbeats_tool_calls_and_system_posts_are_not_progress(self):
        self.job(age=9 * HOUR, agents={"a": 0})
        self.b.tool_started("a", "Bash")   # a watcher polling in a loop
        self.b.tool_finished("a")
        self.h.backdate_agent("a", joined_at=9 * HOUR)
        self.b.post("j", "swarm", "memory from x for bank y failed")   # the system's own post
        self.assertEqual([c.job for c in self.sweep()], ["j"])

    def test_the_run_start_counts_as_progress(self):
        self.job(age=3 * HOUR)
        self.assertEqual(self.sweep(orphan=0), [])
        self.h.backdate_job("j", activated_at=5 * HOUR, created_at=5 * HOUR)
        self.assertEqual([c.job for c in self.sweep(orphan=0)], ["j"])

    def test_a_per_job_limit_overrides_the_default(self):
        self.job("short", age=2 * HOUR, agents={"a": 0})
        self.b.set_job_max_hours("short", 1.5)
        self.job("long", age=9 * HOUR, agents={"b": 0})
        self.b.set_job_max_hours("long", 10)
        self.job("never", age=99 * HOUR, agents={"c": 0})
        self.b.set_job_max_hours("never", 0)
        self.assertEqual([c.job for c in self.sweep()], ["short"])
        self.assertEqual(self.status("short").outcome, "auto-closed: no progress for 1.5 h")
        self.assertEqual(self.status("long").status, "active")
        self.assertEqual(self.status("never").status, "active")
        self.assertEqual(self.status("never").max_hours, 0)

    def test_default_limit_zero_is_off_and_reactivation_resets_the_override(self):
        self.job(age=99 * HOUR, agents={"a": 0})
        self.assertEqual(self.sweep(cap=0), [])
        self.b.set_job_max_hours("j", 7)
        self.assertEqual(self.status().max_hours, 7)
        self.b.open_job("j", None, None, None, None)
        self.assertIsNone(self.status().max_hours)

    def test_stall_limit_of_a_missing_job(self):
        self.assertFalse(self.b.set_job_max_hours("nope", 3))

    # ---- orphans
    def test_no_agents_and_quiet_is_cancelled(self):
        self.job(age=31 * MIN)
        self.assertEqual([c.job for c in self.sweep()], ["j"])
        s = self.status()
        self.assertEqual((s.status, s.closed_by, s.outcome),
                         ("cancelled", base.AUTO_CLOSED_BY, "auto-closed: no live agents for 30 min"))

    def test_not_before_orphan_minutes(self):
        self.job(age=20 * MIN)
        self.assertEqual(self.sweep(), [])

    def test_a_post_counts_as_activity(self):
        self.job(age=2 * HOUR)
        self.h.backdate_message(self.b.post("j", "Someone", "still around").id, 10 * MIN)
        self.assertEqual(self.sweep(), [])

    def test_a_live_agent_keeps_it_open(self):
        for state, quiet in (("started", 2 * MIN), ("idle", 10 * MIN)):
            with self.subTest(state=state):
                self.h.reset()
                self.job(age=2 * HOUR, agents={"a": quiet})
                self.assertEqual(next(a.status for a in self.b.agents("j")), state)
                self.assertEqual(self.sweep(), [])

    def test_dead_and_done_agents_do_not_keep_it_open(self):
        self.job(age=2 * HOUR, agents={"gone": 40 * MIN, "done": 40 * MIN})
        self.b.agent_stopped("done")
        self.h.backdate_agent("done", last_seen=40 * MIN, left_at=40 * MIN)
        self.assertEqual({a.status for a in self.b.agents("j")}, {"dead", "completed"})
        self.assertEqual([c.job for c in self.sweep()], ["j"])
        self.assertEqual(self.status().status, "cancelled")

    # ---- a job with a goal ends with the judge's met verdict (or a person), never in a sweep
    def test_an_unmet_goal_job_is_never_orphan_closed(self):
        # the regression: astroloom-m0 (goal, orchestrator waiting on a person, every subagent
        # finished) was cancelled with "no live agents for 30 min"
        self.job(age=2 * HOUR, goal="ship it")
        self.assertEqual(self.sweep(), [])
        self.assertEqual(self.sweep(orphan=1), [])
        self.assertEqual(self.status().status, "active")
        self.assertIsNone(self.status().outcome)

    def test_an_unmet_goal_job_is_never_orphan_closed_with_dead_agents_or_not_met_verdict(self):
        self.job(age=3 * HOUR, agents={"jj": 0, "dead": 3 * HOUR}, goal="ship it")
        self.assertTrue(self.b.claim_judge("jj", "j"))
        self.assertTrue(self.b.record_verdict("j", self.b.active_agent_name("jj"), "not_met", "no", "fix"))
        self.h.backdate_agent("jj", last_seen=3 * HOUR)
        self.assertEqual(self.sweep(), [])
        self.assertEqual(self.status().status, "active")

    def test_an_unmet_goal_job_is_stall_closed_only_by_its_own_explicit_limit_and_as_failed(self):
        self.job("default", age=99 * HOUR, agents={"a": 0}, goal="g")
        self.job("own", age=99 * HOUR, agents={"b": 0}, goal="g")
        self.b.set_job_max_hours("own", 1)
        self.assertEqual([c.job for c in self.sweep()], ["own"])
        self.assertEqual(self.status("default").status, "active")
        self.assertEqual((self.status("own").status, self.status("own").outcome),
                         ("failed", "auto-closed: no progress for 1 h; goal not met"))

    def test_goal_job_with_all_workers_completed_and_no_judge_stays_open(self):
        self.job(age=3 * HOUR, agents={"w1": 0, "w2": 0}, goal="ship it")
        for k in ("w1", "w2"):
            self.b.agent_stopped(k)   # completed
            self.h.backdate_agent(k, last_seen=3 * HOUR)
        self.assertEqual({a.status for a in self.b.agents("j")}, {"completed"})
        self.assertEqual(self.sweep(), [])
        self.assertEqual(self.b.sweep_auto_close(30), [])
        self.assertEqual(self.status().status, "active")

    def test_goal_job_stays_open_whatever_the_orchestrator_watch_says(self):
        class Gone:
            def __init__(self, job):
                pass

            def active(self):
                return False

        self.job(age=3 * HOUR, goal="ship it")
        self.assertEqual(self.sweep(watch=Gone), [])
        self.assertEqual(self.b.sweep_auto_close(30, Gone), [])
        self.assertEqual(self.status().status, "active")

    def test_goal_job_empty_for_hours_stays_open_and_anyone_can_resume_it(self):
        self.job(age=99 * HOUR, goal="ship it")
        self.h.backdate_job("j", created_at=100 * HOUR)
        self.assertEqual(self.sweep(), [])
        self.assertEqual(self.sweep(cap=0), [])
        self.assertEqual(self.status().status, "active")
        # no orchestrator: a new agent joins, takes the judge seat, and a met verdict is recorded
        self.b.allocate_name("late", "j")
        self.assertTrue(self.b.claim_judge("late", "j"))
        self.assertTrue(self.b.record_verdict("j", self.b.active_agent_name("late"), "met", "done", None))
        self.assertEqual(self.status().verdict, "met")

    def test_a_met_goal_job_is_still_swept_and_a_changed_goal_is_guarded_again(self):
        self.job(age=2 * HOUR, agents={"jj": 0}, goal="ship it")
        self.assertTrue(self.b.claim_judge("jj", "j"))
        self.assertTrue(self.b.record_verdict("j", self.b.active_agent_name("jj"), "met", "ok", None))
        self.h.backdate_agent("jj", last_seen=2 * HOUR)
        self.assertEqual([c.job for c in self.sweep()], ["j"])
        self.job("k", age=2 * HOUR)            # no goal: the orphan rule still applies
        self.assertEqual([c.job for c in self.sweep()], ["k"])
        self.job("m", age=2 * HOUR)
        self.b.set_job_goal("m", "now with a goal")
        self.assertEqual(self.sweep(), [])

    # ---- a goal set, or a verdict changed, between the read and the close is checked at the close
    def racing(self, change, when="after"):
        """self.b.job_status (the sweep's second read, right before the close) runs `change()`
        after it returned its row (when="after": the close then has stale data) or before it."""
        real = self.b.job_status

        def job_status(name):
            if when == "before":
                change()
            row = real(name)
            if when == "after":
                change()
            return row
        self.b.job_status = job_status
        self.addCleanup(delattr, self.b, "job_status")

    def test_a_goal_set_between_the_read_and_an_orphan_close_keeps_the_job_open(self):
        self.job(age=2 * HOUR)
        self.racing(lambda: self.b.set_job_goal("j", "now with a goal"))
        self.assertEqual(self.sweep(), [])
        self.assertEqual(self.status().status, "active")

    def test_a_goal_set_between_the_read_and_a_stall_close_keeps_the_job_open(self):
        self.job(age=9 * HOUR, agents={"a": 0})
        self.racing(lambda: self.b.set_job_goal("j", "now with a goal"))
        self.assertEqual(self.sweep(), [])
        self.assertEqual(self.status().status, "active")
        self.assertEqual([a.status for a in self.b.agents("j")], ["started"])   # nobody was made to leave

    def test_a_goal_set_before_the_second_read_keeps_the_job_open_too(self):
        self.job(age=2 * HOUR)
        self.racing(lambda: self.b.set_job_goal("j", "now with a goal"), when="before")
        self.assertEqual(self.sweep(), [])
        self.assertEqual(self.status().status, "active")

    def test_a_met_verdict_replaced_by_not_met_between_the_read_and_the_close_keeps_the_job_open(self):
        self.job(age=2 * HOUR, agents={"jj": 0}, goal="ship it")
        self.assertTrue(self.b.claim_judge("jj", "j"))
        judge = self.b.active_agent_name("jj")
        self.assertTrue(self.b.record_verdict("j", judge, "met", "ok", None))
        self.h.backdate_agent("jj", last_seen=2 * HOUR)
        self.racing(lambda: self.b.record_verdict("j", judge, "not_met", "regressed", "fix it"))
        self.assertEqual(self.sweep(), [])
        self.assertEqual((self.status().status, self.status().verdict), ("active", "not_met"))

    def test_progress_between_the_read_and_the_stall_close_of_a_goal_job_keeps_it_open(self):
        self.job(age=9 * HOUR, agents={"a": 0}, goal="ship it")
        self.b.set_job_max_hours("j", 4)
        name = self.b.active_agent_name("a")
        self.racing(lambda: self.b.post("j", name, "found it"))
        self.assertEqual(self.sweep(), [])
        self.assertEqual(self.status().status, "active")

    def test_the_own_limit_removed_between_the_read_and_a_goal_job_stall_close_keeps_it_open(self):
        self.job(age=9 * HOUR, agents={"a": 0}, goal="ship it")
        self.b.set_job_max_hours("j", 4)
        self.racing(lambda: self.b.set_job_max_hours("j", 0))     # `activate --stall-hours 0`
        self.assertEqual(self.sweep(), [])
        self.assertEqual(self.status().status, "active")

    def test_close_job_guard_is_atomic_with_the_close(self):
        CloseGuard = base.CloseGuard
        self.job("goalless")
        self.job("unmet", agents={"a": 0}, goal="g")
        self.job("own", goal="g")
        self.b.set_job_max_hours("own", 1)
        for guard in (CloseGuard(), CloseGuard(False, "other goal", None), CloseGuard(False, "g", 3)):
            self.assertFalse(self.b.close_job("unmet", "cancelled", "x", guard=guard))
        self.assertEqual((self.status("unmet").status, [a.status for a in self.b.agents("unmet")]),
                         ("active", ["started"]))   # nothing changed, nobody left
        self.assertFalse(self.b.close_job("own", "cancelled", "x", guard=CloseGuard()))
        self.assertFalse(self.b.close_job("nope", "cancelled", "x", guard=CloseGuard()))
        self.assertTrue(self.b.close_job("own", "failed", "x", guard=CloseGuard(False, "g", 1)))
        self.assertTrue(self.b.close_job("goalless", "cancelled", "x", guard=CloseGuard()))
        self.assertEqual(self.status("goalless").status, "cancelled")

    # ---- [job] goal_stall_hours: the opt-in backstop for a goal job
    def test_goal_stall_hours_closes_an_unmet_goal_job_as_failed(self):
        self.job("g", age=6 * HOUR, agents={"a": 0}, goal="ship it")
        self.assertEqual(self.sweep(), [])                       # off by default
        self.assertEqual(self.b.sweep_expiry(CAP, ORPHAN, None, goal_stall_hours=0), [])
        self.assertEqual(self.b.sweep_expiry(CAP, ORPHAN, None, goal_stall_hours=7), [])   # not long enough
        self.assertEqual([c.job for c in self.b.sweep_expiry(CAP, ORPHAN, None, goal_stall_hours=5)], ["g"])
        s = self.status("g")
        self.assertEqual((s.status, s.closed_by, s.outcome),
                         ("failed", base.AUTO_CLOSED_BY, "auto-closed: no progress for 5 h; goal not met"))

    def test_a_jobs_own_stall_hours_beat_goal_stall_hours(self):
        self.job("never", age=99 * HOUR, goal="g")
        self.b.set_job_max_hours("never", 0)     # its own: never
        self.job("own", age=3 * HOUR, goal="g")
        self.b.set_job_max_hours("own", 2)
        self.job("plain", age=3 * HOUR, goal="g")
        self.assertEqual(sorted(c.job for c in self.b.sweep_expiry(CAP, ORPHAN, None, goal_stall_hours=1)),
                         ["own", "plain"])
        self.assertEqual(self.status("never").status, "active")
        self.assertEqual(self.status("own").outcome, "auto-closed: no progress for 2 h; goal not met")

    def test_goal_stall_hours_spares_a_met_goal_and_progress(self):
        self.job("met", age=9 * HOUR, agents={"jj": 0}, goal="g")
        self.assertTrue(self.b.claim_judge("jj", "met"))
        self.assertTrue(self.b.record_verdict("met", self.b.active_agent_name("jj"), "met", "ok", None))
        self.h.backdate_job("met", verdict_at=9 * HOUR)
        self.job("busy", age=9 * HOUR, agents={"a": 0}, goal="g")
        self.h.backdate_message(self.b.post("busy", self.b.active_agent_name("a"), "working").id, MIN)
        # the met job falls under the ordinary limit (4 h), the busy goal job under neither
        self.assertEqual([c.job for c in self.b.sweep_expiry(CAP, ORPHAN, None, goal_stall_hours=1)], ["met"])
        self.assertEqual(self.status("busy").status, "active")

    # ---- a goal job nobody works on shows as waiting (goal not met), on every backend
    def shown(self, name="j", idle_minutes=5):
        js = self.b.job_status(name)
        word = base.derive_job_status(js, idle_minutes, self.b.now())
        view = self.h.shown_status(name) if hasattr(self.h, "shown_status") else word
        self.assertEqual(view, word, "the job_status view and derive_job_status disagree")
        return word

    def test_a_goal_job_with_no_live_agent_shows_waiting_goal_not_met(self):
        self.job("none", age=2 * HOUR, goal="g")                     # no agent at all
        self.job("done", age=2 * HOUR, agents={"w": 0}, goal="g")
        self.b.agent_stopped("w")                                    # all workers completed, no judge
        self.job("dead", age=2 * HOUR, agents={"d": 3 * HOUR}, goal="g")
        for j in ("none", "done", "dead"):
            self.assertEqual(self.shown(j), base.WAITING_GOAL, j)
            self.assertEqual(self.shown(j, idle_minutes=10000), base.WAITING_GOAL, j)   # not "idle"

    def test_the_shown_status_of_other_jobs_is_unchanged(self):
        self.job("goalless", age=2 * HOUR)
        self.assertEqual(self.shown("goalless"), "idle")
        self.job("live", age=2 * HOUR, agents={"a": 0}, goal="g")    # a started agent
        self.assertEqual(self.shown("live"), "active")
        self.job("quiet", age=2 * HOUR, agents={"i": 6 * MIN}, goal="g")   # an idle agent: nobody is gone yet
        self.assertEqual(self.shown("quiet"), "idle")
        self.job("waits", age=2 * HOUR, goal="g")
        self.b.set_waiting("waits", "the user")
        self.assertEqual(self.shown("waits"), "waiting")
        self.job("met", age=2 * HOUR, agents={"jj": 0}, goal="g")
        self.assertTrue(self.b.claim_judge("jj", "met"))
        self.assertTrue(self.b.record_verdict("met", self.b.active_agent_name("jj"), "met", "ok", None))
        self.b.agent_stopped("jj")
        self.assertEqual(self.shown("met"), "active")                # a met goal is not waiting (just active)
        self.b.close_job("met", "completed", "done")
        self.assertEqual(self.shown("met"), "completed")

    def test_a_joining_agent_takes_a_goal_job_out_of_waiting(self):
        self.job("j", age=2 * HOUR, goal="g")
        self.assertEqual(self.shown(), base.WAITING_GOAL)
        self.b.allocate_name("late", "j")
        self.assertEqual(self.shown(), "active")

    def test_orphan_rule_off_at_zero(self):
        self.job(age=2 * HOUR)
        self.assertEqual(self.sweep(orphan=0), [])

    def test_watch_of_the_orchestrating_session_keeps_it_open_but_not_past_the_limit(self):
        class Active:
            def __init__(self, job):
                pass

            def active(self):
                return True

        self.job("a", age=2 * HOUR)
        self.job("b", age=5 * HOUR)
        self.assertEqual([c.job for c in self.sweep(watch=Active)], ["b"])
        self.assertEqual(self.status("a").status, "active")

    def test_an_agent_joining_meanwhile_keeps_it_open(self):
        self.job(age=2 * HOUR)
        real = type(self.b).job_status
        joined = []

        def racing(board, job):
            if not joined:   # the sweep's re-read right before the close
                joined.append(board.allocate_name("late", job))
            return real(board, job)

        from unittest import mock
        with mock.patch.object(type(self.b), "job_status", racing):
            self.assertEqual(self.sweep(), [])
        self.assertEqual(self.status().status, "active")

    def test_closed_jobs_are_left_alone(self):
        self.job(age=9 * HOUR)
        self.b.close_job("j", "completed", "done by hand", closed_by="me")
        self.assertEqual(self.sweep(), [])
        self.assertEqual(self.status().outcome, "done by hand")

    # ---- bounded waits
    def test_a_waiting_job_is_covered_by_both_rules(self):
        self.job("orphaned", age=2 * HOUR)
        self.b.set_waiting("orphaned", "the user")
        self.job("old", age=5 * HOUR, agents={"a": 0})
        self.b.set_waiting("old", "the user")
        self.assertEqual(sorted(c.job for c in self.sweep()), ["old", "orphaned"])
        self.assertEqual((self.status("orphaned").status, self.status("old").status), ("cancelled", "failed"))
        self.assertIsNone(self.status("old").waiting_on)

    def test_a_bounded_wait_shields_from_the_orphan_rule_until_it_expires(self):
        self.job(age=2 * HOUR)
        until = self.b.now() + base._dt.timedelta(minutes=45)
        self.assertTrue(self.b.set_waiting("j", "CI", until))
        self.assertEqual(self.status().waiting_until, until)
        self.assertEqual(self.sweep(), [])
        self.assertEqual(self.status().waiting_on, "CI")
        self.h.backdate_job("j", waiting_until=10 * MIN)
        # expired: the wait is cleared, and the orphan clock restarts from its end
        self.assertEqual(self.sweep(), [])
        s = self.status()
        self.assertEqual((s.status, s.waiting_on, s.waiting_until), ("active", None, None))
        self.b.set_waiting("j", "again", self.b.now() + base._dt.timedelta(hours=1))
        self.h.backdate_job("j", waiting_until=31 * MIN)
        self.assertEqual([c.job for c in self.sweep()], ["j"])
        self.assertEqual(self.status().outcome, "auto-closed: no live agents for 30 min")

    def test_resume_and_close_clear_the_bound(self):
        self.job(age=MIN)
        until = self.b.now() + base._dt.timedelta(hours=1)
        self.b.set_waiting("j", "CI", until)
        self.b.set_waiting("j", None)
        self.assertIsNone(self.status().waiting_until)
        self.b.set_waiting("j", "CI", until)
        self.b.close_job("j", "completed", None)
        self.assertIsNone(self.status().waiting_until)


class MemoryExpiry(ExpiryContract, unittest.TestCase):
    harness_factory = MemoryHarness


class SqliteExpiry(ExpiryContract, unittest.TestCase):
    harness_factory = SqliteHarness


class FileExpiry(ExpiryContract, unittest.TestCase):
    harness_factory = FileHarness


@unittest.skipUnless(os.environ.get("SWARM_TEST_CONFIG"),
                     "set SWARM_TEST_CONFIG to a throwaway Postgres board config to run")
class PostgresExpiry(ExpiryContract, unittest.TestCase):
    harness_factory = staticmethod(lambda: PostgresHarness(os.environ["SWARM_TEST_CONFIG"]))

    def test_a_schema_12_board_gets_the_shown_status_column_on_upgrade(self):
        from support import SMALL_POOL, setup_board
        conn = self.h.conn
        conn.execute("DROP VIEW job_status")           # the schema-12 view: no shown_status
        conn.execute("CREATE VIEW job_status AS SELECT j.job, j.status FROM jobs j")
        conn.execute("UPDATE board_meta SET value = '12' WHERE key = 'schema_version'")
        self.assertEqual(type(self.b).schema_version(self.h.cfg), 12)
        setup_board(self.h.cfg, SMALL_POOL)              # what ensure_initialized runs for an older board
        self.assertEqual(type(self.b).schema_version(self.h.cfg), base.SCHEMA_VERSION)
        self.job("g", age=2 * HOUR, goal="ship it")
        self.assertEqual(self.h.shown_status("g"), base.WAITING_GOAL)


# --------------------------------------------------------------------------- CLI and hooks

class ExpiryCliTests(Env):
    def activate(self, job="J", *extra):
        rc, _, err = self.cli("activate", "--job", job, *extra)
        self.assertEqual(rc, 0, err)

    def job(self, job="J"):
        with self.board() as b:
            return b.job_status(job)

    def age(self, job="J", seconds=5 * HOUR):
        self.h.backdate_job(job, created_at=seconds + 60, activated_at=seconds)

    def test_defaults(self):
        self.assertEqual(self.cfg["job"]["stall_hours"], 4)
        self.assertEqual(self.cfg["job"]["orphan_minutes"], 30)

    def test_status_closes_an_old_job_and_all_shows_it(self):
        self.activate()
        self.hook("start", agent_id="agent-1")
        self.age()
        self.h.backdate_agent("agent-1", joined_at=5 * HOUR, last_seen=0)
        rc, out, _ = self.cli("status")
        self.assertIn("J: auto-closed: no progress for 4 h", out)
        s = self.job()
        self.assertEqual((s.status, s.closed_by), ("failed", "auto"))
        self.assertFalse(self.markers.joinpath("J.json").exists())
        _, out, _ = self.cli("status", "--all", "--no-color")
        row = next(line for line in out.splitlines() if line.startswith("J "))
        self.assertIn("failed", row)

    def test_activate_stall_hours(self):
        self.activate("J", "--stall-hours", "10")
        self.assertEqual(self.job().max_hours, 10)
        self.activate("K", "--max-hours", "0")   # the old name still works
        self.assertEqual(self.job("K").max_hours, 0)
        self.activate("L")
        self.assertIsNone(self.job("L").max_hours)
        rc, _, err = self.cli("activate", "--job", "M", "--stall-hours", "-1")
        self.assertEqual(rc, 2)
        self.assertIn("--stall-hours", err)

    def test_config_keys_apply(self):
        self.config.write_text(self.config.read_text() + "[job]\nstall_hours = 1\norphan_minutes = 0\n")
        self.cfg = swarm.load_config(self.config)
        self.activate()
        self.hook("start", agent_id="agent-1")
        self.h.backdate_agent("agent-1", joined_at=2 * HOUR, last_seen=0)
        self.age("J", 2 * HOUR)
        self.cli("purge")
        self.assertEqual(self.job().status, "failed")
        self.assertEqual(self.job().outcome, "auto-closed: no progress for 1 h")

    def test_purge_closes_an_orphan(self):
        self.activate()
        self.age("J", 40 * MIN)
        rc, out, _ = self.cli("purge")
        self.assertIn("J: auto-closed: no live agents for 30 min", out)
        self.assertEqual(self.job().status, "cancelled")

    # ---- a goal job is never closed by a sweep
    def test_a_goal_job_survives_every_sweep(self):
        self.activate("J", "--goal", "ship it")
        self.age("J", 40 * MIN)
        _, out, _ = self.cli("purge")
        self.assertNotIn("auto-closed", out)
        self.age("J", 99 * HOUR)
        self.cli("status")
        self.assertEqual(self.job().status, "active")

    def test_an_idle_goal_job_can_be_attached_to_by_any_session(self):
        self.activate("J", "--goal", "ship it")
        self.age("J", 99 * HOUR)
        self.markers.joinpath("J.json").unlink()      # the orchestrator's session is gone
        self.cli("purge")
        self.cli("status")
        self.assertEqual(self.job().status, "active")
        rc, _, err = self.cli("activate", "--job", "J", "--attach", "--session", "22222222-3333-4444-5555-666666666666")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.job().status, "active")

    def test_a_sweep_from_another_user_or_machine_spares_a_goal_job_and_closes_a_goalless_one_as_before(self):
        # a sweep with no marker of the jobs (another OS user's HOME, another machine's hooks):
        # the orphan rule's evidence is unchanged for a job without a goal, and never applies to a goal job
        self.activate("G", "--goal", "ship it")
        self.activate("P")
        for j in ("G", "P"):
            self.age(j, 40 * MIN)
        other = self.tmp / "other-user-markers"
        other.mkdir()
        self.config.write_text(self.config.read_text().replace(str(self.markers), str(other)))
        _, out, _ = self.cli("purge")
        self.assertEqual((self.job("G").status, self.job("P").status), ("active", "cancelled"))
        self.assertIn("P: auto-closed: no live agents for 30 min", out)

    def test_goal_stall_hours_in_the_config_closes_a_stalled_goal_job_as_failed(self):
        self.assertEqual(self.cfg["job"]["goal_stall_hours"], 0)   # never, by default
        self.config.write_text(self.config.read_text() + "[job]\ngoal_stall_hours = 6\n")
        self.activate("G", "--goal", "ship it")
        self.activate("O", "--goal", "ship it", "--stall-hours", "0")   # its own: never
        self.age("G", 5 * HOUR)
        self.age("O", 99 * HOUR)
        self.cli("purge")
        self.assertEqual(self.job("G").status, "active")           # within the limit
        self.age("G", 7 * HOUR)
        _, out, _ = self.cli("purge")
        self.assertEqual((self.job("G").status, self.job("G").outcome),
                         ("failed", "auto-closed: no progress for 6 h; goal not met"))
        self.assertIn("G: auto-closed: no progress for 6 h; goal not met", out)
        self.assertEqual(self.job("O").status, "active")

    # ---- a goal job nobody works on shows as waiting (goal not met) in status and watch
    def frame(self, job=None, compact=False):
        import shutil
        from unittest import mock
        view = {"offset": 0, "wrap": False, "max_offset": 0, "all_agents": False, "anchor": None,
                "scroll": 0, "mark": None, "page": 1, "recent_minutes": None, "compact": compact}
        with self.board() as b, mock.patch.object(shutil, "get_terminal_size", return_value=os.terminal_size((200, 40))):
            return "\n".join(swarm._watch_frame(b, job, 2.0, False, True, view))

    def test_status_and_watch_show_a_goal_job_without_agents_as_waiting_goal_not_met(self):
        self.activate("G", "--goal", "ship it")
        self.activate("P")                             # no goal: nothing new
        self.age("G", 2 * HOUR)
        self.age("P", 10 * MIN)                        # idle, but not yet an orphan
        _, out, _ = self.cli("status", "--no-color")
        row = next(ln for ln in out.splitlines() if ln.startswith("G "))
        self.assertRegex(row, r"^G\s+waiting \(goal not met\)\s")
        self.assertRegex(next(ln for ln in out.splitlines() if ln.startswith("P ")), r"^P\s+idle\s")
        _, out, _ = self.cli("status", "--job", "G", "--no-color")
        self.assertIn("job        G  [waiting (goal not met)]", out)
        self.assertIn("not auto-closed", out)
        self.assertIn("job        P  [idle]", self.cli("status", "--job", "P", "--no-color")[1])
        self.assertIn("waiting (goal not met)", self.frame())              # watch: all jobs
        self.assertIn("[waiting (goal not met)]", self.frame("G"))         # watch --job
        self.assertIn("G [waiting (goal not met)]", self.frame(compact=True))
        self.cli("purge")
        self.assertEqual(self.job("G").status, "active")                    # shown as waiting, never closed
        # an agent joining takes it out of waiting
        with self.board() as b:
            b.allocate_name("newcomer", "G")
        self.assertRegex(next(ln for ln in self.cli("status", "--no-color")[1].splitlines() if ln.startswith("G ")),
                         r"^G\s+active\s")

    def test_wait_for_bounds_the_wait(self):
        self.activate()
        rc, out, _ = self.cli("wait", "--job", "J", "--for", "90m", "--on", "the", "review")
        self.assertEqual(rc, 0)
        self.assertIn("J is waiting on: the review (for up to 1h30m; ", out)
        with self.board() as b:
            js = b.job_status("J")
            self.assertAlmostEqual((js.waiting_until - b.now()).total_seconds(), 90 * MIN, delta=30)
        rc, _, err = self.cli("wait", "--job", "J", "--for", "soon", "--on", "x")
        self.assertEqual(rc, 2)
        self.assertIn("--for", err)

    def test_wait_for_units(self):
        self.assertEqual(swarm.parse_duration("45"), 45 * MIN)   # bare number: minutes
        self.assertEqual(swarm.parse_duration("45m"), 45 * MIN)
        self.assertEqual(swarm.parse_duration("2h"), 2 * HOUR)
        self.assertEqual(swarm.parse_duration("1h30m"), 90 * MIN)
        self.assertEqual(swarm.parse_duration("30s"), 30)
        for bad in ("", "0", "soon", "-5m", "1x"):
            with self.assertRaises(ValueError):
                swarm.parse_duration(bad)

    def test_a_queued_bounded_wait_is_delivered_with_its_bound(self):
        import time
        from swarm import spool
        self.activate()
        spool.spool_wait(self.cfg, "J", "CI", time.time() + 3600)
        with self.board() as b:
            spool.flush_spool(b, self.cfg)
            js = b.job_status("J")
            self.assertEqual(js.waiting_on, "CI")
            self.assertAlmostEqual((js.waiting_until - b.now()).total_seconds(), 3600, delta=30)

    def test_a_failing_expiry_sweep_does_not_break_the_caller(self):
        from unittest import mock
        self.activate()
        with mock.patch("swarm.board.base.Board.sweep_expiry", side_effect=RuntimeError("boom")):
            rc, _, _ = self.cli("status")
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
