"""`[watch_database]`: an optional, separate connection for the watchers (`watch`, `tail`).

Some connection poolers can deadlock a connection that LISTENs and receives NOTIFYs; watch and
tail are the long-lived LISTEN clients, so they may connect somewhere else (e.g. the Postgres
primary directly) while agents, hooks and every other command keep `[database]`. Each key the
section leaves unset (or empty) falls back to `[database]`; no section means exactly today's
behaviour.

The config and routing classes run on any backend ($SWARM_TEST_BACKEND); the Postgres class needs
$SWARM_TEST_CONFIG (a THROWAWAY database) and checks the watcher's real connection.
"""
from __future__ import annotations

import contextlib
import copy
import io
import os
import socket
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from support import E2E_BACKEND, HARNESSES, PostgresHarness, tq, home_env, posix_only  # noqa: F401  (sets sys.path)

from swarm import board as board_pkg  # noqa: E402
from swarm import cli as swarm  # noqa: E402
from test_stall import FlakyBoard  # noqa: E402

def _write(tmp: Path, text: str) -> Path:
    path = tmp / "config.toml"
    path.write_text(text)
    return path


class WatcherConfigTests(unittest.TestCase):
    """swarm.watcher_config(cfg): [database] with [watch_database]'s set keys on top."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="swarm-watchdb-", dir=os.environ.get("TMPDIR")))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.database = ('[database]\nhost = "pooler.example"\nport = 5432\nuser = "board"\n'
                         'dbname = "board-db"\npassword_env_file = "/secrets/board.env"\n')

    def test_missing_section_is_the_swarm_connection(self):
        cfg = swarm.load_config(_write(self.tmp, self.database))
        self.assertEqual(swarm.watcher_config(cfg)["database"], cfg["database"])
        self.assertIsNone(swarm.watcher_db_label(cfg))

    def test_empty_section_is_the_swarm_connection(self):
        cfg = swarm.load_config(_write(self.tmp, self.database + "[watch_database]\n"))
        self.assertEqual(swarm.watcher_config(cfg)["database"], cfg["database"])
        self.assertIsNone(swarm.watcher_db_label(cfg))

    def test_empty_values_fall_back(self):
        cfg = swarm.load_config(_write(self.tmp, self.database
                                       + '[watch_database]\nhost = ""\npassword_env_file = ""\n'))
        self.assertEqual(swarm.watcher_config(cfg)["database"], cfg["database"])

    def test_partial_override_merges_per_key(self):
        cfg = swarm.load_config(_write(self.tmp, self.database
                                       + '[watch_database]\nhost = "primary.example"\nport = 5433\n'))
        db = swarm.watcher_config(cfg)["database"]
        self.assertEqual((db["host"], db["port"]), ("primary.example", 5433))
        for key in ("user", "dbname", "password_env_file", "connect_timeout", "sslmode",
                    "query_timeout_seconds", "prepared_statements", "admin_dbname"):
            self.assertEqual(db[key], cfg["database"][key], key)

    def test_full_override_is_used_as_is(self):
        override = {"host": "primary.example", "port": 6543, "user": "watcher", "dbname": "other-db",
                    "password_env_file": "/secrets/watch.env", "connect_timeout": 2,
                    "sslmode": "require", "query_timeout_seconds": 3, "prepared_statements": True,
                    "application_name": "swarm-watch"}
        cfg = swarm.load_config(_write(self.tmp, self.database))
        cfg["watch_database"] = dict(override)
        db = swarm.watcher_config(cfg)["database"]
        for key, value in override.items():
            self.assertEqual(db[key], value, key)

    def test_does_not_touch_the_swarm_config(self):
        cfg = swarm.load_config(_write(self.tmp, self.database
                                       + '[watch_database]\nhost = "primary.example"\n'))
        before = copy.deepcopy(cfg)
        watch = swarm.watcher_config(cfg)
        self.assertEqual(cfg, before)
        self.assertEqual(cfg["database"]["host"], "pooler.example")
        self.assertEqual(watch["board"], cfg["board"])   # everything but the connection is shared

    def test_label_names_the_server_only_when_it_differs(self):
        cfg = swarm.load_config(_write(self.tmp, self.database
                                       + '[watch_database]\nhost = "primary.example"\n'))
        self.assertEqual(swarm.watcher_db_label(cfg), "primary.example")
        cfg["watch_database"]["port"] = 5433
        self.assertEqual(swarm.watcher_db_label(cfg), "primary.example:5433")
        # same server, only a tunable differs: nothing worth showing
        cfg["watch_database"] = {"query_timeout_seconds": 20, "host": "pooler.example"}
        self.assertIsNone(swarm.watcher_db_label(cfg))

    def test_label_only_for_postgres(self):
        cfg = swarm.load_config(_write(self.tmp, self.database + '[board]\nbackend = "sqlite"\n'
                                       + '[watch_database]\nhost = "primary.example"\n'))
        self.assertIsNone(swarm.watcher_db_label(cfg))

    def test_defaults_document_the_section(self):
        cfg = swarm.load_config(Path("/nonexistent/swarm.toml"))
        self.assertEqual(cfg["watch_database"], {})
        self.assertEqual(swarm.watcher_config(cfg)["database"], cfg["database"])


class DefaultPathsTests(unittest.TestCase):
    """The defaults are per user and outside the sandbox-writable state dir."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="swarm-defaults-", dir=os.environ.get("TMPDIR")))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))

    def load(self, home, text=None, uid=None):
        env = {**home_env(home)}
        with mock.patch.dict(os.environ, env), \
                mock.patch("os.getuid", return_value=uid if uid is not None else os.getuid()):
            cfg = swarm.load_config(_write(self.tmp, text) if text else Path("/nonexistent/swarm.toml"))
            return cfg, {k: str(Path(v).expanduser()) for k, v in (
                ("spool", cfg["board"]["spool_dir"]), ("file", cfg["file"]["path"]),
                ("sqlite", cfg["sqlite"]["path"]))}

    @posix_only("needs Unix uids (no ownership checks on Windows)")
    def test_default_spool_is_per_user(self):
        a, b = self.tmp / "a", self.tmp / "b"
        _, pa = self.load(a, uid=1000)
        _, pb = self.load(b, uid=1001)
        self.assertEqual(pa["spool"], str(a / ".local/state/swarm/spool"))
        self.assertNotEqual(pa["spool"], pb["spool"])
        import support
        self.assertNotIn(support.REAL_OLD_SPOOL, (pa["spool"], pb["spool"]))

    @posix_only("needs Unix uids (no ownership checks on Windows)")
    def test_default_boards_are_outside_the_state_dir(self):
        _, p = self.load(self.tmp)
        self.assertEqual(p["file"], str(self.tmp / ".local/share/swarm-board/board"))
        self.assertEqual(p["sqlite"], str(self.tmp / ".local/share/swarm-board/board.sqlite3"))
        for v in p.values():
            if v != p["spool"]:
                self.assertNotIn("/.local/state/", v)

    @posix_only("needs Unix uids (no ownership checks on Windows)")
    def test_uid_is_expanded_in_a_tmp_spool(self):
        text = '[board]\nspool_dir = "/tmp/claude-{uid}/swarm-spool"\n'
        _, p1 = self.load(self.tmp, text, uid=1000)
        _, p2 = self.load(self.tmp, text, uid=1001)
        self.assertEqual(p1["spool"], "/tmp/claude-1000/swarm-spool")
        self.assertEqual(p2["spool"], "/tmp/claude-1001/swarm-spool")


