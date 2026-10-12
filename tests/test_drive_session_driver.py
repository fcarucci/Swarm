"""The session driver (spec 3.11 [T21], plan Task 21): event-driven, no timers, no model calls.

lib/swarm/supervisor/session_driver.py (the loop), wake.py (wake files, OS notification, the poll fallback),
procwatch.py (the session's process), and what starts and wakes a driver: `swarm activate`, the hooks, the CLI's
board writes, a runner's exit.

Real drivers run as detached subprocesses on real boards (sqlite, file, and postgres when SWARM_TEST_CONFIG names
a throwaway one); the memory board lives in one process, so its driver runs in a thread. The end-to-end test runs
a real drive job (artifact recipe) to MET with the orchestrating "session" an idle child process that never makes
a hook call, seats launched by the real runner as a scripted fake `claude` (tests/drive_fake_seat.py).

Run: cd tests && /var/tmp/test-slots/test-slot -- python3 -B -m unittest test_drive_session_driver
"""
from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import support  # noqa: F401  (puts lib/ on sys.path)
from support import SANDBOX, add_backend_variants, drive_backends, home_env, posix_only, tq, wait_until

import swarm.cli as swarm
from swarm import enrolment
from swarm.board import open_board
from swarm.board.autoinit import store_key
from swarm.supervisor import ledger, procwatch, session_driver as sd, settings, wake

IDLE_SECONDS = float(os.environ.get("SWARM_DRIVER_IDLE_SECONDS", "60"))   # spec: 60 s (never lower in CI)
REPO = Path(__file__).resolve().parent.parent
GOAL = "write a report"
POSIX = sys.platform != "win32"


def watchdog(test, seconds: float) -> None:
    """A hang fails fast: past `seconds` the process prints every thread's stack and exits (no 6-hour CI job)."""
    import faulthandler
    faulthandler.dump_traceback_later(seconds, exit=True)
    test.addCleanup(faulthandler.cancel_dump_traceback_later)


def idle_process() -> subprocess.Popen:
    """The orchestrating session: a real process that does nothing (no hook call, ever)."""
    return subprocess.Popen([sys.executable, "-c", "import time\nwhile True: time.sleep(3600)"])


def stop(p) -> None:
    if p is not None and p.poll() is None:
        p.kill()
    if p is not None:
        p.wait(30)


class Home(unittest.TestCase):
    """A temporary HOME with a config for the board of `harness_factory` (a support harness: sqlite unless a
    backend variant says otherwise), the private directory in place."""
    harness_factory = staticmethod(drive_backends()["sqlite"])

    def setUp(self):
        watchdog(self, 300)
        self.home = Path(tempfile.mkdtemp(dir=SANDBOX, prefix="sdrv-home-"))
        self.addCleanup(shutil.rmtree, self.home, True)
        self.h = self.harness_factory(self.id())
        self.backend = self.h.name
        self.addCleanup(self.h.close)
        self.h.reset()
        self.config = self.home / ".config" / "swarm" / "config.toml"
        self.config.parent.mkdir(parents=True)
        self.config.write_text(f'[board]\nbackend = "{self.backend}"\n{self.h.toml}\n'
                               f'[supervise]\nallowed_workdirs = [{tq(self.home)}]\n')
        env = mock.patch.dict(os.environ, {**home_env(self.home), "SWARM_CONFIG": str(self.config)})
        env.start()
        self.addCleanup(env.stop)
        self.cfg = swarm.load_config(self.config)
        self.key = store_key(self.cfg)
        settings.ensure_private_dir(self.cfg, create=True)

    def board(self):
        return open_board(self.cfg)

    def held_job(self, job="J", session="sess-1"):
        """A drive job of `session` that a pass leaves alone: halted by a person (the driver only settles it)."""
        with self.board() as b:
            b.open_job(job, None, None, None, "me", goal=GOAL)
            b.set_job_data(job, "drive.enabled", "1")
        enrolment.write_job_owner(self.key, job=job, harness="claude", session_id=session, cwd=str(self.home),
                                  roots=[str(self.home)], recipe="artifact", goal=GOAL,
                                  brief=str(self.home / "LEAD.md"))
        led = ledger.create(self.key, job, budgets={}, activated_at=dt.datetime.now(dt.timezone.utc).isoformat())
        led.requester_session = session
        led.halted = {"by": "person", "at": dt.datetime.now(dt.timezone.utc).isoformat(), "reason": "test"}
        led.save()
        sd.record_launch_env(session, str(self.config))     # what the requester's activation records
        return led

    def stats(self, session="sess-1") -> dict:
        raw = settings.read_private(sd.state_name(sd.lock_name(session)))
        return json.loads(raw) if raw else {}

    def start_driver(self, session_proc, session="sess-1"):
        """A real detached driver for `session` watching `session_proc`; its Popen (so the test can wait it)."""
        box = {}

        def popen(*a, **kw):
            box["p"] = subprocess.Popen(*a, **kw)
            return box["p"]
        pid = sd.start(self.cfg, session, session_proc.pid, config_path=str(self.config), popen=popen)
        self.assertIsNotNone(pid)
        proc = box["p"]
        self.addCleanup(stop, proc)
        wait_until(lambda: self.stats(session).get("passes", 0) >= 1, timeout=60)
        return proc


# --- the wake file and the hub -----------------------------------------------------------------------

class WakeFileTests(Home):
    def test_touch_creates_a_private_wake_file_only_on_a_drive_host(self):
        self.assertFalse(wake.touch(self.key))                       # no drive ledger directory: nothing
        self.assertFalse(os.path.lexists(wake.wake_dir()))
        self.held_job()
        self.assertTrue(wake.touch(self.key))
        path = wake.wake_dir() / wake.wake_name(self.key)
        self.assertTrue(path.is_file())
        if POSIX:
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        os.utime(path, (1, 1))
        self.assertTrue(wake.touch_cfg(self.cfg))
        self.assertGreater(path.stat().st_mtime, 1)

    @posix_only("symlinks")
    def test_a_planted_link_is_never_followed(self):
        self.held_job()
        target = self.home / "victim"
        target.write_text("x")
        os.utime(target, (1, 1))
        wake.wake_dir().mkdir(mode=0o700, exist_ok=True)
        (wake.wake_dir() / wake.wake_name(self.key)).symlink_to(target)
        self.assertFalse(wake.touch(self.key))
        self.assertEqual(target.stat().st_mtime, 1)

    def test_a_cli_board_write_touches_and_a_read_does_not(self):
        self.held_job()
        path = wake.wake_dir() / wake.wake_name(self.key)
        wake.touch(self.key)
        os.utime(path, (1, 1))
        swarm.main(["--config", str(self.config), "status"])
        self.assertEqual(path.stat().st_mtime, 1)
        swarm.main(["--config", str(self.config), "post", "--job", "J", "--as", "person", "hello"])
        self.assertGreater(path.stat().st_mtime, 1)

    def test_hub_returns_reasons_once_and_times_out_empty(self):
        hub = wake.Hub()
        hub.signal(wake.CHANGE)
        hub.signal(wake.CHANGE)
        self.assertEqual(hub.wait(5), {wake.CHANGE})
        t0 = time.monotonic()
        self.assertEqual(hub.wait(0.2), set())
        self.assertGreaterEqual(time.monotonic() - t0, 0.15)


class NotificationTests(Home):
    def test_this_platform_has_an_os_notification_and_it_wakes_on_a_touch(self):
        self.held_job()
        src = wake.file_source(self.key)
        self.assertIsNotNone(src, "no OS change notification on this platform (inotify, kqueue, "
                                  "ReadDirectoryChangesW)")
        self.addCleanup(src.close)
        hub = wake.Hub()
        wake.run_source(hub, src)
        threading.Timer(0.2, lambda: wake.touch(self.key)).start()
        t0 = time.monotonic()
        self.assertEqual(hub.wait(5), {wake.CHANGE})
        self.assertLess(time.monotonic() - t0, 1.0)

    def test_another_boards_wake_file_does_not_wake(self):
        self.held_job()
        src = wake.file_source(self.key)
        self.addCleanup(src.close)
        hub = wake.Hub()
        wake.run_source(hub, src)
        wake.touch("another board")
        self.assertEqual(hub.wait(1.0), set())


