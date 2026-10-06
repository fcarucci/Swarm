"""Dependency graphs (Cargo crates, Rust modules, Python imports, JS/TS imports) and Martin/HK metrics."""
import posixpath
import tomllib
from pathlib import Path

from .core import read_text, rnd

JS_EXTS = [".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx"]


# ---------------------------------------------------------------- graph helpers
def tarjan_sccs(nodes, edges):
    """Iterative Tarjan. nodes: sorted list; edges: {node: set(nodes)}. Returns sorted list of SCCs (size>=2)."""
    index, low, on, stack, out = {}, {}, set(), [], []
    counter = [0]
    for start in nodes:
        if start in index:
            continue
        work = [(start, iter(sorted(edges.get(start, ()))))]
        index[start] = low[start] = counter[0]
        counter[0] += 1
        stack.append(start)
        on.add(start)
        while work:
            v, it = work[-1]
            adv = False
            for w in it:
                if w not in index:
                    index[w] = low[w] = counter[0]
                    counter[0] += 1
                    stack.append(w)
                    on.add(w)
                    work.append((w, iter(sorted(edges.get(w, ())))))
                    adv = True
                    break
                elif w in on:
                    low[v] = min(low[v], index[w])
            if adv:
                continue
            work.pop()
            if work:
                low[work[-1][0]] = min(low[work[-1][0]], low[v])
            if low[v] == index[v]:
                comp = []
                while True:
                    w = stack.pop()
                    on.discard(w)
                    comp.append(w)
                    if w == v:
                        break
                if len(comp) >= 2:
                    out.append(sorted(comp))
    return sorted(out)


def degree_metrics(nodes, edges):
    ce = {n: len(edges.get(n, ())) for n in nodes}
    ca = {n: 0 for n in nodes}
    for n in nodes:
        for m in edges.get(n, ()):
            ca[m] = ca.get(m, 0) + 1
    return ca, ce


# ---------------------------------------------------------------- Rust crates
def _norm_crate(n):
    return n.replace("-", "_")


def load_crates(root, files):
    """Parse every Cargo.toml in the repo. files: all discovered rel paths (rust)."""
    root = Path(root)
    tomls = sorted(p.relative_to(root).as_posix() for p in root.rglob("Cargo.toml")
                   if not any(part in ("target", "node_modules", ".git") for part in p.relative_to(root).parts))
    crates = {}
    for rel in tomls:
        try:
            doc = tomllib.loads(read_text(root / rel))
        except Exception:
            continue
        pkg = doc.get("package")
        if not pkg or "name" not in pkg:
            continue
        d = posixpath.dirname(rel)
        crates[d] = {"name": pkg["name"], "dir": d, "toml": rel, "doc": doc}
    by_name = {_norm_crate(c["name"]): d for d, c in crates.items()}
    for d, c in sorted(crates.items()):
        doc = c["doc"]
        src = f"{d}/src" if d else "src"
        lib = (root / src / "lib.rs").exists() or "lib" in doc
        c["has_lib"] = lib
        c["has_bin"] = (root / src / "main.rs").exists() or "bin" in doc or (root / src / "bin").is_dir()
        deps, dev = set(), set()

        def collect(tables, into):
            for tbl in tables:
                for key, val in (tbl or {}).items():
                    cand = [_norm_crate(key)]
                    if isinstance(val, dict):
                        if "package" in val:
                            cand.append(_norm_crate(val["package"]))
                        if "path" in val:
                            tgt = posixpath.normpath(posixpath.join(d, val["path"]))
                            tgt = "" if tgt == "." else tgt
                            if tgt in crates and tgt != d:
                                into.add(tgt)
                                continue
                    for cn in cand:
                        if cn in by_name and by_name[cn] != d:
                            into.add(by_name[cn])
                            break

        tgts = [doc.get("target", {}).get(k, {}) for k in doc.get("target", {})]
        collect([doc.get("dependencies")] + [t.get("dependencies") for t in tgts], deps)
        collect([doc.get("dev-dependencies")] + [t.get("dev-dependencies") for t in tgts], dev)
        c["deps"] = deps
        c["dev_deps"] = dev - deps
    return crates


