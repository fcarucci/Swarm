"""Unit tests for funcs.extract on hand-built rust-code-analysis trees: the code-line partition is exact."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

from cxmetrics import funcs  # noqa: E402


def sp(kind, name, start, end, ploc, cog=0, children=()):
    children = list(children)
    return {
        "kind": kind, "name": name, "start_line": start, "end_line": end, "spaces": children,
        "metrics": {
            "loc": {"sloc": end - start + 1, "ploc": ploc},
            # rca sums are subtree totals
            "cognitive": {"sum": cog + sum(c["metrics"]["cognitive"]["sum"] for c in children)},
            "cyclomatic": {"sum": 1 + sum(c["metrics"]["cyclomatic"]["sum"] for c in children)},
            "nargs": {"total_functions": 0}, "nexits": {"sum": 0},
            "halstead": {"volume": 0.0}, "mi": {"mi_visual_studio": 50.0},
        },
    }


def clo(start, end, ploc, cog=0, children=()):
    return sp("function", "<anonymous>", start, end, ploc, cog, children)


def tree():
    inner = sp("function", "inner", 5, 9, 5, 1, [clo(7, 7, 1, 1)])
    c2 = clo(12, 15, 4, 2, [clo(13, 13, 1, 3)])          # closure in a closure
    outer = sp("function", "outer", 1, 20, 18, 4, [inner, c2, clo(17, 17, 1), clo(17, 17, 1, 5)])  # two on line 17
    one_liner = sp("function", "one_liner", 22, 22, 1, 0, [clo(22, 22, 1), clo(22, 22, 1)])
    impl = sp("impl", "S", 24, 30, 7, 0, [sp("function", "m", 25, 29, 5, 0, [clo(26, 28, 3)])])
    return sp("unit", "f.rs", 1, 30, 30, 0, [outer, one_liner, impl])


def extract():
    _, fs, cs = funcs.extract(tree(), "f.rs", "rust")
    return {f["name"]: f for f in fs}, {c["name"]: c for c in cs}


def test_partition_is_exact_per_top_level_fn():
    fs, cs = extract()
    groups = {"outer": (18, ["outer", "inner"]), "one_liner": (1, ["one_liner"]), "S::m": (5, ["S::m"])}
    for top, (ploc, names) in groups.items():
        total = sum(fs[n]["own_ploc"] for n in names)
        total += sum(c["own_ploc"] for k, c in cs.items() if any(k.startswith(n + "::") for n in names))
        assert total == ploc, top
    assert all(u["own_ploc"] >= 0 for u in list(fs.values()) + list(cs.values()))


def test_partition_values():
    fs, cs = extract()
    assert fs["outer"]["own_ploc"] == 8   # 18 - inner 5 - closure 4 - line-17 closures 1 + 0
    assert fs["inner"]["own_ploc"] == 4 and cs["inner::<closure@7>"]["own_ploc"] == 1
    assert cs["outer::<closure@12>"]["own_ploc"] == 3 and cs["outer::<closure@12>::<closure@13>"]["own_ploc"] == 1
    assert cs["outer::<closure@17>"]["own_ploc"] == 1 and cs["outer::<closure@17#2>"]["own_ploc"] == 0
    assert fs["one_liner"]["own_ploc"] == 0 and cs["one_liner::<closure@22>"]["own_ploc"] == 1
    assert cs["one_liner::<closure@22#2>"]["own_ploc"] == 0
    assert fs["S::m"]["own_ploc"] == 2 and cs["S::m::<closure@26>"]["own_ploc"] == 3


def test_unit_lines_do_not_double_count_nested_fns():
    fs, _ = extract()
    # SIG unit of outer: own 8 + its closures (3 + 1 + 1 + 0) = 13; inner (5) is its own unit
    assert fs["outer"]["unit_ploc"] == 13 and fs["inner"]["unit_ploc"] == 5
    assert fs["outer"]["unit_ploc"] + fs["inner"]["unit_ploc"] == 18


def test_cognitive_is_own_and_closure_names_unique():
    fs, cs = extract()
    assert fs["outer"]["cognitive"] == 4 and fs["inner"]["cognitive"] == 1
    assert cs["outer::<closure@12>"]["cognitive"] == 2 and cs["outer::<closure@12>::<closure@13>"]["cognitive"] == 3
    assert cs["outer::<closure@17#2>"]["cognitive"] == 5
    _, fl, cl = funcs.extract(tree(), "f.rs", "rust")
    assert len({c["name"] for c in cl}) == len(cl) == 8


def test_budget_cap_never_negative():
    # a child reporting more lines than its parent (should not happen) is capped; nothing goes below 0
    t = sp("unit", "g.rs", 1, 3, 3, 0, [sp("function", "p", 1, 2, 2, 0, [clo(1, 3, 3)])])
    _, fs, cs = funcs.extract(t, "g.rs", "rust")
    assert fs[0]["own_ploc"] == 0 and cs[0]["own_ploc"] == 2
