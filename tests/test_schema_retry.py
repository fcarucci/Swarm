"""The Postgres schema setup survives a deadlock against live hooks: it sets a short lock_timeout,
retries a DeadlockDetected / LockNotAvailable with a pause (noted on stderr) and gives up after N."""
from __future__ import annotations

import contextlib
import io
import unittest
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

    def execute(self, q, *a):
        self.sql.append(q)


@unittest.skipIf(psycopg is None, "psycopg not installed")
class SchemaRetryTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
