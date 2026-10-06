"""The four overall scores as pure functions of measured data + thresholds.json."""
from .core import percentile, rnd


def grade(score, th):
    if score is None:
        return None
    for g in ("A", "B", "C", "D"):
        if score >= th["grades"][g]:
            return g
    return "E"


def _class_of(v, above):
    if v > above["very_high"]:
        return "very_high"
    if v > above["high"]:
        return "high"
    if v > above["moderate"]:
        return "moderate"
    return "low"


def risk_profile(pairs, above=None):
    """pairs: [(value, weight)] or [(value, weight, above)] -> shares per class (weight-weighted)."""
    tot = sum(p[1] for p in pairs)
    prof = {"low": 0.0, "moderate": 0.0, "high": 0.0, "very_high": 0.0}
    if tot <= 0:
        return prof
    for p in pairs:
        prof[_class_of(p[0], p[2] if len(p) > 2 else above)] += p[1]
    return {k: v / tot for k, v in prof.items()}


def named(funcs):
    return [f for f in funcs if f["sloc"] > 0]


def code_lines(u, own=True):
    """LOC weight of a unit (rca code lines; sloc if a record has no ploc).
    own=True: the unit's own lines (CXI units: nested fns and closures excluded; may be 0).
    own=False: the SIG unit of a named fn, i.e. its own lines plus its closures', nested named fns excluded (MAI)."""
    key = "own_ploc" if own else "unit_ploc"
    if key in u:
        return u[key]
    return u.get("ploc", u.get("sloc", 0))


def cognitive_limits(c, lang):
    return c.get("cognitive_above_by_language", {}).get(lang, c["cognitive_above"])


def cxi(funcs, th, closures=()):
    """Units = named functions + closures (each scored on its own cognitive, weighted by its own code lines)."""
    c = th["cxi"]
    fs = named(funcs)
    if not fs:
        return None
    units = fs + [x for x in closures if code_lines(x) > 0]  # zero-line closures share a line already counted
    lim = {u["lang"]: cognitive_limits(c, u["lang"]) for u in units if "lang" in u}
    above = lambda u: lim.get(u.get("lang"), c["cognitive_above"])  # noqa: E731
    prof = risk_profile([(u["cognitive"], code_lines(u), above(u)) for u in units])
    w = c["risk_weights"]
    R = w["moderate"] * prof["moderate"] + w["high"] * prof["high"] + w["very_high"] * prof["very_high"]
    over_vh = sum(1 for u in units if u["cognitive"] > above(u)["very_high"])
    V = over_vh / len(fs)
    T = min(1.0, V / c["tail_cap"])
    penalty = c["profile_weight"] * min(1.0, R / c["profile_cap"]) + c["tail_weight"] * T
    score = 100 * (1 - min(1.0, penalty))
    loc = sum(code_lines(u) for u in units)
    cogs = [f["cognitive"] for f in fs]
    ucogs = [u["cognitive"] for u in units]
    hi = c["report_high_over"]
    return {
        "score": rnd(score, 1), "grade": grade(score, th),
        "profile": {k: rnd(v) for k, v in prof.items()}, "risk_density_R": rnd(R),
        "tail_V": rnd(V), "tail_T": rnd(T), "units_over_very_high": over_vh,
        "functions": len(fs), "closures": len(units) - len(fs),
        "p95_cognitive": percentile(cogs, 95), "p90_cognitive": percentile(cogs, 90), "max_cognitive": max(ucogs),
        "mean_cognitive": rnd(sum(cogs) / len(cogs), 2),
        "over_15": sum(1 for x in ucogs if x > hi[0]),
        "over_25": sum(1 for x in ucogs if x > hi[1]),
        "cognitive_per_100_loc": rnd(100.0 * sum(ucogs) / loc, 2) if loc else None,
        "mi_vs_red_share": rnd(sum(1 for f in fs if f["mi_vs"] is not None and f["mi_vs"] < th["mai"]["mi_vs_red_below"]) / len(fs)),
        "mi_vs_yellow_share": rnd(sum(1 for f in fs if f["mi_vs"] is not None and th["mai"]["mi_vs_red_below"] <= f["mi_vs"] < th["mai"]["mi_vs_yellow_below"]) / len(fs)),
    }


