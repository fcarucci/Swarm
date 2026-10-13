"""Timing runner: `discover -s tests` with the wall time of every test recorded to test-times.csv.

Run from the repo root, the same way CI runs the suite:
    python -B tests/_timing_runner.py [OUT.csv]

Columns: test id, module, seconds (startTest -> stopTest, so setUp/tearDown are included).
Exit status matches the plain runner: non-zero when any test failed or errored.
"""
import csv
import os
import sys
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)
os.chdir(ROOT)


class TimingResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.timings = []
        self._started = None

    def startTest(self, test):
        self._started = time.perf_counter()
        super().startTest(test)

    def stopTest(self, test):
        elapsed = time.perf_counter() - self._started
        self.timings.append((test.id(), elapsed))
        super().stopTest(test)


def main(argv):
    out = argv[1] if len(argv) > 1 else "test-times.csv"
    suite = unittest.TestLoader().discover(start_dir="tests", pattern="test_*.py")
    runner = unittest.TextTestRunner(verbosity=1, resultclass=TimingResult, stream=sys.stderr)
    result = runner.run(suite)

    with open(out, "w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["test_id", "module", "seconds"])
        for test_id, seconds in result.timings:
            module = test_id.split(".", 1)[0]
            writer.writerow([test_id, module, f"{seconds:.4f}"])

    total = sum(s for _, s in result.timings)
    print(f"timed {len(result.timings)} tests, sum {total:.1f}s -> {out}", file=sys.stderr)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
