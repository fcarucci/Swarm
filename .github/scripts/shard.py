#!/usr/bin/env python3
"""Run tests/test_*.py as one process per module, N at a time (longest first). Measurement prototype.

  shard.py --jobs 4 [--durations d.json] [--out r.json] [--only a,b] [--py-flags -B] [--coverage DIR]
Exit 1 when any module failed. Each module is `python -m unittest -q <module>` run from tests/ (the
same sys.path discover -s tests gives). Writes {module: {sec, rc, tests}} to --out.
"""
import argparse, json, os, re, subprocess, sys, time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("--jobs", type=int, default=os.cpu_count() or 2)
ap.add_argument("--durations")
ap.add_argument("--out", default="shard-result.json")
ap.add_argument("--only", default="")
ap.add_argument("--flags", default="", help="extra python flags, e.g. -B")
ap.add_argument("--coverage", default="", help="dir for per-module coverage data files")
ap.add_argument("--timeout", type=int, default=1800)
ap.add_argument("--split", default="", help="modules to run one process per TestCase class")
ap.add_argument("--exclude", default="", help="modules (or module.Class) to leave out")
a = ap.parse_args()
T = Path("tests").resolve()
mods = sorted(p.stem for p in T.glob("test_*.py"))
if a.only:
    want = set(a.only.split(","))
    mods = [m for m in mods if m in want]
excl = set(filter(None, a.exclude.split(",")))
mods = [m for m in mods if m not in excl]
if a.split:
    import unittest
    sys.path.insert(0, str(T)); os.chdir(T)
    units = []
    for m in mods:
        if m in a.split.split(","):
            names = sorted({type(t).__name__ for t in unittest.defaultTestLoader.loadTestsFromName(m)._tests for t in (t._tests if hasattr(t, "_tests") else [t])})
            units += [f"{m}.{c}" for c in names if f"{m}.{c}" not in excl]
        else:
            units.append(m)
    os.chdir(T.parent)
    mods = units
dur = json.load(open(a.durations)) if a.durations else {}
size = {m: (T / f"{m.split('.')[0]}.py").stat().st_size / (20 if "." in m else 1) for m in mods}
mods.sort(key=lambda m: -(dur.get(m, {}).get("sec") if dur.get(m) else size[m] / 1e4))
flags = a.flags.split()
res = {}
t0 = time.time()

def run(m):
    env = dict(os.environ)
    cmd = [sys.executable, *flags]
    if a.coverage:
        Path(a.coverage).mkdir(parents=True, exist_ok=True)
        env["COVERAGE_FILE"] = str(Path(a.coverage).resolve() / f".coverage.{m}")
        env["COVERAGE_PROCESS_START"] = str(Path(".coveragerc").resolve())
        cmd += ["-m", "coverage", "run", "--rcfile=.coveragerc", "-m", "unittest", "-q", m]
    else:
        cmd += ["-m", "unittest", "-q", m]
    s = time.time()
    try:
        p = subprocess.run(cmd, cwd=T, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=a.timeout)
        rc, out = p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired as e:
        rc, out = 124, "TIMEOUT\n" + str(e.stdout or "")[-2000:]
    sec = time.time() - s
    n = re.search(r"Ran (\d+) test", out)
    res[m] = {"sec": round(sec, 2), "rc": rc, "tests": int(n.group(1)) if n else 0, "start": round(s - t0, 1)}
    if rc:
        print(f"::group::FAIL {m} rc={rc}\n{out[-6000:]}\n::endgroup::", flush=True)
    return m

with ThreadPoolExecutor(a.jobs) as ex:
    list(ex.map(run, mods))
wall = time.time() - t0
json.dump(res, open(a.out, "w"), indent=1, sort_keys=True)
tot = sum(r["sec"] for r in res.values())
fails = [m for m, r in res.items() if r["rc"]]
print(f"modules={len(res)} tests={sum(r['tests'] for r in res.values())} jobs={a.jobs} wall={wall:.0f}s cpu_sum={tot:.0f}s fails={fails}")
top = sorted(res.items(), key=lambda kv: -kv[1]["sec"])[:15]
for m, r in top:
    print(f"  {r['sec']:8.1f}s {r['tests']:5d} tests  {m}")
sys.exit(1 if fails else 0)