class WatcherRoutingTests(unittest.TestCase):
    """watch and tail open (and re-open) their board with the watcher config; nothing else does."""

    WATCH_HOST = "watch-primary.example"

    def setUp(self):
        self.h = HARNESSES[E2E_BACKEND]()
        self.addCleanup(self.h.close)
        self.h.reset()
        with self.h.board() as b:
            b.open_job("J", None, None, None, "me")
            b.allocate_name("k1", "J")
            self.name = b.active_agent_name("k1")
            b.post("J", self.name, "hello")
        self.cfg = copy.deepcopy(self.h.cfg)
        self.cfg["watch_database"] = {"host": self.WATCH_HOST, "application_name": "swarm-watch"}
        self.seen: list[dict] = []   # cfg["database"] of every open_board call
        self.log: list = []

    def plan(self, *specs: dict):
        """open_board records its cfg and returns FlakyBoards configured by `specs` (last repeats)."""
        opened = []

        def opener(cfg, **_):   # open_board(cfg, init_timeout=...)
            self.seen.append(copy.deepcopy(cfg["database"]))
            opened.append(cfg)
            spec = specs[min(len(opened) - 1, len(specs) - 1)]
            return FlakyBoard(self.h.board(), self.log, spec.get("fail_on"), spec.get("stop_on"))
        return mock.patch.object(board_pkg, "open_board", opener)

    def run_cli(self, fn, *args) -> str:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch.object(swarm.time, "sleep", lambda s: None):
            self.assertEqual(fn(*args), 0)
        return out.getvalue()

    def test_tail_connects_with_the_watcher_config(self):
        with self.plan({"stop_on": ("wait_for_change", 1)}):
            self.run_cli(swarm.cmd_tail, self.cfg, "J", 5, 0.01, False, False)
        self.assertEqual([d["host"] for d in self.seen], [self.WATCH_HOST])
        self.assertEqual(self.seen[0]["application_name"], "swarm-watch")
        self.assertEqual(self.seen[0]["dbname"], self.cfg["database"]["dbname"])  # fallback

    def test_watch_connects_with_the_watcher_config(self):
        with self.plan({"stop_on": ("wait_for_change", 1)}):
            self.run_cli(swarm.cmd_watch, self.cfg, "J", 0.01, False)
        self.assertEqual([d["host"] for d in self.seen], [self.WATCH_HOST])

    def test_reconnects_use_the_watcher_config(self):
        with self.plan({"fail_on": ("messages_after", 1)}, {"stop_on": ("wait_for_change", 1)}):
            self.run_cli(swarm.cmd_tail, self.cfg, "J", 0, 0.01, False, False)
        with self.plan({"fail_on": ("job_status", 2)}, {"stop_on": ("wait_for_change", 2)}):
            self.run_cli(swarm.cmd_watch, self.cfg, "J", 0.01, False)
        self.assertEqual([d["host"] for d in self.seen], [self.WATCH_HOST] * 4)

    def test_without_the_section_watchers_use_the_swarm_connection(self):
        del self.cfg["watch_database"]
        with self.plan({"stop_on": ("wait_for_change", 1)}):
            out = self.run_cli(swarm.cmd_tail, self.cfg, "J", 0, 0.01, False, False)
        self.assertEqual(self.seen, [self.cfg["database"]])
        self.assertNotIn("db:", out)

    def test_tail_start_line_names_the_watcher_server(self):
        self.cfg["board"]["backend"] = "postgres"   # the label is Postgres-only; the board is mocked
        with self.plan({"stop_on": ("wait_for_change", 1)}):
            out = self.run_cli(swarm.cmd_tail, self.cfg, "J", 0, 0.01, False, False)
        start = next(ln for ln in out.splitlines() if ln.startswith("--- following"))
        self.assertIn(f"db: {self.WATCH_HOST}", start)

    def test_watch_header_names_the_watcher_server(self):
        self.cfg["board"]["backend"] = "postgres"
        with self.plan({"stop_on": ("wait_for_change", 1)}):
            out = self.run_cli(swarm.cmd_watch, self.cfg, "J", 0.01, False)
        title = next(ln for ln in out.replace("\033[H", "\n").splitlines() if "swarm watch" in ln)
        self.assertIn(f"db: {self.WATCH_HOST}", title)

    def test_watch_header_is_unchanged_on_the_swarm_connection(self):
        self.cfg["board"]["backend"] = "postgres"
        self.cfg["watch_database"] = {}
        with self.plan({"stop_on": ("wait_for_change", 1)}):
            out = self.run_cli(swarm.cmd_watch, self.cfg, "J", 0.01, False)
        self.assertIn("swarm watch", out)
        self.assertNotIn("db:", out)

    def test_other_commands_keep_the_swarm_connection(self):
        tmp = Path(tempfile.mkdtemp(prefix="swarm-watchdb-", dir=os.environ.get("TMPDIR")))
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        config = tmp / "config.toml"
        config.write_text(f'[board]\nspool_dir = {tq(tmp / "spool")}\n'
                          f'[watch_database]\nhost = "{self.WATCH_HOST}"\n')
        commands = (["status"], ["status", "--job", "J"], ["post", "--job", "J", "--as", self.name, "hi"],
                    ["read", "--job", "J", "--as", self.name], ["who", "--job", "J"], ["job", "J2"])
        with self.plan({}):
            for argv in commands:
                out, err = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    swarm.main(["--config", str(config), *argv])
        self.assertEqual(len(self.seen), len(commands))
        self.assertNotIn(self.WATCH_HOST, [d["host"] for d in self.seen])

    def test_hooks_keep_the_swarm_connection(self):
        from swarm import hooks as swarm_hooks
        cfg = copy.deepcopy(self.cfg)
        tmp = Path(tempfile.mkdtemp(prefix="swarm-watchdb-", dir=os.environ.get("TMPDIR")))
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        cfg["hook"]["marker_dir"] = str(tmp / "markers")
        cfg["board"]["spool_dir"] = str(tmp / "spool")
        (tmp / "markers").mkdir()
        # SubagentStop always opens the board (the agent's job marker may be gone)
        hook_input = '{"session_id": "s1", "agent_id": "a1", "hook_event_name": "SubagentStop"}'
        with self.plan({}), mock.patch("sys.stdin", io.StringIO(hook_input)), \
                contextlib.redirect_stdout(io.StringIO()), mock.patch.dict(os.environ, {**home_env(tmp)}):
            swarm_hooks.run_hook("stop", cfg)
        self.assertEqual(len(self.seen), 1)
        self.assertNotIn(self.WATCH_HOST, [d["host"] for d in self.seen])


