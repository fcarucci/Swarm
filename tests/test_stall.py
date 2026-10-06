"""A stalled board query must never freeze the swarm.

Observed: `swarm watch` froze for good in psycopg's poll() because a query's result never came
back through the connection pooler, and psycopg has no query timeout of its own. The fix gives
every Postgres round trip a client-side deadline ([database] query_timeout_seconds) that raises
BoardUnavailable, and makes the long-lived commands (`watch`, `tail`) reconnect, LISTEN again
and carry on; hooks and one-shot commands still fail fast.

The first two classes run on any backend ($SWARM_TEST_BACKEND): a board that raises
BoardUnavailable mid-loop stands in for the stall. The Postgres classes need $SWARM_TEST_CONFIG
(a THROWAWAY database) and reproduce the real stall with a TCP proxy that swallows the server's
replies, and the busy-server case with pg_sleep.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from support import BIN, E2E_BACKEND, HARNESSES, ROOT, PostgresHarness, tq  # noqa: F401  (sets sys.path)

from swarm import board as board_pkg  # noqa: E402
from swarm import cli as swarm  # noqa: E402
from swarm.board import BoardUnavailable  # noqa: E402


# --------------------------------------------------------------------------- any backend

class FlakyBoard:
    """Wraps a real board; `fail_on` names a method whose call number `after` (1-based) raises
    BoardUnavailable, as a query that hit its deadline does. `stop_on` raises KeyboardInterrupt
    at a method's n-th call, which ends a watch/tail loop the way Ctrl-C does."""

    def __init__(self, inner, log: list, fail_on: tuple | None = None, stop_on: tuple | None = None):
        self.inner, self.log, self.fail_on, self.stop_on = inner, log, fail_on, stop_on
        self.calls: dict[str, int] = {}
        self.closed = False

    def __getattr__(self, name):
        target = getattr(self.inner, name)
        if not callable(target):
            return target

        def call(*args, **kwargs):
            n = self.calls[name] = self.calls.get(name, 0) + 1
            self.log.append((id(self), name, args, kwargs))
            if self.fail_on and self.fail_on == (name, n):
                raise BoardUnavailable("query exceeded its deadline") from TimeoutError("stalled")
            if self.stop_on and self.stop_on == (name, n):
                raise KeyboardInterrupt
            return target(*args, **kwargs)
        return call

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        self.closed = True
        self.inner.close()


