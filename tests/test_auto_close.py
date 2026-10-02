"""Jobs close by themselves once every agent is done and the job has been quiet for
[job] auto_close_minutes (Board.sweep_auto_close, driven by swarm.sweep_jobs).

AutoCloseContract runs on every backend (memory, sqlite, file; Postgres with SWARM_TEST_CONFIG).
AutoCloseCliTests drive the CLI and the hooks end to end on SWARM_TEST_BACKEND.
"""
from __future__ import annotations

import datetime as dt
import os
import threading
import time
import unittest
from unittest import mock

from support import FileHarness, MemoryHarness, PostgresHarness, SqliteHarness  # noqa: F401  (sets sys.path)
from test_hooks_cli import Env  # noqa: E402

from swarm import cli as swarm  # noqa: E402
from swarm import hooks as swarm_hooks  # noqa: E402
from swarm.board import base  # noqa: E402

MIN = 60
WINDOW = 30             # auto_close_minutes used by the contract tests
QUIET = 31 * MIN        # older than the window


class AutoCloseContract:
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

    # ---- helpers
    def run_job(self, job: str = "j", keys=("a", "b", "c"), post: str | None = "all done, see PR"):
        """An activated job whose agents all joined, (optionally) posted, and completed."""
        self.b.open_job(job, "d", None, None, "me")
        names = {k: self.b.allocate_name(k, job) for k in keys}
        ids = []
        if post:
            ids.append(self.b.post(job, names[keys[-1]], post).id)
        for k in keys:
            self.b.agent_stopped(k)
        return names, ids

    def age(self, job: str, keys, ids, seconds: float = QUIET, active=()):
        """Move everything the job did `seconds` into the past: departed agents `keys`, still
        active agents `active` (their left_at stays unset), messages `ids`."""
        self.h.backdate_job(job, created_at=seconds + 60, activated_at=seconds + 60)
        for k in keys:
            self.h.backdate_agent(k, joined_at=seconds + 30, last_seen=seconds, left_at=seconds)
        for k in active:
            self.h.backdate_agent(k, joined_at=seconds + 30, last_seen=seconds)
        for i in ids:
            self.h.backdate_message(i, seconds)

    def sweep(self, minutes: float = WINDOW):
        return self.b.sweep_auto_close(minutes)

    # ---- closing
    def test_closes_after_the_quiet_period(self):
        names, ids = self.run_job()
        self.age("j", "abc", ids)
        closed = self.sweep()
        self.assertEqual([c.job for c in closed], ["j"])
        s = self.b.job_status("j")
        self.assertEqual((s.status, s.closed_by), ("completed", base.AUTO_CLOSED_BY))
        self.assertIsNotNone(s.finished_at)
        self.assertEqual(s.outcome, f"auto-closed: 3/3 agents completed; last post {names['c']}: all done, see PR")
        self.assertEqual(closed[0].outcome, s.outcome)
        self.assertEqual(self.sweep(), [])   # idempotent: closed once

    def test_outcome_without_posts_and_long_post_is_cut(self):
        self.run_job("quiet", keys=("a",), post=None)
        self.age("quiet", "a", [])
        long_names, long_ids = self.run_job("long", keys=("b",), post="x" * 190)
        self.age("long", "b", long_ids)
        self.assertEqual(sorted(c.job for c in self.sweep()), ["long", "quiet"])
        self.assertEqual(self.b.job_status("quiet").outcome, "auto-closed: 1/1 agents completed; no posts")
        outcome = self.b.job_status("long").outcome
        self.assertTrue(outcome.endswith("…"), outcome)
        self.assertLessEqual(len(outcome), base.AUTO_CLOSE_OUTCOME_MAX)

    def test_not_before_the_window(self):
        _, ids = self.run_job()
        self.age("j", "abc", ids, seconds=20 * MIN)
        self.assertEqual(self.sweep(), [])
        self.assertEqual(self.b.job_status("j").status, "active")

    def test_a_started_running_or_idle_agent_keeps_it_open(self):
        for state in ("started", "running", "idle"):
            with self.subTest(state=state):
                self.h.reset()
                _, ids = self.run_job(keys=("a", "b"))
                self.b.allocate_name("w", "j")
                self.age("j", "ab", ids)
                if state == "running":   # a long tool call: no contact for ages, still running
                    self.b.tool_started("w", "Bash")
                    self.h.backdate_agent("w", joined_at=QUIET, last_seen=QUIET, tool_started_at=QUIET)
                elif state == "idle":
                    self.h.backdate_agent("w", joined_at=QUIET, last_seen=10 * MIN)
                else:
                    self.h.backdate_agent("w", joined_at=QUIET, last_seen=2 * MIN)
                self.assertEqual(next(a.status for a in self.b.agents("j") if a.agent_key == "w"), state)
                self.assertEqual(self.sweep(), [])
                self.assertEqual(self.b.job_status("j").status, "active")

    def test_a_dead_agent_does_not_keep_it_open_and_is_reported(self):
        names, ids = self.run_job(keys=("a", "b"))
        self.b.allocate_name("gone", "j")    # never stopped: no hook contact for > dead_minutes
        self.age("j", "ab", ids, active=["gone"])
        self.assertEqual(next(a.status for a in self.b.agents("j") if a.agent_key == "gone"), "dead")
        self.assertEqual([c.job for c in self.sweep()], ["j"])
        s = self.b.job_status("j")
        self.assertTrue(s.outcome.startswith("auto-closed: 2/3 agents completed, 1 dead; last post "), s.outcome)
        self.assertEqual(self.b.agents("j", include_departed=False), [])   # it left with the job

    def test_left_agents_count_as_done(self):
        _, ids = self.run_job(keys=("a",))
        self.b.allocate_name("b", "j")
        self.b.leave(agent_key="b")
        self.age("j", "ab", ids)
        self.assertEqual([c.job for c in self.sweep()], ["j"])
        self.assertTrue(self.b.job_status("j").outcome.startswith("auto-closed: 1/2 agents completed, 1 left;"))

    def test_only_dead_agents_or_no_agents_keep_it_open(self):
        self.b.open_job("empty", None, None, None, None)
        self.h.backdate_job("empty", created_at=QUIET, activated_at=QUIET)
        self.b.open_job("j", None, None, None, None)
        self.b.allocate_name("gone", "j")
        self.age("j", [], [], active=["gone"])
        self.assertEqual(self.sweep(), [])
        self.assertEqual({j.job: j.status for j in self.b.jobs()}, {"empty": "active", "j": "active"})

    def test_a_new_message_resets_the_window(self):
        names, ids = self.run_job()
        self.age("j", "abc", ids)
        self.b.post("j", "Orchestrator", "one more thing")
        self.assertEqual(self.sweep(), [])

    def test_a_join_resets_the_window(self):
        names, ids = self.run_job()
        self.age("j", "abc", ids)
        self.b.allocate_name("late", "j")
        self.b.agent_stopped("late")          # came and went 0 minutes ago
        self.assertEqual(self.sweep(), [])
        self.assertEqual(self.b.job_status("j").status, "active")

    def test_zero_minutes_disables(self):
        _, ids = self.run_job()
        self.age("j", "abc", ids, seconds=10 * QUIET)
        self.assertEqual(self.sweep(0), [])
        self.assertEqual(self.b.job_status("j").status, "active")

    def test_waiting_jobs_and_unmet_goals_stay_open(self):
        _, ids = self.run_job("w")
        self.age("w", "abc", ids)
        self.b.set_waiting("w", "the user")
        self.b.open_job("g", None, None, None, None, goal="it works")
        judge = self.b.allocate_name("x", "g")
        self.assertTrue(self.b.claim_judge("x", "g"))
        self.b.record_verdict("g", judge, "not_met", "tests fail")
        self.b.agent_stopped("x")
        self.age("g", "x", [])
        self.assertEqual(self.sweep(), [])     # a not_met verdict is no better than none
        self.b.allocate_name("x", "g")        # the judge comes back and is satisfied
        self.assertTrue(self.b.claim_judge("x", "g"))
        self.b.record_verdict("g", judge, "met", "tests pass")
        self.b.agent_stopped("x")
        self.age("g", "x", [])
        self.assertEqual([c.job for c in self.sweep()], ["g"])

    def test_the_orchestrator_watch_keeps_or_reverts_it(self):
        # the sweep asks this machine's view of the orchestrating session before the close
        # (active: keep it open) and after it (at work during the close: revert, not reported)
        class Watch:
            def __init__(self, active, touched):
                self._active, self.touched, self.jobs = active, touched, []

            def __call__(self, job):
                self.jobs.append(job)
                return self

            def active(self):
                return self._active

            def after_close(self, undo):
                return bool(undo()) if self.touched else False

        _, ids = self.run_job()
        self.age("j", "abc", ids)
        busy = Watch(True, False)
        self.assertEqual(self.b.sweep_auto_close(WINDOW, busy), [])
        self.assertEqual((busy.jobs, self.b.job_status("j").status), (["j"], "active"))
        run = self.b.job_status("j").activated_at
        self.assertEqual(self.b.sweep_auto_close(WINDOW, Watch(False, True)), [])
        s = self.b.job_status("j")
        self.assertEqual((s.status, s.finished_at, s.closed_by, s.activated_at),
                         ("active", None, None, run))   # reverted: the same run
        self.assertEqual([c.job for c in self.sweep()], ["j"])   # and it closes when quiet
        _, ids = self.run_job("k")
        self.age("k", "abc", ids)
        self.assertEqual([c.job for c in self.b.sweep_auto_close(WINDOW, Watch(False, False))], ["k"])

    def test_undo_auto_close_keeps_the_run_the_agents_and_the_verdict(self):
        self.b.open_job("g", None, None, None, None, goal="it works")
        judge = self.b.allocate_name("x", "g")
        self.assertTrue(self.b.claim_judge("x", "g"))
        self.b.record_verdict("g", judge, "met", "tests pass")
        self.b.agent_stopped("x")
        self.age("g", "x", [])
        before = self.b.job_status("g")
        agents = [(a.agent_key, a.status, a.ended_at) for a in self.b.agents("g")]
        self.assertEqual([c.job for c in self.sweep()], ["g"])
        closed = self.b.job_status("g")
        self.assertFalse(self.b.undo_auto_close("g", closed.finished_at - dt.timedelta(seconds=1)))
        self.assertTrue(self.b.undo_auto_close("g", closed.finished_at))
        s = self.b.job_status("g")
        self.assertEqual((s.status, s.finished_at, s.outcome, s.closed_by), ("active", None, None, None))
        self.assertEqual((s.activated_at, s.verdict, s.verdict_reason), (before.activated_at, "met", "tests pass"))
        self.assertEqual([(a.agent_key, a.status, a.ended_at) for a in self.b.agents("g")], agents)
        self.assertFalse(self.b.undo_auto_close("g", closed.finished_at))   # not closed now
        self.assertEqual([c.job for c in self.sweep()], ["g"])            # the same run closes again

    def test_undo_auto_close_never_undoes_a_later_close(self):
        _, ids = self.run_job()
        self.age("j", "abc", ids)
        self.sweep()
        at = self.b.job_status("j").finished_at
        self.b.close_job("j", "completed", "the real summary", closed_by="me")   # deactivate
        self.assertFalse(self.b.undo_auto_close("j", at))
        s = self.b.job_status("j")
        self.assertEqual((s.status, s.outcome, s.closed_by), ("completed", "the real summary", "me"))
        self.b.open_job("j", None, None, None, None)                        # re-activated
        self.assertFalse(self.b.undo_auto_close("j", at))
        self.assertEqual(self.b.job_status("j").status, "active")

    def test_closed_jobs_are_left_alone(self):
        _, ids = self.run_job()
        self.b.close_job("j", "failed", "broke")
        self.age("j", "abc", ids)
        self.assertEqual(self.sweep(), [])
        self.assertEqual((self.b.job_status("j").status, self.b.job_status("j").outcome), ("failed", "broke"))

    # ---- reopening and overwriting
    def test_reopen_after_auto_close(self):
        _, ids = self.run_job()
        self.age("j", "abc", ids, seconds=3 * QUIET)
        self.sweep()
        self.b.open_job("j", None, None, None, "me")
        s = self.b.job_status("j")
        self.assertEqual((s.status, s.outcome, s.finished_at, s.closed_by), ("active", None, None, None))
        self.assertEqual(self.sweep(), [])
        # even long after, the previous run's agents don't close the reopened job...
        self.h.backdate_job("j", activated_at=QUIET)
        self.assertEqual(self.sweep(), [])
        # ...but this run's do, once they are done and quiet
        self.b.allocate_name("new", "j")
        self.b.agent_stopped("new")
        self.age("j", ["new"], [])
        self.assertEqual([c.job for c in self.sweep()], ["j"])
        self.assertTrue(self.b.job_status("j").outcome.startswith("auto-closed: 1/1 agents completed"))

    def test_close_job_overwrites_the_auto_outcome(self):
        _, ids = self.run_job()
        self.age("j", "abc", ids)
        self.sweep()
        finished = self.b.job_status("j").finished_at
        self.assertTrue(self.b.close_job("j", "completed", "shipped the fix", closed_by="the-orchestrator"))
        s = self.b.job_status("j")
        self.assertEqual((s.status, s.outcome, s.closed_by), ("completed", "shipped the fix", "the-orchestrator"))
        self.assertEqual(s.finished_at, finished)   # it ended when it ended
        self.assertTrue(self.b.close_job("j", "failed", "actually broken"))
        self.assertEqual((self.b.job_status("j").status, self.b.job_status("j").outcome),
                         ("failed", "actually broken"))

    # ---- concurrency
    def test_concurrent_sweeps_close_once(self):
        for i in range(3):
            _, ids = self.run_job(f"j{i}", keys=(f"a{i}",))
            self.age(f"j{i}", [f"a{i}"], ids)
        n = 6
        results, errors = [], []
        barrier = threading.Barrier(n)

        def worker():
            try:
                with self.h.board() as b:
                    barrier.wait()
                    results.extend(c.job for c in b.sweep_auto_close(WINDOW))
            except Exception as exc:  # pragma: no cover - reported below
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
        self.assertEqual(errors, [])
        self.assertEqual(sorted(results), ["j0", "j1", "j2"])

    def test_auto_close_job_is_compare_and_set(self):
        _, ids = self.run_job()
        self.age("j", "abc", ids)
        before = self.b.now() - base_timedelta(WINDOW)
        with self.h.board() as other:
            at = self.b.auto_close_job("j", before, "auto-closed: first")
            self.assertIsNone(other.auto_close_job("j", before, "auto-closed: second"))
        s = self.b.job_status("j")
        self.assertEqual((s.outcome, s.finished_at), ("auto-closed: first", at))   # its own timestamp

    def test_a_later_run_closed_between_our_close_and_our_revert_is_not_undone(self):
        _, ids = self.run_job()
        self.age("j", "abc", ids)
        ours = self.b.auto_close_job("j", self.b.now() - base_timedelta(WINDOW), "auto-closed: ours")
        self.assertIsNotNone(ours)
        # meanwhile another process re-activates it, the new run finishes and auto-closes too
        self.b.open_job("j", None, None, None, None)
        self.b.allocate_name("d", "j")
        self.b.agent_stopped("d")
        self.age("j", "d", [])
        theirs = self.b.auto_close_job("j", self.b.now() - base_timedelta(WINDOW), "auto-closed: theirs")
        self.assertIsNotNone(theirs)
        self.assertNotEqual(ours, theirs)
        self.assertFalse(self.b.undo_auto_close("j", ours))   # our revert: too late, not theirs
        s = self.b.job_status("j")
        self.assertEqual((s.status, s.outcome, s.finished_at), ("completed", "auto-closed: theirs", theirs))

    def test_the_sweep_reverts_only_its_own_close(self):
        # the sweep's close, then (before it can revert for a touch) a re-activation and a later
        # close by another process: the sweep's revert must leave that later close alone
        class Touched:
            def __init__(self, job):
                pass

            def active(self):
                return False

            def after_close(self, undo):
                return bool(undo())

        _, ids = self.run_job()
        self.age("j", "abc", ids)
        real = type(self.b).auto_close_job
        later = []

        def close_then_later_run(board, job, before, outcome):
            at = real(board, job, before, outcome)
            board.open_job(job, None, None, None, None)
            board.allocate_name("d", job)
            board.agent_stopped("d")
            self.age(job, "d", [])
            later.append(real(board, job, board.now() - base_timedelta(WINDOW), "auto-closed: later"))
            return at

        with mock.patch.object(type(self.b), "auto_close_job", close_then_later_run):
            closed = self.b.sweep_auto_close(WINDOW, Touched)
        self.assertEqual([c.job for c in closed], ["j"])   # its revert failed: reported closed
        s = self.b.job_status("j")
        self.assertEqual((s.status, s.outcome, s.finished_at), ("completed", "auto-closed: later", later[0]))


