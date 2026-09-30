"""Several Postgres hosts: `host` may be a list (a Patroni cluster), writes follow the primary,
read-only commands fall back to a standby, watch/tail poll where LISTEN can't work and reconnect.

Parsing and the connection logic run against fakes (no server); the last class, with
$SWARM_TEST_CONFIG (a THROWAWAY database), puts a closed port first in front of the real server.
"""
from __future__ import annotations

import contextlib
import copy
import io
import os
import unittest
from unittest import mock

from support import PostgresHarness  # noqa: F401  (sets sys.path)

from swarm import cli as swarm  # noqa: E402
from swarm import board as board_pkg  # noqa: E402
from swarm.board import BoardError, BoardUnavailable, database_hosts  # noqa: E402

try:
    import psycopg
    from swarm.board import postgres as pg
except ImportError:   # the backend-independent runs have no driver
    psycopg = pg = None

need_pg = unittest.skipUnless(psycopg, "psycopg is not installed")


class HostParsingTests(unittest.TestCase):
    def hosts(self, **db):
        return database_hosts(db)

    def test_single_host_as_ever(self):
        self.assertEqual(self.hosts(host="pg-1", port=5432), [("pg-1", 5432)])
        self.assertEqual(self.hosts(host="pg-1", port="6543"), [("pg-1", 6543)])
        self.assertEqual(self.hosts(host="/var/run/postgresql", port=5432), [("/var/run/postgresql", 5432)])

    def test_comma_separated_string(self):
        self.assertEqual(self.hosts(host="pg-1, pg-2,pg-3", port=5432),
                         [("pg-1", 5432), ("pg-2", 5432), ("pg-3", 5432)])

    def test_toml_array_and_hosts_key(self):
        self.assertEqual(self.hosts(host=["pg-1", "pg-2"], port=5432), [("pg-1", 5432), ("pg-2", 5432)])
        self.assertEqual(self.hosts(hosts=["pg-1", "pg-2"], host="ignored", port=5432),
                         [("pg-1", 5432), ("pg-2", 5432)])
        self.assertEqual(self.hosts(hosts=["pg-1,pg-2", "pg-3"], port=1), [("pg-1", 1), ("pg-2", 1), ("pg-3", 1)])

    def test_per_host_port(self):
        self.assertEqual(self.hosts(host="pg-1:5433,pg-2", port=5432), [("pg-1", 5433), ("pg-2", 5432)])
        self.assertEqual(self.hosts(host=["[::1]:5434", "[fe80::1]"], port=5432),
                         [("::1", 5434), ("fe80::1", 5432)])
        self.assertEqual(self.hosts(host="::1", port=5432), [("::1", 5432)])   # bare IPv6: no port

    def test_port_list(self):
        self.assertEqual(self.hosts(host="a,b", port=[5433, 5434]), [("a", 5433), ("b", 5434)])
        self.assertEqual(self.hosts(host="a,b", port="5433,5434"), [("a", 5433), ("b", 5434)])
        self.assertEqual(self.hosts(host="a,b", port=[5433]), [("a", 5433), ("b", 5433)])
        with self.assertRaises(BoardError):
            self.hosts(host="a,b,c", port=[1, 2])
        with self.assertRaises(BoardError):
            self.hosts(host="a:xyz")

    def test_config_files_keep_working(self):
        cfg = swarm.load_config(__import__("pathlib").Path("/nonexistent/swarm.toml"))
        self.assertEqual(database_hosts(cfg["database"]), [("localhost", 5432)])

    def test_watch_database_host_replaces_a_host_list(self):
        cfg = swarm.load_config(__import__("pathlib").Path("/nonexistent/swarm.toml"))
        cfg["database"]["hosts"] = ["a", "b"]
        cfg["watch_database"] = {"host": "direct"}
        self.assertEqual(database_hosts(swarm.watcher_config(cfg)["database"]), [("direct", 5432)])
        self.assertEqual(swarm.watcher_db_label(cfg | {"board": {"backend": "postgres"}}), "direct")


