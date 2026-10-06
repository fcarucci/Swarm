"""Python structural facts via the stdlib ast module."""
import ast
import re

STOP_BASES = {"ABC", "Protocol"}


def _name(n):
    if isinstance(n, ast.Name):
        return n.id
    if isinstance(n, ast.Attribute):
        return n.attr
    if isinstance(n, ast.Subscript):
        return _name(n.value)
    return None


def _deco_names(f):
    return [_name(d.func if isinstance(d, ast.Call) else d) for d in f.decorator_list]


def _pass_through(f, params):
    body = [s for s in f.body if not (isinstance(s, ast.Expr) and isinstance(getattr(s, "value", None), ast.Constant))]
    if len(body) != 1:
        return False
    s = body[0]
    v = s.value if isinstance(s, (ast.Return, ast.Expr)) else None
    if isinstance(v, ast.Await):
        v = v.value
    if not isinstance(v, ast.Call):
        return False
    ids = []
    for a in v.args:
        if isinstance(a, ast.Starred):
            a = a.value
        if not isinstance(a, ast.Name):
            return False
        ids.append(a.id)
    for kw in v.keywords:
        if not isinstance(kw.value, ast.Name):
            return False
        ids.append(kw.value.id)
    ps = set(params)
    if not ps.issubset(ids) or not all(i in ps or i in ("self", "cls") for i in ids):
        return False
    fn = v.func
    if isinstance(fn, ast.Attribute):
        node = fn.value
        while isinstance(node, ast.Attribute):
            node = node.value
        ok = isinstance(node, ast.Name) and (node.id in ("self", "cls") or node.id in ps)
        module_call = isinstance(node, ast.Name) and len(ids) >= 1
        return (ok and (len(ids) >= 1 or fn.attr == f.name)) or module_call
    if isinstance(fn, ast.Name):
        return len(ids) >= 1 and fn.id != f.name
    return False


def analyze(rel, text):
    facts = {"file": rel, "imports": [], "defs": [], "classes": [], "fns": [], "markers": [],
             "parse_errors": False, "pass_through": 0}
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError):
        facts["parse_errors"] = True
        return facts
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                facts["imports"].append({"level": 0, "module": a.name, "names": []})
        elif isinstance(node, ast.ImportFrom):
            facts["imports"].append({"level": node.level, "module": node.module or "",
                                     "names": [a.name for a in node.names]})
    exported = set()
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets):
            if isinstance(node.value, (ast.List, ast.Tuple)):
                exported |= {e.value for e in node.value.elts if isinstance(e, ast.Constant)}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if not node.name.startswith("_") and not node.decorator_list and node.name not in exported:
                facts["defs"].append({"name": node.name, "line": node.lineno,
                                      "kind": "class" if isinstance(node, ast.ClassDef) else "fn"})
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            bases = [_name(b) for b in node.bases if _name(b)]
            abstract = any(b in STOP_BASES for b in bases) or any(
                isinstance(k.value, (ast.Name, ast.Attribute)) and _name(k.value) == "ABCMeta" for k in node.keywords)
            facts["classes"].append({"name": node.name, "line": node.lineno, "bases": sorted(set(bases)),
                                     "abstract": abstract})
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    item._in_class = True
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = [a.arg for a in node.args.posonlyargs + node.args.args + node.args.kwonlyargs]
            has_self = bool(getattr(node, "_in_class", False) and args and args[0] in ("self", "cls")
                            and "staticmethod" not in _deco_names(node))
            params = [a for a in args if a not in ("self", "cls")] if has_self or getattr(node, "_in_class", False) else args
            decos = _deco_names(node)
            dunder = node.name.startswith("__") and node.name.endswith("__")
            pt = bool(params or has_self) and not decos and not dunder and _pass_through(node, params)
            facts["fns"].append({"name": node.name, "line": node.lineno, "has_self": has_self, "nargs": len(args), "is_method": bool(getattr(node, "_in_class", False)),
                                 "pass_through": pt, "abstract": "abstractmethod" in decos})
            for p in params:
                if p.startswith("_") and len(p) > 1 and not dunder:
                    facts["markers"].append({"kind": "underscore_param", "line": node.lineno})
            if "abstractmethod" not in decos:
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Raise) and sub.exc is not None and \
                            _name(sub.exc.func if isinstance(sub.exc, ast.Call) else sub.exc) == "NotImplementedError":
                        facts["markers"].append({"kind": "not_implemented", "line": sub.lineno})
    return facts
