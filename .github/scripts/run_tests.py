"""Run unittest modules from tests/ in parallel processes, one process per module.

Each module runs in its own `python -B` child (the same isolation the serial run gave a module,
plus a private TMPDIR), longest first by the recorded durations, at most --jobs at a time.
A failing module's full output is printed; the summary ends in unittest's own OK / FAILED line.

  run_tests.py [--jobs N] [--tier fast|all|slow] [--select CHANGED_FILES_LIST] [--skip-class C]
               [--record DURATIONS_JSON] [MODULE ...]

--tier fast  leaves out the slow modules listed in .github/scripts/test_tiers.json (e2e, real
             subprocess trees, venv builds, pty); --tier slow runs only them; --tier all (default)
             runs everything.
--select F   F lists changed files (one per line): run only the test modules they affect, plus the
             smoke set (see affected() below). Falls back to the whole tier when unsure.
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
TIERS = Path(__file__).with_name("test_tiers.json")
SPLIT_SECONDS = 60.0


def all_modules() -> list[str]:
    return sorted(p.stem for p in TESTS.glob("test_*.py"))


def tiers() -> dict:
    return json.loads(TIERS.read_text())


# ---- affected-module selection -------------------------------------------------------------

def _lib_module(path: str) -> str | None:
    """lib/swarm/a/b.py -> 'a.b' (lib/swarm/a/__init__.py -> 'a'); None for anything else."""
    if not (path.startswith("lib/swarm/") and path.endswith(".py")):
        return None
    mod = path[len("lib/swarm/"):-3].replace("/", ".")
    return mod[: -len(".__init__")] if mod.endswith(".__init__") else mod


def _mentions(mod: str) -> re.Pattern:
    """A test's direct import of swarm module `mod` ('a' or 'pkg.a'): `swarm.pkg.a`,
    `from swarm.pkg import ..a..`, `from swarm.pkg.a import ...`."""
    pkg, _, name = mod.rpartition(".")
    frm = r"swarm\." + re.escape(pkg) if pkg else "swarm"
    return re.compile(r"\bswarm\.%s\b|^\s*from %s import [^\n]*\b%s\b"
                      % (re.escape(mod), frm, re.escape(name)), re.M)


def affected(changed: list[str], candidates: list[str]) -> list[str] | None:
    """The test modules a change touches, or None (= run the whole tier).

    - tests/test_x.py -> test_x; lib/swarm/<m>.py -> the tests that import <m> directly;
      bin/<f>, hooks/<f> -> the tests that name <f>.
    - A module most tests import (more than half of them: cli, the package roots) and anything
      else outside docs (tests/ helpers like support.py, workflows, requirements, install
      scripts, skills, ...) is not mapped: run everything.
    Always adds the smoke set."""
    test_src = {m: (TESTS / f"{m}.py").read_text(errors="replace") for m in candidates}
    picked: set[str] = set(tiers()["smoke"]) & set(candidates)
    for path in (p.strip() for p in changed):
        if not path:
            continue
        m = re.fullmatch(r"tests/(test_\w+)\.py", path)
        if m:
            picked |= {m.group(1)} & set(candidates)
            continue
        if path.endswith(".md") or path.startswith("docs/") or path == "LICENSE":
            continue
        mod = _lib_module(path)
        if mod is None and (path.startswith("bin/") or path.startswith("hooks/")):
            hits = {t for t, s in test_src.items() if Path(path).name in s}
        elif mod and not mod.endswith("__init__"):
            pat = _mentions(mod)
            hits = {t for t, s in test_src.items() if pat.search(s)}
        else:
            hits = None
        if hits is None or len(hits) > 0.5 * len(candidates):
            print(f"select: {path} is not mapped to a few tests; running the whole tier", file=sys.stderr)
            return None
        print(f"select: {path} -> {' '.join(sorted(hits)) or '(no direct importer)'}", file=sys.stderr)
        picked |= hits
    return sorted(picked)


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

    suite = keep(unittest.defaultTestLoader.loadTestsFromName(module))
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
    ap.add_argument("--tier", choices=("fast", "all", "slow"), default="all")
    ap.add_argument("--select")
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

    t = tiers()
    mods = a.modules or all_modules()
    if a.tier == "fast":
        mods = [m for m in mods if m not in t["slow"]]
    elif a.tier == "slow":
        mods = [m for m in mods if m in t["slow"]]
    if a.select:
        picked = affected(Path(a.select).read_text().splitlines(), mods)
        if picked is not None:
            mods = picked
    durations = t["durations_postgres" if os.environ.get("SWARM_TEST_CONFIG") else "durations"]
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
            env = dict(os.environ)
            child_tmp = tmp / mod
            child_tmp.mkdir()
            env.update(TMPDIR=str(child_tmp), TEMP=str(child_tmp), TMP=str(child_tmp))
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
