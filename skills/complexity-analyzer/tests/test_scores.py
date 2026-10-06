"""Unit tests for the four score formulas (pure functions of data + thresholds.json)."""
import json
import sys
from pathlib import Path

SKILL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL / "lib"))

from cxmetrics import scores  # noqa: E402
from cxmetrics.core import percentile  # noqa: E402

TH = json.loads((SKILL / "thresholds.json").read_text(encoding="utf-8"))


def fn(cog=0, sloc=10, cyc=1, nargs=1, mi=80.0):
    return {"cognitive": cog, "sloc": sloc, "cyclomatic": cyc, "nargs": nargs, "nargs_adj": nargs, "mi_vs": mi}


def test_percentile_nearest_rank():
    assert percentile([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 90) == 9.0
    assert percentile([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 95) == 10.0
    assert percentile([], 95) == 0.0


def test_grades():
    assert [scores.grade(x, TH) for x in (100, 85, 84.9, 70, 55, 40, 39.9)] == ["A", "A", "B", "B", "C", "D", "E"]


def test_cxi_flat_code_is_100():
    # Rust limits 3/7/16: cognitive <= 3 is 'low'
    assert scores.cxi([fn(0), fn(2), fn(3)], TH)["score"] == 100.0


def test_cxi_all_very_high_is_0():
    r = scores.cxi([fn(cog=30, sloc=100)], TH)
    assert r["score"] == 0.0 and r["grade"] == "E" and r["profile"]["very_high"] == 1.0 and r["tail_T"] == 1.0


def test_cxi_profile_cap():
    # 50% of code lines moderate -> R = 0.5 -> 0.75 * 0.5 / 4.06 = 0.0924; no unit above 16 -> T = 0
    r = scores.cxi([fn(cog=5, sloc=50), fn(cog=0, sloc=50)], TH)
    assert r["risk_density_R"] == 0.5 and r["tail_T"] == 0.0 and r["score"] == 90.8


def test_cxi_risk_weights():
    # 10% of code lines high (x3) -> R = 0.3 -> 0.75 * 0.3 / 4.06 = 0.0554 => 94.5
    fs = [fn(cog=10, sloc=10)] + [fn(cog=0, sloc=10)] * 9
    r = scores.cxi(fs, TH)
    assert r["risk_density_R"] == 0.3 and r["tail_T"] == 0.0 and r["score"] == 94.5


def test_cxi_tail_is_count_based():
    # one short unit above 16 among 10 functions: tiny LOC share but V = 0.1 >= 0.064 -> T = 1
    fs = [fn(cog=17, sloc=1)] + [fn(cog=0, sloc=11)] * 9
    r = scores.cxi(fs, TH)
    assert r["units_over_very_high"] == 1 and r["tail_V"] == 0.1 and r["tail_T"] == 1.0
    assert r["risk_density_R"] == 0.09 and r["score"] == 73.3  # 100 * (1 - 0.75 * 0.09 / 4.06 - 0.25)


def test_cxi_closures_are_own_units():
    parent = dict(fn(cog=2, sloc=20), own_ploc=10, ploc=20, lang="rust")
    clo = {"cognitive": 20, "own_ploc": 10, "lang": "rust", "scope": "production"}
    r = scores.cxi([parent], TH, [clo])
    assert r["closures"] == 1 and r["units_over_very_high"] == 1
    assert r["profile"]["very_high"] == 0.5 and r["profile"]["low"] == 0.5
    assert scores.cxi([parent], TH)["score"] == 100.0  # without the closure unit the parent alone is low


def test_cxi_per_language_limits():
    py = dict(fn(cog=20, sloc=10), lang="python")  # Python limits 11/17/31: 20 is 'high'
    rs = dict(fn(cog=20, sloc=10), lang="rust")    # Rust limits 3/7/16: 20 is 'very high'
    assert scores.cxi([py], TH)["profile"]["high"] == 1.0
    assert scores.cxi([rs], TH)["profile"]["very_high"] == 1.0


def test_coi_cycles_only():
    mods = [{"in_scc": i < 2, "ce": 1, "ca": 1, "henry_kafura": 0} for i in range(4)]
    r = scores.coi({"modules": mods, "crates": []}, TH)
    assert r["score"] == 82.5  # 100 * (1 - 0.35 * 0.5)


def test_coi_clean_is_100():
    mods = [{"in_scc": False, "ce": 2, "ca": 1, "henry_kafura": 0} for _ in range(5)]
    assert scores.coi({"modules": mods, "crates": []}, TH)["score"] == 100.0


def test_coi_distance_term():
    mods = [{"in_scc": False, "ce": 0, "ca": 0, "henry_kafura": 0}]
    crates = [{"in_scc": False, "distance_eligible": True, "distance": 0.7, "sloc": 10}]
    r = scores.coi({"modules": mods, "crates": crates}, TH)
    assert r["score"] == 85.0  # dist_norm = 1 -> 0.15 penalty


def test_coi_fan_and_hub():
    mods = [{"in_scc": False, "ce": 13, "ca": 0, "henry_kafura": 0}] + \
           [{"in_scc": False, "ce": 1, "ca": 6, "henry_kafura": 100 + i} for i in range(9)]
    r = scores.coi({"modules": mods, "crates": []}, TH)
    # fan = 1/10; HK p90 = 108 -> hub members: HK > 108 and ca >= 5 -> 1 module (109) => hub 0.1
    assert r["components"]["fan"] == 0.1 and r["components"]["hub"] == 0.1
    assert r["score"] == 100 * (1 - (0.20 * 0.1 + 0.15 * 0.1))


def test_oei_scale():
    zero = {k: 0.0 for k in TH["oei"]["weights"]}
    assert scores.oei(zero, TH)["score"] == 100.0
    half = {k: 0.5 for k in TH["oei"]["weights"]}
    assert scores.oei(half, TH)["score"] == 0.0
    st_only = dict(zero, st=1.0)
    r = scores.oei(st_only, TH)
    assert r["score"] == 50.0 and r["heuristic"] is True


def test_mai_star_tables():
    tbl = TH["mai"]["star_table"]
    prof = lambda m, h, v: {"moderate": m, "high": h, "very_high": v}  # noqa: E731
    assert scores.stars_from_profile(prof(0.25, 0, 0), tbl) == 5
    assert scores.stars_from_profile(prof(0.26, 0, 0), tbl) == 4
    assert scores.stars_from_profile(prof(0.1, 0.06, 0), tbl) == 3
    assert scores.stars_from_profile(prof(0.1, 0.1, 0.06), tbl) == 1
    assert [scores.stars_from_dup(x, TH["mai"]["duplication_pct_stars"]) for x in (0, 3, 4, 8, 15, 30)] == [5, 5, 4, 3, 2, 1]


def test_mai_interpolated_ratings():
    tbl = TH["mai"]["star_table"]
    prof = lambda m, h, v: {"moderate": m, "high": h, "very_high": v}  # noqa: E731
    r = lambda p: round(scores.rating_from_profile(p, tbl), 4)  # noqa: E731
    assert r(prof(0, 0, 0)) == 5.5
    assert r(prof(0.25, 0, 0)) == 4.5          # top of the 5-star band
    assert r(prof(0.26, 0, 0)) == 4.3          # 4-star band: (0.26-0.25)/(0.30-0.25) = 0.2 used
    assert r(prof(0.1, 0.1, 0.06)) == 1.4895   # 1 star: (0.06-0.05)/(1-0.05)
    assert r(prof(1, 1, 1)) == 0.5
    # continuity at a band edge: just above the 5-star limit is just below 4.5
    assert 4.49 < scores.rating_from_profile(prof(0.2501, 0, 0), tbl) < 4.5
    d = TH["mai"]["duplication_pct_stars"]
    assert [round(scores.rating_from_dup(x, d), 4) for x in (0, 3, 4, 30, 100)] == [5.5, 4.5, 4.0, 1.375, 0.5]


def test_mai_all_small_is_100_and_partial():
    r = scores.mai([fn(), fn()], [], None, TH)
    assert r["score"] == 100.0 and r["partial"] is True and r["missing"] == ["duplication", "module_coupling"]


def test_mai_full_mean():
    mods = [{"ca": 1, "sloc": 10}]
    r = scores.mai([fn(sloc=40)], mods, 12.0, TH)  # unit size: 100% 'high' (31-60) -> 1 star, rating 0.5; dup 12% -> 2.3
    subs = r["sub_ratings"]
    assert subs["unit_size"]["stars"] == 1 and subs["unit_size"]["rating"] == 0.5
    assert subs["duplication"]["stars"] == 2 and subs["duplication"]["rating"] == 2.3
    assert subs["unit_complexity"]["rating"] == 5.5 and subs["unit_interfacing"]["rating"] == 5.5
    assert r["mean_rating"] == 3.86 and r["score"] == 67.2 and not r["partial"]
