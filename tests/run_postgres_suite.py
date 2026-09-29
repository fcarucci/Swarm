"""The CI postgres job's runner: the whole suite except the known-stall recovery tests.

A real file with a __main__ guard rather than `python - <<EOF` in the workflow: the process tests
start "spawn" children, which re-import the parent's __main__ from its file and fail on "<stdin>".
Run from tests/:  python -B run_postgres_suite.py
"""
import sys
import unittest


def without_stall_recovery(node):
    kept = unittest.TestSuite()
    for t in node:
        if isinstance(t, unittest.TestSuite):
            kept.addTest(without_stall_recovery(t))
        elif type(t).__name__ != "PostgresFollowRecoveryTests":
            kept.addTest(t)
    return kept


if __name__ == "__main__":
    suite = unittest.TestLoader().discover(".", pattern="test_*.py")
    result = unittest.TextTestRunner(verbosity=1).run(without_stall_recovery(suite))
    sys.exit(0 if result.wasSuccessful() else 1)
