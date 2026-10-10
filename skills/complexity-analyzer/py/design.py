#!/usr/bin/env python3
"""Design-level signals for Python, for architectural refactoring loops (stdlib ast only).

  design.py <file-or-dir> [more...]

Prints tab-separated rows, sorted by file, so the same input always gives the same output:

  class      file line name methods public_methods attributes loc lcom4
  longparams file line name nparams                       (more than LONG_PARAMS, self/cls excluded)
  clump      n_functions names functions                  (3+ parameter names shared by 2+ functions)
  module     file sloc fanout hint

Definitions (see SKILL.md, "Design signals"):
  methods          every def directly in the class body, __init__ included
  attributes       distinct self.<name> used by the class's methods (method names and class-level
                   assignments included), i.e. the state the class touches
  loc              non-blank lines spanned by the class
  lcom4            connected components of the method graph (LCOM4). Nodes are the methods except
                   __init__ (every constructor touches every field, which would hide the split).
                   Two methods are linked when they use a common self attribute or one calls the other
                   through self. A method that uses no state is its own component. "-" when no method
                   is left to compare. 1 = cohesive; more than 1 = the class is really several classes.
  clump            the functions that all share the same 3+ parameter names; the names are the
                   intersection over those functions, so the list is maximal
  sloc             non-blank, non-comment lines of the module
  fanout           distinct import targets (`import a.b` -> a.b; `from a import b` -> a;
                   `from . import x` -> .; `from .m import x` -> .m); relative imports are not resolved
  hint             large (sloc > MODULE_SLOC_HINT), fan-out (fanout > MODULE_FANOUT_HINT), or -
"""
import ast
import itertools
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "lib"))
import pyast  # noqa: E402
from cxmetrics import core  # noqa: E402

LONG_PARAMS = 5           # flagged when a function has MORE than this many parameters
CLUMP_MIN_NAMES = 3
CLUMP_MIN_FUNCTIONS = 2
MODULE_SLOC_HINT = 500
MODULE_FANOUT_HINT = 15


def _is_method_def(node):
    return isinstance(node, pyast.DEF_NODES)


def _class_attributes(node, method_names):
    """Distinct state used by the class: self.<x> read or written in any method, plus class-level names."""
    attrs = set()
    for stmt in node.body:
        if isinstance(stmt, ast.Assign):
            attrs |= {t.id for t in stmt.targets if isinstance(t, ast.Name)}
        elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            attrs.add(stmt.target.id)
    attrs -= method_names
    for m in node.body:
        if _is_method_def(m):
            s = pyast.self_name(m, node)
            for n in ast.walk(m):
                if s and isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == s:
                    if n.attr not in method_names:
                        attrs.add(n.attr)
    return attrs


