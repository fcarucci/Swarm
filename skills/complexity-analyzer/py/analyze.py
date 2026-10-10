#!/usr/bin/env python3
"""Per-function complexity ranking for Python: the Python counterpart of js/analyze.sh.

  analyze.py [--threshold N] [--cyclomatic N] [--all] <file-or-dir> [more...]

Prints TSV, highest cognitive first:  cognitive<TAB>cyclomatic<TAB>file<TAB>line<TAB>function
  cognitive   rust-code-analysis-cli (Sonar rules; the same numbers bin/metrics reports for Python)
  cyclomatic  radon cc (pinned in tool-manifest.json); "-" when radon gave no block for the function
A function is listed when cognitive > --threshold (default 15) or cyclomatic > --cyclomatic (default 10),
the same strict comparison the JavaScript driver makes. --all, or --threshold 0, lists every function.
Totals for the whole input go to stderr, for before/after comparisons in refactoring loops.
"""
import argparse
import importlib.util
import json
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "lib"))
import pyast  # noqa: E402
from cxmetrics import core, funcs, pytools  # noqa: E402

COG_THRESHOLD = 15
CYC_THRESHOLD = 10


def build_rows(rca_by_stage, mapping, radon_cc, sources):
    """Join cognitive (rca) and cyclomatic (radon) per function.

    rca_by_stage: {stage name: rca JSON}; mapping: {stage name: displayed path};
    radon_cc: {stage name: {"blocks": [...]} | {"error": ...}} from pytools.parse_radon_cc;
    sources: {stage name: source text}, used only to give nested functions their qualified names."""
    rows = []
    for stage in sorted(mapping):
        js = rca_by_stage.get(stage)
        if js is None:
            continue
        rel = mapping[stage]
        _sloc, fns, _closures = funcs.extract(js, rel, "python")
        cyc_by_line = {}
        for b in radon_cc.get(stage, {}).get("blocks", []):
            cyc_by_line.setdefault(b["line"], b["cyclomatic"])
        qual = pyast.qualnames(sources.get(stage, ""))
        for f in fns:
            rows.append({
                "cognitive": int(f["cognitive"]),
                "cyclomatic": cyc_by_line.get(f["line"]),
                "file": rel,
                "line": int(f["line"]),
                "function": qual.get(f["line"], f["name"]),
            })
    rows.sort(key=lambda r: (-r["cognitive"], -(r["cyclomatic"] or 0), r["file"], r["line"]))
    return rows


def select(rows, cog_threshold=COG_THRESHOLD, cyc_threshold=CYC_THRESHOLD, show_all=False):
    if show_all or cog_threshold == 0:
        return list(rows)
    return [r for r in rows if r["cognitive"] > cog_threshold or (r["cyclomatic"] or 0) > cyc_threshold]


def totals(rows):
    return {
        "functions": len(rows),
        "cognitive": sum(r["cognitive"] for r in rows),
        "cyclomatic": sum(r["cyclomatic"] or 0 for r in rows),
    }


def format_row(r):
    cyc = "-" if r["cyclomatic"] is None else str(r["cyclomatic"])
    return f"{r['cognitive']}\t{cyc}\t{r['file']}\t{r['line']}\t{r['function']}"


def analyze_paths(paths):
    """Run both tools over the given files and directories. Returns all rows (unfiltered)."""
    files = pyast.iter_python_files(paths)
    if not files:
        raise SystemExit("no Python files found under: " + " ".join(paths))
    rca_bin = core.find_rca()
    if not rca_bin:
        raise SystemExit("rust-code-analysis-cli not found. Run: python3 bin/install_tools.py")
    if importlib.util.find_spec("radon") is None:
        raise SystemExit("radon not importable; run via: uv run --no-project --with radon==6.0.1 python py/analyze.py ...")
    work = Path(tempfile.mkdtemp(prefix="pycx-", dir=core.tmp_base()))
    try:
        staged = [{"rel": rel, "lang": "python", "test": False} for rel, _ in files]
        paths_by_rel = dict(files)
        mapping = core.stage(".", staged, work / "stage")  # display paths are relative to the cwd
        rca = core.run_rca(work, rca_bin)
        rc, out, err = pytools._run(["radon", "cc", "-j", "stage/prod"], work)
        if rc != 0:
            raise SystemExit("radon cc failed: " + err.strip()[-300:])
        radon_cc = pytools.parse_radon_cc(json.loads(out or "{}"))
        sources = {name: core.read_text(paths_by_rel[rel]) for name, rel in mapping.items()}
        return build_rows(rca, mapping, radon_cc, sources)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("paths", nargs="+")
    ap.add_argument("--threshold", type=int, default=COG_THRESHOLD, help="cognitive threshold (0 lists all)")
    ap.add_argument("--cyclomatic", type=int, default=CYC_THRESHOLD, help="cyclomatic threshold")
    ap.add_argument("--all", action="store_true", help="list every function")
    a = ap.parse_args(argv)
    rows = analyze_paths(a.paths)
    shown = select(rows, a.threshold, a.cyclomatic, a.all)
    for r in shown:
        print(format_row(r))
    t = totals(rows)
    print(f"# python functions: {t['functions']}  cognitive total: {t['cognitive']}  "
          f"cyclomatic total: {t['cyclomatic']}  listed: {len(shown)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