class FakeConn:
    def __init__(self, host, log=None, listen_error=None):
        self.info = mock.Mock(host=host)
        self.closed = self.broken = False
        self.query_timeout = 0.0
        self.log, self.listen_error = log if log is not None else [], listen_error

    def execute(self, q, *a):
        self.log.append((self.info.host, q))
        if q.startswith("LISTEN") and self.listen_error:
            raise self.listen_error
        return mock.Mock()

    def notifies(self, **kw):
        return iter(())

    def close(self):
        self.closed = True


def cfg_for(host, port=5432, **extra):
    cfg = swarm.load_config(__import__("pathlib").Path("/nonexistent/swarm.toml"))
    cfg["board"]["backend"] = "postgres"
    cfg["database"].update(host=host, port=port, **extra)
    return cfg


@need_pg
class ConnectionStringTests(unittest.TestCase):
    def connect(self, cfg, **kw):
        with mock.patch.object(pg._DeadlineConnection, "connect", return_value=FakeConn("x")) as c:
            pg._connect(cfg, **kw)
        return c.call_args.kwargs

    def test_single_host_is_unchanged(self):
        kw = self.connect(cfg_for("pg-1"))
        self.assertEqual((kw["host"], kw["port"]), ("pg-1", 5432))
        self.assertNotIn("target_session_attrs", kw)

    def test_several_hosts_want_the_primary_in_order(self):
        kw = self.connect(cfg_for(["pg-1", "pg-2:5433", "pg-3"]))
        self.assertEqual((kw["host"], kw["port"]), ("pg-1,pg-2,pg-3", "5432,5433,5432"))
        self.assertEqual(kw["target_session_attrs"], "read-write")

    def test_any_host_accepts_a_standby(self):
        self.assertEqual(self.connect(cfg_for("a,b"), any_host=True)["target_session_attrs"], "any")

    def test_no_primary_is_said_so(self):
        with mock.patch.object(pg._DeadlineConnection, "connect", side_effect=psycopg.OperationalError("read-only")):
            with self.assertRaisesRegex(BoardUnavailable, "no primary reachable among a, b"):
                pg._connect(cfg_for("a,b"))
            with self.assertRaises(BoardUnavailable) as single:
                pg._connect(cfg_for("a"))
        self.assertEqual(str(single.exception), "read-only")

    def test_identity(self):
        self.assertEqual(pg.PostgresBoard.identity(cfg_for("pg-1")), "pg-1:5432/swarm_board")
        self.assertEqual(pg.PostgresBoard.identity(cfg_for("a,b:6")), "a:5432,b:6/swarm_board")