# --- the poll fallback ---------------------------------------------------------------------------------

class PollFallbackTests(unittest.TestCase):
    def test_poll_fallback_backs_off_2_to_60_s_and_resets(self):
        sleeps, changes = [], iter([None] * 7 + [1.0] + [1.0] * 2 + [2.0])
        src = wake.PollSource("bk", sleep=sleeps.append, stat=lambda: next(changes))
        self.assertTrue(src.block())                  # 6 quiet looks, then the change
        self.assertEqual(sleeps, [2.0, 4.0, 8.0, 16.0, 32.0, 60.0, 60.0])
        del sleeps[:]
        self.assertTrue(src.block())                  # after a change: 2 s again, then doubling
        self.assertEqual(sleeps, [2.0, 4.0, 8.0])

    def test_backoff_caps_and_resets(self):
        b = wake.Backoff()
        self.assertEqual([b.next(False) for _ in range(8)], [4, 8, 16, 32, 60, 60, 60, 60])
        self.assertEqual(b.next(True), 2)

    def test_no_notification_means_poll_mode(self):
        drv = sd.Driver.__new__(sd.Driver)
        drv.notify, drv.open_board, drv.board_key, drv.sources = False, None, "bk", []
        drv.hub, drv.watch = wake.Hub(), procwatch.NoWatch()
        with mock.patch.object(wake, "run_source"):
            drv.setup()
        self.assertEqual(drv.mode, "poll")
        self.assertTrue(any(isinstance(s, wake.PollSource) for s in drv.sources))


# --- deadlines -----------------------------------------------------------------------------------------

T0 = dt.datetime(2026, 10, 12, 12, 0, tzinfo=dt.timezone.utc)


def led_of(**kw):
    base = dict(terminal=None, halted=None, halted_total_seconds=0.0, ruling_due_at=None, activated_at=None,
                terminal_intent=None, watermarks={})
    base.update(kw)
    return SimpleNamespace(**base)


class LedgerDeadlineTests(unittest.TestCase):
    FLAT = {"ruling_deadline_minutes": 120, "ruling_grace_minutes": 15, "max_wall_hours": 24,
            "close_deadline_minutes": 60}

    def test_ruling_due_gives_the_grace_and_the_deadline_shifted_by_the_halt(self):
        led = led_of(ruling_due_at=T0.isoformat(), halted_total_seconds=600.0)
        self.assertEqual(sorted(sd.ledger_deadlines(led, self.FLAT)),
                         [T0 + dt.timedelta(minutes=15), T0 + dt.timedelta(minutes=130)])

    def test_the_wall_clock_and_its_ruling_deadline(self):
        led = led_of(activated_at=T0.isoformat())
        self.assertEqual(sorted(sd.ledger_deadlines(led, self.FLAT)),
                         [T0 + dt.timedelta(hours=24), T0 + dt.timedelta(hours=26)])

    def test_a_pending_close_has_its_close_deadline(self):
        led = led_of(terminal_intent={"at": T0.isoformat(), "verdict": "met"})
        self.assertEqual(sd.ledger_deadlines(led, self.FLAT), [T0 + dt.timedelta(minutes=60)])

    def test_the_stalled_notice_is_due_two_periods_after(self):
        led = led_of(watermarks={"stalled.since": int(T0.timestamp())})
        (when,) = sd.ledger_deadlines(led, self.FLAT, {"timer_minutes": 5})
        self.assertEqual(when, T0 + dt.timedelta(minutes=10, seconds=1))

    def test_halted_and_terminal_jobs_have_no_deadline(self):
        for led in (led_of(activated_at=T0.isoformat(), halted={"by": "p"}),
                    led_of(activated_at=T0.isoformat(), terminal={"verdict": "met"})):
            self.assertEqual(sd.ledger_deadlines(led, self.FLAT), [])

    def test_snapshot_deadlines_take_holds_questions_stall_and_leases(self):
        from swarm.supervisor import driver_model as dm
        hold = SimpleNamespace(kind="hold", not_before=T0 + dt.timedelta(minutes=3))
        launch = SimpleNamespace(kind="launch", not_before=T0 + dt.timedelta(minutes=1))
        ctx = SimpleNamespace(seats={"a": None, "b": None}, live={"a"}, cfg={"stall_minutes": 90,
                                                                            "lease_hold_minutes": 30},
                              gated=lambda n: T0 + dt.timedelta(minutes=7) if n == "b" else None,
                              last_activity=lambda: T0)
        snap = SimpleNamespace(integrated={"g": {"lease_held_since": T0.isoformat()}})
        with mock.patch.object(dm, "_Ctx", return_value=ctx):
            got = sorted(sd.snapshot_deadlines(snap, {}, [hold, launch]))
        self.assertEqual(got, [T0 + dt.timedelta(minutes=3), T0 + dt.timedelta(minutes=7),
                               T0 + dt.timedelta(minutes=30, seconds=1), T0 + dt.timedelta(minutes=90)])
        ctx.live = set()                              # no live seat: no stall wake
        with mock.patch.object(dm, "_Ctx", return_value=ctx):
            self.assertNotIn(T0 + dt.timedelta(minutes=90), sd.snapshot_deadlines(snap, {}, [hold]))


class TimeoutTests(unittest.TestCase):
    def driver(self):
        drv = sd.Driver.__new__(sd.Driver)
        drv.busy, drv.pending = wake.Backoff(sd.BUSY_MIN, sd.BUSY_MAX), wake.Backoff(sd.PENDING_MIN, sd.PENDING_MAX)
        return drv

    def test_nothing_due_sleeps_until_an_event(self):
        self.assertIsNone(self.driver().timeout(sd.PassResult("ok")))

    def test_a_deadline_is_slept_to_exactly(self):
        when = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=30)
        t = self.driver().timeout(sd.PassResult("ok", when))
        self.assertAlmostEqual(t, 30.05, delta=0.5)

    def test_a_busy_pass_lock_backs_off_and_an_ok_pass_resets_it(self):
        drv = self.driver()
        self.assertEqual([drv.timeout(sd.PassResult("busy")) for _ in range(6)], [2, 4, 8, 16, 30, 30])
        drv.timeout(sd.PassResult("ok"))
        self.assertEqual(drv.timeout(sd.PassResult("busy")), 2)

    def test_a_pending_launch_backs_off_to_60_s_and_resets(self):
        drv = self.driver()
        self.assertEqual([drv.timeout(sd.PassResult("ok", None, True)) for _ in range(6)], [4, 8, 16, 32, 60, 60])
        self.assertIsNone(drv.timeout(sd.PassResult("ok", None, False)))
        self.assertEqual(drv.timeout(sd.PassResult("ok", None, True)), 4)

    def test_an_unreachable_board_is_retried_with_backoff(self):
        drv = self.driver()
        self.assertEqual(drv.timeout(sd.PassResult("unreachable")), 4)


class NextDeadlineTests(Home):
    def test_next_deadline_wakes_exactly_once(self):
        """One pass now, whose deadline is 1 s away; then no deadline: exactly one more pass, at the deadline."""
        times: list = []

        def pass_fn(jobs):
            times.append(time.monotonic())
            if len(times) == 1:
                return sd.PassResult("ok", dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=1.0))
            return sd.PassResult("ok")
        self.held_job()
        drv = sd.Driver(self.cfg, session="sess-1", pass_fn=pass_fn)
        done = threading.Event()
        drv.jobs = lambda: [] if done.is_set() else ["J"]
        t = threading.Thread(target=drv.run, daemon=True)
        t.start()
        time.sleep(3.5)
        self.assertEqual(len(times), 2, times)
        self.assertAlmostEqual(times[1] - times[0], 1.05, delta=0.3)
        done.set()
        drv.hub.signal(wake.CHANGE)
        t.join(10)
        self.assertFalse(t.is_alive())


# --- the session's process -----------------------------------------------------------------------------

