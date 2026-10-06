#!/usr/bin/env -S uv run --no-project --quiet --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["tree-sitter==0.26.0", "tree-sitter-rust==0.24.2", "radon==6.0.1", "vulture==2.16", "pytest"]
# ///
"""Self test: determinism (run twice, byte-identical), CRLF/path independence, expected fixture values,
golden file comparison and the score-formula unit tests.

Usage: python bin/selftest.py [--update-golden]
"""
import hashlib
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

SKILL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL / "lib"))
sys.path.insert(0, str(SKILL / "bin"))

import install_tools  # noqa: E402
from cxmetrics import core, nodetools, pipeline, report  # noqa: E402

FIXTURE = SKILL / "tests" / "fixtures" / "sample"
GOLDEN = SKILL / "tests" / "fixtures" / "sample.metrics.json"
GOLDEN_NODUP = SKILL / "tests" / "fixtures" / "sample.nodup.metrics.json"
failures = []


def check(cond, msg):
    print(("ok   " if cond else "FAIL ") + msg)
    if not cond:
        failures.append(msg)


def produce(root, out, include_tests=True, with_dup=True, node=True):
    if node:
        os.environ.pop("CXM_NO_NODE", None)
    else:
        os.environ["CXM_NO_NODE"] = "1"  # same effect as a machine without node
    data = pipeline.run(root, None, include_tests, (), pipeline.load_thresholds(), with_dup=with_dup)
    out.mkdir(parents=True, exist_ok=True)
    (out / "metrics.json").write_bytes(pipeline.dumps(data).encode("utf-8"))
    (out / "report.md").write_bytes((report.render(data, pipeline.load_thresholds(), "fixture") + "\n").encode("utf-8"))
    return data


def sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def check_fixture(d1, node, mode):
    fn = {(f["file"], f["name"]): f for f in d1["functions"]}
    c = fn[("crates/core/src/lib.rs", "classify")]
    check((c["cognitive"], c["cyclomatic"], c["scope"]) == (15, 9, "production"), f"[{mode}] classify: cognitive 15, cyclomatic 9")
    check(fn[("crates/core/src/lib.rs", "classify_zero")]["scope"] == "test", f"[{mode}] inline #[cfg(test)] fn is classified test")
    check(fn[("py/test_pkg.py", "test_decide")]["scope"] == "test", f"[{mode}] tests by path are classified test")
    check(fn[("crates/core/src/lib.rs", "Repo::insert")]["nargs_adj"] == 2, f"[{mode}] self parameter not counted in nargs_adj")
    # Python: radon is the inventory + cyclomatic source, rust-code-analysis supplies cognitive
    u = fn[("py/pkg/y.py", "unused_py_helper")]
    check((u["cognitive"], u["cyclomatic"], u["source"]) == (7, 5, "radon+rca"), f"[{mode}] python fn: radon cyclomatic 5, rca cognitive 7")
    xc = d1["cross_check"]["python"]
    check(xc["matched"] == xc["primary_functions"] == xc["rca_functions"] and xc["cyclomatic_max_abs_diff"] == 0,
          f"[{mode}] radon and rust-code-analysis agree on every Python function")
    clo = {(x["file"], x["name"]): x for x in d1["closures"]}
    sc = clo.get(("crates/core/src/util.rs", "signs::<closure@15>"))
    check(fn[("crates/core/src/util.rs", "signs")]["cognitive"] == 0 and sc is not None and sc["cognitive"] == 7,
          f"[{mode}] closure is its own unit: signs 0, signs::<closure@15> 7 (not folded into the parent)")
    oe = d1["over_engineering"]
    names = lambda k: sorted(i["name"] for i in oe[k])  # noqa: E731
    check(names("single_impl") == ["Base", "Rect", "Storage"], f"[{mode}] single-impl abstractions: Storage (Rust), Base (Python), Rect (TS interface, regex-detected, no implementer); Shape has two impls")
    check(names("pass_through") == ["forward", "insert", "use_y"], f"[{mode}] pass-through fns: forward, insert, use_y")
    check(names("generic_heavy") == ["merge_all"], f"[{mode}] generic-heavy: merge_all")
    check("never_used_elsewhere" in names("unused_pub"), f"[{mode}] unreferenced pub fn detected")
    check("unused_py_helper" in names("unused_pub") and oe["dead_code"]["python"]["function"] >= 1, f"[{mode}] vulture flags unused_py_helper")
    sccs = d1["sccs"]["modules"]
    check(["js/a.js", "js/b.js"] in sccs and ["py/pkg/x.py", "py/pkg/y.py"] in sccs and
          ["crates/core/src/a.rs", "crates/core/src/b.rs"] in sccs and len(sccs) == 3,
          f"[{mode}] module cycles: rust a<->b, js a<->b, python x<->y")
    crate = {x["name"]: x for x in d1["crates"]}
    check(crate["core-lib"]["ca"] == 1 and crate["app"]["ce"] == 1 and crate["core-lib"]["distance"] == 0.6667,
          f"[{mode}] crate graph: app -> core-lib, D(core-lib) = 0.6667")
    dup = d1["duplication"]
    if node:
        check(dup.get("available") and dup["clones_count"] == 1 and dup["clones"][0]["a"]["file"] == "crates/core/src/a.rs",
              f"[{mode}] duplicate block a.rs/b.rs detected")
    else:
        check(not dup.get("available"), f"[{mode}] duplication reported as not measured")
    mods = {m["file"]: m for m in d1["modules"]}
    if node:
        check(mods["ts/app.ts"]["imports"] == ["ts/lib/foo.ts", "ts/lib/shape.ts"],
              f"[{mode}] TS path alias @lib/foo resolved through tsconfig paths")
        g = d1["js_graph"]
        check(g["type_only"] == 1 and g["reexport"] == 3 and g["aliased"] == 2 and g["tsconfig"] == "tsconfig.json",
              f"[{mode}] TS type-only import and re-exports are flagged in the module graph ({g['type_only']}, {g['reexport']}, {g['aliased']})")
        t = fn[("ts/app.ts", "classify")]
        check((t["cognitive"], t["cyclomatic"], t["source"]) == (8, 6, "eslint+sonarjs"),
              f"[{mode}] TS classify: sonarjs cognitive 8, cyclomatic 6 (includes the callback's ternary)")
        tc = clo.get(("ts/app.ts", "classify::<closure@16>"))
        check(tc is not None and tc["cognitive"] == 1 and tc["parent"] == "classify",
              f"[{mode}] TS callback is its own closure unit (cognitive 1), not folded into classify")
        check(sorted(i["name"] for i in oe["unused_pub"] if i["lang"] == "js") == ["UnusedAlias", "unusedRectHelper"] and
              oe["unused_files"] == ["ts/orphan.ts"], f"[{mode}] knip: unused export, unused type, unused file")
        check(d1["cross_check"]["js"]["matched"] == d1["cross_check"]["js"]["primary_functions"],
              f"[{mode}] every ESLint function matched a rust-code-analysis function")
    else:
        check(("ts/app.ts" in mods) and mods["ts/app.ts"]["imports"] == ["ts/lib/shape.ts"],
              f"[{mode}] regex fallback cannot resolve the tsconfig alias (known limitation)")
        check(len(d1["header"]["degraded"]) == 1, f"[{mode}] one degraded reason recorded")
    for k in ("CXI", "COI", "OEI", "MAI"):
        s = d1["scores"]["production"][k]
        check(s is not None and 0 <= s["score"] <= 100, f"[{mode}] {k} computed in 0..100 ({s and s['score']})")


