"""Design-level signals for Python (py/design.py): class cohesion (LCOM4), long parameter lists,
data clumps and module size/fan-out. Plain ast, no tools needed; every expectation is a hand-counted answer."""
import importlib.util
import sys
from pathlib import Path

SKILL = Path(__file__).resolve().parent.parent
FIX = Path(__file__).resolve().parent / "fixtures" / "py"

spec = importlib.util.spec_from_file_location("cx_py_design", SKILL / "py" / "design.py")
design = importlib.util.module_from_spec(spec)
spec.loader.exec_module(design)


def load(name):
    return (FIX / name).read_text(encoding="utf-8")


def cls(result, name):
    return next(c for c in result["classes"] if c["name"] == name)


def test_lcom4_known_answers():
    res = design.analyze_source("cohesion.py", load("cohesion.py"))
    split = cls(res, "Split")
    # read_a/write_a share self.a; read_b uses only self.b; pure touches no attribute -> 3 components
    assert split["lcom4"] == 3
    assert split["methods"] == 5 and split["public_methods"] == 4
    assert split["attributes"] == 2
    joined = cls(res, "Joined")
    # total() calls self.get_b() and reads self.a, which links both groups -> one component
    assert joined["lcom4"] == 1
    assert joined["methods"] == 5 and joined["public_methods"] == 4


def test_class_loc_counts_non_blank_lines_of_the_span():
    res = design.analyze_source("cohesion.py", load("cohesion.py"))
    # Split spans lines 1-16; 4 of them are blank -> 12
    assert cls(res, "Split")["loc"] == 12
    assert cls(res, "Split")["line"] == 1


def test_lcom4_is_unavailable_without_methods_to_compare():
    res = design.analyze_source("x.py", "class Only:\n    def __init__(self):\n        self.a = 1\n")
    assert cls(res, "Only")["lcom4"] == "-"  # __init__ is excluded from the graph, nothing left


def test_long_parameter_lists_exclude_self_and_cls():
    res = design.analyze_source("params.py", load("params.py"))
    flagged = {f["name"]: f["nparams"] for f in res["functions"] if f["nparams"] > design.LONG_PARAMS}
    assert flagged == {"six": 6, "Api.call": 6}  # five() and Five.m have exactly 5 -> not flagged


def test_data_clump_is_the_shared_three_or_more_names():
    res = design.analyze_source("clumps.py", load("clumps.py"))
    clumps = design.clumps([dict(f, file="clumps.py") for f in res["functions"]])
    assert len(clumps) == 1
    assert clumps[0]["params"] == ("host", "port", "user")
    assert [f["name"] for f in clumps[0]["functions"]] == ["connect", "probe", "reconnect"]


def test_module_sloc_and_fanout():
    res = design.analyze_source("imports.py", load("imports.py"))
    # docstring + 6 import lines + X = 1 -> 8 (comment and blank lines excluded)
    assert res["module"]["sloc"] == 8
    # os, os.path, json, pathlib, ".", ".util" (relative imports keep their dots)
    assert res["module"]["fanout"] == 6


def test_report_is_deterministic_and_tab_separated():
    sources = [(n, load(n)) for n in ("clumps.py", "cohesion.py", "imports.py", "params.py")]
    a = design.render(sources)
    b = design.render(list(reversed(sources)))
    assert a == b  # input order does not change output
    kinds = {line.split("\t")[0] for line in a.splitlines() if line and not line.startswith("#")}
    assert kinds == {"class", "longparams", "clump", "module"}
    for line in a.splitlines():
        if line and not line.startswith("#"):
            assert "\t" in line


def test_cli_runs_on_a_directory(tmp_path):
    import subprocess
    out = subprocess.run([sys.executable, "-B", str(SKILL / "py" / "design.py"), str(FIX)],
                         capture_output=True, text=True, check=True).stdout
    assert "\tSplit\t" in out and "clump\t" in out
