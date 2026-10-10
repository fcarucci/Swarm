"""Per-function Python ranking (py/analyze.py): joins rust-code-analysis cognitive with radon cyclomatic.
The join, selection and totals are pure functions over the tools' JSON, so they run without any tool
installed; the end-to-end test runs only where rust-code-analysis-cli and radon are available."""
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

SKILL = Path(__file__).resolve().parent.parent
FIX = Path(__file__).resolve().parent / "fixtures" / "py"
sys.path.insert(0, str(SKILL / "lib"))

from cxmetrics import core, pytools  # noqa: E402

spec = importlib.util.spec_from_file_location("cx_py_analyze", SKILL / "py" / "analyze.py")
analyze = importlib.util.module_from_spec(spec)
spec.loader.exec_module(analyze)


def fixture_rows():
    rca = {"f00000.py": json.loads((FIX / "cognitive.rca.json").read_text(encoding="utf-8"))}
    radon = pytools.parse_radon_cc(json.loads((FIX / "cognitive.radon.json").read_text(encoding="utf-8")))
    src = {"f00000.py": (FIX / "cognitive.py").read_text(encoding="utf-8")}
    return analyze.build_rows(rca, {"f00000.py": "cognitive.py"}, radon, src)


def key(r):
    return (r["cognitive"], r["cyclomatic"], r["file"], r["line"], r["function"])


def test_rows_have_known_cognitive_and_radon_cyclomatic():
    rows = fixture_rows()
    assert [key(r) for r in rows] == [
        (8, 5, "cognitive.py", 7, "nested"),
        (1, 2, "cognitive.py", 1, "flat"),
        (0, 1, "cognitive.py", 19, "Box.__init__"),
        (0, 1, "cognitive.py", 23, "Box.get"),
    ]


def test_totals_are_before_after_numbers():
    t = analyze.totals(fixture_rows())
    assert t == {"functions": 4, "cognitive": 9, "cyclomatic": 9}


def test_default_selection_flags_only_over_threshold():
    rows = fixture_rows()
    assert analyze.select(rows, cog_threshold=15, cyc_threshold=10, show_all=False) == []
    assert len(analyze.select(rows, cog_threshold=7, cyc_threshold=10, show_all=False)) == 1
    # cyclomatic alone can flag a function
    assert len(analyze.select(rows, cog_threshold=15, cyc_threshold=4, show_all=False)) == 1


def test_show_all_and_threshold_zero_list_everything():
    rows = fixture_rows()
    assert len(analyze.select(rows, 15, 10, show_all=True)) == 4
    # --threshold 0 is the before/after form: every function, like --all
    assert analyze.select(rows, 0, 10, show_all=False) == analyze.select(rows, 15, 10, show_all=True)


def test_missing_radon_block_gives_no_cyclomatic():
    rca = {"f00000.py": json.loads((FIX / "cognitive.rca.json").read_text(encoding="utf-8"))}
    rows = analyze.build_rows(rca, {"f00000.py": "cognitive.py"},
                              {"f00000.py": {"error": "invalid syntax"}},
                              {"f00000.py": (FIX / "cognitive.py").read_text(encoding="utf-8")})
    assert all(r["cyclomatic"] is None for r in rows)
    assert analyze.format_row(rows[0]).split("\t")[1] == "-"


def test_tsv_line_matches_the_js_driver_layout():
    r = fixture_rows()[0]
    assert analyze.format_row(r) == "8\t5\tcognitive.py\t7\tnested"


def test_end_to_end_when_tools_are_installed(tmp_path):
    rca = core.find_rca()
    if not rca or importlib.util.find_spec("radon") is None:
        pytest.skip("rust-code-analysis-cli and radon are needed for the end-to-end run")
    out = subprocess.run([sys.executable, "-B", str(SKILL / "py" / "analyze.py"), "--all",
                          str(FIX / "cognitive.py")], capture_output=True, text=True, check=True)
    lines = out.stdout.splitlines()
    assert lines[0].split("\t")[:2] == ["8", "5"]
    assert len(lines) == 4
    assert "# python functions: 4" in out.stderr
