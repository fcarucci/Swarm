#!/usr/bin/env python3
"""classtime.py MODULE... (run from tests/): per-class and top-test wall time."""
import sys, time, unittest, collections
cls = collections.defaultdict(float); tests = {}
class R(unittest.TextTestResult):
    def startTest(self, t): self._s = time.time(); super().startTest(t)
    def stopTest(self, t):
        d = time.time() - self._s; super().stopTest(t)
        cls[f"{type(t).__module__}.{type(t).__name__}"] += d; tests[t.id()] = d
for m in sys.argv[1:]:
    s = unittest.defaultTestLoader.loadTestsFromName(m)
    t0 = time.time()
    unittest.TextTestRunner(resultclass=R, verbosity=0, stream=open("/dev/null", "w")).run(s)
    print(f"## {m} total {time.time()-t0:.1f}s")
for c, d in sorted(cls.items(), key=lambda x: -x[1])[:25]: print(f"{d:8.1f}s {c}")
print("-- slowest tests")
for c, d in sorted(tests.items(), key=lambda x: -x[1])[:25]: print(f"{d:8.1f}s {c}")