class ProcWatchTests(unittest.TestCase):
    def test_exit_is_seen_without_polling(self):
        p = idle_process()
        self.addCleanup(stop, p)
        w = procwatch.ProcWatch(p.pid, procwatch.start_token(p.pid))
        self.addCleanup(w.close)
        self.assertTrue(w.alive())
        self.assertNotEqual(w.kind, "poll")
        hub = wake.Hub()
        wake.run_source(hub, w, wake.EXIT)
        threading.Timer(0.2, p.kill).start()
        t0 = time.monotonic()
        self.assertEqual(hub.wait(10), {wake.EXIT})
        self.assertLess(time.monotonic() - t0, 2.0)
        self.assertFalse(w.alive())

    def test_a_wrong_token_is_a_gone_process(self):
        p = idle_process()
        self.addCleanup(stop, p)
        self.assertFalse(procwatch.ProcWatch(p.pid, "not-its-start").alive())
        self.assertFalse(procwatch.ProcWatch(p.pid, None).alive())       # cannot be told: gone (fails closed)

    def test_nearest_host_is_the_nearest_claude_or_codex_ancestor(self):
        tree = {10: (9, ["/bin/sh", "-c", "x"]), 9: (8, ["/usr/bin/claude", "--resume"]),
                8: (7, ["/usr/bin/codex"]), 7: (1, ["init"])}
        self.assertEqual(procwatch.nearest_host(10, parent=tree.get), 9)
        self.assertIsNone(procwatch.nearest_host(7, parent=tree.get))
        self.assertIsNone(procwatch.nearest_host(10, parent=lambda pid: None))   # unreadable: none


# --- one driver per session, its exit, its start ----------------------------------------------------------

class LifecycleTests(Home):
    def test_one_driver_per_session(self):
        self.held_job()
        session = idle_process()
        self.addCleanup(stop, session)
        procs = []

        def popen(*a, **kw):
            procs.append(subprocess.Popen(*a, **kw))
            return procs[-1]
        for _ in range(3):   # started at once: the lock decides
            sd.start(self.cfg, "sess-1", session.pid, config_path=str(self.config), popen=popen)
        for p in procs:
            self.addCleanup(stop, p)
        wait_until(lambda: sum(p.poll() is None for p in procs) == 1 and self.stats().get("pid"), timeout=30)
        time.sleep(1.0)
        live = [p for p in procs if p.poll() is None]
        self.assertEqual(len(live), 1)
        self.assertEqual(self.stats()["pid"], live[0].pid)
        with mock.patch.dict(os.environ, {"SWARM_SESSION_DRIVER": "1"}):
            self.assertEqual(sd.ensure(self.cfg, "sess-1"), "running")
        self.assertIsNone(sd.start(self.cfg, "sess-1", session.pid, config_path=str(self.config)))

    def test_driver_exits_without_orphans_when_the_session_dies(self):
        self.held_job()
        session = idle_process()
        self.addCleanup(stop, session)
        drv = self.start_driver(session)
        self.assertTrue(sd.held(sd.lock_name("sess-1")))
        session.kill()
        session.wait(10)
        t0 = time.monotonic()
        self.assertEqual(drv.wait(10), 0)
        self.assertLess(time.monotonic() - t0, 3.0)
        self.assertEqual(self.stats()["exit"], "session gone")
        self.assertFalse(sd.held(sd.lock_name("sess-1")))
        if POSIX:   # its own process group (session): nothing of it is left
            with self.assertRaises(ProcessLookupError):
                os.killpg(drv.pid, 0)

    def test_driver_exits_when_its_last_job_ends(self):
        led = self.held_job()
        session = idle_process()
        self.addCleanup(stop, session)
        drv = self.start_driver(session)
        led = ledger.load(self.key, "J")
        led.terminal = {"verdict": "cancelled", "at": T0.isoformat()}
        led.save()
        wake.touch(self.key)
        self.assertEqual(drv.wait(10), 0)
        self.assertEqual(self.stats()["exit"], "no unfinished drive job")
        self.assertIsNone(session.poll())          # the session is untouched

    def test_kill_switch_stops_the_driver(self):
        self.held_job()
        session = idle_process()
        self.addCleanup(stop, session)
        drv = self.start_driver(session)
        settings.off_file().parent.mkdir(parents=True, exist_ok=True)
        settings.off_file().write_text("")
        wake.touch(self.key)
        self.assertEqual(drv.wait(10), 0)
        self.assertEqual(self.stats()["exit"], "kill switch")

    def test_a_session_whose_process_cannot_be_identified_gets_no_driver(self):
        self.held_job()
        gone = idle_process()
        gone.kill()
        gone.wait(10)
        popen = mock.Mock()
        self.assertIsNone(sd.start(self.cfg, "sess-1", gone.pid, popen=popen))
        popen.assert_not_called()

    def test_ensure_starts_only_for_a_requester_of_an_unfinished_job(self):
        self.held_job(session="sess-1")
        popen = mock.Mock(return_value=SimpleNamespace(pid=4242, stdin=mock.Mock()))
        with mock.patch.dict(os.environ, {"SWARM_SESSION_DRIVER": "1", "PGPASSWORD": "pw",
                                          "ANTHROPIC_API_KEY": "sk"}):
            self.assertEqual(sd.ensure(self.cfg, "other", host_pid=os.getpid(), popen=popen), "no job")
            self.assertEqual(sd.ensure(self.cfg, None, host_pid=os.getpid(), popen=popen), "no session")
            popen.assert_not_called()
            self.assertEqual(sd.ensure(self.cfg, "sess-1", host_pid=os.getpid(), popen=popen), "started")
        argv, kw = popen.call_args
        self.assertEqual(argv[0][1:], ["-m", "swarm.supervisor.session_driver"])
        params = json.loads(popen.return_value.stdin.write.call_args.args[0])
        self.assertEqual((params["session"], params["pid"]), ("sess-1", os.getpid()))
        self.assertNotIn("sess-1", " ".join(argv[0]))         # the session id never goes on a command line
        self.assertTrue(kw.get("start_new_session") or kw.get("creationflags"))
        for secret in ("PGPASSWORD", "ANTHROPIC_API_KEY"):
            self.assertNotIn(secret, kw["env"])

    def test_a_seat_never_starts_a_driver(self):
        """Review rr1: a seat (its runner tag, or an agent host the supervisor launched) starts nothing, even
        with the requester's session id in a forged SessionStart payload."""
        import io
        from swarm.supervisor import runner
        self.held_job(session="sess-1")
        popen = mock.Mock()
        with mock.patch.dict(os.environ, {"SWARM_SESSION_DRIVER": "1", runner.RUN_ENV: "r7-00"}):
            self.assertTrue(sd.ensure(self.cfg, "sess-1", host_pid=os.getpid(), popen=popen).startswith("refused"))
        with mock.patch.dict(os.environ, {"SWARM_SESSION_DRIVER": "1"}), \
                mock.patch.object(procwatch, "under_runner", return_value=True), \
                mock.patch.object(procwatch, "nearest_host", return_value=os.getpid()), \
                mock.patch.object(sd, "start", popen):
            sd.hook_session_start(self.cfg, io.StringIO(json.dumps({"session_id": "sess-1"})))
        popen.assert_not_called()

    def test_a_driver_takes_the_requesters_recorded_path_and_config_never_the_starters(self):
        self.held_job(session="sess-1")       # recorded with this test's PATH and config
        recorded = sd.launch_env("sess-1")
        popen = mock.Mock(return_value=SimpleNamespace(pid=4242, stdin=mock.Mock()))
        with mock.patch.dict(os.environ, {"PATH": "/planted/bin", "SWARM_CONFIG": "/planted/config.toml"}):
            sd.start(self.cfg, "sess-1", os.getpid(), config_path="/planted/config.toml", popen=popen)
        env = popen.call_args.kwargs["env"]
        self.assertEqual((env["PATH"], env["SWARM_CONFIG"]), (recorded["path"], recorded["config"]))
        self.assertNotIn("/planted", json.dumps(popen.return_value.stdin.write.call_args.args[0].decode()))

    def run_main(self, payload: dict):
        import io
        seen = {}

        def serve(cfg, **kw):
            seen.update(environ=dict(os.environ), kw=kw)
            return "ok"
        with mock.patch.object(sd, "serve", side_effect=serve), \
                mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))):
            rc = sd.main()
        return rc, seen

    def test_the_module_entry_refuses_a_caller_under_a_runner(self):
        """rr1b: run by hand with the requester's own pid, the module still checks its CALLER's ancestry."""
        self.held_job(session="sess-1")
        session = idle_process()
        self.addCleanup(stop, session)
        payload = {"session": "sess-1", "pid": session.pid, "token": procwatch.start_token(session.pid)}
        with mock.patch.dict(os.environ), \
                mock.patch.object(procwatch, "under_runner", side_effect=lambda pid, **k: pid == os.getpid()):
            rc, seen = self.run_main(payload)
        self.assertEqual((rc, seen), (1, {}))

    def test_the_module_entry_rebuilds_its_environment_from_the_record(self):
        self.held_job(session="sess-1")
        rec = sd.launch_env("sess-1")
        session = idle_process()
        self.addCleanup(stop, session)
        payload = {"session": "sess-1", "pid": session.pid, "token": procwatch.start_token(session.pid)}
        planted = {"PATH": "/planted/bin", "SWARM_CONFIG": "/planted/c.toml", "CLAUDE_CONFIG_DIR": "/planted/cl",
                   "XDG_CONFIG_HOME": "/planted/x", "EVIL": "1"}
        with mock.patch.dict(os.environ, planted):
            rc, seen = self.run_main(payload)
        self.assertEqual(rc, 0)
        env = seen["environ"]
        self.assertEqual((env["PATH"], env["SWARM_CONFIG"]), (rec["path"], rec["config"]))
        self.assertNotIn("EVIL", env)
        self.assertFalse([v for v in env.values() if "/planted" in v], env)
        self.assertEqual(env.get("HOME"), rec["env"].get("HOME"))

    def test_no_recorded_activation_starts_nothing(self):
        self.held_job(session="sess-1")
        from swarm.supervisor import privfs
        with privfs.dir_fd() as d:
            privfs.unlink(d, sd.record_name("sess-1"))
        popen = mock.Mock()
        self.assertIsNone(sd.start(self.cfg, "sess-1", os.getpid(), popen=popen))
        popen.assert_not_called()

    def test_under_runner_sees_a_runner_ancestor_and_fails_closed(self):
        tree = {10: (9, ["claude", "-p"]), 9: (8, ["python3", "-m", "swarm.supervisor.runner", "--lock-fd", "3"]),
                8: (1, ["python3", "-m", "swarm.supervisor.session_driver"])}
        self.assertTrue(procwatch.under_runner(10, parent=tree.get))
        self.assertFalse(procwatch.under_runner(8, parent=tree.get))
        self.assertTrue(procwatch.under_runner(10, parent=lambda pid: None))       # unreadable: a seat

    def test_the_session_start_hook_starts_the_requesters_driver(self):
        import io
        with mock.patch.object(sd, "ensure", return_value="started") as ensure:
            sd.hook_session_start(self.cfg, io.StringIO(json.dumps({"session_id": "sess-1"})))
            sd.hook_session_start(self.cfg, io.StringIO(json.dumps({"session_id": "s", "agent_id": "sub"})))
            sd.hook_session_start(self.cfg, io.StringIO("not json"))
        ensure.assert_called_once_with(self.cfg, "sess-1")

    def test_the_requesters_first_hook_starts_its_driver_and_a_seat_does_not(self):
        from swarm import hooks
        self.held_job(session="sess-1")
        with mock.patch.object(sd, "ensure", return_value="started") as ensure, \
                mock.patch("swarm.drivegate.pre_tool", return_value=None), \
                mock.patch("swarm.drivegate.write_grants"):
            hooks._CURRENT["host"] = __import__("swarm.hosts", fromlist=["get"]).get("claude")
            hooks._drive_event("turn", self.cfg, {"session_id": "sess-1", "tool_name": "Bash"}, None)
            hooks._drive_event("turn", self.cfg, {"session_id": "sess-1", "agent_id": "a1", "tool_name": "Bash"},
                               None)
            hooks._drive_event("turn", self.cfg, {"session_id": "other", "tool_name": "Bash"}, None)
        ensure.assert_called_once_with(self.cfg, "sess-1")


