"""Facade over the per-language tool adapters (pytools, nodetools) used by pipeline.py.

collect()          run radon/vulture and ESLint/dependency-cruiser/knip, or record why they are unavailable
merge_functions()  replace rust-code-analysis records of Python/JS/TS files by the tool-based inventory
tool_table()       per-language, per-metric tool names for the report header
"""
import os

from . import nodetools, pytools
from .core import rnd

NO_NODE_ENV = "CXM_NO_NODE"  # force the node-less fallback (tests, machines without node)
NO_PYTOOLS_ENV = "CXM_NO_PYTOOLS"  # validation aid: skip radon/vulture to measure the old Python method


def _match(primary, rca):
    """Pair primary records with rca records of the same file: exact start line first, then same name within 5
    lines. Deterministic (sorted, greedy). Returns list parallel to primary with an rca record or None."""
    used, res = set(), [None] * len(primary)
    order = sorted(range(len(primary)), key=lambda i: (primary[i]["line"], primary[i]["name"]))
    by_line = {}
    for j, r in enumerate(rca):
        by_line.setdefault(r["line"], []).append(j)
    for i in order:
        for j in by_line.get(primary[i]["line"], []):
            if j not in used:
                used.add(j)
                res[i] = rca[j]
                break
    for i in order:
        if res[i] is not None:
            continue
        pname = primary[i]["name"].rsplit(".", 1)[-1].rsplit("::", 1)[-1]
        best = None
        for j, r in enumerate(rca):
            rname = r["name"].rsplit(".", 1)[-1].rsplit("::", 1)[-1]
            d = abs(r["line"] - primary[i]["line"])
            if j not in used and rname == pname and d <= 5 and (best is None or d < best[0]):
                best = (d, j)
        if best:
            used.add(best[1])
            res[i] = rca[best[1]]
    return res, [r for j, r in enumerate(rca) if j not in used]


def collect(root, work, files, mapping):
    """-> ext dict (see keys used below). Never raises for a missing tool: it records `degraded` reasons."""
    pyf = [f for f in files if f["lang"] == "python"]
    jsf = [f for f in files if f["lang"] == "js"]
    ext = {"py": None, "js": None, "degraded": [], "tools": {}, "errors": []}
    if pyf:
        ext["py"] = pytools.run(work, mapping) if not os.environ.get(NO_PYTOOLS_ENV) else \
            {"available": False, "reason": "radon/vulture disabled (CXM_NO_PYTOOLS)", "functions": {}, "files": {}, "vulture": []}
        if ext["py"]["available"]:
            ext["tools"].update({"radon": ext["py"]["versions"]["radon"], "vulture": ext["py"]["versions"]["vulture"]})
            if ext["py"].get("errors"):
                ext["errors"] += ext["py"]["errors"]
                ext["degraded"].append(f"python: radon/vulture could not parse {len({e.split(' (')[0] for e in ext['py']['errors']})} file(s) "
                                       "(listed under skipped_or_partial_files; their functions come from rust-code-analysis)")
        else:
            ext["degraded"].append("python: " + ext["py"]["reason"] + " -> functions from rust-code-analysis, "
                                   "unused code from the name-reference heuristic")
    if jsf:
        if os.environ.get(NO_NODE_ENV):
            ext["js"] = {"available": False, "reason": nodetools.NO_NODE_REASON}  # same text as a real missing node
        else:
            ext["js"] = nodetools.run(root, work, jsf, mapping)
        if ext["js"]["available"]:
            ext["tools"].update({k: v for k, v in ext["js"]["versions"].items()
                                 if k in ("eslint", "eslint-plugin-sonarjs", "@typescript-eslint/parser", "typescript")})
            ext["tools"]["node"] = "available"
            if ext["js"].get("errors"):
                ext["errors"] += ext["js"]["errors"]
                ext["degraded"].append(f"js/ts: ESLint could not parse {len(ext['js']['errors'])} file(s) "
                                       "(listed under skipped_or_partial_files; their functions come from rust-code-analysis)")
            if not ext["js"].get("graph_reason"):
                ext["tools"]["dependency-cruiser"] = ext["js"]["versions"]["dependency-cruiser"]
            else:
                ext["tools"]["dependency-cruiser"] = "failed"
            if ext["js"].get("knip") is not None:
                ext["tools"]["knip"] = ext["js"]["versions"]["knip"]
            if ext["js"].get("graph_reason"):
                ext["degraded"].append("js/ts module graph: " + _first_line(ext["js"]["graph_reason"]) + " -> regex import graph")
            if ext["js"].get("knip") is None:
                why = _first_line(ext["js"].get("knip_reason"))
                ext["tools"]["knip"] = "skipped (" + why + ")"
                ext["degraded"].append("js/ts unused exports: knip skipped (" + why + ") -> regex export heuristic")
        else:
            ext["degraded"].append("js/ts: " + str(ext["js"]["reason"]) + " -> regex import graph, "
                                   "rust-code-analysis functions, regex unused-export heuristic, no duplication (jscpd)")
    ext["degraded"].sort()
    ext["errors"].sort()
    return ext


