"""The Postgres schema setup survives a deadlock against live hooks: it sets a short lock_timeout,
retries a DeadlockDetected / LockNotAvailable with a pause (noted on stderr) and gives up after N."""
from __future__ import annotations

import contextlib
import io
import os
import unittest
import uuid
from pathlib import Path
from unittest import mock

from support import MemoryHarness  # noqa: F401  (also sets sys.path)

try:
    import psycopg
    from swarm.board import postgres
except ImportError:   # psycopg is optional
    psycopg = None


class FakeConn:
    def __init__(self):
        self.sql = []
        self.query_timeout = 8.0

    def execute(self, q, *a):
        self.sql.append(q)


@unittest.skipIf(psycopg is None, "psycopg not installed")
class SchemaRetryTests(unittest.TestCase):
    def test_grouped_schema_has_its_own_budget_and_restores_the_query_timeout(self):
        from swarm.board import BoardUnavailable
        conn = FakeConn()
        # Ten individually valid 4.5s lock waits plus 5s of DDL: no statement hits
        # the 5s server lock_timeout, but the grouped round trip exceeds 8s.
        work = 10 * 4.5 + 5

        def install(c, board):
            if c.query_timeout and work > c.query_timeout:
                c.execute = mock.Mock(side_effect=postgres._closed_error())
                raise BoardUnavailable("grouped schema exceeded the client deadline")

        with mock.patch.object(postgres, "_install_schema", side_effect=install):
            postgres._install_schema_retrying(conn, {})
        self.assertEqual(conn.query_timeout, 8.0)

    def test_setup_keeps_disabled_or_larger_query_deadlines(self):
        for timeout in (0.0, 180.0):
            with self.subTest(timeout=timeout):
                conn = FakeConn()
                conn.query_timeout = timeout
                with mock.patch.object(postgres, "_install_schema") as install:
                    install.side_effect = lambda c, board: self.assertEqual(c.query_timeout, timeout)
                    postgres._install_schema_retrying(conn, {})
                self.assertEqual(conn.query_timeout, timeout)

    def test_closed_connection_cleanup_preserves_the_original_failure(self):
        from swarm.board import BoardUnavailable
        conn = FakeConn()
        original = BoardUnavailable("no reply within the query deadline")

        def install(c, b):
            c.execute = mock.Mock(side_effect=postgres._closed_error())
            raise original

        with mock.patch.object(postgres, "_install_schema", side_effect=install) as run, \
                mock.patch.object(postgres.time, "sleep") as sleep:
            with self.assertRaises(BoardUnavailable) as raised:
                postgres._install_schema_retrying(conn, {})
        self.assertIs(raised.exception, original)
        self.assertEqual(conn.query_timeout, 8.0)
        run.assert_called_once()
        sleep.assert_not_called()

    def run_with(self, failures, attempts=5):
        conn, calls = FakeConn(), []

        def install(c, b):
            calls.append(1)
            if len(calls) <= len(failures):
                raise failures[len(calls) - 1]
        err = io.StringIO()
        with mock.patch.object(postgres, "_install_schema", install), \
                mock.patch.object(postgres.time, "sleep") as sleep, contextlib.redirect_stderr(err):
            try:
                postgres._install_schema_retrying(conn, {}, attempts)
                result = None
            except psycopg.Error as exc:
                result = exc
        return conn, calls, sleep, err.getvalue(), result

    def test_deadlock_is_retried_and_logged(self):
        conn, calls, sleep, err, result = self.run_with(
            [psycopg.errors.DeadlockDetected("deadlock detected")] * 2)
        self.assertIsNone(result)
        self.assertEqual(len(calls), 3)
        self.assertEqual(sleep.call_count, 2)
        self.assertEqual(err.count("DeadlockDetected"), 2)
        self.assertIn("SET lock_timeout", conn.sql[0])
        self.assertEqual(conn.sql[-1], "RESET lock_timeout")

    def test_lock_timeout_is_retried(self):
        _, calls, _, _, result = self.run_with([psycopg.errors.LockNotAvailable("timeout")])
        self.assertIsNone(result)
        self.assertEqual(len(calls), 2)

    def test_gives_up_after_the_attempts(self):
        conn, calls, _, _, result = self.run_with([psycopg.errors.DeadlockDetected("d")] * 9, attempts=3)
        self.assertIsInstance(result, psycopg.errors.DeadlockDetected)
        self.assertEqual(len(calls), 3)
        self.assertEqual(conn.sql[-1], "RESET lock_timeout")

    def test_other_errors_are_not_retried(self):
        _, calls, _, _, result = self.run_with([psycopg.errors.SyntaxError("x")])
        self.assertIsInstance(result, psycopg.errors.SyntaxError)
        self.assertEqual(len(calls), 1)


@unittest.skipUnless(psycopg is not None and os.environ.get("SWARM_TEST_CONFIG"),
                     "set SWARM_TEST_CONFIG to a throwaway Postgres")
class PostgresSchemaDeadlineTests(unittest.TestCase):
    def test_real_lock_timeout_retries_on_a_still_usable_connection(self):
        from swarm import cli
        cfg = cli.load_config(Path(os.environ["SWARM_TEST_CONFIG"]).expanduser())
        table = psycopg.sql.Identifier("swarm_retry_" + uuid.uuid4().hex)
        with postgres._connect(cfg, admin=True) as conn, postgres._connect(cfg, admin=True) as holder:
            conn.execute(psycopg.sql.SQL("CREATE TABLE {} (id int)").format(table))
            try:
                holder.execute("BEGIN")
                holder.execute(psycopg.sql.SQL("LOCK TABLE {} IN ACCESS EXCLUSIVE MODE").format(table))

                def install(c, board):
                    c.execute(psycopg.sql.SQL("SELECT * FROM {}").format(table))

                with mock.patch.object(postgres, "SETUP_LOCK_TIMEOUT_MS", 1), \
                        mock.patch.object(postgres, "_install_schema", side_effect=install) as run, \
                        mock.patch.object(postgres.time, "sleep", side_effect=lambda _: holder.execute("ROLLBACK")), \
                        contextlib.redirect_stderr(io.StringIO()):
                    postgres._install_schema_retrying(conn, {})
                self.assertEqual(run.call_count, 2)
                self.assertFalse(conn.closed)
                self.assertEqual(conn.execute("SHOW lock_timeout").fetchone()[0], "0")
            finally:
                holder.execute("ROLLBACK")
                conn.execute(psycopg.sql.SQL("DROP TABLE {}").format(table))

    def test_schema_deadline_survives_cleanup_on_the_real_closed_connection(self):
        from swarm import cli
        from swarm.board import BoardUnavailable
        cfg = cli.load_config(Path(os.environ["SWARM_TEST_CONFIG"]).expanduser())
        errors = []

        def stalled(conn, board):
            conn.query_timeout = 1
            try:
                conn.execute("SELECT pg_sleep(30)")
            except BoardUnavailable as exc:
                errors.append(exc)
                raise

        with postgres._connect(cfg, admin=True) as conn, \
                mock.patch.object(postgres, "_install_schema", side_effect=stalled) as install:
            with self.assertRaises(BoardUnavailable) as raised:
                postgres._install_schema_retrying(conn, cfg["board"])
            self.assertTrue(conn.closed)
        self.assertEqual(len(errors), 1)
        self.assertIs(raised.exception, errors[0])
        self.assertIn("deadline", str(raised.exception))
        install.assert_called_once()


if __name__ == "__main__":
    unittest.main()