def crate_of(rel, crates):
    best = None
    for d in crates:
        if d == "" or rel.startswith(d + "/"):
            if best is None or len(d) > len(best):
                best = d
    return best


def rust_crate_graph(crates, crate_items, crate_sloc):
    nodes = sorted(crates)
    edges = {d: set(c["deps"]) for d, c in crates.items()}
    ca, ce = degree_metrics(nodes, edges)
    sccs = tarjan_sccs(nodes, edges)
    in_scc = {n for s in sccs for n in s}
    rows = []
    for d in nodes:
        c = crates[d]
        it = crate_items.get(d, {"trait": 0, "struct": 0, "enum": 0})
        nitems = it["trait"] + it["struct"] + it["enum"]
        a = it["trait"] / nitems if nitems else None
        i = ce[d] / (ca[d] + ce[d]) if (ca[d] + ce[d]) else None
        dist = abs(a + i - 1) if a is not None and i is not None else None
        kind = "lib" if c["has_lib"] else "bin"
        eligible = kind == "lib" and dist is not None and nitems >= 3
        rows.append({"name": c["name"], "dir": d or ".", "kind": kind, "ca": ca[d], "ce": ce[d],
                     "instability": rnd(i), "abstractness": rnd(a), "distance": rnd(dist),
                     "items": nitems, "sloc": crate_sloc.get(d, 0), "in_scc": d in in_scc,
                     "deps": sorted(crates[x]["name"] for x in c["deps"]),
                     "dev_deps": sorted(crates[x]["name"] for x in c["dev_deps"]),
                     "distance_eligible": eligible})
    rows.sort(key=lambda r: (r["dir"], r["name"]))
    return rows, [[crates[x]["name"] for x in s] for s in sccs]


# ---------------------------------------------------------------- Rust modules
def rust_module_edges(rust_facts, crates, file_crate):
    """rust_facts: {rel: facts} for all rust files (prod+test). Returns edges {file: set(file)} among prod files,
    and bookkeeping counts of resolved/unresolved/glob/reexport uses."""
    # crate -> root files
    roots = {}
    for d, c in crates.items():
        src = f"{d}/src" if d else "src"
        rs = []
        for nm in ("lib.rs", "main.rs"):
            p = f"{src}/{nm}"
            if p in rust_facts:
                rs.append((nm[:-3], p))
        for p in sorted(rust_facts):
            if p.startswith(src + "/bin/") and p.count("/") == src.count("/") + 2:
                rs.append((p.rsplit("/", 1)[1][:-3], p))
        roots[d] = rs
    lib_root = {}
    for d, c in crates.items():
        p = (f"{d}/src" if d else "src") + "/lib.rs"
        if p in rust_facts:
            lib_root[_norm_crate(c["name"])] = (d, p)
    # module tables per (crate dir, root file)
    tables = {}
    file_mod = {}  # (rootfile, file) -> modpath tuple

    def build(crate_dir, root_file):
        mods = {(): root_file}
        seen = set()

        def visit(f, mp):
            if (f, mp) in seen:
                return
            seen.add((f, mp))
            facts = rust_facts.get(f)
            if not facts:
                return
            fdir = posixpath.dirname(f)
            base_name = posixpath.basename(f)
            stem = base_name[:-3]
            own_dir = fdir if (base_name in ("mod.rs", "lib.rs", "main.rs") or f == root_file) else posixpath.join(fdir, stem)
            for m in facts["mods"]:
                inl = tuple(m["inline"])
                cp = mp + inl + (m["name"],)
                if m["body"]:
                    mods[cp] = f
                    continue
                if m["path"]:
                    cand = [posixpath.normpath(posixpath.join(fdir, m["path"]))]
                else:
                    d2 = posixpath.join(own_dir, *inl)
                    cand = [posixpath.join(d2, m["name"] + ".rs"), posixpath.join(d2, m["name"], "mod.rs")]
                for cf in cand:
                    if cf in rust_facts:
                        mods[cp] = cf
                        file_mod.setdefault((root_file, cf), cp)
                        visit(cf, cp)
                        break

        file_mod[(root_file, root_file)] = ()
        visit(root_file, ())
        return mods

    for d, rs in roots.items():
        for rname, rf in rs:
            tables[(d, rf)] = build(d, rf)

    edges = {}
    stats = {"uses_resolved": 0, "uses_external": 0, "uses_reexport_skipped": 0}
    for (d, rf), mods in sorted(tables.items()):
        for f, facts in sorted(rust_facts.items()):
            if (rf, f) not in file_mod:
                continue
            mp_file = file_mod[(rf, f)]
            for u in facts["uses"]:
                if u["test"]:
                    continue
                if u["reexport"]:
                    stats["uses_reexport_skipped"] += 1
                    continue
                cur = mp_file + tuple(u["inline"])
                segs = list(u["path"])
                first = segs[0]
                tgt_mods, rest, base = mods, segs, None
                if first == "crate":
                    base, rest = (), segs[1:]
                elif first == "self":
                    base, rest = cur, segs[1:]
                elif first == "super":
                    base = cur
                    while rest and rest[0] == "super":
                        base = base[:-1]
                        rest = rest[1:]
                elif _norm_crate(first) in lib_root and not (cur + (first,)) in mods:
                    cd, lf = lib_root[_norm_crate(first)]
                    tgt_mods = tables.get((cd, lf), {})
                    base, rest = (), segs[1:]
                elif (cur + (first,)) in mods:
                    base, rest = cur, segs
                if base is None:
                    stats["uses_external"] += 1
                    continue
                t = base
                for s in rest:
                    if t + (s,) in tgt_mods:
                        t = t + (s,)
                    else:
                        break
                tf = tgt_mods.get(t)
                if tf and tf != f:
                    edges.setdefault(f, set()).add(tf)
                    stats["uses_resolved"] += 1
    return edges, stats


