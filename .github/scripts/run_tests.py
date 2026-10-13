"""Run the unittest modules of tests/ in parallel processes (CI's test runner).

Each module runs in its own `python -B` child (the isolation the serial run gave a module), at
most --jobs (default: CPU count) at a time, longest first by the recorded durations
(.github/scripts/test_durations.json; unknown modules count as 30 s). A module of a minute or more
runs one child per test class, so it does not set the wall time alone. A failing module's full
output is printed; the summary ends in unittest's own OK / FAILED line.

  run_tests.py [--jobs N] [--backend-dependent] [--skip-class C] [--verbose] [--log FILE]
               [--record FILE] [MODULE ...]

--backend-dependent  only the modules whose tests change with SWARM_TEST_BACKEND (they name it,
                     E2E_BACKEND, e2e_harness or HARNESSES[...], or import a test module that
                     does, like test_hooks_cli's Env). The rest run the same on every backend.
--record FILE        write the measured seconds per module (refresh test_durations.json).
With SWARM_TEST_CONFIG set (Postgres), each parallel slot gets its own copy of the config naming
its own database, so slots never truncate each other's tables.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TESTS = ROOT / "tests"
DURATIONS = Path(__file__).with_name("test_durations.json")
SPLIT_SECONDS = 60.0


def all_modules() -> list[str]:
    return sorted(p.stem for p in TESTS.glob("test_*.py"))


def backend_dependent(mods: list[str]) -> list[str]:
    """The modules (of `mods`) whose tests read the backend under test (see the docstring)."""
    src = {p.stem: p.read_text(errors="replace") for p in TESTS.glob("*.py") if p.stem != "support"}
    dep = {m for m, s in src.items() if re.search(r"SWARM_TEST_BACKEND|E2E_BACKEND|e2e_harness|HARNESSES\[", s)}
    while True:     # ... or import (from) a module that does
        more = {m for m, s in src.items() if m not in dep and any(
            re.search(r"^\s*(?:from|import)\s+%s\b" % re.escape(d), s, re.M) for d in dep)}
        if not more:
            return [m for m in mods if m in dep]
        dep |= more


# ---- child: one module ---------------------------------------------------------------------

def run_child(module: str, skip_classes: list[str], verbosity: int) -> int:
    sys.path.insert(0, str(TESTS))

    def keep(node):
        kept = unittest.TestSuite()
        for t in node:
            if isinstance(t, unittest.TestSuite):
                kept.addTest(keep(t))
            elif type(t).__name__ not in skip_classes:
                kept.addTest(t)
        return kept

    try:
        suite = keep(unittest.defaultTestLoader.loadTestsFromName(module))
    except unittest.SkipTest as e:      # a module-level skip, as `unittest discover` reports it
        print(f"module skipped: {e}\n\nRan 0 tests\n\nOK (skipped=1)")
        return 0
    result = unittest.TextTestRunner(verbosity=verbosity).run(suite)
    return 0 if result.wasSuccessful() else 1


# ---- parent: schedule modules --------------------------------------------------------------

def slot_config(slot: int, tmp: Path) -> str | None:
    cfg = os.environ.get("SWARM_TEST_CONFIG")
    if not cfg:
        return None
    text = Path(cfg).read_text()
    text = re.sub(r'^(dbname\s*=\s*")([^"]+)(")', rf"\g<1>\g<2>_s{slot}\g<3>", text, count=1, flags=re.M)
    out = tmp / f"test-config-s{slot}.toml"
    out.write_text(text)
    return str(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--jobs", type=int, default=os.cpu_count() or 2)
    ap.add_argument("--backend-dependent", action="store_true")
    ap.add_argument("--skip-class", action="append", default=[])
    ap.add_argument("--record")
    ap.add_argument("--verbose", action="store_true", help="unittest -v in every module")
    ap.add_argument("--log", help="write every module's full output to this file")
    ap.add_argument("--list", help=argparse.SUPPRESS)
    ap.add_argument("--child")
    ap.add_argument("modules", nargs="*")
    a = ap.parse_args()
    if a.list:
        sys.path.insert(0, str(TESTS))
        names = set()
        for suite in unittest.defaultTestLoader.loadTestsFromName(a.list):
            for t in suite:
                names.add(type(t).__name__)
        print("\n".join(sorted(names)))
        return 0
    if a.child:
        return run_child(a.child, a.skip_class, 2 if a.verbose else 1)

    mods = a.modules or all_modules()
    if a.backend_dependent:
        mods = backend_dependent(mods)
    durations = json.loads(DURATIONS.read_text())["postgres" if os.environ.get("SWARM_TEST_CONFIG") else "memory"]
    # A module that takes a minute or more runs one process per test class, so one slow module
    # does not set the wall time.
    units: dict[str, float] = {}
    for m in mods:
        d = durations.get(m, 30.0)
        classes = []
        if d >= SPLIT_SECONDS:
            out = subprocess.run([sys.executable, "-B", __file__, "--list", m], cwd=ROOT, text=True,
                                 stdin=subprocess.DEVNULL, capture_output=True).stdout.split()
            classes = [f"{m}.{c}" for c in out]
        for u in classes or [m]:
            units[u] = d / (len(classes) or 1)
    mods = sorted(units, key=lambda u: -units[u])
    print(f"running {len(mods)} test modules, {a.jobs} at a time: {' '.join(sorted(mods))}", flush=True)

    tmp = Path(tempfile.mkdtemp(prefix="swarm-ci-"))
    slots = list(range(a.jobs))
    lock = threading.Lock()
    results: dict[str, tuple[int, float, str]] = {}
    queue = list(mods)

    def worker() -> None:
        with lock:
            slot = slots.pop()
        cfg = slot_config(slot, tmp)
        while True:
            with lock:
                if not queue:
                    return
                mod = queue.pop(0)
            env = dict(os.environ)    # no private TMPDIR: longer temp paths break AF_UNIX sockets
                                      # (macOS) and git under MAX_PATH (Windows)
            if cfg:
                env["SWARM_TEST_CONFIG"] = cfg
            cmd = [sys.executable, "-B", __file__, "--child", mod]
            if a.verbose:
                cmd.append("--verbose")
            for c in a.skip_class:
                cmd += ["--skip-class", c]
            start = time.monotonic()
            p = subprocess.run(cmd, cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace")
            took = time.monotonic() - start
            with lock:
                results[mod] = (p.returncode, took, p.stdout)
                ran = re.search(r"^Ran (\d+) test", p.stdout, re.M)
                print(f"{'ok  ' if p.returncode == 0 else 'FAIL'} {took:6.1f}s {mod} "
                      f"({ran.group(1) if ran else '?'} tests)", flush=True)

    start = time.monotonic()
    threads = [threading.Thread(target=worker) for _ in range(min(a.jobs, len(mods)) or 1)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    wall = time.monotonic() - start

    failed = sorted(m for m, (rc, _, _) in results.items() if rc != 0)
    for m in failed:
        print(f"\n{'#' * 70}\n# {m}\n{'#' * 70}\n{results[m][2]}", flush=True)
    total = sum(int(x) for _, _, out in results.values() for x in re.findall(r"^Ran (\d+) test", out, re.M))
    busy = sum(t for _, t, _ in results.values())
    print(f"\n{len(results)} modules, {total} tests, {wall:.1f}s wall ({busy:.1f}s summed); slowest:")
    for m, (_, took, _) in sorted(results.items(), key=lambda kv: -kv[1][1])[:10]:
        print(f"  {took:6.1f}s {m}")
    if a.log:
        Path(a.log).write_text("".join(results[m][2] for m in sorted(results)))
    if a.record:
        per_module: dict[str, float] = {}
        for u, (_, took, _) in results.items():
            per_module[u.split(".")[0]] = per_module.get(u.split(".")[0], 0.0) + took
        Path(a.record).write_text(json.dumps({m: round(t, 1) for m, t in sorted(per_module.items())},
                                             indent=1) + "\n")
    if failed:
        print(f"\nFAILED modules: {' '.join(failed)}")
        print("FAILED")
        return 1
    print("\nOK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
