"""Background commands of swarm members (schema 24): recorded by the `swarm bg` wrapper the hooks
wrap a member's background shell call in, listed, and reaped once orphaned (agent finished or job
closed). The reap only ever signals processes it can prove are the recorded command's: this host,
boot and pid namespace, the recorded group, started no earlier than the recorded leader, the row's
tag in their environment. The storage contract runs on every backend (Postgres needs
SWARM_TEST_CONFIG, as elsewhere); the process tests need Linux (/proc, pidfd)."""
from __future__ import annotations

import datetime as dt
import os
import shlex
import signal
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

from support import (FileHarness, MemoryHarness, PostgresHarness, SqliteHarness, wait_until,  # noqa: F401
                     ROOT)

from swarm import bg
from swarm.board import BgCommand, SCHEMA_VERSION
from swarm.board.base import BG_COMMAND_MAX

LINUX = sys.platform.startswith("linux") and hasattr(os, "pidfd_open")


def start_kw(**over):
    kw = dict(host="h1", boot="b/1", pid=100, pgid=100, proc_start=5000, tag="t")
    kw.update(over)
    return kw


# --------------------------------------------------------------------------- storage contract

class BgContract:
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
        self.b.open_job("J", "bg", None, None, None)
        self.b.open_job("K", "other", None, None, None)
        self.homer = self.b.allocate_name("k-homer", "J", "worker")
        self.marge = self.b.allocate_name("k-marge", "J", "worker")

    def test_schema_is_24(self):
        self.assertGreaterEqual(SCHEMA_VERSION, 24)
        self.assertEqual(type(self.b).schema_version(self.h.cfg), SCHEMA_VERSION)

    def test_setup_twice_is_idempotent_and_keeps_rows(self):
        from swarm.board import setup_board
        bid = self.b.bg_start("J", "k-homer", self.homer, "sleep 60", **start_kw())
        setup_board(self.h.cfg, {})
        setup_board(self.h.cfg, {})
        with self.h.board() as b:
            self.assertEqual([r.id for r in b.bg_commands("J")], [bid])

    def test_start_records_every_field_and_end_is_once(self):
        before = self.b.now()
        bid = self.b.bg_start("J", "k-homer", self.homer, "sleep 60", **start_kw())
        (r,) = self.b.bg_commands("J")
        self.assertIsInstance(r, BgCommand)
        self.assertEqual((r.id, r.job, r.agent_key, r.agent_name, r.command, r.host, r.boot, r.pid, r.pgid,
                          r.proc_start, r.tag), (bid, "J", "k-homer", self.homer, "sleep 60", "h1", "b/1", 100,
                                                 100, 5000, "t"))
        self.assertTrue(r.running)
        self.assertGreaterEqual(r.started_at, before - dt.timedelta(seconds=1))
        self.assertTrue(self.b.bg_end(bid, "exited", 3))
        self.assertFalse(self.b.bg_end(bid, "reaped", None, "late"))   # the first end stands
        (r,) = self.b.bg_commands("J")
        self.assertEqual((r.outcome, r.exit_code, r.detail, r.running), ("exited", 3, None, False))
        self.assertIsNotNone(r.ended_at)
        self.assertEqual(self.b.bg_commands("J", running=True), [])
        self.assertFalse(self.b.bg_end(9999, "gone"))

    def test_a_reap_claims_a_signal_exit_recorded_after_it_signalled(self):
        bid = self.b.bg_start("J", "k-homer", self.homer, "sleep 60", **start_kw())
        signalled = self.b.now() - dt.timedelta(seconds=1)
        self.assertTrue(self.b.bg_end(bid, "exited", 143))             # the wrapper wins the race
        self.assertTrue(self.b.bg_end(bid, "reaped", None, "1 process(es): SIGTERM", signalled_at=signalled))
        (r,) = self.b.bg_commands("J")
        self.assertEqual((r.outcome, r.exit_code, r.detail), ("reaped", 143, "1 process(es): SIGTERM"))
        self.assertFalse(self.b.bg_end(bid, "killed", None, "again"))  # without signalled_at: first end stands
        clean = self.b.bg_start("J", "k-homer", self.homer, "true", **start_kw())
        self.b.bg_end(clean, "exited", 0)
        self.assertFalse(self.b.bg_end(clean, "reaped", None, "x", signalled_at=signalled))   # not a signal exit

    def test_checks_refuse_bad_rows(self):
        with self.assertRaises(ValueError):
            self.b.bg_start("nope", "k", self.homer, "x", **start_kw())
        with self.assertRaises(ValueError):
            self.b.bg_start("J", "k", "bad\nname", "x", **start_kw())
        with self.assertRaises(ValueError):
            self.b.bg_start("J", "k", self.homer, "   ", **start_kw())
        with self.assertRaises(ValueError):
            self.b.bg_start("J", "k", self.homer, "x", **start_kw(host=" "))
        with self.assertRaises(ValueError):
            self.b.bg_start("J", "k", self.homer, "x", **start_kw(pid=-1))
        bid = self.b.bg_start("J", "k", self.homer, "x", **start_kw())
        with self.assertRaises(ValueError):
            self.b.bg_end(bid, "exploded")
        self.assertEqual(len(self.b.bg_commands()), 1)

    def test_command_is_one_line_and_capped(self):
        self.b.bg_start("J", "k-homer", self.homer, "echo a\n  echo b\t" + "x" * 2000, **start_kw())
        (r,) = self.b.bg_commands("J")
        self.assertNotIn("\n", r.command)
        self.assertTrue(r.command.startswith("echo a echo b x"))
        self.assertLessEqual(len(r.command), BG_COMMAND_MAX)

    def test_filters_by_job_and_running(self):
        a = self.b.bg_start("J", "k-homer", self.homer, "a", **start_kw())
        b = self.b.bg_start("K", "k-x", self.homer, "b", **start_kw())
        c = self.b.bg_start("J", "k-marge", self.marge, "c", **start_kw())
        self.b.bg_end(c, "exited", 0)
        self.assertEqual([r.id for r in self.b.bg_commands()], [a, b, c])
        self.assertEqual([r.id for r in self.b.bg_commands("J")], [a, c])
        self.assertEqual([r.id for r in self.b.bg_commands("J", running=True)], [a])
        self.assertEqual([r.id for r in self.b.bg_commands(running=True)], [a, b])

    def test_orphans_are_running_rows_of_finished_agents_or_closed_jobs(self):
        live = self.b.bg_start("J", "k-homer", self.homer, "a", **start_kw())
        done = self.b.bg_start("J", "k-marge", self.marge, "b", **start_kw())
        ghost = self.b.bg_start("J", "k-nobody", self.homer, "c", **start_kw())
        ended = self.b.bg_start("J", "k-marge", self.marge, "d", **start_kw())
        self.b.bg_end(ended, "exited", 0)
        self.b.agent_stopped("k-marge")
        self.assertEqual(sorted(r.id for r in self.b.bg_orphans("J")), [done, ghost])
        self.assertEqual(self.b.bg_counts("J"), (3, 2))
        self.b.close_job("J", "completed", None)
        self.assertEqual(sorted(r.id for r in self.b.bg_orphans("J")), [live, done, ghost])
        self.assertEqual(self.b.bg_counts(), (3, 3))

    def test_an_agent_active_on_another_job_is_not_finished(self):
        """Its row on J is gone (moved to K while the command ran): still its live command."""
        self.b.allocate_name("k-moved", "K", "worker")
        self.b.bg_start("J", "k-moved", self.homer, "a", **start_kw())
        self.assertEqual(self.b.bg_orphans("J"), [])

    def test_purge_keeps_running_rows_and_drops_old_ended_ones(self):
        running = self.b.bg_start("J", "k-homer", self.homer, "a", **start_kw())
        old = self.b.bg_start("J", "k-homer", self.homer, "b", **start_kw())
        recent = self.b.bg_start("J", "k-homer", self.homer, "c", **start_kw())
        for bid in (old, recent):
            self.b.bg_end(bid, "exited", 0)
        days = int(self.h.cfg["board"]["retention_days"])
        self.h.backdate_bg(running, started_at=(days + 30) * 86400)
        self.h.backdate_bg(old, started_at=(days + 30) * 86400, ended_at=(days + 1) * 86400)
        self.b.purge()
        self.assertEqual([r.id for r in self.b.bg_commands()], [running, recent])