class ReconnectTests(unittest.TestCase):
    """watch/tail survive a board that becomes unavailable mid-loop; one-shot commands don't retry."""

    def setUp(self):
        self.h = HARNESSES[E2E_BACKEND]()
        self.addCleanup(self.h.close)
        self.h.reset()
        self.cfg = self.h.cfg
        with self.h.board() as b:
            b.open_job("J", None, None, None, "me")
            b.allocate_name("k1", "J")
            self.name = b.active_agent_name("k1")
            b.post("J", self.name, "before the stall")
        self.log: list = []
        self.opened: list[FlakyBoard] = []
        self.sleeps: list[float] = []

    def plan(self, *boards: dict):
        """open_board returns FlakyBoards configured by `boards`, in order (the last repeats)."""
        def opener(cfg, **kw):
            spec = boards[min(len(self.opened), len(boards) - 1)]
            if spec.get("refuse"):
                self.opened.append(None)
                raise BoardUnavailable("connection refused") from ConnectionRefusedError()
            fb = FlakyBoard(self.h.board(), self.log, spec.get("fail_on"), spec.get("stop_on"))
            if spec.get("post_on_open"):
                fb.inner.post("J", self.name, spec["post_on_open"])
            self.opened.append(fb)
            return fb
        return mock.patch.object(board_pkg, "open_board", opener)

    def run_cli(self, fn, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch.object(swarm.time, "sleep", self.sleeps.append):
            rc = fn(*args)
        return rc, out.getvalue()

    def subscribed(self, fb) -> list:
        return [kw.get("messages_only", a[0] if a else False)
                for bid, name, a, kw in self.log if bid == id(fb) and name == "subscribe"]

    def test_tail_reconnects_resubscribes_and_carries_on(self):
        # board 1 fails on its first poll; board 2 (a message was posted meanwhile) runs two
        # polls and is stopped by Ctrl-C on the third wait
        with self.plan({"fail_on": ("messages_after", 1)},
                       {"post_on_open": "during the stall", "stop_on": ("wait_for_change", 3)}):
            rc, out = self.run_cli(swarm.cmd_tail, self.cfg, "J", 5, 0.01, False, False)
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.opened), 2)
        self.assertTrue(all(fb.closed for fb in self.opened))
        self.assertEqual(self.subscribed(self.opened[0]), [True])
        self.assertEqual(self.subscribed(self.opened[1]), [True])       # LISTEN again
        self.assertIn("reconnecting", out)
        self.assertEqual(out.count("before the stall"), 1)             # no backlog replay
        self.assertEqual(out.count("during the stall"), 1)             # picked up where it left off
        self.assertEqual(len([ln for ln in out.splitlines() if "reconnecting" in ln]), 1)
        self.assertEqual(out.count("--- following"), 1)

    def test_tail_survives_a_stall_during_its_startup(self):
        """The backlog is printed, then the next startup query stalls: no crash, no replay."""
        with self.plan({"fail_on": ("now", 1)},
                       {"post_on_open": "during the stall", "stop_on": ("wait_for_change", 2)}):
            rc, out = self.run_cli(swarm.cmd_tail, self.cfg, "J", 5, 0.01, True, False)
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.opened), 2)
        self.assertEqual(out.count("before the stall"), 1)
        self.assertEqual(out.count("during the stall"), 1)
        self.assertEqual(out.count("--- following"), 1)

    def test_tail_backs_off_while_the_board_stays_unreachable(self):
        with self.plan({"fail_on": ("messages_after", 1)}, {"refuse": True}, {"refuse": True},
                       {"refuse": True}, {"stop_on": ("wait_for_change", 1)}):
            rc, out = self.run_cli(swarm.cmd_tail, self.cfg, "J", 0, 0.01, False, False)
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.opened), 5)
        delays = [s for s in self.sleeps if s >= 1]
        self.assertEqual(delays, sorted(delays))                        # never shorter
        self.assertGreater(delays[-1], delays[0])                       # and growing
        self.assertLessEqual(max(delays), swarm.RECONNECT_MAX_SECONDS)

    def test_tail_retries_when_the_first_connection_fails(self):
        """Observed: `swarm watch`/`tail` died with a BoardUnavailable traceback when the board
        was unreachable at startup. They must wait for it instead, with the stall notice."""
        with self.plan({"refuse": True}, {"refuse": True}, {"stop_on": ("wait_for_change", 1)}):
            rc, out = self.run_cli(swarm.cmd_tail, self.cfg, "J", 5, 0.01, False, False)
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.opened), 3)
        notices = [ln for ln in out.splitlines() if "board unreachable" in ln]
        self.assertEqual(len(notices), 2)                               # one per failed attempt
        self.assertIn("connection refused", notices[0])
        self.assertIn("retrying in", notices[0])
        delays = [s for s in self.sleeps if s >= 1]
        self.assertEqual(len(delays), 2)
        self.assertGreater(delays[1], delays[0])                        # backing off
        self.assertEqual(out.count("--- following"), 1)                 # then as normal
        self.assertEqual(out.count("before the stall"), 1)
        self.assertNotIn("reconnected", out)

    def test_tail_initial_retries_stop_on_ctrl_c(self):
        def sleep(seconds):
            self.sleeps.append(seconds)
            if len(self.sleeps) == 3:
                raise KeyboardInterrupt
        out = io.StringIO()
        with self.plan({"refuse": True}), contextlib.redirect_stdout(out), \
                mock.patch.object(swarm.time, "sleep", sleep):
            rc = swarm.cmd_tail(self.cfg, "J", 0, 0.01, False, False)
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.opened), 3)
        self.assertLessEqual(max(self.sleeps), swarm.RECONNECT_MAX_SECONDS)

    def test_watch_retries_when_the_first_connection_fails(self):
        with self.plan({"refuse": True}, {"refuse": True}, {"stop_on": ("wait_for_change", 2)}):
            rc, out = self.run_cli(swarm.cmd_watch, self.cfg, "J", 0.01, False)
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.opened), 3)
        self.assertEqual(out.count("board unreachable"), 2)
        self.assertIn("retrying in", out)
        self.assertEqual(len(self.subscribed(self.opened[2])), 1)
        self.assertIn("before the stall", out)                          # the view is drawn

    def test_watch_reconnects_resubscribes_and_carries_on(self):
        with self.plan({"fail_on": ("job_status", 2)},
                       {"post_on_open": "during the stall", "stop_on": ("wait_for_change", 3)}):
            rc, out = self.run_cli(swarm.cmd_watch, self.cfg, "J", 0.01, False)
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.opened), 2)
        self.assertTrue(all(fb.closed for fb in self.opened))
        self.assertEqual(len(self.subscribed(self.opened[0])), 1)
        self.assertEqual(len(self.subscribed(self.opened[1])), 1)      # LISTEN again
        self.assertIn("reconnecting", out)
        self.assertIn("during the stall", out)                         # redrawn from board 2

    def test_one_shot_command_fails_fast(self):
        tmp = Path(tempfile.mkdtemp(prefix="swarm-stall-", dir=os.environ.get("TMPDIR")))
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        config = tmp / "config.toml"   # its own spool dir: never the user's config or spool
        config.write_text(f'[board]\nspool_dir = {tq(tmp / "spool")}\n')
        with self.plan({"fail_on": ("job_status", 1)}):
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                rc, out = self.run_cli(swarm.main, ["--config", str(config), "status", "--job", "J"])
        self.assertEqual(rc, 1)
        self.assertEqual(len(self.opened), 1)                           # no retry
        self.assertEqual(self.sleeps, [])
        self.assertIn("cannot reach the board database", err.getvalue())