def coi(cp, th):
    """cp: coupling dict {modules: [...], crates: [...], module_sccs, crate_sccs}."""
    c = th["coi"]
    mods = cp["modules"]
    if not mods:
        return None
    n = len(mods)
    cyc_mod = sum(1 for m in mods if m["in_scc"]) / n
    crates = cp["crates"]
    cyc_crate = (sum(1 for x in crates if x["in_scc"]) / len(crates)) if crates else 0.0
    fan = sum(1 for m in mods if m["ce"] > c["fan_ce_over"]) / n
    p = percentile([m["henry_kafura"] for m in mods], c["hub_percentile"])
    hub = sum(1 for m in mods if m["henry_kafura"] > p and m["ca"] >= c["hub_min_ca"]) / n
    elig = [x for x in crates if x["distance_eligible"]]
    wsum = sum(x["sloc"] for x in elig)
    mean_d = (sum(x["distance"] * x["sloc"] for x in elig) / wsum) if wsum else 0.0
    dist_norm = min(1.0, mean_d / c["dist_cap"])
    w = c["weights"]
    pen = (w["cyc_mod"] * cyc_mod + w["cyc_crate"] * cyc_crate + w["fan"] * fan + w["hub"] * hub
           + w["dist"] * dist_norm)
    score = 100 * (1 - min(1.0, max(0.0, pen)))
    return {"score": rnd(score, 1), "grade": grade(score, th),
            "components": {"cyc_mod": rnd(cyc_mod), "cyc_crate": rnd(cyc_crate), "fan": rnd(fan),
                           "hub": rnd(hub), "dist_norm": rnd(dist_norm), "mean_D": rnd(mean_d),
                           "hk_p90": p, "distance_crates": len(elig)},
            "modules": n, "modules_in_cycles": sum(1 for m in mods if m["in_scc"])}


def oei(ratios, th):
    c = th["oei"]
    w = c["weights"]
    wm = sum(w[k] * ratios[k] for k in w)
    score = 100 * (1 - min(1.0, wm / c["cap"]))
    return {"score": rnd(score, 1), "grade": grade(score, th), "weighted_mean": rnd(wm),
            "components": {k: ratios[k] for k in sorted(w)}, "heuristic": True}


_NONE = {"moderate": 0.0, "high": 0.0, "very_high": 0.0}
_ALL = {"moderate": 1.0, "high": 1.0, "very_high": 1.0}


def rating_from_profile(prof, table):
    """SIG rating with linear interpolation (Alves, Correia, Visser 2011): 0.5 .. 5.5.
    The star row k is the first row the profile satisfies; within it the rating drops from k + 0.5 to k - 0.5
    as the worst category moves from the next-better row's limit to row k's limit."""
    rows = sorted(table, key=lambda r: -r["stars"])
    k, lim, better = 1, _ALL, rows[-1]
    prev = _NONE
    for row in rows:
        if all(prof[c] <= row[c] for c in _NONE):
            k, lim, better = row["stars"], row, prev
            break
        prev = row
    f = 0.0
    for cat in _NONE:
        span = lim[cat] - better[cat]
        if span > 0:
            f = max(f, min(1.0, max(0.0, (prof[cat] - better[cat]) / span)))
    return k + 0.5 - f


def stars_from_profile(prof, table):
    for row in table:
        if (prof["moderate"] <= row["moderate"] and prof["high"] <= row["high"]
                and prof["very_high"] <= row["very_high"]):
            return row["stars"]
    return 1


def rating_from_dup(pct, rows):
    lo = 0.0
    for lim, st in rows:
        if pct <= lim:
            return st + 0.5 - (pct - lo) / (lim - lo)
        lo = lim
    return 1.5 - min(1.0, (pct - lo) / (100.0 - lo))


def stars_from_dup(pct, rows):
    for lim, st in rows:
        if pct <= lim:
            return st
    return 1


def mai(funcs, modules, dup_pct, th):
    c = th["mai"]
    fs = named(funcs)
    if not fs:
        return None
    subs = {}
    tbl = c["star_table"]
    for key, field, above in (("unit_size", "ploc", c["unit_size_loc_above"]),
                              ("unit_complexity", "cyclomatic", c["unit_complexity_cyclomatic_above"]),
                              ("unit_interfacing", "nargs_adj", c["unit_interfacing_params_above"])):
        def val(f):
            if field == "ploc":
                return code_lines(f, own=False)
            return f[field] if field != "nargs_adj" else f.get("nargs_adj", f["nargs"])
        prof = risk_profile([(val(f), code_lines(f, own=False)) for f in fs], above)
        subs[key] = {"stars": stars_from_profile(prof, tbl), "rating": rnd(rating_from_profile(prof, tbl), 3),
                     "profile": {k: rnd(v) for k, v in prof.items()}}
    if dup_pct is not None:
        rows = c["duplication_pct_stars"]
        subs["duplication"] = {"stars": stars_from_dup(dup_pct, rows), "rating": rnd(rating_from_dup(dup_pct, rows), 3),
                               "percentage": rnd(dup_pct, 2)}
    if modules:
        prof = risk_profile([(m["ca"], max(m["sloc"], 1)) for m in modules], c["module_coupling_ca_above"])
        subs["module_coupling"] = {"stars": stars_from_profile(prof, tbl), "rating": rnd(rating_from_profile(prof, tbl), 3),
                                   "profile": {k: rnd(v) for k, v in prof.items()}}
    mean = sum(s["rating"] for s in subs.values()) / len(subs)
    score = 100 * min(1.0, max(0.0, (mean - 0.5) / 5))
    return {"score": rnd(score, 1), "grade": grade(score, th), "mean_rating": rnd(mean, 3),
            "mean_stars": rnd(sum(s["stars"] for s in subs.values()) / len(subs), 3),
            "sub_ratings": subs, "partial": len(subs) < 5,
            "missing": sorted({"unit_size", "unit_complexity", "unit_interfacing", "duplication", "module_coupling"} - set(subs))}