class MemoryBg(BgContract, unittest.TestCase):
    harness_factory = MemoryHarness


class FileBg(BgContract, unittest.TestCase):
    harness_factory = FileHarness


class SqliteBg(BgContract, unittest.TestCase):
    harness_factory = SqliteHarness


@unittest.skipUnless(os.environ.get("SWARM_TEST_CONFIG"), "set SWARM_TEST_CONFIG to a throwaway Postgres board config")
class PostgresBg(BgContract, unittest.TestCase):
    harness_factory = staticmethod(lambda: PostgresHarness(os.environ["SWARM_TEST_CONFIG"]))


# --------------------------------------------------------------------------- what is recorded

class CleanCommandTests(unittest.TestCase):
    def test_env_assignments_are_dropped_and_secrets_redacted(self):
        text = bg.clean_command('GH_TOKEN=ghp_abcdefghijklmnopqrstuvwxyz0123456789 FOO="a b" gh run list; '
                                'export PGPASSWORD=hunter2; env API_KEY=xyz make test')
        self.assertNotIn("GH_TOKEN", text)
        self.assertNotIn("ghp_abcdefghijklmnopqrstuvwxyz", text)
        self.assertNotIn("hunter2", text)
        self.assertNotIn("API_KEY", text)
        self.assertIn("gh run list", text)
        self.assertIn("make test", text)

    def test_wrap_command_round_trips_through_the_shell(self):
        cmd = """for i in 1 2; do echo "it's $i"; done > /dev/null"""
        wrapped = bg.wrap_command(cmd, "J", "agent-1", "Homer Simpson", {"_config_path": "/x/config.toml"})
        words = shlex.split(wrapped)
        self.assertEqual(words[1:], ["--config", "/x/config.toml", "bg", "--job", "J", "--key", "agent-1",
                                     "--as", "Homer Simpson", "--", cmd])
        self.assertIn("swarm", words[0])