# --- no model calls ----------------------------------------------------------------------------------------

class NoModelCallTests(Home):
    def test_driver_makes_no_model_call(self):
        """The loop's own code imports no model client and opens no connection on a local board; a pass that
        launches a seat does it only through the runner (runner.start), never by running a harness."""
        import socket
        for name in ("session_driver", "wake", "procwatch"):
            src = (REPO / "lib" / "swarm" / "supervisor" / f"{name}.py").read_text()
            for word in ("anthropic", "openai", "urllib", "http.client", "requests", "claude -p", "codex exec"):
                self.assertNotIn(word, src, f"{name}.py mentions {word}")
        self.held_job()
        connects, started = [], []
        real_connect = socket.socket.connect
        with mock.patch.object(socket.socket, "connect",
                               lambda s, addr: connects.append(addr) or real_connect(s, addr)):
            res = sd.one_pass(self.cfg, ["J"], start_runner=lambda cfg, run: started.append(run) or 1)
        self.assertEqual(res.status, "ok")
        self.assertEqual([a for a in connects if not isinstance(a, str)], [])   # no TCP at all on sqlite
        self.assertNotIn("anthropic", sys.modules)
        self.assertNotIn("openai", sys.modules)


# --- idle cost and wake latency, per backend --------------------------------------------------------------

class IdleCase(Home):
    """Per backend: a real driver process idles for IDLE_SECONDS with a held job; its own CPU times (os.times(),
    written at its exit) and its wakeups are bounded. Then board changes wake it in under a second."""

    def latency(self, drv_stats, change) -> float:
        before = self.stats()["passes"]
        t0 = time.monotonic()
        change()
        wait_until(lambda: self.stats().get("passes", 0) > before, timeout=10, interval=0.005)
        return time.monotonic() - t0

    def change(self, i):
        with self.board() as b:
            b.post("J", "person", f"change {i}")
        if self.backend != "postgres":      # the CLI touches the wake file after a write (cli._wake)
            wake.touch_cfg(self.cfg)

    def test_idle_cpu_and_wakeups(self):
        self.held_job()
        session = idle_process()
        self.addCleanup(stop, session)
        drv = self.start_driver(session)
        first = self.stats()
        self.assertIn(first["mode"], ("listen", "notify"))
        if self.backend == "postgres":
            self.assertEqual(first["mode"], "listen")
        time.sleep(IDLE_SECONDS)
        session.kill()
        drv.wait(15)
        last = self.stats()
        cpu = last["cpu_seconds"] - first["cpu_seconds"]
        self.assertLess(cpu, 0.005 * IDLE_SECONDS, f"{self.backend}: {cpu:.3f} s CPU while idle")
        self.assertLessEqual(last["wakeups"] - first["wakeups"] - 1, 3, last)   # -1: the session's exit
        self.assertEqual(last["passes"], first["passes"], "an idle driver ran passes")

    def test_wake_latency_under_one_second(self):
        self.held_job()
        session = idle_process()
        self.addCleanup(stop, session)
        self.start_driver(session)
        worst = max(self.latency(None, lambda i=i: self.change(i)) for i in range(3))
        self.assertLess(worst, 1.0, f"{self.backend}: a board change woke the driver after {worst:.2f} s")


# IdleCase runs on sqlite; IdleCaseFile, and IdleCasePostgres with SWARM_TEST_CONFIG (CI's postgres job)
add_backend_variants(globals(), IdleCase, skip=("memory", "sqlite"))