class FileAutoCloseOldState(unittest.TestCase):
    def test_a_job_row_written_before_closed_by_existed(self):
        h = FileHarness()
        self.addCleanup(h.close)
        h.reset()
        with h.board() as b:
            b.open_job("j", None, None, None, None)
            b.allocate_name("a", "j")
            b.agent_stopped("a")
        with h.store.lock:
            del h.store.jobs["j"]["closed_by"]   # as the previous release wrote state.json
        h.backdate_job("j", created_at=QUIET + 60, activated_at=QUIET + 60)
        h.backdate_agent("a", joined_at=QUIET + 30, last_seen=QUIET, left_at=QUIET)
        with h.board() as b:
            self.assertIsNone(b.job_status("j").closed_by)
            self.assertEqual([c.job for c in b.sweep_auto_close(WINDOW)], ["j"])
            self.assertEqual(b.job_status("j").closed_by, base.AUTO_CLOSED_BY)


def base_timedelta(minutes: float):
    import datetime as dt
    return dt.timedelta(minutes=minutes)


class MemoryAutoClose(AutoCloseContract, unittest.TestCase):
    harness_factory = MemoryHarness


class SqliteAutoClose(AutoCloseContract, unittest.TestCase):
    harness_factory = SqliteHarness


class FileAutoClose(AutoCloseContract, unittest.TestCase):
    harness_factory = FileHarness