# ---------------------------------------------------------------- Python imports
def python_edges(py_facts):
    mods = {}
    pkg_of = {}
    for rel in sorted(py_facts):
        parts = rel[:-3].split("/")
        # package root: highest dir chain with __init__.py
        i = len(parts) - 1
        dirs = parts[:-1]
        k = len(dirs)
        while k > 0 and "/".join(dirs[:k]) + "/__init__.py" in py_facts:
            k -= 1
        dotted = parts[k:]
        if dotted[-1] == "__init__":
            dotted = dotted[:-1]
        name = ".".join(dotted)
        pkg_of[rel] = ".".join(parts[k:-1]) if parts[-1] != "__init__" else name
        if name:
            mods.setdefault(name, rel)
    edges = {}
    for rel, facts in sorted(py_facts.items()):
        for imp in facts["imports"]:
            mod, lvl = imp["module"], imp["level"]
            if lvl:
                pk = pkg_of[rel].split(".") if pkg_of[rel] else []
                pk = pk[:len(pk) - (lvl - 1)] if lvl > 1 else pk
                base = ".".join(pk + ([mod] if mod else []))
            else:
                base = mod
            cands = []
            for nm in imp["names"]:
                cands.append(base + "." + nm if base else nm)
            cands.append(base)
            tgt = None
            for c in cands:
                parts = c.split(".")
                while parts:
                    if ".".join(parts) in mods:
                        tgt = mods[".".join(parts)]
                        break
                    parts.pop()
                if tgt:
                    if tgt != rel:
                        edges.setdefault(rel, set()).add(tgt)
                    tgt = None
    return edges


# ---------------------------------------------------------------- JS/TS imports
def js_edges(js_facts):
    known = set(js_facts)

    def resolve(frm, spec, include=False):
        if not (spec.startswith(".") or include):
            return None
        base = posixpath.normpath(posixpath.join(posixpath.dirname(frm), spec))
        cands = [base] + [base + e for e in JS_EXTS] + [posixpath.join(base, "index" + e) for e in JS_EXTS]
        for c in cands:
            if c in known:
                return c
        return None

    edges = {}
    for rel, f in sorted(js_facts.items()):
        for spec in f["imports"]:
            t = resolve(rel, spec)
            if t and t != rel:
                edges.setdefault(rel, set()).add(t)
        for spec in f["includes"]:
            t = resolve(rel, spec, include=True)
            if t and t != rel:
                edges.setdefault(rel, set()).add(t)
    return edges
