"""Summarise the `unittest -v` log of CI's "Drive suites on Postgres" step and fail it when the
step did not really run the drive tests on Postgres.

    python .github/scripts/drive_pg_summary.py drive-pg.log [tests-dir]

Per test_drive_* module: tests run, and how many of them ran in a Postgres variant (a class
whose name ends in "Postgres", made by tests/support.add_backend_variants). Fails when
  - a test_drive_*.py module ran no test at all;
  - a module that builds Postgres variants (it calls add_backend_variants) ran none of them;
  - any test was skipped for a reason other than Windows-only or systemd-only.
Modules that build no board (the pure logic ones) have no Postgres variants; the modules that
drive the CLI on a board (activate, hooks) run entirely on Postgres through SWARM_TEST_BACKEND.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

RESULT = re.compile(r"\((?P<id>[\w.]+)\)[^\n]*?(?:\n[^\n]*?)? \.\.\. (?P<res>ok|FAIL|ERROR|skipped[^\n]*|expected failure|unexpected success)")
ALLOWED_SKIP = re.compile(r"windows|systemd", re.I)


def summarise(log: str, modules: dict[str, bool]) -> tuple[list[str], list[str]]:
    """(table lines, problems). `modules` maps each test_drive_* module to whether it builds
    Postgres variants."""
    runs: dict[str, list[tuple[str, str]]] = {m: [] for m in modules}
    for m in RESULT.finditer(log):
        test_id = m.group("id")
        module = test_id.split(".")[0]
        if module in runs:
            runs[module].append((test_id, m.group("res")))
    lines, problems = [f"{'module':28} {'tests':>6} {'on postgres':>12} {'skipped':>8}"], []
    for module, builds_variants in sorted(modules.items()):
        results = runs[module]
        variants = [r for r in results if r[0].split(".")[1].endswith("Postgres")] if builds_variants else []
        skipped = [r for r in results if r[1].startswith("skipped")]
        lines.append(f"{module:28} {len(results):>6} {len(variants):>12} {len(skipped):>8}")
        if not results:
            problems.append(f"{module}: no test ran")
        if builds_variants and not variants:
            problems.append(f"{module}: builds Postgres variants but none ran")
        problems += [f"{t}: skipped ({res})" for t, res in skipped if not ALLOWED_SKIP.search(res)]
    return lines, problems


def main(argv: list[str]) -> int:
    log = Path(argv[1]).read_text(errors="replace")
    tests = Path(argv[2]) if len(argv) > 2 else Path("tests")
    modules = {p.stem: re.search(r"^add_backend_variants\(", p.read_text(), re.M) is not None
               for p in sorted(tests.glob("test_drive_*.py"))}
    lines, problems = summarise(log, modules)
    print("\n".join(lines))
    for p in problems:
        print(f"::error::{p}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
