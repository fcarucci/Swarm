#!/usr/bin/env python3
"""Compare two metrics.json files per language (old method vs new): function counts, top-10 cognitive,
cyclomatic mix, module/edge counts, unused-code counts and the four scores.

Usage: python bin/compare_runs.py OLD/metrics.json NEW/metrics.json [--lang python,js,rust] [--top 10]
Output is Markdown on stdout (read-only: only the two files are read)."""
import argparse
import json


def load(p):
    with open(p, encoding="utf-8") as fh:
        return json.load(fh)


def per_lang(d, lang):
    fs = [f for f in d["functions"] if f["lang"] == lang and f["scope"] == "production"]
    ts = [f for f in d["functions"] if f["lang"] == lang and f["scope"] == "test"]
    mods = [m for m in d["modules"] if m["lang"] == lang]
    ids = {m["file"] for m in mods}
    sccs = [s for s in d["sccs"]["modules"] if s and s[0] in ids]
    oe = d["over_engineering"]
    return {
        "functions (production)": len(fs), "functions (test)": len(ts),
        "SLOC (production functions)": sum(f["sloc"] for f in fs),
        "cognitive sum": sum(f["cognitive"] for f in fs),
        "cognitive > 15": sum(1 for f in fs if f["cognitive"] > 15),
        "cyclomatic sum": sum(f["cyclomatic"] for f in fs),
        "cyclomatic > 10": sum(1 for f in fs if f["cyclomatic"] > 10),
        "modules": len(mods), "import edges": sum(len(m["imports"]) for m in mods),
        "modules in cycles": sum(len(s) for s in sccs), "cycles (SCCs)": len(sccs),
        "unused items (OEI numerator)": sum(1 for i in oe["unused_pub"] if i["lang"] == lang),
        "top": sorted(fs, key=lambda f: (-f["cognitive"], -f["cyclomatic"], f["file"], f["line"])),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("old")
    ap.add_argument("new")
    ap.add_argument("--lang", default="python,js")
    ap.add_argument("--top", type=int, default=10)
    a = ap.parse_args()
    old, new = load(a.old), load(a.new)
    print("| Score | old | new |\n|---|---|---|")
    for k in ("CXI", "COI", "OEI", "MAI"):
        o, n = old["scores"]["production"].get(k), new["scores"]["production"].get(k)
        print(f"| {k} | {o and o['score']} ({o and o['grade']}) | {n and n['score']} ({n and n['grade']}) |")
    for lang in a.lang.split(","):
        o, n = per_lang(old, lang), per_lang(new, lang)
        print(f"\n### {lang}\n\n| Metric | old | new |\n|---|---|---|")
        for k in o:
            if k != "top":
                print(f"| {k} | {o[k]} | {n[k]} |")
        print(f"\nTop {a.top} by cognitive complexity (production):\n\n| # | old function | cog | cyc | new function | cog | cyc |\n|---|---|---|---|---|---|---|")
        for i in range(a.top):
            def cell(t):
                if i >= len(t):
                    return "| | | "
                f = t[i]
                return f"| `{f['file']}:{f['line']} {f['name']}` | {f['cognitive']} | {f['cyclomatic']} "
            print(f"| {i + 1} " + cell(o["top"]) + cell(n["top"]) + "|")


if __name__ == "__main__":
    main()