class DeadlineConfigTests(unittest.TestCase):
    def test_prepared_statements_off_by_default(self):
        cfg = swarm.load_config(Path("/nonexistent/swarm.toml"))
        self.assertIs(cfg["database"]["prepared_statements"], False)

    def test_connect_passes_prepare_threshold(self):
        try:
            from swarm.board import postgres
        except ImportError:
            self.skipTest("psycopg not installed")
        cfg = swarm.load_config(Path("/nonexistent/swarm.toml"))
        seen = []

        def fake_connect(**kwargs):
            seen.append(kwargs)
            return mock.MagicMock()
        with mock.patch.object(postgres._DeadlineConnection, "connect", staticmethod(fake_connect)):
            postgres._connect(cfg)
            cfg["database"]["prepared_statements"] = True
            postgres._connect(cfg)
        self.assertIsNone(seen[0]["prepare_threshold"])
        self.assertNotIn("prepare_threshold", seen[1])   # psycopg's own default

    def test_default_deadline_is_set_and_below_the_hook_timeouts(self):
        cfg = swarm.load_config(Path("/nonexistent/swarm.toml"))
        deadline = cfg["database"]["query_timeout_seconds"]
        self.assertGreater(deadline, 0)
        hooks = json.loads((ROOT / "hooks/hooks.json").read_text())["hooks"]   # the plugin's hooks
        self.assertLess(deadline, min(h["timeout"] for groups in hooks.values() for g in groups for h in g["hooks"]))


# --------------------------------------------------------------------------- Postgres

class StallProxy:
    """A TCP proxy to the test database. stall() makes every connection open at that moment
    swallow the server's replies from then on (the client's bytes still reach the server), which
    is what froze `watch`: the query went out, its result never came back. Connections made
    after stall() work normally, so a client that reconnects recovers."""

    def __init__(self, host: str, port: int):
        self.upstream = (host, port)
        self.listener = socket.create_server(("127.0.0.1", 0))
        self.port = self.listener.getsockname()[1]
        self.links: list[tuple[socket.socket, socket.socket, threading.Event]] = []
        self.lock = threading.Lock()
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            try:
                client, _ = self.listener.accept()
            except OSError:
                return
            server = socket.create_connection(self.upstream)
            swallow = threading.Event()
            with self.lock:
                self.links.append((client, server, swallow))
            threading.Thread(target=self._pump, args=(client, server, None), daemon=True).start()
            threading.Thread(target=self._pump, args=(server, client, swallow), daemon=True).start()

    @staticmethod
    def _pump(src, dst, swallow):
        try:
            while data := src.recv(65536):
                if swallow is None or not swallow.is_set():
                    dst.sendall(data)
        except OSError:
            pass
        for s in (src, dst):
            with contextlib.suppress(OSError):
                s.shutdown(socket.SHUT_RDWR)

    def stall(self):
        with self.lock:
            for _, _, swallow in self.links:
                swallow.set()

    def close(self):
        self.listener.close()
        with self.lock:
            for client, server, _ in self.links:
                for s in (client, server):
                    with contextlib.suppress(OSError):
                        s.shutdown(socket.SHUT_RDWR)
                    s.close()