def main():
    if install_tools.find_rca() is None:
        print("rust-code-analysis-cli missing; installing")
        if install_tools.main() != 0:
            return 2
    work = Path(tempfile.mkdtemp(prefix="cxm-selftest-", dir=core.tmp_base()))
    node_ok = nodetools.available()[0]
    if not node_ok:
        print("skip node mode: " + str(nodetools.available()[1]) + " (run bin/install_tools.py)")
    try:
        crlf = work / "other-name" / "sample"
        shutil.copytree(FIXTURE, crlf)
        for p in sorted(crlf.rglob("*")):
            if p.is_file() and p.suffix in (".rs", ".py", ".js", ".ts", ".toml", ".json"):
                p.write_bytes(p.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
        for mode, node, golden in (("node", True, GOLDEN), ("nonode", False, GOLDEN_NODUP)):
            if node and not node_ok:
                continue
            print(f"--- mode: {mode} ({'ESLint+SonarJS, dependency-cruiser, knip, jscpd' if node else 'regex fallback, CXM_NO_NODE=1'})")
            r1, r2, r3 = work / mode / "run1", work / mode / "run2", work / mode / "run3"
            d1 = produce(FIXTURE, r1, node=node)
            produce(FIXTURE, r2, node=node)
            for name in ("metrics.json", "report.md"):
                check((r1 / name).read_bytes() == (r2 / name).read_bytes(), f"[{mode}] {name} byte-identical across two runs")
            # CRLF + different directory name must not change metrics.json
            produce(crlf, r3, node=node)
            check((r1 / "metrics.json").read_bytes() == (r3 / "metrics.json").read_bytes(),
                  f"[{mode}] metrics.json identical for CRLF copy in a different directory")
            check_fixture(d1, node, mode)
            got = (r1 / "metrics.json").read_bytes()
            if "--update-golden" in sys.argv:
                golden.write_bytes(got)
                print(f"golden updated: {golden.name}")
            else:
                check(got == golden.read_bytes(), f"[{mode}] metrics.json equals committed golden tests/fixtures/{golden.name}")
            print(f"[{mode}] metrics.json sha256", sha(r1 / "metrics.json"))
            check(("degraded" in (r1 / "report.md").read_text(encoding="utf-8").lower()) == (not node),
                  f"[{mode}] report header {'marks the output degraded' if not node else 'is not marked degraded'}")
        # 5. unit tests for formulas
        import pytest
        rc = pytest.main(["-q", "-p", "no:cacheprovider", str(SKILL / "tests" / "test_scores.py"),
                          str(SKILL / "tests" / "test_funcs.py"), str(SKILL / "tests" / "test_parsers.py")])
        check(rc == 0, "pytest tests/test_scores.py tests/test_funcs.py tests/test_parsers.py")
    finally:
        shutil.rmtree(work, ignore_errors=True)
    print("SELFTEST " + ("FAILED: " + "; ".join(failures) if failures else "PASSED"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