@need_pg
class FallbackTests(unittest.TestCase):
    """Failover order, the reader fallback, polling and the switch back, on fake connections."""

    def boards(self, cfg, primary_up, standby="pg-2", readers=True, listen_error=None):
        self.log, self.calls = [], []

        def fake_connect(cfg, admin=False, any_host=False):
            self.calls.append("any" if any_host else "read-write")
            if not any_host and not self.primary_up:
                raise BoardUnavailable("no primary")
            return FakeConn(standby if any_host else "pg-1", self.log, listen_error)
        self.primary_up = primary_up
        with mock.patch.object(pg, "_connect", fake_connect):
            return pg.PostgresBoard(cfg, readers=readers), fake_connect

    def test_primary_up_is_normal(self):
        b, _ = self.boards(cfg_for("pg-1,pg-2"), True)
        self.assertIsNone(b.degraded)
        self.assertEqual(self.calls, ["read-write"])

    def test_readers_fall_back_to_a_standby(self):
        b, _ = self.boards(cfg_for("pg-1,pg-2"), False)
        self.assertEqual(b.degraded, "pg-2")
        self.assertEqual(self.calls, ["read-write", "any"])   # the primary first

    def test_writers_do_not_fall_back(self):
        with self.assertRaises(BoardUnavailable):
            self.boards(cfg_for("pg-1,pg-2"), False, readers=False)
        self.assertEqual(self.calls, ["read-write"])

    def test_single_host_never_falls_back(self):
        with self.assertRaises(BoardUnavailable):
            self.boards(cfg_for("pg-1"), False)
        self.assertEqual(self.calls, ["read-write"])

    def test_nothing_reachable_raises(self):
        def dead(cfg, admin=False, any_host=False):
            raise BoardUnavailable("down")
        with mock.patch.object(pg, "_connect", dead), self.assertRaises(BoardUnavailable):
            pg.PostgresBoard(cfg_for("a,b"), readers=True)

    def test_no_listen_on_a_standby_and_polling(self):
        b, _ = self.boards(cfg_for("pg-1,pg-2"), False)
        b.subscribe()
        self.assertFalse([q for _, q in self.log if q.startswith("LISTEN")])
        with mock.patch.object(pg.time, "sleep") as sleep:
            self.assertFalse(b.wait_for_change(0.5))
        sleep.assert_called_once_with(0.5)

    def test_listen_refused_falls_back_to_polling(self):
        err = psycopg.errors.ReadOnlySqlTransaction("cannot execute LISTEN during recovery")
        b, _ = self.boards(cfg_for("pg-1,pg-2"), True, listen_error=err)
        b.subscribe(messages_only=True)
        self.assertTrue(b._polling)
        with mock.patch.object(pg.time, "sleep") as sleep:
            b.wait_for_change(0.25)
        sleep.assert_called_once_with(0.25)

    def test_listen_on_a_primary_is_as_ever(self):
        b, _ = self.boards(cfg_for("pg-1,pg-2"), True)
        b.subscribe()
        self.assertEqual([q for _, q in self.log if q.startswith("LISTEN")],
                         [f"LISTEN {pg.CHANNEL_MESSAGES}", f"LISTEN {pg.CHANNEL_STATE}"])
        self.assertFalse(b._polling)

    def test_switches_back_when_the_primary_returns(self):
        b, connect = self.boards(cfg_for("pg-1,pg-2"), False)
        b.subscribe()
        standby_conn = b._conn
        self.primary_up = True
        with mock.patch.object(pg, "_connect", connect), mock.patch.object(pg.time, "sleep"), \
                mock.patch.object(pg.time, "monotonic", return_value=1e9):
            self.assertTrue(b.wait_for_change(0.1))   # redraw: the view changed
        self.assertIsNone(b.degraded)
        self.assertTrue(standby_conn.closed)
        self.assertEqual(b._conn.info.host, "pg-1")
        self.assertIn(f"LISTEN {pg.CHANNEL_MESSAGES}", [q for h, q in self.log if h == "pg-1"])

    def test_stays_degraded_while_no_primary(self):
        b, connect = self.boards(cfg_for("pg-1,pg-2"), False)
        b.subscribe()
        with mock.patch.object(pg, "_connect", connect), mock.patch.object(pg.time, "sleep"), \
                mock.patch.object(pg.time, "monotonic", return_value=1e9):
            self.assertFalse(b.wait_for_change(0.1))
        self.assertEqual(b.degraded, "pg-2")

    def test_schema_version_asks_a_standby(self):
        seen = []

        def fake(cfg, admin=False, any_host=False):
            seen.append(any_host)
            if not any_host:
                raise BoardUnavailable("no primary")
            conn = mock.MagicMock()
            conn.__enter__.return_value = conn
            conn.execute.return_value.fetchone.side_effect = [("board_meta",), ("9",)]
            return conn
        with mock.patch.object(pg, "_connect", fake):
            self.assertEqual(pg.PostgresBoard.schema_version(cfg_for("a,b")), 9)
        self.assertEqual(seen, [False, True])