def _toml(cfg: dict) -> str:
    def val(v):
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, (int, float)):
            return repr(v)
        return '"' + str(v).replace("\\", "\\\\").replace('"', '\\"') + '"'
    return "".join(f"[{sec}]\n" + "".join(f"{k} = {val(v)}\n" for k, v in vals.items() if v is not None)
                   for sec, vals in cfg.items() if isinstance(vals, dict))


def _in_thread(fn):
    """Run fn in a daemon thread; returns (thread, box) where box gets 'result' or 'error'."""
    box: dict = {}

    def run():
        try:
            box["result"] = fn()
        except BaseException as exc:  # noqa: BLE001  (the test inspects it)
            box["error"] = exc
    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t, box


DEADLINE = 1.0
SLACK = 2.5   # thread scheduling, socket teardown; far below "forever"


@unittest.skipUnless(os.environ.get("SWARM_TEST_CONFIG"),
                     "set SWARM_TEST_CONFIG to a throwaway Postgres board config to run")
class PostgresDeadlineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.h = PostgresHarness(os.environ["SWARM_TEST_CONFIG"])

    @classmethod
    def tearDownClass(cls):
        cls.h.close()

    def setUp(self):
        self.h.reset()
        db = self.h.cfg["database"]
        self.proxy = StallProxy(db["host"], db["port"])
        self.addCleanup(self.proxy.close)   # also releases a client the old code left hanging
        with self.h.board() as b:
            b.open_job("j", None, None, None, "me")

    def proxied(self, deadline: float = DEADLINE):
        return self.h.board_via("127.0.0.1", self.proxy.port, query_timeout_seconds=deadline)

    def assert_gives_up(self, fn, budget: float):
        started = time.monotonic()
        t, box = _in_thread(fn)
        t.join(budget + SLACK)
        self.assertFalse(t.is_alive(), f"still blocked {budget + SLACK:.1f}s later: the query hangs")
        self.assertIsInstance(box.get("error"), BoardUnavailable, box)
        return box["error"]

    def test_stalled_query_raises_unavailable_within_the_deadline(self):
        b = self.proxied()
        self.addCleanup(b.close)
        self.assertEqual(b.job_status("j").job, "j")
        self.proxy.stall()
        err = self.assert_gives_up(lambda: b.job_status("j"), DEADLINE)
        self.assertIn("deadline", str(err))

    def test_after_a_stall_the_board_fails_fast(self):
        """A hook must not pay the deadline again for each of its remaining queries."""
        b = self.proxied()
        self.addCleanup(b.close)
        self.proxy.stall()
        self.assert_gives_up(lambda: b.job_status("j"), DEADLINE)
        started = time.monotonic()
        with self.assertRaises(BoardUnavailable):
            b.jobs()

    def test_stalled_listen_raises_unavailable(self):
        b = self.proxied()
        self.addCleanup(b.close)
        self.proxy.stall()
        self.assert_gives_up(b.subscribe, DEADLINE)

    def test_stalled_transaction_raises_unavailable(self):
        """post() runs BEGIN/INSERT/COMMIT: the commit is a round trip of its own."""
        b = self.proxied()
        self.addCleanup(b.close)
        name = b.allocate_name("k1", "j")
        self.proxy.stall()
        self.assert_gives_up(lambda: b.post("j", name, "hello"), DEADLINE)

    def test_wait_for_change_is_bounded(self):
        """A stalled connection just looks quiet to LISTEN; wait_for_change still returns on time."""
        b = self.proxied()
        self.addCleanup(b.close)
        b.subscribe()
        self.proxy.stall()
        started = time.monotonic()
        t, box = _in_thread(lambda: b.wait_for_change(0.5))
        t.join(0.5 + DEADLINE + SLACK)
        self.assertFalse(t.is_alive())

    def test_busy_server_query_is_abandoned_at_the_deadline(self):
        """The other stall shape: the server is still running the query (pg_sleep)."""
        b = self.proxied()
        self.addCleanup(b.close)
        self.assert_gives_up(lambda: b._conn.execute("SELECT pg_sleep(8)"), DEADLINE)

    def test_connections_do_not_use_prepared_statements_by_default(self):
        """The pooler's stall needs named prepared statements on a LISTENing connection;
        psycopg prepares a query after its 5th run unless prepare_threshold is None."""
        with self.h.board() as b:
            self.assertIsNone(b._conn.prepare_threshold)
            for _ in range(8):
                b.job_status("j")
            n = b._conn.execute("SELECT count(*) FROM pg_prepared_statements").fetchone()[0]
            self.assertEqual(n, 0)
        with self.h.board_via(self.h.cfg["database"]["host"], self.h.cfg["database"]["port"],
                              prepared_statements=True) as b:
            self.assertEqual(b._conn.prepare_threshold, 5)   # psycopg's default: opt back in

    def test_healthy_queries_are_unaffected(self):
        b = self.proxied(deadline=5)
        self.addCleanup(b.close)
        for _ in range(50):
            b.job_status("j")
        b._conn.execute("SELECT pg_sleep(0.3)")
        self.assertEqual(b.job_status("j").job, "j")