# --------------------------------------------------------------------------- the reap, on real processes

def proc_alive(pid: int) -> bool:
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except OSError:
        return False
    return state != "Z"


@unittest.skipUnless(LINUX, "the reap needs Linux /proc and pidfd")
class ReapTests(unittest.TestCase):
    def setUp(self):
        self.h = MemoryHarness(store=f"bg-{self.id()}")
        self.h.reset()
        self.b = self.h.board()
        self.addCleanup(self.b.close)
        self.b.open_job("J", "bg", None, None, None)
        self.name = self.b.allocate_name("k1", "J", "worker")
        self.procs = []
        self.addCleanup(self._kill_all)
        self.out = []

    def _kill_all(self):
        for p in self.procs:
            if p.poll() is None:
                try:
                    os.killpg(p.pid, signal.SIGKILL)
                except OSError:
                    p.kill()
            p.wait(5)

    def spawn(self, script: str = "sleep 60", tag: str | None = "tag-1", wait_members: int = 1):
        env = dict(os.environ)
        if tag:
            env[bg.TAG_ENV] = tag
        p = subprocess.Popen(["bash", "-c", script], env=env, process_group=0,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.procs.append(p)
        wait_until(lambda: bg.proc_start(p.pid) is not None, timeout=10)
        if tag:
            wait_until(lambda: len(_members_raw(p.pid, tag)) >= wait_members, timeout=10)
        return p

    def record(self, p, tag="tag-1", key="k1", **over):
        kw = dict(host=bg.this_host(), boot=bg.boot_key(), pid=p.pid, pgid=p.pid,
                  proc_start=bg.proc_start(p.pid), tag=tag)
        kw.update(over)
        return self.b.bg_start("J", key, self.name, "sleep 60", **kw)

    def row(self, bid):
        return next(r for r in self.b.bg_commands("J") if r.id == bid)

    def finish_agent(self):
        self.b.agent_stopped("k1")

    def reap(self, **kw):
        kw.setdefault("say", self.out.append)
        return bg.reap_orphans(self.b, job="J", **kw)

    def test_orphan_is_terminated_and_recorded_reaped(self):
        p = self.spawn("sleep 60 & sleep 60; wait", wait_members=3)
        bid = self.record(p)
        self.finish_agent()
        [(row, outcome, why)] = self.reap()
        self.assertEqual(outcome, "reaped")
        self.assertIn("3 process(es): SIGTERM", why)
        p.wait(5)
        r = self.row(bid)
        self.assertEqual((r.outcome, r.running), ("reaped", False))
        self.assertTrue(any("reaped" in line for line in self.out))

    def test_term_ignoring_group_is_killed_after_the_grace(self):
        p = self.spawn("trap '' TERM; sleep 60; true", wait_members=2)
        bid = self.record(p)
        self.finish_agent()
        started = time.monotonic()
        [(_, outcome, why)] = self.reap(grace=0.5)
        self.assertEqual(outcome, "killed")
        self.assertIn("SIGKILL", why)
        self.assertGreaterEqual(time.monotonic() - started, 0.5)
        p.wait(5)
        self.assertEqual(self.row(bid).outcome, "killed")

    def test_a_live_agents_command_is_not_reaped(self):
        p = self.spawn()
        bid = self.record(p)
        self.assertEqual(self.reap(), [])
        self.assertTrue(proc_alive(p.pid))
        self.assertTrue(self.row(bid).running)

    def test_closed_job_makes_its_commands_orphans(self):
        p = self.spawn()
        self.record(p)
        self.b.close_job("J", "completed", None)
        [(_, outcome, _)] = self.reap()
        self.assertEqual(outcome, "reaped")
        p.wait(5)

    def test_reused_pid_is_never_signalled(self):
        """The recorded leader is gone and its pid now belongs to an unrelated process (no tag,
        another start time): nothing is signalled; the row is closed as gone."""
        other = self.spawn(tag=None)
        bid = self.record(other, proc_start=bg.proc_start(other.pid) - 1)
        self.finish_agent()
        [(_, outcome, why)] = self.reap(grace=0.2)
        self.assertEqual(outcome, "gone")
        self.assertIn("reused", why)
        time.sleep(0.2)
        self.assertTrue(proc_alive(other.pid))
        self.assertEqual(self.row(bid).outcome, "gone")

    def test_start_time_mismatch_alone_protects_a_tagged_process(self):
        """Even a process carrying the tag is not touched when it started BEFORE the recorded
        leader (it can't be a member of that group's command)."""
        p = self.spawn()
        self.record(p, proc_start=bg.proc_start(p.pid) + 10_000)
        self.finish_agent()
        [(_, outcome, _)] = self.reap(grace=0.2)
        self.assertEqual(outcome, "gone")
        time.sleep(0.2)
        self.assertTrue(proc_alive(p.pid))

    def test_wrong_tag_is_never_signalled(self):
        p = self.spawn(tag="someone-else")
        self.record(p, tag="tag-1")
        self.finish_agent()
        [(_, outcome, why)] = self.reap(grace=0.2)
        self.assertIsNone(outcome)    # its leader with the recorded start time: unproven, left alone
        self.assertIn("left alone", why)
        time.sleep(0.2)
        self.assertTrue(proc_alive(p.pid))

    def test_foreign_host_is_never_touched(self):
        p = self.spawn()
        bid = self.record(p, host="some-other-box")
        self.finish_agent()
        self.assertEqual(self.reap(), [])                       # not even considered here
        out = bg.reap(self.b, [self.row(bid)], grace=0.2, say=self.out.append)
        self.assertEqual(out[0][1], None)
        self.assertIn("not this host", out[0][2])
        time.sleep(0.2)
        self.assertTrue(proc_alive(p.pid))
        self.assertTrue(self.row(bid).running)

    def test_other_pid_namespace_of_this_boot_is_left_alone(self):
        p = self.spawn()
        boot = bg.boot_key().split("/")[0]
        bid = self.record(p, boot=f"{boot}/1")
        self.finish_agent()
        [(_, outcome, why)] = self.reap(grace=0.2)
        self.assertIsNone(outcome)
        time.sleep(0.2)
        self.assertTrue(proc_alive(p.pid))
        self.assertTrue(self.row(bid).running)

    def test_reboot_closes_the_row_without_signalling(self):
        p = self.spawn()
        bid = self.record(p, boot="another-boot-id/4026531836")
        self.finish_agent()
        [(_, outcome, why)] = self.reap(grace=0.2)
        self.assertEqual(outcome, "gone")
        self.assertIn("rebooted", why)
        time.sleep(0.2)
        self.assertTrue(proc_alive(p.pid))   # a pid of this boot is not the recorded one: untouched
        self.assertEqual(self.row(bid).outcome, "gone")

    def test_unverifiable_row_is_left_alone(self):
        p = self.spawn()
        bid = self.record(p, tag=None)
        self.finish_agent()
        [(_, outcome, why)] = self.reap(grace=0.2)
        self.assertIsNone(outcome)
        self.assertIn("verify", why)
        self.assertTrue(proc_alive(p.pid))
        self.assertTrue(self.row(bid).running)

    def test_vanished_command_is_marked_gone(self):
        p = self.spawn()
        bid = self.record(p)
        os.killpg(p.pid, signal.SIGKILL)
        p.wait(5)
        self.finish_agent()
        [(_, outcome, why)] = self.reap(grace=0.2)
        self.assertEqual((outcome, why), ("gone", "no longer running"))

    def test_dry_run_signals_and_records_nothing(self):
        p = self.spawn()
        bid = self.record(p)
        self.finish_agent()
        self.reap(dry_run=True)
        self.assertTrue(any(line.startswith("would reap") for line in self.out))
        time.sleep(0.2)
        self.assertTrue(proc_alive(p.pid))
        self.assertTrue(self.row(bid).running)

    def test_agent_filter_and_no_pidfd_means_no_signal(self):
        p = self.spawn()
        bid = self.record(p)
        self.finish_agent()
        self.assertEqual(bg.reap_orphans(self.b, job="J", agent="someone-else", say=self.out.append), [])
        with mock.patch.object(bg, "_pidfd_ok", return_value=False):
            [(_, outcome, why)] = self.reap()
        self.assertIsNone(outcome)
        self.assertIn("pidfd", why)
        self.assertTrue(proc_alive(p.pid))
        self.assertTrue(self.row(bid).running)


def _members_raw(pgid, tag):
    from swarm.supervisor.runner import _members
    return _members(pgid, tag, 0, bg.TAG_ENV)




# --------------------------------------------------------------------------- the wrapper

@unittest.skipUnless(LINUX, "the wrapper records /proc start times (Linux)")
class WrapperTests(unittest.TestCase):
    def setUp(self):
        self.h = MemoryHarness(store=f"bgw-{self.id()}")
        self.h.reset()
        with self.h.board() as b:
            b.open_job("J", "bg", None, None, None)
            self.name = b.allocate_name("k1", "J", "worker")

    def rows(self):
        with self.h.board() as b:
            return b.bg_commands("J")

    def test_runs_records_and_passes_the_exit_code(self):
        rc = bg.run_wrapper(self.h.cfg, "J", "k1", self.name, ["--", 'test "$SWARM_BG_TAG" && exit 3'])
        self.assertEqual(rc, 3)
        (r,) = self.rows()
        self.assertEqual((r.agent_key, r.agent_name, r.outcome, r.exit_code), ("k1", self.name, "exited", 3))
        self.assertEqual((r.host, r.boot, r.pgid), (bg.this_host(), bg.boot_key(), r.pid))
        self.assertIsNotNone(r.proc_start)
        self.assertEqual(len(r.tag), 24)
        self.assertIn("exit 3", r.command)

    def test_an_argv_runs_as_is(self):
        self.assertEqual(bg.run_wrapper(self.h.cfg, "J", "k1", self.name, ["--", "true", "x"]), 0)
        self.assertEqual(self.rows()[0].command, "true x")

    def test_unreachable_board_still_runs_the_command(self):
        self.h.set_available(False)
        try:
            with mock.patch("sys.stderr") as err:
                rc = bg.run_wrapper(self.h.cfg, "J", "k1", self.name, ["exit 4"])
        finally:
            self.h.set_available(True)
        self.assertEqual(rc, 4)
        self.assertIn("not recorded", "".join(c.args[0] for c in err.write.call_args_list))
        self.assertEqual(self.rows(), [])

    def test_unknown_job_still_runs_the_command(self):
        self.assertEqual(bg.run_wrapper(self.h.cfg, "nope", "k1", self.name, ["exit 0"]), 0)

    def test_signal_ends_the_group_and_is_the_exit_code(self):
        """A real `swarm bg` process: SIGTERM to the wrapper reaches the command's whole group."""
        import tempfile
        tmp = Path(tempfile.mkdtemp(prefix="swarm-bgw-", dir=os.environ.get("TMPDIR")))
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        cfg_path = tmp / "config.toml"
        sq = tmp / "board.sqlite3"
        cfg_path.write_text(f'[board]\nbackend = "sqlite"\n[sqlite]\npath = "{sq}"\n')
        from swarm import cli
        from swarm.board import open_board, setup_board
        cfg = cli.load_config(cfg_path)
        setup_board(cfg, {"simpsons": ["Homer Simpson"]})
        with open_board(cfg) as b:
            b.open_job("J", "bg", None, None, None)
        env = {**os.environ, "PYTHONPATH": str(ROOT / "lib"), "SWARM_AUTO_INIT": "0"}
        p = subprocess.Popen([sys.executable, "-m", "swarm.cli", "--config", str(cfg_path), "bg", "--job", "J",
                              "--key", "k1", "--as", "Homer Simpson", "--", "sleep 60 & sleep 60; wait"],
                             env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        def recorded():
            with open_board(cfg) as b:
                return b.bg_commands("J")
        wait_until(lambda: recorded(), timeout=20)
        (r,) = recorded()
        wait_until(lambda: len(_members_raw(r.pgid, r.tag)) >= 3, timeout=10)
        p.send_signal(signal.SIGTERM)
        self.assertEqual(p.wait(20), 143)
        wait_until(lambda: not _members_raw(r.pgid, r.tag), timeout=10)
        (r,) = recorded()
        self.assertEqual((r.outcome, r.exit_code), ("exited", 143))


class CallerEnvTests(unittest.TestCase):
    def test_launcher_changes_are_undone_and_the_record_removed(self):
        env = {"PATH": "/bin", "PYTHONPATH": "/plugin/lib:/mine", "PYTHONPYCACHEPREFIX": "/pyc",
               "_SWARM_PRE_PYTHONPATH": "/mine", "_SWARM_LAUNCHER": "1", "X": "y"}
        self.assertEqual(bg.caller_env(env), {"PATH": "/bin", "PYTHONPATH": "/mine", "X": "y"})
        env = {"PYTHONDONTWRITEBYTECODE": "1", "_SWARM_PRE_PYTHONDONTWRITEBYTECODE": "", "_SWARM_LAUNCHER": "1"}
        self.assertEqual(bg.caller_env(env), {"PYTHONDONTWRITEBYTECODE": ""})
        self.assertEqual(bg.caller_env({"PYTHONPATH": "/x"}), {"PYTHONPATH": "/x"})   # no launcher: as is


class SqliteBoardDir:
    """A throwaway SQLite board with job J and agent k1 (Homer Simpson), for real `swarm bg` runs."""

    def setUp(self):
        import tempfile
        from swarm import cli
        from swarm.board import open_board, setup_board
        self.tmp = Path(tempfile.mkdtemp(prefix="swarm-bgl-", dir=os.environ.get("TMPDIR")))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.cfg_path = self.tmp / "config.toml"
        self.cfg_path.write_text(f'[board]\nbackend = "sqlite"\n[sqlite]\npath = "{self.tmp / "board.sqlite3"}"\n')
        self.cfg = cli.load_config(self.cfg_path)
        setup_board(self.cfg, {"simpsons": ["Homer Simpson"]})
        with open_board(self.cfg) as b:
            b.open_job("J", "bg", None, None, None)
            self.name = b.allocate_name("k1", "J", "worker")
        self.procs = []
        self.addCleanup(self._stop)

    def _stop(self):
        for p in self.procs:
            if p.poll() is None:
                p.kill()
            p.wait(10)

    def board(self):
        from swarm.board import open_board
        return open_board(self.cfg)

    def rows(self):
        with self.board() as b:
            return b.bg_commands("J")

    def wrapper(self, command: str):
        env = {**os.environ, "PYTHONPATH": str(ROOT / "lib"), "SWARM_AUTO_INIT": "0"}
        p = subprocess.Popen([sys.executable, "-m", "swarm.cli", "--config", str(self.cfg_path), "bg", "--job", "J",
                              "--key", "k1", "--as", self.name, "--", command],
                             env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.procs.append(p)
        wait_until(lambda: self.rows(), timeout=20)
        return p, self.rows()[-1]


@unittest.skipUnless(LINUX, "needs Linux /proc and pidfd")
class LiveWrapperReapTests(SqliteBoardDir, unittest.TestCase):
    """The incident case: the agent finished while its wrapped background command runs. The reap
    stops it through the live wrapper, and the row says reaped/killed, not the wrapper's 143/137."""

    def test_reap_through_a_live_wrapper_records_reaped(self):
        p, row = self.wrapper("sleep 60 & sleep 60; wait")
        wait_until(lambda: len(_members_raw(row.pgid, row.tag)) >= 3, timeout=10)
        with self.board() as b:
            b.agent_stopped("k1")
            [(_, outcome, _)] = bg.reap_orphans(b, job="J", say=lambda line: None)
        self.assertEqual(outcome, "reaped")
        self.assertEqual(p.wait(20), 143)                     # the wrapper saw its command die of TERM
        (r,) = self.rows()
        self.assertEqual((r.outcome, r.exit_code), ("reaped", 143))

    def test_term_ignoring_command_through_a_live_wrapper_records_killed(self):
        p, row = self.wrapper("trap '' TERM; sleep 60; true")
        wait_until(lambda: len(_members_raw(row.pgid, row.tag)) >= 2, timeout=10)
        with self.board() as b:
            b.agent_stopped("k1")
            [(_, outcome, _)] = bg.reap_orphans(b, job="J", grace=0.5, say=lambda line: None)
        self.assertEqual(outcome, "killed")
        self.assertEqual(p.wait(20), 137)
        (r,) = self.rows()
        self.assertEqual((r.outcome, r.exit_code), ("killed", 137))

    def test_a_signal_exit_before_the_reap_is_not_claimed(self):
        with self.board() as b:
            bid = b.bg_start("J", "k1", self.name, "x", host="h", boot=None, pid=1, pgid=1, proc_start=1, tag="t")
            b.bg_end(bid, "exited", 143)
            later = b.now() + dt.timedelta(seconds=5)
            self.assertFalse(b.bg_end(bid, "reaped", None, "late", signalled_at=later))
            self.assertEqual(b.bg_commands("J")[0].outcome, "exited")
            bid2 = b.bg_start("J", "k1", self.name, "y", host="h", boot=None, pid=1, pgid=1, proc_start=1, tag="t")
            b.bg_end(bid2, "exited", 0)
            self.assertFalse(b.bg_end(bid2, "reaped", None, "x", signalled_at=b.now() - dt.timedelta(seconds=5)))


@unittest.skipUnless(LINUX, "the launcher test runs bin/swarm (POSIX sh)")
class WrapperEnvironmentTests(SqliteBoardDir, unittest.TestCase):
    def test_the_wrapped_command_sees_exactly_the_unwrapped_environment(self):
        from support import temp_venv
        home = self.tmp / "home"
        (home / ".local/share/swarm").mkdir(parents=True)
        pyc = self.tmp / "pyc"
        pyc.mkdir(mode=0o700)
        agent_env = {k: v for k, v in os.environ.items()
                     if not k.startswith(("PYTHON", "_SWARM_", "SWARM_"))}
        agent_env.update(HOME=str(home), SWARM_VENV=str(temp_venv()), SWARM_PYCACHE=str(pyc),
                         SWARM_AUTO_INIT="0", PYTHONPATH="/agent/own/path")

        def env_of(argv, out):
            subprocess.run(argv, env=agent_env, check=True, timeout=60,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            pairs = (Path(out).read_bytes().split(b"\0"))
            return dict(x.decode().split("=", 1) for x in pairs if x)
        plain = env_of(["bash", "-c", f"env -0 > {self.tmp}/plain"], self.tmp / "plain")
        wrapped = env_of([str(ROOT / "bin/swarm"), "--config", str(self.cfg_path), "bg", "--job", "J", "--key", "k1",
                          "--as", self.name, "--", f"env -0 > {self.tmp}/wrapped"], self.tmp / "wrapped")
        tag = wrapped.pop(bg.TAG_ENV)
        self.assertEqual(len(tag), 24)
        self.assertEqual(wrapped, plain)
        self.assertEqual(wrapped["PYTHONPATH"], "/agent/own/path")
        self.assertNotIn("PYTHONPYCACHEPREFIX", wrapped)
        (r,) = self.rows()
        self.assertEqual((r.outcome, r.exit_code), ("exited", 0))   # and it was recorded


if __name__ == "__main__":
    unittest.main()
