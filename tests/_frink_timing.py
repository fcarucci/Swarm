"""Measurement only: per-test wall time and gap before it (class/module fixtures) -> CSV.
Run from repo root:  python -B tests/_frink_timing.py OUT.csv [module ...]"""
import csv, os, sys, time, unittest
here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, here)
os.chdir(here)
out = os.path.abspath(sys.argv[1]) if not os.path.isabs(sys.argv[1]) else sys.argv[1]
mods = sys.argv[2:]
def drop_stall(node):
    kept = unittest.TestSuite()
    for t in node:
        if isinstance(t, unittest.TestSuite): kept.addTest(drop_stall(t))
        elif type(t).__name__ != "PostgresFollowRecoveryTests": kept.addTest(t)
    return kept
loader = unittest.TestLoader()
suite = loader.loadTestsFromNames(mods) if mods else loader.discover(".", pattern="test_*.py")
suite = drop_stall(suite)
rows = []
class R(unittest.TextTestResult):
    last = None
    def startTest(self, test):
        now = time.perf_counter(); self._gap = now - (R.last or now); self._t = now; self._o = ""
        super().startTest(test)
    def stopTest(self, test):
        now = time.perf_counter(); R.last = now
        rows.append((test.id(), round(now - self._t, 4), round(self._gap, 4), self._o)); super().stopTest(test)
    def addSkip(self, t, r): self._o = "skip"; super().addSkip(t, r)
    def addSuccess(self, t): self._o = "ok"; super().addSuccess(t)
    def addFailure(self, t, e): self._o = "fail"; super().addFailure(t, e)
    def addError(self, t, e): self._o = "error"; super().addError(t, e)
    def startTestRun(self): R.last = time.perf_counter(); super().startTestRun()
t1 = time.perf_counter()
res = unittest.TextTestRunner(verbosity=0, resultclass=R, stream=sys.stderr).run(suite)
with open(out, "w", newline="") as f:
    w = csv.writer(f); w.writerow(["test_id", "test_s", "gap_before_s", "outcome"]); w.writerows(rows)
print(f"run={time.perf_counter()-t1:.1f}s tests={len(rows)}", file=sys.stderr)