@unittest.skipUnless(os.environ.get("SWARM_TEST_CONFIG"),
                     "set SWARM_TEST_CONFIG to a throwaway Postgres board config to run")
class PostgresFollowRecoveryTests(unittest.TestCase):
    """`swarm tail` and `swarm watch` as real processes, through a proxy that stalls them."""

    @classmethod
    def setUpClass(cls):
        cls.h = PostgresHarness(os.environ["SWARM_TEST_CONFIG"])

    @classmethod
    def tearDownClass(cls):
        cls.h.close()

    def setUp(self):
        self.h.reset()
        db = self.h.cfg["database"]
        self.proxy = StallProxy(db["host"], db["port"])
        self.addCleanup(self.proxy.close)
        self.tmp = Path(tempfile.mkdtemp(prefix="swarm-stall-", dir=os.environ.get("TMPDIR")))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        cfg = {sec: dict(vals) for sec, vals in self.h.cfg.items() if isinstance(vals, dict)}
        cfg["database"].update(host="127.0.0.1", port=self.proxy.port, query_timeout_seconds=DEADLINE)
        if "PGPASSWORD" not in os.environ and not cfg["database"].get("password_env_file"):
            self.skipTest("no password source for the subprocess")
        self.config = self.tmp / "config.toml"
        self.config.write_text(_toml(cfg))
        self.b = self.h.board()
        self.addCleanup(self.b.close)
        self.b.open_job("j", None, None, None, "me")
        self.name = self.b.allocate_name("k1", "j")

    def spawn(self, *argv) -> tuple[subprocess.Popen, list]:
        env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONPATH=str(ROOT / "lib"))
        if self.h._password(self.h.cfg["database"]):
            env["PGPASSWORD"] = self.h._password(self.h.cfg["database"])
        proc = subprocess.Popen([sys.executable, "-B", "-m", "swarm.cli", "--config", str(self.config),
                                 *argv], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, env=env)
        chunks: list = []

        def read():
            while data := proc.stdout.read1(65536):
                chunks.append(data.decode(errors="replace"))
        threading.Thread(target=read, daemon=True).start()

        def stop():
            proc.kill()
            proc.wait(5)
            proc.stdout.close()
        self.addCleanup(stop)
        return proc, chunks

    def wait_for(self, chunks, text: str, timeout: float) -> str:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            out = "".join(chunks)
            if text in out:
                return out
            time.sleep(0.05)
        self.fail(f"{text!r} not seen within {timeout}s; output:\n{''.join(chunks)[-3000:]}")

    def test_tail_recovers_from_a_stall(self):
        self.b.post("j", self.name, "before the stall")
        proc, chunks = self.spawn("tail", "--job", "j", "--interval", "0.3", "--no-color")
        self.wait_for(chunks, "before the stall", 10)
        self.proxy.stall()
        self.b.post("j", self.name, "during the stall")
        self.wait_for(chunks, "reconnecting", DEADLINE + 5)
        out = self.wait_for(chunks, "during the stall", DEADLINE + 10)
        self.b.post("j", self.name, "after the stall")
        out = self.wait_for(chunks, "after the stall", 10)
        self.assertIsNone(proc.poll())
        self.assertEqual(out.count("before the stall"), 1)
        self.assertEqual(out.count("during the stall"), 1)

    def test_watch_recovers_from_a_stall(self):
        proc, chunks = self.spawn("watch", "--job", "j", "--interval", "0.3", "--no-color")
        self.wait_for(chunks, "MESSAGES", 10)
        self.proxy.stall()
        self.b.post("j", self.name, "posted during the stall")
        self.wait_for(chunks, "reconnecting", DEADLINE + 5)
        self.wait_for(chunks, "posted during the stall", DEADLINE + 10)
        self.assertIsNone(proc.poll())


if __name__ == "__main__":
    unittest.main()
