"""Shared stdlib-ast helpers for the Python analyzers (analyze.py, design.py).

Nothing here needs a third-party tool. Functions are named by their qualified name
(`Class.method`, `outer.inner`); parameters exclude `self` / `cls`; `*args` and `**kwargs` are not counted.
"""
import ast
import os

DEF_NODES = (ast.FunctionDef, ast.AsyncFunctionDef)
SKIP_DIRS = {".git", "__pycache__", ".venv", "venv", "site-packages", "build", "dist", "node_modules",
             ".tox", ".mypy_cache", ".pytest_cache", "target", "vendor", ".idea", ".vscode"}


def norm(p):
    return str(p).replace("\\", "/")


def iter_python_files(paths):
    """Sorted [(display_path, path)] for the given files and directories (.py only, deterministic)."""
    found = {}
    for p in paths:
        if os.path.isdir(p):
            for dp, dns, fns in os.walk(p):
                dns[:] = sorted(d for d in dns if d not in SKIP_DIRS)
                for f in fns:
                    if f.endswith(".py"):
                        full = os.path.join(dp, f)
                        found[norm(full)] = full
        elif os.path.isfile(p):
            found[norm(p)] = p
        else:
            raise SystemExit(f"no such file or directory: {p}")
    return sorted(found.items())


def _walk_defs(stmts, prefix, owner):
    for node in stmts:
        if isinstance(node, ast.ClassDef):
            yield from _walk_defs(node.body, prefix + node.name + ".", node)
        elif isinstance(node, DEF_NODES):
            qual = prefix + node.name
            yield qual, node, owner
            yield from _walk_defs(node.body, qual + ".", None)
        else:
            for field in ("body", "orelse", "finalbody", "handlers"):
                child = getattr(node, field, None)
                if isinstance(child, list):
                    yield from _walk_defs(child, prefix, owner)


def functions(tree):
    """[(qualname, def node, owning ClassDef or None)] in source order."""
    return list(_walk_defs(tree.body, "", None))


def self_name(node, owner):
    """Name of the receiver of a method (`self`/`cls`), or None for a plain function."""
    if owner is None:
        return None
    args = node.args.posonlyargs + node.args.args
    return args[0].arg if args else None


def param_names(node, owner):
    """Named parameters (posonly, positional, keyword-only) with the receiver dropped."""
    args = node.args
    names = [a.arg for a in args.posonlyargs + args.args + args.kwonlyargs]
    if owner is not None and names and names[0] in ("self", "cls"):
        names = names[1:]
    return names


def qualnames(text):
    """{def line: qualname} for one source text; {} when it does not parse."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return {}
    return {node.lineno: qual for qual, node, _ in functions(tree)}