class MemoryIdle(Home):
    """The memory board is in-process only: its driver runs in a thread (the board pushes changes)."""
    harness_factory = staticmethod(drive_backends()["memory"])

    def setUp(self):
        super().setUp()
        self.held_job()
        self.passes = []
        self.drv = sd.Driver(self.cfg, session="sess-1", open_board=lambda: open_board(self.cfg),
                             pass_fn=lambda jobs: self.passes.append(time.monotonic()) or sd.one_pass(self.cfg, jobs))
        self.done = threading.Event()
        real = self.drv.jobs
        self.drv.jobs = lambda: [] if self.done.is_set() else real()
        self.t = threading.Thread(target=self.drv.run, daemon=True)
        self.t.start()
        wait_until(lambda: self.passes, timeout=30)
        time.sleep(0.5)
        self.addCleanup(self.finish)

    def finish(self):
        self.done.set()
        self.drv.hub.signal(wake.CHANGE)
        self.t.join(10)

    def test_idle_cpu_and_wakeups(self):
        self.assertEqual(self.drv.mode, "listen")
        n, wakeups, t0 = len(self.passes), self.drv.hub.wakeups, os.times()
        time.sleep(IDLE_SECONDS)
        t1 = os.times()
        cpu = (t1.user + t1.system) - (t0.user + t0.system)   # this whole test process: the driver is a thread
        self.assertLess(cpu, 0.005 * IDLE_SECONDS, f"memory: {cpu:.3f} s CPU while idle")
        self.assertLessEqual(self.drv.hub.wakeups - wakeups, 3)
        self.assertEqual(len(self.passes), n)

    def test_wake_latency_under_one_second(self):
        for i in range(3):
            n = len(self.passes)
            t0 = time.monotonic()
            with self.board() as b:
                b.post("J", "person", f"change {i}")
            wait_until(lambda: len(self.passes) > n, timeout=10, interval=0.005)
            self.assertLess(time.monotonic() - t0, 1.0)
            time.sleep(0.3)


# --- the end-to-end proof --------------------------------------------------------------------------------