def note_duplication(ext, duplication):
    """Missing jscpd is a degradation too (also for Rust-only runs); --no-dup is a choice, not one."""
    if duplication.get("available") or duplication.get("reason") == dup_not_run():
        return
    if not any("jscpd" in d for d in ext["degraded"]):
        ext["degraded"].append("duplication not measured: " + _first_line(duplication.get("reason")) +
                               " (MAI is computed without the duplication sub-rating)")
        ext["degraded"].sort()


def dup_not_run():
    from . import dup
    return dup.NOT_RUN


def _first_line(text):
    """First non-empty line of a tool error, so headers stay one line and machine-independent."""
    for ln in str(text).splitlines():
        if ln.strip():
            return ln.strip()[:160]
    return ""


def js_graph_ok(ext):
    return bool(ext["js"] and ext["js"]["available"] and ext["js"].get("edges") is not None)


def js_knip_ok(ext):
    return bool(ext["js"] and ext["js"]["available"] and ext["js"].get("knip") is not None)


def py_ok(ext):
    return bool(ext["py"] and ext["py"]["available"])


def merge_functions(all_funcs, all_clos, ext, is_test, py_facts):
    """Schema-2 function and closure records come from rust-code-analysis for every file. For Python files radon
    is the function inventory and cyclomatic source (cognitive, ploc, own_ploc stay from rust-code-analysis, which
    radon cannot provide); for JS/TS files ESLint + SonarJS replace the records. Returns (funcs, closures, cross_check)."""
    funcs_by_file = {}
    for f in all_funcs:
        funcs_by_file.setdefault(f["file"], []).append(f)
    replaced_js = set()
    new_funcs, new_clos, cross = [], [], {}

    def stat(lang):
        return cross.setdefault(lang, {"files": 0, "primary_functions": 0, "rca_functions": 0, "matched": 0,
                                       "only_primary": 0, "only_rca": 0, "cyc_diff": [], "cog_diff": [], "top": []})

    if py_ok(ext):
        for rel, v in sorted(ext["py"]["functions"].items()):
            if "blocks" not in v:
                continue
            rca = sorted(funcs_by_file.get(rel, []), key=lambda r: (r["line"], r["name"]))
            blocks = v["blocks"]
            pairs, leftover = _match(blocks, rca)
            facts = py_facts.get(rel, {}).get("fns", [])
            has_self = {x["line"]: x["has_self"] for x in facts}
            pnargs = {x["line"]: x["nargs"] for x in facts}
            c = stat("python")
            c["files"] += 1
            c["primary_functions"] += len(blocks)
            c["rca_functions"] += len(rca)
            c["only_rca"] += len(leftover)
            for p, r in zip(blocks, pairs):
                if r is None:  # radon sees a function rust-code-analysis does not: cognitive unknown (0)
                    c["only_primary"] += 1
                    span = p["end_line"] - p["line"] + 1
                    nargs = pnargs.get(p["line"], 0)
                    new_funcs.append({"file": rel, "line": p["line"], "end_line": p["end_line"], "name": p["name"],
                                      "lang": "python", "cognitive": 0, "cyclomatic": p["cyclomatic"], "sloc": span,
                                      "ploc": span, "own_ploc": span, "unit_ploc": span, "nargs": nargs, "nexits": 0,
                                      "halstead_volume": None, "mi_vs": None,
                                      "scope": "test" if is_test[rel] else "production",
                                      "nargs_adj": max(0, nargs - (1 if has_self.get(p["line"]) else 0)),
                                      "source": "radon"})
                    continue
                c["matched"] += 1
                c["cyc_diff"].append(abs(p["cyclomatic"] - r["cyclomatic"]))
                r["cyclomatic_rca"] = r["cyclomatic"]
                r["cyclomatic"] = p["cyclomatic"]
                r["source"] = "radon+rca"
            for r in leftover:  # e.g. methods of classes defined inside functions: radon does not list them
                r["source"] = "rca (not listed by radon)"
    if ext["js"] and ext["js"]["available"]:
        clos_by_file = {}
        for c_ in all_clos:
            clos_by_file.setdefault(c_["file"], []).append(c_)
        for rel, units in sorted(ext["js"]["functions"].items()):
            rca = sorted(funcs_by_file.get(rel, []), key=lambda r: (r["line"], r["name"]))
            prim = [u for u in units if u["kind"] == "fn"]
            pairs, leftover = _match(prim, rca)
            c = stat("js")
            c["files"] += 1
            c["primary_functions"] += len(prim)
            c["rca_functions"] += len(rca)
            c["only_rca"] += len(leftover)
            scope = "test" if is_test[rel] else "production"
            for u, r in zip(prim, pairs):
                if r is not None:
                    c["matched"] += 1
                    c["cyc_diff"].append(abs(u["cyclomatic"] - r["cyclomatic"]))
                    c["cog_diff"].append(abs(u["cognitive"] - r["cognitive"]))
                    c["top"].append((abs(u["cognitive"] - r["cognitive"]), rel, u["line"], u["name"], u["cognitive"], r["cognitive"]))
                else:
                    c["only_primary"] += 1
                new_funcs.append({
                    "file": rel, "line": u["line"], "end_line": u["end_line"], "name": u["name"], "lang": "js",
                    "cognitive": u["cognitive"], "cyclomatic": u["cyclomatic"],
                    "sloc": r["sloc"] if r is not None else u["end_line"] - u["line"] + 1,
                    "ploc": u["ploc"], "own_ploc": u["own_ploc"], "unit_ploc": u["unit_ploc"], "nargs": u["nargs"], "nargs_adj": u["nargs"],
                    "nexits": r["nexits"] if r is not None else 0,
                    "halstead_volume": r["halstead_volume"] if r is not None else None,
                    "mi_vs": r["mi_vs"] if r is not None else None, "scope": scope, "source": "eslint+sonarjs"})
            for u in units:
                if u["kind"] == "closure":
                    new_clos.append({"file": rel, "line": u["line"], "end_line": u["end_line"], "name": u["name"],
                                     "lang": "js", "parent": u["parent"], "cognitive": u["cognitive"],
                                     "own_ploc": u["own_ploc"], "scope": scope})
            replaced_js.add(rel)
    funcs = [f for f in all_funcs if f["file"] not in replaced_js] + new_funcs
    clos = [x for x in all_clos if x["file"] not in replaced_js] + new_clos
    funcs.sort(key=lambda x: (x["file"], x["line"], x["name"]))
    clos.sort(key=lambda x: (x["file"], x["line"], x["name"]))
    return funcs, clos, _summarise(cross)