@unittest.skipUnless(os.environ.get("SWARM_TEST_CONFIG"),
                     "set SWARM_TEST_CONFIG to a throwaway Postgres board config to run")
class PostgresAutoClose(AutoCloseContract, unittest.TestCase):
    harness_factory = staticmethod(lambda: PostgresHarness(os.environ["SWARM_TEST_CONFIG"]))


# --------------------------------------------------------------------------- CLI and hooks

class AutoCloseCliTests(Env):
    def activate(self, job: str = "J"):
        rc, _, _ = self.cli("activate", "--job", job)
        self.assertEqual(rc, 0)

    def marker(self, job: str = "J"):
        return self.markers / f"{swarm.safe_job(job)}.json"

    def finished_job(self, job: str = "J", agents=("agent-1", "agent-2")):
        """An activated job whose agents joined through the hooks, posted, stopped, and then
        went quiet for longer than the default window (30 minutes)."""
        self.activate(job)
        for a in agents:
            self.hook("start", agent_id=a)
        with self.board() as b:
            name = b.active_agent_name(agents[-1])
            mid = b.post(job, name, "done: fixed the thing").id
        for a in agents:
            self.hook("stop", agent_id=a)
        self.h.backdate_job(job, created_at=QUIET + 60, activated_at=QUIET + 60)
        for a in agents:
            self.h.backdate_agent(a, joined_at=QUIET + 30, last_seen=QUIET, left_at=QUIET)
        self.h.backdate_message(mid, QUIET)
        return name

    def job(self, job: str = "J"):
        with self.board() as b:
            return b.job_status(job)

    def test_a_queued_post_keeps_the_job_open(self):
        # a sandboxed Codex agent without network posts into the spool; until a hook
        # delivers it, the queued post counts as activity: the sweep doesn't close the job
        from swarm import spool
        self.finished_job()
        spool.spool_post(self.cfg, "J", "Somebody", "still at it", None)
        with self.board() as b:
            self.assertEqual(swarm.sweep_jobs(b, self.cfg), [])
        self.assertEqual(self.job().status, "active")
        for p in self.spool_dir.glob("*.json"):   # queued long ago: no longer keeps it open
            os.utime(p, (1, 1))
        with self.board() as b:
            self.assertEqual([c.job for c in swarm.sweep_jobs(b, self.cfg)], ["J"])

    def test_status_closes_a_finished_quiet_job_and_drops_its_marker(self):
        name = self.finished_job()
        self.assertTrue(self.marker().exists())
        rc, out, _ = self.cli("status")
        self.assertEqual(rc, 0)
        self.assertIn(f"J: auto-closed: 2/2 agents completed; last post {name}: done: fixed the thing",
                      out)
        self.assertIn("no active jobs", out)
        s = self.job()
        self.assertEqual((s.status, s.closed_by), ("completed", "auto"))
        self.assertFalse(self.marker().exists())
        _, out, _ = self.cli("status", "--job", "J")
        self.assertIn("finished", out)
        self.assertRegex(out, r"\nfinished   \d+s ago \(auto-closed; activate reopens it\)\n")

    def test_config_zero_disables(self):
        self.config.write_text(self.config.read_text() + "[job]\nauto_close_minutes = 0\norphan_minutes = 0\n")
        self.cfg = swarm.load_config(self.config)
        self.finished_job()
        self.cli("status")
        self.cli("purge")
        self.assertEqual(self.job().status, "active")
        self.assertTrue(self.marker().exists())

    def test_purge_and_activate_of_another_job_sweep(self):
        self.finished_job()
        self.cli("purge")
        self.assertEqual(self.job().status, "completed")
        self.finished_job("K", agents=("agent-3",))
        self.activate("L")
        self.assertEqual(self.job("K").status, "completed")
        self.assertFalse(self.marker("K").exists())
        self.assertTrue(self.marker("L").exists())

    def test_activate_reopens_an_auto_closed_job(self):
        self.finished_job()
        self.cli("status")
        self.assertEqual(self.job().status, "completed")
        self.activate()
        s = self.job()
        self.assertEqual((s.status, s.outcome, s.finished_at, s.closed_by), ("active", None, None, None))
        self.assertTrue(self.marker().exists())
        out = self.hook("start", agent_id="agent-new")
        self.assertIn("[swarm] You are **", self.context(out))
        self.assertEqual(self.agent("agent-new").status, "started")
        self.cli("status")   # the new agent keeps it open
        self.assertEqual(self.job().status, "active")

    def test_deactivate_overwrites_the_auto_outcome(self):
        self.finished_job()
        self.cli("status")
        rc, out, _ = self.cli("deactivate", "--job", "J", "--outcome", "fixed it: PR 12 merged")
        self.assertEqual((rc, out), (0, "deactivated J (completed; it was already completed, auto-closed)\n"))
        s = self.job()
        self.assertEqual((s.status, s.outcome, s.closed_by), ("completed", "fixed it: PR 12 merged", "tester"))
        rc, _, _ = self.cli("deactivate", "--job", "J", "--status", "failed", "--outcome", "reverted")
        self.assertEqual((self.job().status, self.job().outcome), ("failed", "reverted"))

    def test_a_failing_sweep_does_not_break_activate_or_join(self):
        def broken(board, minutes, watch=None):
            raise RuntimeError("sweep exploded")

        with mock.patch.object(base.Board, "sweep_auto_close", broken):
            rc, out, err = self.cli("activate", "--job", "J")
            self.assertEqual(rc, 0)
            self.assertIn("auto-close sweep failed (RuntimeError: sweep exploded)", err)
            self.assertTrue(self.marker().exists())
            rc, out, err = self.cli("join", "--job", "J", "--key", "cli-agent")
            self.assertEqual(rc, 0)
            self.assertRegex(out, r"^[^\n]+\n$")   # the name, and nothing else
            self.hook("stop", agent_id="cli-agent")   # logged, never raised
        self.assertIn("stop sweep", self.error_log.read_text())

    def test_stop_hook_sweeps_turn_and_done_do_not(self):
        self.activate()
        self.hook("start", agent_id="agent-1")
        calls = []
        real = base.Board.sweep_auto_close

        def spy(board, minutes, watch=None):
            calls.append(minutes)
            return real(board, minutes, watch)

        with mock.patch.object(base.Board, "sweep_auto_close", spy):
            self.hook("turn", agent_id="agent-1", tool_name="Bash")
            self.hook("done", agent_id="agent-1", tool_name="Bash")
            self.assertEqual(calls, [])
            self.hook("stop", agent_id="agent-1")
            self.assertEqual(calls, [30])

    def test_stop_hook_closes_another_finished_job(self):
        self.finished_job()
        self.activate("K")
        self.hook("start", agent_id="agent-k", session="sess-2")
        self.hook("stop", agent_id="agent-k", session="sess-2")
        self.assertEqual(self.job().status, "completed")
        self.assertFalse(self.marker().exists())
        self.assertEqual(self.job("K").status, "active")   # its agent stopped just now

    def test_marker_of_a_job_auto_closed_elsewhere_is_dropped(self):
        self.finished_job()
        with self.board() as b:   # another machine's sweep: closes it, can't see our marker
            self.assertEqual([c.job for c in b.sweep_auto_close(30)], ["J"])
        self.assertTrue(self.marker().exists())
        self.cli("status")
        self.assertFalse(self.marker().exists())

    def test_marker_rewritten_after_the_close_is_kept(self):
        self.finished_job()
        with self.board() as b:
            b.sweep_auto_close(30)
            # reopened meanwhile (activate: open_job, then the marker), sweep read stale state
            finished = b.job_status("J").finished_at
        os.utime(self.marker(), (finished.timestamp() + 5, finished.timestamp() + 5))
        with self.board() as b:
            swarm.sweep_jobs(b, self.cfg)
        self.assertTrue(self.marker().exists())

    # ---- the orchestrating session keeps its job open
    def main_session(self, event: str = "turn", session: str = "sess-1") -> dict | None:
        """A hook event of the orchestrating session itself: no agent_id."""
        return self.hook(event, agent_id=None, session=session, tool_name="Bash")

    def seen(self, job: str = "J"):
        return self.markers / f"{swarm.safe_job(job)}.seen"

    def test_orchestrator_tool_calls_keep_a_finished_job_open(self):
        self.finished_job()
        self.assertIsNone(self.main_session("turn"))   # says nothing to the session
        self.cli("status")
        self.assertEqual(self.job().status, "active")
        self.assertTrue(self.marker().exists())
        self.assertIsNone(self.main_session("done"))
        with self.board() as b:
            self.assertEqual(swarm.sweep_jobs(b, self.cfg), [])
        self.assertEqual(self.job().status, "active")

    def test_orchestrator_quiet_for_the_window_no_longer_does(self):
        self.finished_job()
        self.main_session()
        old = self.seen().stat().st_mtime - QUIET
        os.utime(self.seen(), (old, old))
        self.cli("status")
        self.assertEqual(self.job().status, "completed")
        self.assertFalse(self.marker().exists())
        self.assertFalse(self.seen().exists())

    def test_another_sessions_tool_calls_do_not_count(self):
        self.finished_job()
        self.main_session(session="sess-other")
        self.assertFalse(self.seen().exists())
        self.cli("status")
        self.assertEqual(self.job().status, "completed")

    def test_a_main_session_with_no_job_touches_nothing(self):
        self.main_session(session="sess-1")
        self.assertFalse(self.markers.exists() and any(self.markers.iterdir()))

    def test_a_failing_orchestrator_mark_never_breaks_the_hook(self):
        self.finished_job()
        with mock.patch("swarm.safefs.touch", side_effect=PermissionError("read-only")):
            self.assertIsNone(self.main_session())
        self.assertIn("PermissionError", self.error_log.read_text())

    def test_deactivate_removes_the_orchestrator_mark(self):
        self.finished_job()
        self.main_session()
        self.assertTrue(self.seen().exists())
        self.cli("deactivate", "--job", "J")
        self.assertFalse(self.seen().exists())

    def test_a_touch_after_the_candidates_were_picked_still_keeps_it_open(self):
        # the freshness check is the last thing before the close, not a snapshot taken earlier
        self.finished_job()
        with self.board() as b:
            real = type(b).recent_messages

            def touched_meanwhile(board, *a, **kw):
                self.main_session()
                return real(board, *a, **kw)

            with mock.patch.object(type(b), "recent_messages", touched_meanwhile):
                self.assertEqual(swarm.sweep_jobs(b, self.cfg), [])
        self.assertEqual(self.job().status, "active")

    def slow_close(self, during):
        """sweep_jobs with `during()` run inside the board's close of the job."""
        with self.board() as b:
            real = type(b).auto_close_job

            def closing(board, *a, **kw):
                during()
                return real(board, *a, **kw)

            with mock.patch.object(type(b), "auto_close_job", closing):
                return swarm.sweep_jobs(b, self.cfg)

    def test_a_touch_during_a_slow_close_reverts_the_close(self):
        # the close holds no lock the hook needs: even with the marker's lock taken by someone
        # else, the touch lands at once, and the sweep reverts its close right after it
        from swarm import compat
        self.finished_job()
        self.main_session()
        old = self.seen().stat().st_mtime - QUIET
        os.utime(self.seen(), (old, old))
        waited = []

        def during():
            with open(self.marker()) as fh:
                compat.flock(fh, compat.LOCK_EX)
                t = time.monotonic()
                self.assertIsNone(self.main_session())
                waited.append(time.monotonic() - t)

        run = self.job().activated_at
        self.assertEqual(self.slow_close(during), [])
        self.assertLess(waited[0], 0.5)
        s = self.job()
        self.assertEqual((s.status, s.finished_at, s.closed_by, s.activated_at),
                         ("active", None, None, run))   # the close reverted: the same run
        self.assertTrue(self.marker().exists())
        self.assertTrue(self.seen().exists())
        # once the orchestrator is quiet too, the next sweep closes it as usual
        os.utime(self.seen(), (old, old))
        self.cli("status")
        s = self.job()
        self.assertEqual((s.status, s.closed_by, s.activated_at), ("completed", "auto", run))
        self.assertFalse(self.marker().exists())

    def test_the_first_touch_during_a_slow_close_reverts_it_too(self):
        # no .seen yet: created under the marker's lock, which the close doesn't hold
        self.finished_job()
        self.assertEqual(self.slow_close(lambda: self.main_session()), [])
        self.assertEqual(self.job().status, "active")
        self.assertTrue(self.seen().exists())

    def test_a_deactivate_during_the_close_is_not_undone(self):
        # touched, then removed (deactivate removes the marker and .seen under the lock before
        # closing): the job stays closed and nothing comes back
        self.finished_job()

        def during():
            self.main_session()
            self.assertTrue(swarm.remove_marker(self.marker()))

        self.assertEqual([c.job for c in self.slow_close(during)], ["J"])
        self.assertEqual(self.job().status, "completed")
        self.assertFalse(self.marker().exists())
        self.assertFalse(self.seen().exists())

    def test_touches_of_several_busy_markers_share_one_deadline(self):
        from swarm import compat
        for job in ("J", "K", "L"):
            rc, _, err = self.cli("activate", "--job", job, "--session", "sess-1")
            self.assertEqual(rc, 0, err)
        held = [open(self.marker(job)) for job in ("J", "K", "L")]
        try:
            for fh in held:
                compat.flock(fh, compat.LOCK_EX)
            t = time.monotonic()
            self.assertIsNone(self.main_session())
            waited = time.monotonic() - t
        finally:
            for fh in held:
                fh.close()
        self.assertLess(waited, swarm.MARKER_SWEEP_WAIT + 0.5)   # not one wait per marker
        self.assertFalse(any(self.seen(job).exists() for job in ("J", "K", "L")))
        self.main_session()   # locks free again: all three recorded
        self.assertTrue(all(self.seen(job).exists() for job in ("J", "K", "L")))

    def test_a_touch_racing_the_removal_leaves_no_seen_file(self):
        # the hook read the marker, then deactivate removed it: the touch must not recreate .seen
        self.finished_job()
        real = swarm_hooks._markers

        def removed_meanwhile(cfg):
            out = real(cfg)
            self.assertTrue(swarm.remove_marker(self.marker()))
            return out

        with mock.patch.object(swarm_hooks, "_markers", removed_meanwhile):
            self.assertIsNone(self.main_session())
        self.assertFalse(self.marker().exists())
        self.assertFalse(self.seen().exists())

    def test_activate_starts_unseen(self):
        self.markers.mkdir(parents=True, exist_ok=True)
        self.seen().touch()   # left over from an older version, say
        self.activate()
        self.assertFalse(self.seen().exists())

    def test_workers_are_told_to_mark_the_job_waiting_before_they_park(self):
        self.activate()
        text = self.context(self.hook("start", agent_id="agent-1"))
        self.assertIn("wait --job 'J' --on", text)
        self.assertIn("resume --job 'J'", text)

    def test_watch_and_tail_sweep_at_most_once_a_minute(self):
        clock = [1000.0]
        sweeper = swarm.Sweeper(self.cfg, every=60, clock=lambda: clock[0])
        calls = []
        with mock.patch.object(swarm, "sweep_jobs", lambda b, cfg, *_: calls.append(b) or []):
            sweeper("board")
            sweeper("board")
            clock[0] += 59
            sweeper("board")
            clock[0] += 2
            sweeper("board")
        self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()
