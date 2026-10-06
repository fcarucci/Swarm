"""Over-engineering proxies (heuristic): single-impl abstractions, pass-through fns, generic-heavy items,
unreferenced public items, speculative markers, single-use abstractions."""
from . import langtools, pytools
from .core import rnd

STOP_NAMES = {"new", "main", "default", "from", "into", "fmt", "run", "get", "set", "len", "build", "init",
              "test", "drop", "clone", "eq", "hash", "cmp", "next", "name", "id", "value", "key", "apply",
              "handle", "parse", "setup", "teardown", "create", "update", "close", "open", "start", "stop",
              "read", "write", "load", "save", "add", "remove", "reset", "check", "process", "execute"}
PUB_KINDS = {"fn", "struct", "enum", "trait", "type", "const", "static"}
GENERIC_KINDS = {"fn", "struct", "enum", "trait", "type"}


def _index(idents_by_file):
    files, total = {}, {}
    for f, d in idents_by_file.items():
        for k, v in d.items():
            files.setdefault(k, set()).add(f)
            total[k] = total.get(k, 0) + v
    return files, total


def _vulture_unused(py, is_test, ext, out):
    """(unused, defined, dead_code_counts_by_kind): vulture findings for functions and classes against all
    non-method, non-dunder function and class definitions of production Python files (methods: report only)."""
    defined = 0
    for rel, f in py.items():
        if not is_test[rel]:
            defined += sum(1 for x in f["fns"] if not x["is_method"] and not (x["name"].startswith("__") and x["name"].endswith("__")))
            defined += len(f["classes"])
    unused, dead = 0, {}
    for it in langtools.unused_python(ext):
        if is_test.get(it["file"], True):
            continue
        dead[it["kind"]] = dead.get(it["kind"], 0) + 1
        if it["kind"] in pytools.VULTURE_KINDS_IN_RATIO:
            unused += 1
            out["unused_pub"].append({"lang": "python", "name": it["name"], "file": it["file"], "line": it["line"],
                                      "confidence": it["confidence"], "kind": it["kind"]})
    return unused, defined, dict(sorted(dead.items()))


def _knip_unused(js, is_test, ext, out):
    """(unused, exported): knip unused exports + exported types vs. names exported by production JS/TS files."""
    knip, exports = ext["js"]["knip"], ext["js"]["exports"]
    defined = unused = 0
    for rel in sorted(js):
        if is_test[rel]:
            continue
        names = exports.get(rel, [])
        defined += len(names)
        bad = sorted(set(knip["exports"].get(rel, [])) | set(knip["types"].get(rel, [])))
        unused += len(bad)
        for n in bad:
            out["unused_pub"].append({"lang": "js", "name": n, "file": rel, "line": 0})
    out["unused_files"] = [f for f in knip["files"] if f in js and not is_test[f]]
    return unused, defined