@unittest.skipUnless(os.environ.get("SWARM_TEST_CONFIG"), "set SWARM_TEST_CONFIG to a throwaway database")
class PostgresWatcherConnectionTests(unittest.TestCase):
    """On a real server: the watchers' connection is the one [watch_database] describes."""

    APP = "swarm-watch-test"

    @classmethod
    def setUpClass(cls):
        cls.h = PostgresHarness(os.environ["SWARM_TEST_CONFIG"])

    @classmethod
    def tearDownClass(cls):
        cls.h.close()

    def setUp(self):
        self.h.reset()
        with self.h.board() as b:
            b.open_job("J", None, None, None, "me")
        db = self.h.cfg["database"]
        # the same server by another name: its address, so the host visibly comes from the override
        self.addr = socket.getaddrinfo(db["host"], db["port"], proto=socket.IPPROTO_TCP)[0][4][0]
        self.cfg = copy.deepcopy(self.h.cfg)
        self.cfg["watch_database"] = {"host": self.addr, "application_name": self.APP}
        self.conns: list[dict] = []

    def plan(self):
        """Real boards; at the first wait for a change, record what the connection is, then stop."""
        real_open = board_pkg.open_board
        conns = self.conns

        class Probe:
            def __init__(self, inner):
                self.inner = inner

            def __getattr__(self, name):
                return getattr(self.inner, name)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                self.inner.close()

            def wait_for_change(self, timeout):
                conn = self.inner._conn
                conns.append({"host": conn.info.host,
                              "app": conn.execute("SELECT current_setting('application_name')").fetchone()[0]})
                raise KeyboardInterrupt

        return mock.patch.object(board_pkg, "open_board", lambda cfg, **kw: Probe(real_open(cfg, **kw)))

    def test_tail_uses_the_watcher_connection(self):
        with self.plan(), contextlib.redirect_stdout(io.StringIO()) as out:
            swarm.cmd_tail(self.cfg, "J", 0, 0.01, False, False)
        self.assertEqual(self.conns, [{"host": self.addr, "app": self.APP}])
        if self.addr != self.h.cfg["database"]["host"]:
            self.assertIn(f"db: {self.addr}", out.getvalue())

    def test_watch_uses_the_watcher_connection(self):
        with self.plan(), contextlib.redirect_stdout(io.StringIO()):
            swarm.cmd_watch(self.cfg, "J", 0.01, False)
        self.assertEqual(self.conns, [{"host": self.addr, "app": self.APP}])

    def test_other_boards_use_the_swarm_connection(self):
        with board_pkg.open_board(self.cfg) as b:
            self.assertEqual(b._conn.info.host, self.h.cfg["database"]["host"])
            app = b._conn.execute("SELECT current_setting('application_name')").fetchone()[0]
        self.assertNotEqual(app, self.APP)


if __name__ == "__main__":
    unittest.main()
