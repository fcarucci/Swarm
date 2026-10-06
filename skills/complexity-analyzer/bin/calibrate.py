#!/usr/bin/env python3
"""Derive CXI constants from a benchmark of metrics.json outputs (one directory per system).

Method (docs/scores.md, "CXI calibration"):
1. Class limits per language: Alves, Ypma, Visser (ICSM 2010). Each system weighs the same, each unit (named
   function or closure) weighs its own code lines within its system; the limits are the cognitive values at
   70 / 80 / 90 % of the aggregated weight (moderate / high / very high).
2. Caps: per system, R (risk density) and V (units above the very-high limit per named function) with the limits
   of thresholds.json; cap = nearest-rank p95 over all systems / 0.6, so a system at the benchmark p95 on both terms
   scores 40, the D/E boundary (Alves, Correia, Visser 2011: the lowest rating is the worst 5 % of the benchmark).

Usage: python3 bin/calibrate.py LANG=DIR [LANG=DIR ...]   e.g. rust=~/work/cxv-out/bench python=... js=...
Each DIR holds <system>/metrics.json produced by bin/metrics with --lang LANG. Production code only.
"""
import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
from cxmetrics import scores  # noqa: E402

TH = json.loads((Path(__file__).resolve().parent.parent / "thresholds.json").read_text(encoding="utf-8"))
E_BOUNDARY_PENALTY = 1 - TH["grades"]["D"] / 100.0  # 0.6


def weighted_quantiles(pairs, qs):
    pairs = sorted(pairs)
    tot = sum(w for _, w in pairs)
    out, acc, qs = {}, 0.0, list(qs)
    for v, w in pairs:
        acc += w
        while qs and acc / tot >= qs[0] - 1e-12:
            out[qs.pop(0)] = v
    return out


def nearest_rank(vals, p):
    v = sorted(vals)
    return v[max(0, math.ceil(p / 100 * len(v)) - 1)]


def load(d, lang):
    m = json.loads((d / "metrics.json").read_text(encoding="utf-8"))
    fs = [f for f in m["functions"] if f["scope"] == "production" and f["lang"] == lang and f["sloc"] > 0]
    cs = [c for c in m.get("closures", []) if c["scope"] == "production" and c["lang"] == lang]
    return fs, cs


def main(argv):
    systems = []
    for arg in argv:
        lang, d = arg.split("=", 1)
        for sd in sorted(p for p in Path(d).expanduser().iterdir() if (p / "metrics.json").is_file()):
            fs, cs = load(sd, lang)
            if fs:
                systems.append((lang, sd.name, fs, cs))
    print("Class limits (Alves 2010, LOC-weighted 70/80/90 %):")
    for lang in sorted({s[0] for s in systems}):
        pairs = []
        for lg, _, fs, cs in systems:
            if lg != lang:
                continue
            units = fs + cs
            tot = sum(scores.code_lines(u) for u in units)
            pairs += [(u["cognitive"], scores.code_lines(u) / tot) for u in units]
        q = weighted_quantiles(pairs, (0.7, 0.8, 0.9))
        n = sum(1 for s in systems if s[0] == lang)
        print(f"  {lang}: {n} systems, moderate > {q[0.7]}, high > {q[0.8]}, very high > {q[0.9]}")
    print("Per system with thresholds.json limits:")
    Rs, Vs = [], []
    for lang, name, fs, cs in systems:
        r = scores.cxi(fs, TH, cs)
        Rs.append(r["risk_density_R"])
        Vs.append(r["tail_V"])
        print(f"  {lang:7s} {name:14s} R {r['risk_density_R']:.4f}  V {r['tail_V']:.4f}  CXI {r['score']} {r['grade']}")
    r95, v95 = nearest_rank(Rs, 95), nearest_rank(Vs, 95)
    print(f"p95 over {len(systems)} systems: R {r95}, V {v95}; caps = p95 / {E_BOUNDARY_PENALTY:.1f}: "
          f"profile_cap {r95 / E_BOUNDARY_PENALTY:.4f}, tail_cap {v95 / E_BOUNDARY_PENALTY:.4f}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
