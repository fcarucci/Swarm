"""Orchestration: discover -> stage -> rca + source facts -> coupling / over-engineering / duplication -> scores."""
import importlib.metadata as md
import json
import posixpath
import shutil
import tempfile
from pathlib import Path

from . import SCHEMA_VERSION, coupling, dup, funcs, jssrc, langtools, overeng, pysrc, scores
from .core import (SKILL_DIR, discover, find_rca, percentile, read_text, rca_version, rnd, run_rca,
                   stage, tmp_base)


def load_thresholds(path=None):
    p = Path(path) if path else SKILL_DIR / "thresholds.json"
    return json.loads(p.read_text(encoding="utf-8"))


def _in_ranges(line, ranges):
    return any(a <= line <= b for a, b in ranges)


def run(root, langs=None, include_tests=False, excludes=(), thresholds=None, with_dup=True):
    root = Path(root).resolve()
    th = thresholds or load_thresholds()
    langs = set(langs) if langs else None
    files = discover(root, langs, excludes)
    if not files:
        raise SystemExit("no Rust/Python/JS/TS source files found under " + str(root))
    rca_bin = find_rca()
    if not rca_bin:
        raise SystemExit("rust-code-analysis-cli not found. Run: python3 bin/install_tools.py")
    work = Path(tempfile.mkdtemp(prefix="cxm-", dir=tmp_base()))
    try:
        return _run(root, files, include_tests, th, work, rca_bin, with_dup)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _run(root, files, include_tests, th, work, rca_bin, with_dup):
    mapping = stage(root, files, work / "stage")
    rca = run_rca(work, rca_bin)
    ext = langtools.collect(root, work, files, mapping)  # radon/vulture, eslint/depcruise/knip (or why not)
    is_test = {f["rel"]: f["test"] for f in files}
    lang_of = {f["rel"]: f["lang"] for f in files}

    rust, py, js, idents, skipped = {}, {}, {}, {}, []
    for f in files:
        rel, lang = f["rel"], f["lang"]
        text = read_text(root / rel)
        if lang == "rust":
            from . import rustsrc
            facts = rustsrc.analyze(rel, text)
            rust[rel] = facts
            idents[rel] = facts["idents"]
        elif lang == "python":
            facts = pysrc.analyze(rel, text)
            py[rel] = facts
            idents[rel] = jssrc._count(text)
        else:
            facts = jssrc.analyze(rel, (root / rel).read_bytes().decode("utf-8", "replace").replace("\r\n", "\n"))
            js[rel] = facts
            idents[rel] = facts["idents"]
        if facts.get("parse_errors"):
            skipped.append(rel + " (syntax errors; structural facts may be partial)")

    # ---- functions
    all_funcs, all_clos, file_sloc, prod_sloc_by_file = [], [], {}, {}
    for f in files:
        rel = f["rel"]
        j = rca.get(f["stage"])
        if j is None:
            if not read_text(root / rel).strip():
                continue
            skipped.append(rel + " (not analysed by rust-code-analysis)")
            continue
        fs, fl, cl = funcs.extract(j, rel, f["lang"])
        file_sloc[rel] = fs
        tr = rust[rel]["test_ranges"] if f["lang"] == "rust" else []
        has_self = {}
        if f["lang"] == "rust":
            has_self = {x["line"]: x["has_self"] for x in rust[rel]["fns"]}
        elif f["lang"] == "python":
            has_self = {x["line"]: x["has_self"] for x in py[rel]["fns"]}
        test_sloc = 0
        for fn in fl:
            fn["scope"] = "test" if (f["test"] or _in_ranges(fn["line"], tr)) else "production"
            fn["nargs_adj"] = max(0, fn["nargs"] - (1 if has_self.get(fn["line"]) else 0))
            if fn["scope"] == "test" and not f["test"]:
                test_sloc += fn["sloc"]
        for c in cl:
            c["scope"] = "test" if (f["test"] or _in_ranges(c["line"], tr)) else "production"
        prod_sloc_by_file[rel] = 0 if f["test"] else max(0, fs - test_sloc)
        all_funcs += fl
        all_clos += cl
    all_funcs, all_clos, cross_check = langtools.merge_functions(all_funcs, all_clos, ext, is_test, py)
    all_funcs.sort(key=lambda x: (x["file"], x["line"], x["name"]))
    prod_funcs = [x for x in all_funcs if x["scope"] == "production"]
    test_funcs = [x for x in all_funcs if x["scope"] == "test"]
    all_clos.sort(key=lambda x: (x["file"], x["line"], x["name"]))
    prod_clos = [x for x in all_clos if x["scope"] == "production"]
    test_clos = [x for x in all_clos if x["scope"] == "test"]

    # ---- coupling (production modules only)
    crates = coupling.load_crates(root, rust) if rust else {}
    file_crate = {rel: coupling.crate_of(rel, crates) for rel in rust} if crates else {}
    prod_rust = {r: x for r, x in rust.items() if not is_test[r]}
    redges, rstats = ({}, {})
    if rust:
        redges, rstats = coupling.rust_module_edges(rust, crates, file_crate) if crates else ({}, {})
        if not crates:  # loose .rs files: treat as no module graph
            rstats = {"uses_resolved": 0, "uses_external": 0, "uses_reexport_skipped": 0}
    pedges = coupling.python_edges({r: x for r, x in py.items() if not is_test[r]}) if py else {}
    jedges = coupling.js_edges({r: x for r, x in js.items() if not is_test[r]}) if js else {}
    if langtools.js_graph_ok(ext):
        jedges = {a: set(b) for a, b in ext["js"]["edges"].items()}
    prod_files = sorted(r for r in is_test if not is_test[r] and r in file_sloc)
    nodes = prod_files
    edges = {}
    for e in (redges, pedges, jedges):
        for a, bs in e.items():
            if a in nodes and not is_test.get(a):
                edges.setdefault(a, set()).update(b for b in bs if not is_test.get(b, True))
    ca, ce = coupling.degree_metrics(nodes, edges)
    sccs = coupling.tarjan_sccs(nodes, edges)
    scc_id = {}
    for i, s in enumerate(sccs):
        for m in s:
            scc_id[m] = i
    modules = []
    for n in nodes:
        sl = prod_sloc_by_file.get(n, 0)
        modules.append({"file": n, "lang": lang_of[n], "crate": (crates[file_crate[n]]["name"] if file_crate.get(n) is not None and n in file_crate else None),
                        "sloc": sl, "ca": ca[n], "ce": ce[n],
                        "instability": rnd(ce[n] / (ca[n] + ce[n])) if (ca[n] + ce[n]) else None,
                        "henry_kafura": sl * (ca[n] * ce[n]) ** 2, "in_scc": n in scc_id,
                        "scc": scc_id.get(n), "imports": sorted(edges.get(n, ()))})
    crate_items, crate_sloc = {}, {}
    for rel, fa in prod_rust.items():
        d = file_crate.get(rel)
        if d is None:
            continue
        ci = crate_items.setdefault(d, {"trait": 0, "struct": 0, "enum": 0})
        for it in fa["items"]:
            if not it["test"] and it["kind"] in ci:
                ci[it["kind"]] += 1
        crate_sloc[d] = crate_sloc.get(d, 0) + prod_sloc_by_file.get(rel, 0)
    crate_rows, crate_sccs = coupling.rust_crate_graph(crates, crate_items, crate_sloc) if crates else ([], [])
    cp = {"modules": modules, "crates": crate_rows, "module_sccs": sccs, "crate_sccs": crate_sccs,
          "rust_use_resolution": rstats}

    # ---- over-engineering
    lib_files = set()
    for rel in prod_rust:
        d = file_crate.get(rel)
        if d is None:
            lib_files.add(rel)
        elif crates[d]["has_lib"] and not rel.endswith("/main.rs") and "/src/bin/" not in "/" + rel:
            lib_files.add(rel)
    prod_sloc = sum(prod_sloc_by_file.values())
    oe = overeng.analyze({"rust": rust, "py": py, "js": js, "is_test": is_test, "idents": idents,
                          "lib_files": lib_files, "prod_sloc": prod_sloc, "ext": ext}, th)

    # ---- duplication
    if with_dup:
        dres = dup.run(work, "prod")
        duplication = dup.summarize(dres, mapping)
    else:
        duplication = {"available": False, "reason": dup.NOT_RUN}

    langtools.note_duplication(ext, duplication)

    # ---- scores
    dpct = duplication["percentage"] if duplication.get("available") else None
    S = {"production": {
        "CXI": scores.cxi(prod_funcs, th, prod_clos), "COI": scores.coi(cp, th), "OEI": scores.oei(oe["ratios"], th),
        "MAI": scores.mai(prod_funcs, modules, dpct, th)}}
    by_lang = {}
    for lg in sorted({x["lang"] for x in prod_funcs}):
        sub = [x for x in prod_funcs if x["lang"] == lg]
        c = scores.cxi(sub, th, [x for x in prod_clos if x["lang"] == lg])
        by_lang[lg] = {"CXI": c["score"] if c else None, "functions": len(sub)}
    S["production"]["by_language"] = by_lang
    if include_tests:
        S["test"] = {"CXI": scores.cxi(test_funcs, th, test_clos), "MAI": scores.mai(test_funcs, [], None, th),
                     "note": "COI and OEI are not computed for test code; duplication and module coupling are not part of the test MAI."}

    def counts(fl):
        return {"functions": len(fl), "sloc": sum(x["sloc"] for x in fl), "loc": sum(scores.code_lines(x, own=False) for x in fl)}

    repo = {"files": {"production": sum(1 for f in files if not f["test"]), "test": sum(1 for f in files if f["test"])},
            "languages": sorted({f["lang"] for f in files}),
            "production": counts(prod_funcs), "test": counts(test_funcs),
            "closures": {"production": len(prod_clos), "test": len(test_clos)},
            "production_sloc_files": prod_sloc,
            "modules": len(modules), "crates": len(crate_rows)}
    tools = {"rust-code-analysis": rca_version(rca_bin), "tree-sitter": md.version("tree-sitter"),
             "tree-sitter-rust": md.version("tree-sitter-rust"),
             "jscpd": duplication.get("tool", "unavailable"), "js-import-graph": "builtin-regex"}
    tools.update(ext["tools"])
    if langtools.js_graph_ok(ext):
        tools["js-import-graph"] = "dependency-cruiser " + ext["tools"]["dependency-cruiser"]
    header = {"schema_version": SCHEMA_VERSION, "thresholds_version": th["version"], "tools": tools,
              "include_tests": include_tests, "languages": repo["languages"],
              "skipped_or_partial_files": sorted(skipped + ext["errors"]),
              "degraded": ext["degraded"], "tool_table": langtools.tool_table(ext, repo["languages"], duplication)}
    out = {"header": header, "repo": repo, "scores": S,
           "functions": prod_funcs + (test_funcs if include_tests else []),
           "closures": prod_clos + (test_clos if include_tests else []),
           "modules": modules, "crates": crate_rows,
           "sccs": {"modules": sccs, "crates": crate_sccs}, "rust_use_resolution": rstats,
           "over_engineering": oe, "duplication": duplication, "cross_check": cross_check,
           "python_files": langtools.python_files(ext), "js_graph": langtools.js_graph(ext)}
    return _round_tree(out)


def _round_tree(o):
    if isinstance(o, float):
        return rnd(o, 4)
    if isinstance(o, dict):
        return {k: _round_tree(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_round_tree(v) for v in o]
    return o


def dumps(data):
    return json.dumps(data, sort_keys=True, indent=2, ensure_ascii=True) + "\n"
