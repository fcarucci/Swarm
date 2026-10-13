"""Measurement prototype: the Postgres job runs only what Postgres changes.

  python -B tests/_frink_pg_select.py list OUT.json      (WITHOUT SWARM_TEST_CONFIG) ids that run without Postgres
  python -B tests/_frink_pg_select.py pg  NONPG.json CSV  (WITH it) only the ids that did not run without it
  python -B tests/_frink_pg_select.py env NONPG.json CSV  (WITH it, SWARM_TEST_BACKEND=postgres) the test_drive_*
        tests that reach the board through SWARM_TEST_BACKEND (test_hooks_cli.Env), minus what `pg` already ran
"""
import json, os, sys, time, unittest, csv
here = os.path.dirname(os.path.abspath(__file__))


def walk(node):
    for t in node:
        if isinstance(t, unittest.TestSuite):
            yield from walk(t)
        else:
            yield t


def skipped(t):
    if getattr(type(t), "__unittest_skip__", False):
        return True
    m = getattr(t, t._testMethodName, None)
    return bool(getattr(m, "__unittest_skip__", False))


def runs_on_env_backend(t):
    import test_hooks_cli
    return isinstance(t, test_hooks_cli.Env)


def main():
    sys.path.insert(0, here)
    os.chdir(here)
    mode = sys.argv[1]
    suite = unittest.TestLoader().discover(".", pattern="test_*.py")
    tests = [t for t in walk(suite) if type(t).__name__ != "PostgresFollowRecoveryTests"
             and type(t).__name__ != "_FailedTest" or mode == "list"]
    if mode == "list":
        ids = [t.id() for t in tests if not skipped(t)]
        json.dump(ids, open(sys.argv[2], "w"))
        print(f"{len(ids)} ids run without Postgres")
        return
    nonpg = set(json.load(open(sys.argv[2])))
    if mode == "pg":
        keep = [t for t in tests if t.id() not in nonpg]
    else:
        keep = [t for t in tests if t.id().startswith("test_drive_") and t.id() in nonpg and runs_on_env_backend(t)]
    print(f"{mode}: {len(keep)} of {len(tests)} tests", file=sys.stderr)
    t0 = time.perf_counter()
    r = unittest.TextTestRunner(verbosity=1).run(unittest.TestSuite(keep))
    print(f"{mode}: ran {r.testsRun} in {time.perf_counter() - t0:.1f}s, failures={len(r.failures)} errors={len(r.errors)} skipped={len(r.skipped)}", file=sys.stderr)


if __name__ == "__main__":
    main()
