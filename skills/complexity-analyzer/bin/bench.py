#!/usr/bin/env -S uv run --no-project --quiet --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["tree-sitter==0.26.0", "tree-sitter-rust==0.24.2", "radon==6.0.1", "vulture==2.16"]
# ///
"""Fetch the CXI calibration benchmark (calibration/benchmark.tsv) at its pinned commits and run the metrics on it.

Usage: uv run bin/bench.py --dest DIR [--out DIR] [--only LANG[,LANG]]
  --dest  where the repositories are fetched (one shallow checkout per repo; reused if already at the commit).
          Use a disk-backed directory, not a RAM /tmp.
  --out   metrics outputs, <out>/<lang>/<name>/metrics.json (default <dest>/metrics)
Then calibrate in two passes (docs/scores.md, "CXI calibration"):
  1. python3 bin/calibrate.py rust=<out>/rust python=<out>/python js=<out>/js
     -> copy the printed per-language class limits into thresholds.json (cxi.cognitive_above_by_language)
  2. run bin/calibrate.py again: R and V are now computed with the new limits; copy profile_cap / tail_cap.
The metrics do not depend on thresholds.json (only the scores do), so step 2 needs no new metrics run.
"""
import argparse
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import metrics  # noqa: E402

MANIFEST = HERE.parent / "calibration" / "benchmark.tsv"


def git(*args, cwd=None):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def fetch(url, commit, d):
    if (d / ".git").is_dir():
        try:
            if git("rev-parse", "HEAD", cwd=d) == commit:
                return
        except subprocess.CalledProcessError:
            pass
    d.mkdir(parents=True, exist_ok=True)
    if not (d / ".git").is_dir():
        git("init", "-q", cwd=d)
        git("remote", "add", "origin", url, cwd=d)
    git("fetch", "-q", "--depth", "1", "origin", commit, cwd=d)
    git("checkout", "-q", "--detach", "FETCH_HEAD", cwd=d)
    got = git("rev-parse", "HEAD", cwd=d)
    if got != commit:
        raise SystemExit(f"{url}: expected {commit}, got {got}")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dest", required=True)
    ap.add_argument("--out")
    ap.add_argument("--only", help="comma list of rust,python,js")
    a = ap.parse_args(argv)
    dest = Path(a.dest).expanduser().resolve()
    out = Path(a.out).expanduser().resolve() if a.out else dest / "metrics"
    only = set(a.only.split(",")) if a.only else None
    rows = [ln.split("\t") for ln in MANIFEST.read_text(encoding="utf-8").splitlines() if ln and not ln.startswith("#")]
    for lang, name, url, commit in rows:
        if only and lang not in only:
            continue
        repo = dest / lang / name
        fetch(url, commit, repo)
        print(f"{lang} {name} @ {commit[:9]}", flush=True)
        metrics.main([str(repo), "--lang", lang, "--no-dup", "--out", str(out / lang / name)])
    print("next: python3 bin/calibrate.py " + " ".join(f"{lg}={out / lg}" for lg in ("rust", "python", "js")
                                                        if not only or lg in only))
    return 0


if __name__ == "__main__":
    sys.exit(main())
