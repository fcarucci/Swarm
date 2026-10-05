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
        # (a met verdict: a job whose goal is not met is never closed by the sweep, see below)
        self.job(age=5 * HOUR, agents={"j1": 0}, goal="ship it")
        self.assertTrue(self.b.claim_judge("j1", "j"))
        self.assertTrue(self.b.record_verdict("j", self.b.active_agent_name("j1"), "met", "tests pass", None))
        self.h.backdate_job("j", verdict_at=4 * HOUR + MIN)
        self.sweep()
        self.assertEqual(self.status().outcome, "auto-closed: no progress for 4 h; last verdict met: tests pass")

    def test_a_job_within_its_limit_stays_open(self):
        self.job(age=3 * HOUR, agents={"a": 0})
        self.assertEqual(self.sweep(), [])
        self.assertEqual(self.status().status, "active")

    def test_progress_keeps_an_old_job_open(self):
        # a message posted by an agent, a verdict, an agent joining: each restarts the clock
        self.job("posted", age=9 * HOUR, agents={"a": 0})
        self.h.backdate_message(self.b.post("posted", self.b.active_agent_name("a"), "found it").id, 2 * HOUR)
        self.job("judged", age=9 * HOUR, agents={"j1": 0}, goal="g")
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
        self.assertEqual(sorted(c.job for c in self.sweep()), ["joined"])   # "judged" has a goal: never
        self.assertEqual(self.status("judged").status, "active")

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

    def test_goal_job_stays_open_when_the_sweep_cannot_see_or_the_orchestrator_is_gone(self):
        class Blind:
            def __init__(self, job):
                pass

            def can_tell(self):
                return False

            def active(self):
                return False

        self.job(age=3 * HOUR, goal="ship it")
        self.assertEqual(self.sweep(watch=Blind), [])
        self.assertEqual(self.b.sweep_auto_close(30, Blind), [])
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

    def test_a_watch_that_cannot_tell_never_closes(self):
        class Blind:
            def __init__(self, job):
                pass

            def can_tell(self):
                return False

            def active(self):
                return False

        self.job(age=2 * HOUR)
        self.assertEqual(self.sweep(watch=Blind), [])
        self.assertEqual(self.status().status, "active")

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

    # ---- a goal job is never closed by a sweep; "no live agents" needs evidence the orchestrator is gone
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

    def test_a_sweep_that_cannot_see_the_orchestrator_does_not_orphan_close(self):
        # another OS user's (or machine's) hooks sweep too, and have no marker of this job
        self.activate()
        self.age("J", 40 * MIN)
        self.markers.joinpath("J.json").unlink()
        self.cli("purge")
        self.assertEqual(self.job().status, "active")

    def test_a_board_outage_in_the_hook_log_keeps_the_job_open(self):
        import time
        self.activate()
        self.age("J", 40 * MIN)
        self.error_log.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(self.error_log.parent, 0o700)
        old = time.strftime("%F %T", time.localtime(time.time() - 3 * HOUR))
        self.error_log.write_text(f"{old} turn a1: OperationalError: no reply from the server\n")
        self.cli("purge")
        self.assertEqual(self.job().status, "cancelled")           # an old outage proves nothing
        self.activate()                                              # reopen
        self.age("J", 40 * MIN)
        now = time.strftime("%F %T", time.localtime(time.time() - 5 * MIN))
        self.error_log.write_text(f"{now} turn a1: OperationalError: no reply from the server\n")
        self.cli("purge")
        self.assertEqual(self.job().status, "active")

    def test_a_live_orchestrator_session_keeps_the_job_open(self):
        import json
        from swarm import liveness
        sid = "11111111-2222-3333-4444-555555555555"
        cfgdir = self.tmp / "claude"
        (cfgdir / "sessions").mkdir(parents=True)
        os.environ["CLAUDE_CONFIG_DIR"] = str(cfgdir)
        self.addCleanup(os.environ.pop, "CLAUDE_CONFIG_DIR", None)
        self.activate("J", "--session", sid)
        self.age("J", 40 * MIN)
        reg = cfgdir / "sessions" / f"{os.getpid()}.json"
        reg.write_text(json.dumps({"pid": os.getpid(), "sessionId": sid,
                                   "procStart": liveness._proc_start(os.getpid())}))
        self.assertTrue(liveness.claude_session_alive(sid))
        self.cli("purge")
        self.assertEqual(self.job().status, "active")   # blocked on a question: no tool call, still alive
        # the process is gone (or the pid was recycled): nobody is there
        gone = cfgdir / "sessions" / "999999.json"
        reg.unlink()
        gone.write_text(json.dumps({"pid": 999999, "sessionId": sid, "procStart": "1"}))
        self.assertFalse(liveness.claude_session_alive(sid))
        self.cli("purge")
        self.assertEqual(self.job().status, "cancelled")

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
