#!/usr/bin/env -S uv run --no-project --quiet --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["tree-sitter==0.26.0", "tree-sitter-rust==0.24.2", "radon==6.0.1", "vulture==2.16"]
# ///
"""Deterministic complexity / coupling / over-engineering metrics and four overall scores.

Usage: python bin/metrics.py REPO [--lang rust,python,js] [--include-tests] [--out DIR] [--exclude GLOB]
Needs the tree-sitter packages (run through `uv run --no-project --with tree-sitter==0.26.0
--with tree-sitter-rust==0.24.2 python bin/metrics.py ...`, or the bin/metrics wrapper) and, once,
`python bin/install_tools.py`.
"""
import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

from cxmetrics import pipeline, report  # noqa: E402


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("repo")
    ap.add_argument("--lang", help="comma list of rust,python,js (default: all found)")
    ap.add_argument("--include-tests", action="store_true", help="also measure and score test code (separately)")
    ap.add_argument("--out", default="metrics-out", help="output directory (default ./metrics-out)")
    ap.add_argument("--exclude", action="append", default=[], help="glob of repo-relative paths to skip")
    ap.add_argument("--no-dup", action="store_true", help="skip jscpd duplication")
    ap.add_argument("--no-node", action="store_true",
                    help="do not use node tools (ESLint, dependency-cruiser, knip, jscpd): regex fallback, output marked degraded")
    ap.add_argument("--thresholds", help="alternative thresholds.json")
    a = ap.parse_args(argv)
    if a.no_node:
        os.environ["CXM_NO_NODE"] = "1"
    langs = {x.strip() for x in a.lang.split(",")} if a.lang else None
    th = pipeline.load_thresholds(a.thresholds)
    data = pipeline.run(a.repo, langs, a.include_tests, a.exclude, th, with_dup=not a.no_dup)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "metrics.json").write_bytes(pipeline.dumps(data).encode("utf-8"))
    md = report.render(data, th, Path(a.repo).resolve().name)
    (out / "report.md").write_bytes((md + "\n").encode("utf-8"))
    s = data["scores"]["production"]
    print("scores (production): " + "  ".join(
        f"{k} {s[k]['score']} {s[k]['grade']}" if s.get(k) else f"{k} n/a" for k in ("CXI", "COI", "OEI", "MAI")))
    print(f"wrote {out / 'metrics.json'} and {out / 'report.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