def _summarise(cross):
    res = {}
    for lang, c in sorted(cross.items()):
        d = {"files": c["files"], "primary_functions": c["primary_functions"], "rca_functions": c["rca_functions"],
             "matched": c["matched"], "only_primary": c["only_primary"], "only_rca": c["only_rca"],
             "cyclomatic_mean_abs_diff": rnd(sum(c["cyc_diff"]) / len(c["cyc_diff"]), 3) if c["cyc_diff"] else None,
             "cyclomatic_max_abs_diff": max(c["cyc_diff"]) if c["cyc_diff"] else None,
             "primary": "radon (cc)" if lang == "python" else "eslint complexity + sonarjs"}
        if lang == "js":
            d["cognitive_mean_abs_diff"] = rnd(sum(c["cog_diff"]) / len(c["cog_diff"]), 3) if c["cog_diff"] else None
            d["cognitive_max_abs_diff"] = max(c["cog_diff"]) if c["cog_diff"] else None
            d["cognitive_largest_disagreements"] = [
                {"file": f, "line": ln, "name": n, "sonarjs": a, "rca": b}
                for _, f, ln, n, a, b in sorted(c["top"], key=lambda t: (-t[0], t[1], t[2]))[:5] if _ > 0]
        res[lang] = d
    return res


def tool_table(ext, langs, duplication):
    """Per-language tool for each metric, as rows [language, metric, tool] for the report header."""
    rows = []
    node_ok = bool(ext["js"] and ext["js"]["available"])
    py_good = py_ok(ext)
    t = ext["tools"]
    if "rust" in langs:
        rows += [["Rust", "cognitive, cyclomatic, SLOC, Halstead, MI", "rust-code-analysis"],
                 ["Rust", "module graph, abstractions, unused pub", "tree-sitter-rust + Cargo.toml (builtin)"]]
    if "python" in langs:
        rows += [["Python", "function inventory, cyclomatic, MI, Halstead, raw LOC",
                  f"radon {t['radon']}" if py_good else "rust-code-analysis (DEGRADED: radon/vulture unavailable or failed)"],
                 ["Python", "cognitive, SLOC (cross-check of cyclomatic)", "rust-code-analysis"],
                 ["Python", "import graph", "ast (builtin)"],
                 ["Python", "unused code (OEI)", f"vulture {t['vulture']}" if py_good else "name-reference heuristic (DEGRADED)"]]
    if "js" in langs:
        if node_ok:
            rows += [["JS/TS", "cognitive complexity", f"eslint {t['eslint']} + eslint-plugin-sonarjs {t['eslint-plugin-sonarjs']}"],
                     ["JS/TS", "cyclomatic, parameters", f"eslint {t['eslint']} (complexity rule), parser @typescript-eslint/parser {t['@typescript-eslint/parser']}"],
                     ["JS/TS", "SLOC, Halstead, MI (cross-check)", "rust-code-analysis"],
                     ["JS/TS", "module graph, cycles, instability",
                      f"dependency-cruiser {t['dependency-cruiser']}" if not ext["js"].get("graph_reason") else "regex import graph (DEGRADED)"],
                     ["JS/TS", "unused exports and files (OEI)",
                      f"knip {t['knip']}" if ext["js"].get("knip") is not None else "regex export heuristic (DEGRADED: knip skipped)"]]
        else:
            rows += [["JS/TS", "cognitive, cyclomatic, SLOC", "rust-code-analysis (DEGRADED: no node)"],
                     ["JS/TS", "module graph, cycles, instability", "regex import graph (DEGRADED: no node)"],
                     ["JS/TS", "unused exports (OEI)", "regex export heuristic (DEGRADED: no node)"]]
    if duplication.get("available"):
        dtool = duplication["tool"]
    elif duplication.get("reason") == dup_not_run():
        dtool = "disabled (--no-dup)"
    else:
        dtool = "not run (DEGRADED: " + _first_line(duplication.get("reason")) + ")"
    rows.append(["all", "duplication", dtool])
    return rows


def python_files(ext):
    """Per-file radon metrics (MI, Halstead totals, raw SLOC), sorted by path."""
    if not py_ok(ext):
        return []
    return [dict(file=f, **v) for f, v in sorted(ext["py"]["files"].items())]


def unused_python(ext):
    """vulture findings in production-or-test files, sorted: {file, line, kind, name, confidence}."""
    return ext["py"]["vulture"] if py_ok(ext) else []


def js_graph(ext):
    """Summary of the JS/TS module graph tool: tool, tsconfig used, edge counts by flavour (or None)."""
    if not js_graph_ok(ext):
        return None
    return dict(ext["js"]["edge_stats"], tool="dependency-cruiser " + ext["tools"]["dependency-cruiser"],
                tsconfig=ext["js"]["tsconfig"])