def _lcom4(node, methods, method_names):
    graph = [m for m in methods if m.name != "__init__"]
    if not graph:
        return "-"
    uses, calls = [], []
    for m in graph:
        s = pyast.self_name(m, node)
        used, called = set(), set()
        for n in ast.walk(m):
            if s and isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == s:
                (called if n.attr in method_names else used).add(n.attr)
        uses.append(used)
        calls.append(called)
    parent = list(range(len(graph)))

    def root(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i, j in itertools.combinations(range(len(graph)), 2):
        linked = (uses[i] & uses[j]) or (graph[j].name in calls[i]) or (graph[i].name in calls[j])
        if linked:
            parent[root(i)] = root(j)
    return len({root(i) for i in range(len(graph))})


def _span_loc(lines, first, last):
    return sum(1 for ln in lines[first - 1:last] if ln.strip())


def _classes(tree, lines):
    out = []
    stack = [(tree.body, "")]
    while stack:
        stmts, prefix = stack.pop()
        for node in stmts:
            if isinstance(node, ast.ClassDef):
                qual = prefix + node.name
                methods = [m for m in node.body if _is_method_def(m)]
                names = {m.name for m in methods}
                out.append({
                    "name": qual, "line": node.lineno,
                    "methods": len(methods),
                    "public_methods": sum(1 for m in methods if not m.name.startswith("_")),
                    "attributes": len(_class_attributes(node, names)),
                    "loc": _span_loc(lines, node.lineno, node.end_lineno),
                    "lcom4": _lcom4(node, methods, names),
                })
                stack.append((node.body, qual + "."))
            elif isinstance(node, pyast.DEF_NODES):
                stack.append((node.body, prefix + node.name + "."))
            else:
                for field in ("body", "orelse", "finalbody", "handlers"):
                    child = getattr(node, field, None)
                    if isinstance(child, list):
                        stack.append((child, prefix))
    return sorted(out, key=lambda c: (c["line"], c["name"]))


def _fanout(tree):
    targets = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            targets |= {a.name for a in n.names}
        elif isinstance(n, ast.ImportFrom):
            targets.add("." * n.level + (n.module or ""))
    return len(targets), sorted(targets)


def _module_sloc(lines):
    return sum(1 for ln in lines if ln.strip() and not ln.strip().startswith("#"))


def analyze_source(rel, text):
    """Design facts for one file. Raises SyntaxError for unparsable source."""
    tree = ast.parse(text)
    lines = text.splitlines()
    functions = []
    for qual, node, owner in pyast.functions(tree):
        params = tuple(pyast.param_names(node, owner))
        functions.append({"name": qual, "line": node.lineno, "params": params, "nparams": len(params)})
    fanout, targets = _fanout(tree)
    return {
        "classes": _classes(tree, lines),
        "functions": functions,
        "module": {"sloc": _module_sloc(lines), "fanout": fanout, "targets": targets},
    }


def clumps(functions):
    """Parameter clumps: maximal name sets (3+) shared by 2+ functions. Each function dict needs
    name, line, params and file. Returns [{"params": tuple, "functions": [...]}], biggest first."""
    by_triple = {}
    for f in functions:
        ps = sorted(set(f["params"]))
        for t in itertools.combinations(ps, CLUMP_MIN_NAMES):
            by_triple.setdefault(t, []).append(f)
    merged = {}
    for t, fs in by_triple.items():
        if len(fs) < CLUMP_MIN_FUNCTIONS:
            continue
        common = set(fs[0]["params"])
        for f in fs[1:]:
            common &= set(f["params"])
        key = tuple(sorted(common))
        merged.setdefault(key, {})
        for f in fs:
            merged[key][(f["file"], f["name"], f["line"])] = f
    out = []
    for key, fmap in merged.items():
        if len(fmap) >= CLUMP_MIN_FUNCTIONS:
            fs = [fmap[k] for k in sorted(fmap)]
            out.append({"params": key, "functions": [
                {"file": f["file"], "name": f["name"], "line": f["line"]} for f in fs]})
    out.sort(key=lambda c: (-len(c["functions"]), -len(c["params"]), c["params"]))
    return out


def render(sources):
    """TSV report for [(rel, text)]. Input order does not matter."""
    rows, all_funcs, unparsable = [], [], []
    for rel, text in sorted(sources):
        try:
            res = analyze_source(rel, text)
        except SyntaxError as exc:
            unparsable.append(f"# unparsable\t{rel}\tline {exc.lineno}")
            continue
        for c in res["classes"]:
            rows.append((rel, c["line"], f"class\t{rel}\t{c['line']}\t{c['name']}\t{c['methods']}\t"
                         f"{c['public_methods']}\t{c['attributes']}\t{c['loc']}\t{c['lcom4']}"))
        for f in res["functions"]:
            all_funcs.append(dict(f, file=rel))
            if f["nparams"] > LONG_PARAMS:
                rows.append((rel, f["line"], f"longparams\t{rel}\t{f['line']}\t{f['name']}\t{f['nparams']}"))
        m = res["module"]
        hints = [h for h, on in (("large", m["sloc"] > MODULE_SLOC_HINT),
                                 ("fan-out", m["fanout"] > MODULE_FANOUT_HINT)) if on]
        rows.append((rel, 0, f"module\t{rel}\t{m['sloc']}\t{m['fanout']}\t{','.join(hints) or '-'}"))
    lines = ["# class\tfile\tline\tname\tmethods\tpublic_methods\tattributes\tloc\tlcom4"]
    lines += [r[2] for r in sorted(rows, key=lambda r: (r[0], r[1], r[2]))]
    for c in clumps(all_funcs):
        where = ";".join(f"{f['file']}:{f['name']}@{f['line']}" for f in c["functions"])
        lines.append(f"clump\t{len(c['functions'])}\t{','.join(c['params'])}\t{where}")
    return "\n".join(lines + unparsable) + "\n"


def main(argv):
    if not argv:
        print(__doc__.split("\n\n")[0], file=sys.stderr)
        print("usage: design.py <file-or-dir> [more...]", file=sys.stderr)
        return 2
    files = pyast.iter_python_files(argv)
    sources = [(rel, core.read_text(path)) for rel, path in files]
    sys.stdout.write(render(sources))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
