"""Unit tests for the parsers of each external tool's output (no tool needs to be installed to run these)
and for the manifest/pin consistency checks."""
import json
import re
import sys
from pathlib import Path

SKILL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL / "lib"))

from cxmetrics import langtools, nodetools, pytools  # noqa: E402

MANIFEST = json.loads((SKILL / "tool-manifest.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------- radon
RADON_CC = {
    "stage/prod/f00000.py": [
        {"type": "class", "name": "B", "lineno": 6, "endline": 9, "complexity": 3, "methods": [
            {"type": "method", "classname": "B", "name": "run", "lineno": 7, "endline": 9, "complexity": 2, "closures": []}]},
        # radon repeats every method at top level
        {"type": "method", "classname": "B", "name": "run", "lineno": 7, "endline": 9, "complexity": 2, "closures": []},
        {"type": "function", "name": "outer", "lineno": 1, "endline": 5, "complexity": 2, "closures": [
            {"type": "function", "name": "inner", "lineno": 2, "endline": 4, "complexity": 3, "closures": []}]},
    ],
    "stage\\prod\\f00001.py": {"error": "invalid syntax (line 3)"},
}


def test_radon_cc_flattens_dedupes_and_reports_errors():
    out = pytools.parse_radon_cc(RADON_CC)
    assert out["f00001.py"] == {"error": "invalid syntax (line 3)"}
    blocks = out["f00000.py"]["blocks"]
    assert [(b["name"], b["line"], b["cyclomatic"]) for b in blocks] == [
        ("outer", 1, 2), ("inner", 2, 3), ("B.run", 7, 2)]  # one B.run, class entry dropped, closure kept


def test_radon_file_metrics():
    mi = {"a/f0.py": {"mi": 65.087, "rank": "A"}}
    hal = {"a/f0.py": {"total": {"volume": 93.765, "difficulty": 2.909, "effort": 272.77}, "functions": {}}}
    raw = {"a/f0.py": {"loc": 118, "lloc": 95, "sloc": 93, "comments": 14}}
    assert pytools.parse_radon_file_metrics(mi, hal, raw) == {
        "f0.py": {"mi": 65.09, "volume": 93.77, "difficulty": 2.91, "effort": 272.8, "sloc": 93, "comments": 14}}


def test_vulture_parser():
    text = ("stage/prod/f00002.py:21: unused function 'helper' (60% confidence)\n"
            "stage/prod/f00002.py:3: unused import 'os' (90% confidence)\n"
            "stage/prod/f00001.py:7: unused method 'run' (60% confidence)\n"
            "stage/prod/f00001.py:9: unreachable code after 'return' (100% confidence, 2 lines)\n"
            "garbage line\n")
    got = pytools.parse_vulture(text)
    assert [(g["file"].rsplit("/", 1)[1], g["line"], g["kind"], g["name"], g["confidence"]) for g in got] == [
        ("f00001.py", 7, "method", "run", 60),
        ("f00001.py", 9, "other", "unreachable code after 'return'", 100),
        ("f00002.py", 3, "import", "os", 90),
        ("f00002.py", 21, "function", "helper", 60)]


# ---------------------------------------------------------------- ESLint
def _fn(name, sl, sc, el, ec, params=1, cls="", anon=False):
    return {"ruleId": "cx/fn", "message": json.dumps({"name": name, "cls": cls, "params": params, "sl": sl, "sc": sc,
                                                       "el": el, "ec": ec, "anon": anon})}


def _cyc(line, col, n):
    return {"ruleId": "complexity", "line": line, "column": col, "message": f"Function has a complexity of {n}. Maximum allowed is 0."}


def _cog(line, col, n):
    return {"ruleId": "sonarjs/cognitive-complexity", "line": line, "column": col,
            "message": f"Refactor this function to reduce its Cognitive Complexity from {n} to the 0 allowed."}


ESLINT = [{"filePath": "/w/stage/prod/f00000.ts", "messages": [
    {"ruleId": "cx/exports", "message": json.dumps({"exports": ["outer", "K", "outer"]}), "line": 1, "column": 1},
    _fn("outer", 1, 8, 7, 2), _cyc(1, 8, 1),
    _fn("helper", 2, 18, 5, 4), _cog(2, 30, 3), _cyc(2, 30, 3),
    _fn("<anonymous>", 6, 16, 6, 65, anon=True), _cog(6, 20, 1), _cyc(6, 20, 2),
    _fn("run", 9, 3, 9, 59, cls="K"), _cog(9, 10, 1), _cyc(9, 3, 2)]},
    {"filePath": "C:\\w\\stage\\prod\\f00001.js", "messages": [{"ruleId": None, "fatal": True, "message": "Parsing error: x"}]}]
MAPPING = {"f00000.ts": "src/a.ts", "f00001.js": "src/b.js"}


def test_eslint_attributes_metrics_to_innermost_function():
    p = nodetools.parse_eslint(ESLINT, MAPPING)
    assert p["src/b.js"]["error"] == "Parsing error: x"
    a = p["src/a.ts"]
    assert a["exports"] == ["K", "outer"]
    by = {f["name"]: (f["cog"], f["cyc"]) for f in a["functions"]}
    assert by == {"outer": (0, 1), "helper": (3, 3), "<anonymous>": (1, 2), "run": (1, 2)}


def test_build_units_closures_are_own_units():
    units = nodetools.build_units(nodetools.parse_eslint(ESLINT, MAPPING)["src/a.ts"]["functions"])
    got = {u["name"]: (u["kind"], u["cognitive"], u["cyclomatic"], u["nargs"], u["parent"]) for u in units}
    # the callback keeps its own cognitive (1) as a closure of `outer`; its decisions count in outer's cyclomatic (1 + (2-1))
    assert got == {"outer": ("fn", 0, 2, 1, None), "helper": ("fn", 3, 3, 1, None),
                   "outer::<closure@6>": ("closure", 1, 2, 1, "outer"), "K.run": ("fn", 1, 2, 1, None)}


def test_build_units_own_ploc_excludes_nested_units():
    src = ["function a() {", "  const b = () => {", "    return 1;", "  };", "  return b();", "}"]
    fns = [{"name": "a", "cls": "", "params": 0, "sl": 1, "sc": 1, "el": 6, "ec": 2, "anon": False, "cog": 0, "cyc": 1},
           {"name": "b", "cls": "", "params": 0, "sl": 2, "sc": 13, "el": 4, "ec": 4, "anon": False, "cog": 0, "cyc": 1}]
    u = {x["name"]: x for x in nodetools.build_units(fns, src)}
    assert (u["a"]["ploc"], u["b"]["ploc"], u["a"]["own_ploc"]) == (6, 3, 3)
    assert u["a"]["unit_ploc"] == 3 and u["b"]["unit_ploc"] == 3  # b is a named unit: not part of a's unit lines


def test_sloc_fallback_skips_blank_and_comment_lines():
    src = ["function f() {", "  // c", "", "  return 1;", "}"]
    assert nodetools._sloc(src, 1, 5) == 3


# ---------------------------------------------------------------- dependency-cruiser
DEPCRUISE = {"modules": [
    {"source": "ts/app.ts", "dependencies": [
        {"resolved": "ts/lib/rect.ts", "dependencyTypes": ["local", "import"]},
        {"resolved": "ts/lib/foo.ts", "dependencyTypes": ["aliased", "aliased-tsconfig-paths", "local", "import"]},
        {"resolved": "node_modules/x/index.js", "dependencyTypes": ["npm", "import"]},
        {"resolved": "ts/missing.ts", "couldNotResolve": True, "dependencyTypes": ["unknown"]}]},
    {"source": "ts/index.ts", "dependencies": [
        {"resolved": "ts/app.ts", "dependencyTypes": ["local", "export"]},
        {"resolved": "ts/lib/rect.ts", "dependencyTypes": ["local", "type-only", "import"]}]},
    {"source": "js/a.js", "dependencies": [{"resolved": "js/b.js", "dependencyTypes": ["local", "import"], "circular": True}]},
    {"source": "js/b.js", "dependencies": [{"resolved": "js/a.js", "dependencyTypes": ["local", "import"], "circular": True}]},
]}


def test_depcruise_edges_and_flags():
    known = {"ts/app.ts", "ts/index.ts", "ts/lib/rect.ts", "ts/lib/foo.ts", "js/a.js", "js/b.js"}
    edges, st = nodetools.parse_depcruise(DEPCRUISE, known)
    assert edges == {"js/a.js": ["js/b.js"], "js/b.js": ["js/a.js"], "ts/app.ts": ["ts/lib/foo.ts", "ts/lib/rect.ts"],
                     "ts/index.ts": ["ts/app.ts", "ts/lib/rect.ts"]}
    assert st == {"edges": 6, "type_only": 1, "reexport": 1, "aliased": 1, "dynamic": 0, "unresolved": 1, "circular": 2}


def test_pick_tsconfig():
    import tempfile
    from cxmetrics import core  # never /tmp: it can be RAM
    with tempfile.TemporaryDirectory(dir=core.tmp_base()) as td:
        _pick_tsconfig(Path(td))


def _pick_tsconfig(tmp_path):
    (tmp_path / "web").mkdir()
    (tmp_path / "web" / "tsconfig.json").write_text("{}")
    assert nodetools.pick_tsconfig(tmp_path, ["web/src/a.ts"]) == "web/tsconfig.json"
    (tmp_path / "tsconfig.json").write_text("{}")
    assert nodetools.pick_tsconfig(tmp_path, ["web/src/a.ts"]) == "tsconfig.json"
    assert nodetools.pick_tsconfig(tmp_path / "web" / "src", []) is None


# ---------------------------------------------------------------- knip
def test_knip_parser():
    obj = {"files": ["ts\\orphan.ts"], "issues": [
        {"file": "ts/lib/rect.ts", "exports": [{"name": "b", "line": 1}, {"name": "a", "line": 2}, {"name": "a", "line": 3}],
         "types": [{"name": "T", "line": 9}], "dependencies": []},
        {"file": "ts/x.ts", "exports": [], "types": [], "dependencies": [{"name": "left-pad"}]}]}
    assert nodetools.parse_knip(obj) == {"files": ["ts/orphan.ts"], "exports": {"ts/lib/rect.ts": ["a", "b"]},
                                         "types": {"ts/lib/rect.ts": ["T"]}}


# ---------------------------------------------------------------- matching radon/eslint <-> rca
def test_match_by_line_then_by_name():
    prim = [{"name": "A.run", "line": 7}, {"name": "dec", "line": 20}, {"name": "gone", "line": 40}]
    rca = [{"name": "run", "line": 7}, {"name": "dec", "line": 18}, {"name": "extra", "line": 50}]
    pairs, left = langtools._match(prim, rca)
    assert [p and p["name"] for p in pairs] == ["run", "dec", None]  # decorator offset matched by name within 5 lines
    assert [r["name"] for r in left] == ["extra"]


# ---------------------------------------------------------------- pins
def test_node_pins_match_manifest_and_package_json():
    pj = json.loads((SKILL / MANIFEST["node-install"]["dir"] / "package.json").read_text(encoding="utf-8"))
    assert pj["dependencies"] == MANIFEST["node-packages"]
    assert all(re.fullmatch(r"\d+\.\d+\.\d+", v) for v in MANIFEST["node-packages"].values())
    lock = json.loads((SKILL / MANIFEST["node-install"]["dir"] / "package-lock.json").read_text(encoding="utf-8"))
    for name, ver in MANIFEST["node-packages"].items():
        assert lock["packages"]["node_modules/" + name]["version"] == ver


def test_python_pins_match_launchers():
    pins = [f"{k}=={v}" for k, v in sorted(MANIFEST["python-packages"].items())]
    for rel in ("bin/metrics", "bin/metrics.cmd", "bin/selftest", "bin/selftest.cmd"):
        text = (SKILL / rel).read_text(encoding="utf-8")
        for pin in pins:
            assert f"--with {pin}" in text, (rel, pin)
    for rel in ("bin/metrics.py", "bin/selftest.py", "bin/bench.py"):
        m = re.search(r"# dependencies = \[(.*)\]", (SKILL / rel).read_text(encoding="utf-8"))
        have = {x.strip().strip('"') for x in m.group(1).split(",")}
        assert set(pins) <= have, rel


def test_tsconfig_solution_style_and_jsonc():
    import tempfile
    from cxmetrics import core
    with tempfile.TemporaryDirectory(dir=core.tmp_base()) as td:
        root = Path(td)
        (root / "tsconfig.json").write_text('{"files": [], "references": [{"path": "./tsconfig.node.json"}, {"path": "./tsconfig.app.json"}]}')
        (root / "tsconfig.node.json").write_text('{"compilerOptions": {"module": "esnext"}}')
        (root / "tsconfig.app.json").write_text(
            '{\n  // line comment\n  "compilerOptions": {\n    /* block */ "baseUrl": ".", "url": "http://x//y",\n'
            '    "paths": {"@/*": ["./src/*"],},\n  },\n}\n')
        assert nodetools.pick_tsconfig(root, ["src/a.ts"]) == "tsconfig.app.json"
        assert nodetools.load_jsonc(root / "tsconfig.app.json")["compilerOptions"]["url"] == "http://x//y"


def test_eslint_unrecognised_message_fails_loudly():
    import pytest
    bad = [{"filePath": "/w/stage/prod/f00000.ts", "messages": [
        _fn("f", 1, 1, 2, 1), {"ruleId": "sonarjs/cognitive-complexity", "line": 1, "column": 1, "message": "format changed"}]}]
    with pytest.raises(ValueError):
        nodetools.parse_eslint(bad, MAPPING)


def test_pytools_run_never_raises(monkeypatch):
    import subprocess
    def boom(*a, **k):
        raise subprocess.TimeoutExpired(["python", "-m", "radon"], 1)
    monkeypatch.setattr(pytools, "_run", boom)
    import tempfile
    from cxmetrics import core
    with tempfile.TemporaryDirectory(dir=core.tmp_base()) as td:
        (Path(td) / "stage" / "prod").mkdir(parents=True)
        (Path(td) / "stage" / "prod" / "f0.py").write_text("x = 1\n")
        r = pytools.run(td, {"f0.py": "a.py"})
    assert r["available"] is False and "timed out" in r["reason"]