class CliTests(unittest.TestCase):
    def setUp(self):
        self.cfg = cfg_for("pg-1,pg-2")
        self.mem = copy.deepcopy(self.cfg)
        self.mem["board"]["backend"] = "memory"

    def run_main(self, argv, opened):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(swarm, "load_config", return_value=self.cfg), \
                mock.patch.object(swarm, "auto_init"), \
                mock.patch.object(board_pkg, "open_board", opened), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = swarm._main(argv)
        return rc, out.getvalue(), err.getvalue()

    def test_read_only_commands_ask_for_readers(self):
        for argv, expected in ((["who", "--job", "J"], True), (["status"], True), (["read", "--peek"], True),
                               (["transcript", "list"], True), (["read"], False), (["purge"], False),
                               (["join", "--job", "J", "--key", "k"], False)):
            args = swarm._parser().parse_args(argv)
            self.assertEqual(swarm._reads_only(args), expected, argv)

    def test_a_degraded_read_says_so_and_writes_nothing(self):
        board = board_pkg.backend_class(self.mem)(self.mem)
        board.degraded = "pg-2"
        seen = {}

        def opened(cfg, init_timeout=60.0, readers=False):
            seen["readers"] = readers
            return board
        with mock.patch("swarm.spool.flush_spool") as flush:
            rc, out, err = self.run_main(["who", "--job", "J"], opened)
        self.assertEqual(rc, 0)
        self.assertTrue(seen["readers"])
        self.assertIn("degraded: reading from pg-2 (no primary)", err)
        flush.assert_not_called()

    def test_a_write_without_a_primary_is_a_message_not_a_traceback(self):
        def opened(cfg, init_timeout=60.0, readers=False):
            self.assertFalse(readers)
            raise BoardUnavailable("no primary reachable among pg-1, pg-2: down")
        rc, out, err = self.run_main(["purge"], opened)
        self.assertEqual(rc, 1)
        self.assertIn("cannot reach the board database: no primary reachable", err)
        self.assertIn("needs the primary", err)
        self.assertNotIn("Traceback", err)

    def test_the_sweeper_leaves_a_standby_alone(self):
        board = mock.Mock(degraded="pg-2")
        with mock.patch.object(swarm, "sweep_jobs") as sweep:
            self.assertEqual(swarm.Sweeper(self.cfg)(board), [])
        sweep.assert_not_called()


class FollowTests(unittest.TestCase):
    def test_reconnects_with_bounded_backoff_and_asks_for_readers(self):
        cfg = cfg_for("a,b")
        calls, notices, pauses = [], [], []
        good = mock.MagicMock()
        good.__enter__.return_value = good

        def opened(cfg, init_timeout=60.0, readers=False):
            calls.append(readers)
            if len(calls) < 5:
                raise BoardUnavailable("down")
            return good
        runs = []

        def run(board):
            runs.append(board)
            if len(runs) == 1:
                raise BoardUnavailable("connection lost")   # a switchover
        with mock.patch.object(board_pkg, "open_board", opened):
            swarm._follow(cfg, run, notices.append, lambda s: pauses.append(s) or True)
        self.assertTrue(all(calls))
        self.assertEqual(len(runs), 2)
        self.assertEqual(pauses, [1.0, 2.0, 4.0, 8.0, 1.0][:len(pauses)])
        self.assertTrue(all(p <= swarm.RECONNECT_MAX_SECONDS for p in pauses))
        self.assertTrue(any("board unreachable" in n for n in notices))


@unittest.skipUnless(os.environ.get("SWARM_TEST_CONFIG") and psycopg, "set SWARM_TEST_CONFIG to a throwaway database")
class PostgresFailoverTests(unittest.TestCase):
    """On a real server: a dead first host (a closed port) is skipped."""

    @classmethod
    def setUpClass(cls):
        cls.h = PostgresHarness(os.environ["SWARM_TEST_CONFIG"])

    @classmethod
    def tearDownClass(cls):
        cls.h.close()

    def setUp(self):
        self.h.reset()
        import socket
        with socket.socket() as s:   # a port nothing listens on
            s.bind(("127.0.0.1", 0))
            closed = s.getsockname()[1]
        db = self.h.cfg["database"]
        self.cfg = copy.deepcopy(self.h.cfg)
        self.cfg["database"]["connect_timeout"] = 2
        self.cfg["database"]["host"] = [f"127.0.0.1:{closed}", f"{db['host']}:{db['port']}"]

    def test_writes_reach_the_live_host_behind_a_dead_one(self):
        with board_pkg.open_board(self.cfg) as b:
            self.assertIsNone(b.degraded)
            b.open_job("J", None, None, None, "me")
            self.assertEqual(b.job_status("J").job, "J")

    def test_readers_and_polling_on_a_real_connection(self):
        with board_pkg.open_board(self.cfg, readers=True) as b:
            b.subscribe(messages_only=True)
            self.assertFalse(b.wait_for_change(0.05))   # a primary: LISTEN works, nothing pending
            self.assertFalse(b._polling)