def analyze(ctx, th):
    """ctx keys: rust, py, js (facts by rel), is_test (rel->bool), idents ({rel: {name: n}}),
    lib_files (set of rust rels in library crates), prod_sloc (int), sloc_by_file."""
    rust, py, js, is_test = ctx["rust"], ctx["py"], ctx["js"], ctx["is_test"]
    files_with, total = _index(ctx["idents"])
    out = {"single_impl": [], "pass_through": [], "generic_heavy": [], "unused_pub": [],
           "markers": [], "single_use": []}

    # ---------- abstractions and implementations
    impls_p, impls_t = {}, {}
    for rel, f in rust.items():
        for im in f["impls"]:
            if im["trait"]:
                tgt = impls_t if (is_test[rel] or im["test"]) else impls_p
                tgt[("rust", im["trait"])] = tgt.get(("rust", im["trait"]), 0) + 1
    for rel, f in py.items():
        for c in f["classes"]:
            for b in c["bases"]:
                tgt = impls_t if is_test[rel] else impls_p
                tgt[("python", b)] = tgt.get(("python", b), 0) + 1
    for rel, f in js.items():
        for b in f["implements"]:
            tgt = impls_t if is_test[rel] else impls_p
            tgt[("js", b)] = tgt.get(("js", b), 0) + 1
    abstractions = []
    for rel, f in sorted(rust.items()):
        if is_test[rel]:
            continue
        for it in f["items"]:
            if it["kind"] == "trait" and not it["test"]:
                abstractions.append(("rust", it["name"], rel, it["line"], it["allow"]))
    for rel, f in sorted(py.items()):
        if is_test[rel]:
            continue
        for c in f["classes"]:
            if c["abstract"]:
                abstractions.append(("python", c["name"], rel, c["line"], False))
    for rel, f in sorted(js.items()):
        if is_test[rel]:
            continue
        for a in f["abstractions"]:
            abstractions.append(("js", a["name"], rel, 0, False))
    weighted = 0.0
    for lang, name, rel, line, allow in abstractions:
        p, t = impls_p.get((lang, name), 0), impls_t.get((lang, name), 0)
        if allow or p >= 2:
            w = 0.0
        elif t >= 1:
            w = th["oei"]["mock_only_weight"]
        else:
            w = 1.0
        weighted += w
        if w > 0:
            out["single_impl"].append({"lang": lang, "name": name, "file": rel, "line": line,
                                       "impls_prod": p, "impls_test": t,
                                       "class": "mock-only" if t >= 1 else "single-impl"})
    n_abs = len(abstractions)

    # ---------- pass-through
    pt_n = pt_d = 0
    for rel, f in sorted(rust.items()):
        if is_test[rel]:
            continue
        for fn in f["fns"]:
            if fn["test"] or fn["trait_impl"] or not fn["has_body"]:
                continue
            pt_d += 1
            if fn["pass_through"]:
                pt_n += 1
                out["pass_through"].append({"lang": "rust", "name": fn["name"], "file": rel, "line": fn["line"]})
    for rel, f in sorted(py.items()):
        if is_test[rel]:
            continue
        for fn in f["fns"]:
            if fn["abstract"]:
                continue
            pt_d += 1
            if fn["pass_through"]:
                pt_n += 1
                out["pass_through"].append({"lang": "python", "name": fn["name"], "file": rel, "line": fn["line"]})

    # ---------- generic heavy
    gn = gd = 0
    gmin, wmin = th["oei"]["generic_min_params"], th["oei"]["generic_min_where"]
    for rel, f in sorted(rust.items()):
        if is_test[rel]:
            continue
        for it in f["items"]:
            if it["test"] or it["kind"] not in GENERIC_KINDS:
                continue
            gd += 1
            if it["generics"] >= gmin or it["where"] >= wmin:
                gn += 1
                out["generic_heavy"].append({"name": it["name"], "file": rel, "line": it["line"],
                                             "generics": it["generics"], "where": it["where"]})
        for im in f["impls"]:
            if im["test"]:
                continue
            gd += 1
            if im["generics"] >= gmin or im["where"] >= wmin:
                gn += 1
                out["generic_heavy"].append({"name": "impl " + (im["trait"] + " for " if im["trait"] else "") + im["type"],
                                             "file": rel, "line": im["line"], "generics": im["generics"],
                                             "where": im["where"]})

    # ---------- unused pub
    un = ud = 0
    for rel, f in sorted(rust.items()):
        if is_test[rel] or rel not in ctx["lib_files"]:
            continue
        for it in f["items"]:
            if not (it["pub"] and it["top"] and not it["test"] and it["kind"] in PUB_KINDS):
                continue
            if it["name"] in STOP_NAMES:
                continue
            ud += 1
            if not (files_with.get(it["name"], set()) - {rel}):
                un += 1
                out["unused_pub"].append({"lang": "rust", "name": it["name"], "file": rel, "line": it["line"]})
    ext = ctx.get("ext")
    if ext is not None and langtools.py_ok(ext):  # vulture (>= pytools.VULTURE_MIN_CONFIDENCE) instead of name counting
        pn, pd, dead = _vulture_unused(py, is_test, ext, out)
        un, ud = un + pn, ud + pd
        out["dead_code"] = {"python": dead}
    else:
        for rel, f in sorted(py.items()):
            if is_test[rel]:
                continue
            for d in f["defs"]:
                if d["name"] in STOP_NAMES:
                    continue
                ud += 1
                if not (files_with.get(d["name"], set()) - {rel}):
                    un += 1
                    out["unused_pub"].append({"lang": "python", "name": d["name"], "file": rel, "line": d["line"]})
    if ext is not None and langtools.js_knip_ok(ext):  # knip unused exports/types instead of regex export counting
        jn, jd = _knip_unused(js, is_test, ext, out)
        un, ud = un + jn, ud + jd
    else:
        for rel, f in sorted(js.items()):
            if is_test[rel]:
                continue
            for name in f["exports"]:
                if name in STOP_NAMES or name == "default":
                    continue
                ud += 1
                if not (files_with.get(name, set()) - {rel}):
                    un += 1
                    out["unused_pub"].append({"lang": "js", "name": name, "file": rel, "line": 0})

    # ---------- speculative markers
    mk = 0
    for rel, f in sorted(list(rust.items()) + list(py.items())):
        if is_test[rel]:
            continue
        for m in f["markers"]:
            mk += 1
            out["markers"].append({"file": rel, "line": m["line"], "kind": m["kind"]})
    kloc = max(ctx["prod_sloc"], 1) / 1000.0
    per_k = mk / kloc

    # ---------- single-use abstractions
    sn = sd = 0
    cands = []
    for rel, f in sorted(rust.items()):
        if is_test[rel]:
            continue
        for it in f["items"]:
            if it["test"]:
                continue
            if it["kind"] in ("trait", "type", "macro") or (it["kind"] == "fn" and it["generics"] >= 1 and it["top"]):
                cands.append(("rust", it["kind"], it["name"], rel, it["line"]))
    for rel, f in sorted(py.items()):
        if is_test[rel]:
            continue
        for c in f["classes"]:
            if c["abstract"]:
                cands.append(("python", "abstract class", c["name"], rel, c["line"]))
    for lang, kind, name, rel, line in cands:
        if name in STOP_NAMES:
            continue
        sd += 1
        refs = total.get(name, 0) - 1
        if refs <= 1:
            sn += 1
            out["single_use"].append({"lang": lang, "kind": kind, "name": name, "file": rel, "line": line, "refs": max(refs, 0)})

    def ratio(n, d):
        return n / d if d else 0.0

    out["ratios"] = {
        "st": rnd(weighted / n_abs if n_abs else 0.0), "pt": rnd(ratio(pt_n, pt_d)),
        "gen": rnd(ratio(gn, gd)), "up": rnd(ratio(un, ud)),
        "sg": rnd(min(1.0, per_k / th["oei"]["markers_cap_per_kloc"])), "abs": rnd(ratio(sn, sd)),
    }
    out["counts"] = {"abstractions": n_abs, "single_impl_weighted": rnd(weighted, 2), "functions": pt_d,
                     "pass_through": pt_n, "generic_capable_items": gd, "generic_heavy": gn,
                     "public_items": ud, "unused_public": un, "markers": mk, "markers_per_kloc": rnd(per_k, 2),
                     "abstraction_candidates": sd, "single_use": sn}
    for k in out:
        if isinstance(out[k], list) and k != "unused_files":
            out[k].sort(key=lambda r: (r["file"], r.get("line", 0), r.get("name", ""), r.get("kind", "")))
    return out