class IdleOrchestratorEndToEnd(unittest.TestCase):
    """A real drive job (artifact recipe) on a sqlite board: `swarm activate`, then nothing but the session
    driver. The orchestrating session is an idle child process that never makes a hook call; seats are launched
    by the real runner (no systemd: their own process group, a Job Object on Windows) as a scripted `claude`;
    the job reaches MET. Linux, macOS and Windows."""

    WINDOWS_KEEP = ("SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP", "PROGRAMDATA",
                    "PROGRAMFILES", "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "OS", "USERNAME")

    def venv(self, venv: Path) -> None:
        """The launcher's venv: this interpreter, stamped as up to date (a stub zstandard where the launcher
        looks; the interpreter itself does not read that venv: it has no pyvenv.cfg)."""
        if POSIX:
            (venv / "bin").mkdir(parents=True)
            os.symlink(sys.executable, venv / "bin" / "python")
            stamp = subprocess.run(f'cat "{REPO}/requirements.txt" "{REPO}/requirements-postgres.txt" | cksum',
                                   shell=True, capture_output=True, text=True).stdout.split()[0]
            site = venv / "lib" / "python3.x" / "site-packages"
        else:
            from swarm import winlaunch
            (venv / "Scripts").mkdir(parents=True)
            try:
                os.symlink(sys.executable, venv / "Scripts" / "python.exe")
            except OSError as exc:
                self.skipTest(f"no symlink here for the launcher's venv: {exc}")
            stamp = winlaunch.requirements_stamp(REPO)
            site = venv / "Lib" / "site-packages"
        (venv / ".swarm-requirements").write_text(stamp)
        (site / "zstandard").mkdir(parents=True)
        (site / "zstandard" / "__init__.py").write_text("")

    def setUp(self):
        watchdog(self, 420)
        # never under /tmp: activation and the driver refuse work dirs there (spec 4.2)
        base = Path.home() / ".cache" / "swarm-session-driver-tests"
        base.mkdir(parents=True, exist_ok=True)
        self.x = Path(tempfile.mkdtemp(prefix="e2e-", dir=base))
        self.addCleanup(shutil.rmtree, self.x, True)
        self.home = self.x / "home"
        self.home.mkdir(mode=0o700)
        self.work = self.x / "work"
        self.work.mkdir()
        bindir = self.x / "bin"
        bindir.mkdir()
        venv = self.x / "venv"
        self.venv(venv)
        self.seat_log = self.x / "seats.jsonl"
        seat = REPO / "tests" / "drive_fake_seat.py"
        if POSIX:
            self.launcher = REPO / "bin" / "swarm"
            claude = bindir / "claude"
            claude.write_text(f'#!/bin/sh\nexport FAKE_SWARM="{self.launcher}" FAKE_SEAT_LOG="{self.seat_log}"\n'
                              f'PYTHONPATH="{REPO / "lib"}" exec "{sys.executable}" "{seat}" "$@"\n')
            claude.chmod(0o755)
            path = f"{bindir}{os.pathsep}/usr/local/bin:/usr/bin:/bin"
        else:
            self.launcher = REPO / "bin" / "swarm.cmd"
            claude = bindir / "claude.cmd"
            claude.write_text(f'@echo off\r\nset "FAKE_SWARM={self.launcher}"\r\nset "FAKE_SEAT_LOG={self.seat_log}"\r\n'
                              f'set "PYTHONPATH={REPO / "lib"}"\r\n"{sys.executable}" "{seat}" %*\r\n')
            path = os.pathsep.join([str(bindir), os.path.dirname(sys.executable), os.environ.get("PATH", "")])
        self.config = self.home / ".config" / "swarm" / "config.toml"
        self.config.parent.mkdir(parents=True)
        self.config.write_text(f'[board]\nbackend = "sqlite"\n[sqlite]\npath = {tq(self.x / "board.sqlite3")}\n'
                               f'[supervise]\nallowed_workdirs = [{tq(self.x)}]\nclaude_bin = {tq(claude)}\n'
                               f'enrol_minutes = 2\n[hook]\nmarker_dir = {tq(self.home / ".local/state/swarm/active")}\n')
        (self.x / "LEAD.md").write_text("# lead\nWrite REPORT.md and post goal-ready.\n")
        self.env = {"HOME": str(self.home), "USERPROFILE": str(self.home), "PATH": path,
                    "SWARM_CONFIG": str(self.config), "SWARM_VENV": str(venv), "SWARM_NO_SYSTEMD": "1",
                    "LANG": "C.UTF-8", "SWARM_SESSION_DRIVER": "0", "PYTHONPATH": str(REPO / "lib")}
        if not POSIX:
            self.env.update({k: os.environ[k] for k in self.WINDOWS_KEEP if k in os.environ})
        patch = mock.patch.dict(os.environ, self.env, clear=True)
        patch.start()
        self.addCleanup(patch.stop)

    def evidence(self) -> str:
        """What a failure shows: the seats' log, the supervisor log and the runners' kept output tails."""
        out = []
        for p in [self.seat_log, self.home / ".local/share/swarm/supervisor/supervise.log",
                  *sorted((self.home / ".local/share/swarm/supervisor").rglob("*.tail.txt")),
                  *sorted((self.home / ".local/share/swarm/supervisor").glob("driver-*.json")),
                  self.home / ".local/share/swarm/host/hook-stderr.log",
                  self.home / ".local/share/swarm/host/receipt-failures.log"]:
            try:
                out.append(f"== {p.name}\n{p.read_text(errors='replace')[-3000:]}")
            except OSError:
                out.append(f"== {p.name}: none")
        return "\n".join(out)

    def swarm_cli(self, *args):
        r = subprocess.run([str(self.launcher), *args], env=self.env, capture_output=True, text=True,
                           cwd=self.work, timeout=300)
        self.assertEqual(r.returncode, 0, f"swarm {' '.join(args)}: {r.stdout[-800:]} {r.stderr[-800:]}")
        return r

    def test_idle_orchestrator_job_reaches_met_via_session_driver(self):
        subprocess.run(["git", "init", "-q", str(self.work)], check=True)
        self.swarm_cli("init")
        out = self.swarm_cli("activate", "--job", "J", "--goal", GOAL, "--recipe", "artifact",
                             "--workdir", str(self.work), "--session", "sess-1",
                             "--lead-brief", str(self.x / "LEAD.md")).stdout
        self.assertIn("activated J (drive)", out)
        cfg = swarm.load_config(self.config)
        session = idle_process()                     # the orchestrator: open, idle, no hook call after this
        self.addCleanup(stop, session)
        box = {}

        def popen(*a, **kw):
            box["p"] = subprocess.Popen(*a, **kw)
            return box["p"]
        self.assertIsNotNone(sd.start(cfg, "sess-1", session.pid, config_path=str(self.config), popen=popen))
        self.addCleanup(stop, box["p"])

        def status():
            with open_board(cfg) as b:
                js = b.job_status("J")
            return js.status, js.verdict
        try:
            wait_until(lambda: status()[0] != "active", timeout=240, interval=0.5)
        except AssertionError:
            self.fail(f"not terminal after 240 s: {status()}\n{self.evidence()}")
        self.assertEqual(status(), ("completed", "met"), self.evidence())
        self.assertEqual(box["p"].wait(30), 0)       # the driver ends itself once its last job is done
        st = json.loads(settings.read_private(sd.state_name(sd.lock_name("sess-1"))))
        self.assertEqual(st["exit"], "no unfinished drive job")
        self.assertIsNone(session.poll())             # the session never did anything and is still open
        seats = [json.loads(line) for line in self.seat_log.read_text().splitlines()]
        roles = [e["head"] for e in seats if e["ev"] == "start"]
        self.assertTrue(any("role engineering_lead" in r for r in roles))
        self.assertTrue(any("goal judge" in r or "role judge" in r for r in roles))
        self.assertFalse([e for e in seats if e["ev"] == "denied"])
        led = ledger.load(store_key(cfg), "J")
        self.assertEqual((led.terminal or {}).get("verdict"), "met")
        self.assertEqual(led.requester_session, "sess-1")


# --- seat containment without systemd ---------------------------------------------------------------------

try:
    from test_supervise_runner import RunnerBase, SURVIVOR, _gone, fake  # noqa: E402  (the runner fixture)
except Exception:          # a platform the runner fixture does not load on (Windows: its Job Object tests)
    @unittest.skip("the runner fixture is POSIX-only")
    class RunnerBase(unittest.TestCase):
        pass


class NoSystemdContainmentTests(RunnerBase):
    """Spec 3.11 [T21]: with no systemd user manager (SWARM_NO_SYSTEMD=1, macOS, a container), a drive seat
    launches in its own process group, and stopping it kills its whole process tree. Nothing goes through
    systemd-run or systemctl."""

    def setUp(self):
        super().setUp()
        p = mock.patch.dict(os.environ, {"SWARM_NO_SYSTEMD": "1"})
        p.start()
        self.addCleanup(p.stop)
        # containment, not the Task 20 credential jail (tests/test_seat_isolation covers that): the seat's argv as is
        from swarm.supervisor import runner
        j = mock.patch.object(runner, "_seat_argv", lambda cfg, run: (list(run["argv"]), None))
        j.start()
        self.addCleanup(j.stop)

    def drive_run(self, argv, **kw):
        run = self.run_dict(argv, **kw)
        run.update(launch_id="t1-0123456789abcdef0123456789abcdef", action_id="gen1|seat|eng|-|0|1",
                   seat="eng")
        return run

    def survivor_pid(self):
        f = self.bin / "claude.pid"
        wait_until(lambda: f.exists() and f.read_text().strip())
        return int(f.read_text())

    def test_a_drive_seat_launches_without_a_user_manager(self):
        from swarm.supervisor import runner
        sid = "3f0c1e9a-0000-4000-8000-000000000001"
        argv = [fake(self.bin / "claude", 'touch "$0.ran"\necho \'{"subtype":"success"}\'\n')]
        self.enrol_soon(sid)
        self.assertEqual(self.execute(self.drive_run(argv)), "completed")
        self.assertTrue((self.bin / "claude.ran").exists())
        self.assertFalse([c for c in self.calls() if c.startswith(("systemd-run", "systemctl"))])
        self.assertFalse(runner.scope_available())

    def test_stopping_it_kills_the_whole_process_tree(self):
        sid = "3f0c1e9a-0000-4000-8000-000000000001"
        argv = [fake(self.bin / "claude", SURVIVOR + "sleep 30 & wait\n")]
        self.enrol_soon(sid)
        self.assertEqual(self.execute(self.drive_run(argv, limit=1.5)), "timeout")
        self.assertTrue(_gone(self.survivor_pid()))      # a member that ignored SIGTERM, in the background
        self.assertFalse([c for c in self.calls() if c.startswith(("systemd-run", "systemctl"))])

    def test_a_non_drive_replacement_is_still_refused_without_a_scope(self):
        argv = [fake(self.bin / "claude", 'touch "$0.ran"\n')]
        self.assertEqual(self.execute(self.run_dict(argv)), "refused")
        self.assertFalse((self.bin / "claude.ran").exists())


@unittest.skipUnless(POSIX and os.path.isdir("/proc"), "process groups are listed through /proc")
class GroupPresenceTests(unittest.TestCase):
    """A seat contained by its process group is "present" while a tagged member lives, "absent" only when the
    members can be listed and none is left; the driver's reconcile treats it as live meanwhile."""

    def test_presence_follows_the_tagged_group(self):
        from swarm.supervisor import driver_exec, runner
        tag = "r9-00000000000000ff"
        p = subprocess.Popen(["sh", "-c", "sleep 30 & wait"], start_new_session=True,
                             env={**os.environ, runner.RUN_ENV: tag})
        self.addCleanup(stop, p)
        run = {"child_pgid": p.pid, "child_pid": p.pid, "run_tag": tag, "child_start": runner._proc_start(p.pid),
               "unit": None}
        wait_until(lambda: runner.group_presence(run) == "present")
        self.assertEqual(runner.session_presence(run), "present")
        self.assertTrue(driver_exec._live_evidence("bk", None, run))
        runner._stop_session(run, grace=1.0)
        p.wait(10)
        wait_until(lambda: runner.group_presence(run) == "absent")
        self.assertFalse(driver_exec._live_evidence("bk", None, run))
        self.assertEqual(runner.group_presence({"unit": None}), "absent")         # never started

    def test_without_proc_an_empty_answer_is_unknown_not_absent(self):
        from swarm.supervisor import runner
        run = {"child_pgid": 12345, "run_tag": "r1-x", "child_start": 1, "unit": None}
        real = os.path.isdir
        with mock.patch("os.path.isdir", side_effect=lambda p: False if p == "/proc/self" else real(p)):
            with mock.patch.object(runner, "_ps_members", return_value=None):      # ps cannot tell
                self.assertEqual(runner.group_presence(run), "unknown")
                self.assertTrue(runner._session_alive(run))
            with mock.patch.object(runner, "_ps_members", return_value=[]):        # ps lists no member
                self.assertEqual(runner.group_presence(run), "absent")
            with mock.patch.object(runner, "_ps_members", return_value=[4242]):
                self.assertEqual(runner.group_presence(run), "present")

    def test_ps_lists_the_tagged_members_of_the_group(self):
        from swarm.supervisor import runner
        tag = "r9-00000000000000fe"
        p = subprocess.Popen(["sh", "-c", "sleep 30 & wait"], start_new_session=True,
                             env={**os.environ, runner.RUN_ENV: tag})
        self.addCleanup(stop, p)
        listing = f"{p.pid} {p.pid} S\n999999 {p.pid} Z\n7 7 S\n"

        def ps(argv, **kw):
            if argv[:2] == ["ps", "-A"]:
                return SimpleNamespace(returncode=0, stdout=listing)
            pid = argv[argv.index("-p") + 1]
            env = f"{runner.RUN_ENV}={tag}" if pid == str(p.pid) else "OTHER=1"
            return SimpleNamespace(returncode=0, stdout=f"sh -c x {env}\n")
        with mock.patch.object(runner.subprocess, "run", side_effect=ps):
            self.assertEqual(runner._ps_members(p.pid, f"{runner.RUN_ENV}={tag}"), [p.pid])
            self.assertEqual(runner._ps_members(p.pid, f"{runner.RUN_ENV}=other"), [])


class RunnerExitWakesTests(NoSystemdContainmentTests):
    def test_a_runners_exit_touches_the_wake_file(self):
        from swarm.supervisor import privfs
        with privfs.dir_fd(ledger.SUBDIR):
            pass                                   # a drive host
        key = store_key(self.cfg)
        wake.touch(key)
        path = wake.wake_dir() / wake.wake_name(key)
        os.utime(path, (1, 1))
        sid = "3f0c1e9a-0000-4000-8000-000000000001"
        argv = [fake(self.bin / "claude", 'echo \'{"subtype":"success"}\'\n')]
        self.enrol_soon(sid)
        self.assertEqual(self.execute(self.drive_run(argv)), "completed")
        self.assertGreater(path.stat().st_mtime, 1)


for _n in [n for n in vars(NoSystemdContainmentTests) if n.startswith("test_")]:
    setattr(RunnerExitWakesTests, _n, None)


class DriveVerdictRecordTests(unittest.TestCase):
    """The snapshot reads goal and ruling verdicts where review.verdict_data stores them: one record per
    (generation, scope, candidate, req), chunked, bound to its VERDICT message."""

    def data(self, **recs):
        from swarm import review
        out = {}
        for (scope, cand, mid) in recs.values():
            key = review.drive_verdict_key(1, scope, cand, "r" * 64)
            rec = json.dumps({"verdict": "met", "scope": scope, "generation": 1, "candidate": cand,
                              "req": "r" * 64, "message_id": mid})
            out[key], out[key + ".0"] = "1", rec
        return out

    def test_every_bound_record_of_the_generation_and_scope(self):
        from swarm.supervisor import driver_snapshot as ds
        data = self.data(a=("goal", "x@" + "a" * 40, 3), b=("goal", "y@" + "b" * 40, 5),
                         c=("goal", "z@" + "c" * 40, 0), d=("ruling", "w@" + "d" * 40, 7))
        got = ds._drive_verdict_records(data, 1, "goal")
        self.assertEqual(sorted(r["message_id"] for r in got), [3, 5])     # 0: never bound, never read
        self.assertEqual([r["message_id"] for r in ds._drive_verdict_records(data, 1, "ruling")], [7])
        self.assertEqual(ds._drive_verdict_records(data, 2, "goal"), [])

    def test_a_broken_record_is_skipped(self):
        from swarm.supervisor import driver_snapshot as ds
        data = self.data(a=("goal", "x@" + "a" * 40, 3))
        key = next(k for k in data if not k.endswith(".0"))
        data[key + ".0"] = "{not json"
        self.assertEqual(ds._drive_verdict_records(data, 1, "goal"), [])


class DriveRunTests(Home):
    """`swarm drive run [--job J]`: the loop in the foreground, no session to watch, until no job is left."""

    def args(self, job="J", yes=True):
        return SimpleNamespace(job=job, config=str(self.config), yes=yes)

    def setUp(self):
        super().setUp()
        from swarm import person   # a person at a terminal (these tests may run under an agent host)
        p = mock.patch.object(person, "_ancestor_host", lambda: None)
        p.start()
        self.addCleanup(p.stop)
        t = mock.patch.object(person, "_tty_confirm", return_value=True)   # a person typed y on the tty
        t.start()
        self.addCleanup(t.stop)
        e = mock.patch.dict(os.environ)
        e.start()
        self.addCleanup(e.stop)
        for k in person.HOST_ENV:
            os.environ.pop(k, None)

    def test_it_is_refused_inside_an_agent_host_and_for_a_seat(self):
        from swarm import drive_cli, person
        from swarm.supervisor import runner
        self.held_job()
        with mock.patch.object(person, "_ancestor_host", lambda: "claude"):
            self.assertEqual(drive_cli.cmd_drive_run(self.cfg, self.args()), 1)
        with mock.patch.dict(os.environ, {runner.RUN_ENV: "r1-00"}):
            self.assertEqual(drive_cli.cmd_drive_run(self.cfg, self.args()), 1)
        with mock.patch.object(person, "_tty_confirm", return_value=False):
            self.assertEqual(drive_cli.cmd_drive_run(self.cfg, self.args(yes=False)), 1)   # no person said yes
        # rr1a: a seat that cleared its variables and detached is still under its runner: refused
        with mock.patch.object(procwatch, "under_runner", side_effect=lambda pid, **k: pid == os.getpid()):
            self.assertEqual(drive_cli.cmd_drive_run(self.cfg, self.args()), 1)
        self.assertFalse(sd.held(sd.run_lock_name(self.key, "J")))

    def test_there_is_no_yes_flag(self):
        import argparse
        from swarm import drive_cli
        sub = argparse.ArgumentParser().add_subparsers(dest="cmd")
        drive_cli.register(sub, {}, {})
        tree = sub._swarm_drive_tree
        with self.assertRaises(SystemExit):
            tree.choices["run"].parse_args(["--yes"])

    def test_nothing_to_drive_returns_at_once(self):
        from swarm import drive_cli
        self.assertEqual(drive_cli.cmd_drive_run(self.cfg, self.args()), 0)

    def test_it_drives_until_the_job_ends_and_one_runs_at_a_time(self):
        from swarm import drive_cli
        self.held_job()
        box = {}
        t = threading.Thread(target=lambda: box.setdefault("rc", drive_cli.cmd_drive_run(self.cfg, self.args())),
                             daemon=True)
        t.start()
        lock = sd.run_lock_name(self.key, "J")
        wait_until(lambda: sd.held(lock), timeout=30)
        self.assertEqual(drive_cli.cmd_drive_run(self.cfg, self.args()), 1)       # already running
        self.assertTrue(t.is_alive())
        led = ledger.load(self.key, "J")
        led.terminal = {"verdict": "cancelled", "at": T0.isoformat()}
        led.save()
        wake.touch(self.key)
        t.join(15)
        self.assertFalse(t.is_alive())
        self.assertEqual(box["rc"], 0)
        self.assertFalse(sd.held(lock))

    def test_it_is_registered_as_a_drive_command(self):
        import argparse
        from swarm import drive_cli
        sub = argparse.ArgumentParser().add_subparsers(dest="cmd")
        drive_cli.register(sub, {}, {})
        self.assertIn("run", drive_cli.DRIVE_COMMANDS)


class HookWakeTests(Home):
    def test_hooks_wake_drivers_after_a_swarm_command_and_not_before(self):
        from swarm import hooks, hosts
        self.held_job()
        path = wake.wake_dir() / wake.wake_name(self.key)
        wake.touch(self.key)
        hooks._CURRENT["host"] = hosts.get("claude")
        cases = [("turn", "swarm post --job J hi", False), ("done", "ls -la", False),
                 ("done", "swarm post --job J hi", True), ("stop", "", True)]
        for event, cmd, woke in cases:
            with self.subTest(event=event, cmd=cmd):
                os.utime(path, (1, 1))
                hooks._wake_drivers(event, self.cfg, {"session_id": "s", "tool_name": "Bash",
                                                      "tool_input": {"command": cmd}})
                self.assertEqual(path.stat().st_mtime > 1, woke)


# --- Windows ports: cmd /c under the scanned-literal rule, ancestry --------------------------------------

class CmdScannedLiteralTests(unittest.TestCase):
    """`cmd /c` (and /k, /r) is a shell: its command is walked and scanned like `sh -c`'s (spec 3.11 [T21])."""
    P = "git push origin HEAD:main"

    def refused(self, cmd):
        from swarm.shellguard import drive_command_refusal
        return drive_command_refusal(cmd, ["main"], branch_of=lambda *a, **k: "feat/a")

    def test_cmd_c_runs_what_it_is_given_and_that_is_checked(self):
        for cmd in (f'cmd /c "{self.P}"', f"cmd /c {self.P}", f"cmd.exe /d /s /c \"{self.P}\"",
                    f"CMD.EXE /C {self.P}", f"cmd /k {self.P}", f"cmd /cgit push origin HEAD:main",
                    f"cmd /c dir & {self.P}", "cmd /c %X%", "cmd /v:on /c !X!", "cmd", "cmd /q"):
            with self.subTest(cmd=cmd):
                self.assertTrue(self.refused(cmd), cmd)

    def test_harmless_cmd_c_is_allowed(self):
        for cmd in ("cmd /c dir", 'cmd /d /c "echo hello"', "cmd /c git status"):
            with self.subTest(cmd=cmd):
                self.assertIsNone(self.refused(cmd), cmd)

    def test_powershell_command_is_still_scanned(self):
        self.assertTrue(self.refused(f"powershell -Command \"{self.P}\""))
        self.assertIsNone(self.refused("powershell -Command Get-ChildItem"))


class AncestryTests(unittest.TestCase):
    def test_unreadable_ancestry_fails_closed_for_the_person_gate(self):
        from swarm import person
        with mock.patch.object(procwatch, "parent_of", return_value=None):
            self.assertEqual(person._ancestor_host(), "unknown")
            self.assertIsNotNone(person.agent_host({}))           # refused as inside an agent host

    def test_a_walk_that_reaches_the_top_without_a_host_is_none(self):
        tree = {10: (9, ["bash"]), 9: (1, ["login"])}
        self.assertIsNone(procwatch.ancestor_host(10, parent=tree.get))
        tree = {10: (9, ["bash"]), 9: (8, ["/usr/local/bin/codex"])}
        self.assertEqual(procwatch.ancestor_host(10, parent=tree.get), "codex")
        with self.assertRaises(LookupError):
            procwatch.ancestor_host(10, parent=lambda pid: None)

    def test_windows_ancestry_comes_from_a_toolhelp_snapshot(self):
        snap = {10: (9, "cmd.exe"), 9: (8, "Claude.exe"), 8: (4, "explorer.exe")}
        with mock.patch.object(procwatch.compat, "IS_WINDOWS", True), \
                mock.patch.object(procwatch, "_win_parents", return_value=snap):
            self.assertEqual(procwatch.parent_of(10), (9, ["cmd.exe"]))
            self.assertEqual(procwatch.ancestor_host(10), "claude")
            self.assertEqual(procwatch.nearest_host(10), 9)
            self.assertIsNone(procwatch.parent_of(77))


class GitpinWindowsPathTests(unittest.TestCase):
    B = "\\"

    def test_windows_local_remotes_are_native_paths(self):
        from swarm import gitpin
        b = self.B
        self.assertEqual(gitpin.native_path("/C:/work/hub.git", nt=True), "C:/work/hub.git")     # file:///C:/...
        self.assertEqual(gitpin.native_path(f"C:{b}work{b}hub.git", nt=True), f"C:{b}work{b}hub.git")
        self.assertEqual(gitpin.native_path(f"{b}{b}?{b}C:{b}work", nt=True), f"C:{b}work")
        for unc in (f"{b}{b}server{b}share{b}hub.git", f"{b}{b}?{b}UNC{b}server{b}share", "//server/share/x"):
            with self.subTest(unc=unc), self.assertRaises(gitpin.GitPinError):
                gitpin.native_path(unc, nt=True)

    def test_posix_paths_are_unchanged(self):
        from swarm import gitpin
        self.assertEqual(gitpin.native_path("/C:/x", nt=False), "/C:/x")
        self.assertEqual(gitpin.native_path("//srv/x", nt=False), "//srv/x")


class LeadAuthorizationTests(unittest.TestCase):
    """The activation authorizes only the lead row it would have written, still requested and not yet authorized."""
    OWNER = {"roots": ["/w"], "brief": "/w/LEAD.md"}

    def row(self, **kw):
        base = dict(role="engineering_lead", harness="claude", model="opus", workdir="/w", brief_path="/w/LEAD.md",
                    state="requested")
        base.update(kw)
        return SimpleNamespace(**base)

    def authorize(self, row, authorized=False):
        from swarm import drive_activate as da
        from swarm.supervisor import driver_exec
        with mock.patch.object(driver_exec, "authorize_seat") as auth, \
                mock.patch.object(driver_exec, "seat_authorized", return_value=authorized):
            da._authorize_lead(SimpleNamespace(), row, self.OWNER, "claude", "opus")
        return auth.called

    def test_the_row_it_wrote_is_authorized(self):
        self.assertTrue(self.authorize(self.row()))

    def test_a_changed_or_running_or_authorized_row_is_not(self):
        for change in ({"model": "haiku"}, {"role": "engineer"}, {"workdir": "/elsewhere"},
                       {"brief_path": "/x.md"}, {"harness": "codex"}, {"state": "running"}):
            with self.subTest(change=change):
                self.assertFalse(self.authorize(self.row(**change)))
        self.assertFalse(self.authorize(self.row(), authorized=True))
        self.assertFalse(self.authorize(None))


class StopReasonTests(Home):
    """Before any pass: a gone session or the kill switch ends the loop at once (no pass runs for it)."""

    def run_driver(self, watch):
        passes = []
        self.held_job()
        drv = sd.Driver(self.cfg, session="sess-1", watch=watch, pass_fn=lambda jobs: passes.append(jobs) or
                        sd.PassResult("ok"))
        with mock.patch.object(wake, "run_source"):
            return drv.run(), passes

    def test_a_session_already_gone_runs_no_pass(self):
        why, passes = self.run_driver(SimpleNamespace(kind="none", alive=lambda: False, close=lambda: None))
        self.assertEqual((why, passes), ("session gone", []))

    def test_the_kill_switch_runs_no_pass(self):
        settings.off_file().parent.mkdir(parents=True, exist_ok=True)
        settings.off_file().write_text("")
        why, passes = self.run_driver(procwatch.NoWatch())
        self.assertEqual((why, passes), ("kill switch", []))

    def test_an_unreadable_start_is_a_gone_process(self):
        p = idle_process()
        self.addCleanup(stop, p)
        with mock.patch.object(procwatch, "start_token", return_value=None):
            self.assertFalse(procwatch.ProcWatch(p.pid, None).alive())


class ReviewRetryAndCoalesceTests(Home):
    def test_unreadable_ledgers_are_unknown_not_none_left(self):
        """Review rr6: a ledger enumeration that fails is retried, never taken for "no job left"."""
        from swarm.supervisor import driver
        self.held_job()
        with mock.patch.object(ledger, "unfinished", side_effect=OSError("busy")), \
                mock.patch.object(driver, "_scan_files", side_effect=OSError("busy")):
            self.assertIsNone(sd.unfinished(self.key))
            self.assertIsNone(sd.session_jobs(self.key, "sess-1"))
        self.assertEqual(sd.session_jobs(self.key, "sess-1"), ["J"])

    def test_the_driver_retries_through_an_unreadable_moment(self):
        answers = iter([None, None, ["J"], []])         # read once per turn, and once after the pass
        passes = []
        drv = sd.Driver(self.cfg, session="sess-1", pass_fn=lambda jobs: passes.append(jobs) or sd.PassResult("ok"))
        drv.pending = wake.Backoff(0.01, 0.02)
        drv.jobs = lambda: next(answers, [])
        box = {}
        with mock.patch.object(wake, "run_source"):
            t = threading.Thread(target=lambda: box.setdefault("why", drv.run()), daemon=True)
            t.start()
            t.join(30)                                   # never hangs the suite
        self.assertFalse(t.is_alive(), "the driver loop did not end")
        self.assertEqual(box.get("why"), "no unfinished drive job")
        self.assertEqual(passes, [["J"]])

    def test_a_burst_of_touches_is_one_pass(self):
        """Review rr5: fifty touches in a row cost one or two passes, not fifty."""
        self.held_job()
        times = []
        drv = sd.Driver(self.cfg, session="sess-1", pass_fn=lambda jobs: times.append(time.monotonic())
                        or sd.PassResult("ok"))
        done = threading.Event()
        drv.jobs = lambda: [] if done.is_set() else ["J"]
        t = threading.Thread(target=drv.run, daemon=True)
        t.start()
        wait_until(lambda: times, timeout=10)
        time.sleep(0.3)
        for _ in range(50):
            wake.touch(self.key)
        time.sleep(1.5)
        n = len(times) - 1
        done.set()
        drv.hub.signal(wake.CHANGE)
        t.join(10)
        self.assertGreaterEqual(n, 1)
        self.assertLessEqual(n, 5, f"{n} passes for one burst of 50")
