"""Turn rust-code-analysis JSON into per-function and per-closure records.

Units (schema 2): every named function and every closure/lambda is its own cognitive-complexity unit,
as in Clippy, which skips closure bodies when scoring the enclosing fn (`for_each_expr_without_closures`)
and reports each closure on its own span. rca models closures as nested spaces whose `sum` is added
into the parent, so a unit's own value is its space sum minus the sums of its direct child function spaces.
"""
from .core import rnd

CONTAINERS = ("impl", "trait", "class", "namespace", "unit", "interface")


def _count_desc(s):
    return sum(1 + _count_desc(c) for c in s["spaces"])


def _fn_children(s):
    """Direct function-like child spaces (named fns and closures), looking through non-function spaces."""
    out = []
    for c in s["spaces"]:
        if c["kind"] == "function":
            out.append(c)
        else:
            out += _fn_children(c)
    return out


def _own_cognitive(s):
    """Cognitive of this space alone: nested named fns and closures are separate units."""
    return s["metrics"]["cognitive"]["sum"] - sum(c["metrics"]["cognitive"]["sum"] for c in _fn_children(s))


def _ploc(s):
    return int(s["metrics"]["loc"]["ploc"])


def _allocate(s, budget, own):
    """Exact partition of code lines (rca `ploc`: no blank or comment lines) over a unit tree.

    `budget` lines belong to space `s` and its nested fns/closures. Children are visited in source order; each
    gets its ploc, minus 1 if it starts on the line where the previous sibling ends (that shared line is already
    given away), capped by what is left of the budget. The remainder is `s`'s own lines (>= 0). rca counts a line
    shared by a parent and its closures in every one of them, so plain subtraction can go negative; this way the
    own lines of a top-level fn and of all units nested in it add up to that fn's ploc exactly."""
    used, prev_end = 0, None
    for c in _fn_children(s):
        p = _ploc(c)
        if prev_end is not None and int(c["start_line"]) <= prev_end:
            p -= 1
        a = max(0, min(p, budget - used))
        used += a
        _allocate(c, a, own)
        prev_end = max(prev_end or 0, int(c["end_line"]))
    own[id(s)] = budget - used


def own_ploc_map(rca_json):
    """{id(space): own code lines} for every function space of one rca file tree."""
    own = {}

    def top(s):
        for c in s["spaces"]:
            if c["kind"] == "function":
                _allocate(c, _ploc(c), own)
            else:
                top(c)

    top(rca_json)
    return own


def _unit_ploc(s, own):
    """Own lines of a named fn plus those of its closures (at any depth), excluding nested named fns: the SIG unit."""
    total = own[id(s)]
    for c in _fn_children(s):
        if c["name"] == "<anonymous>":
            total += _unit_ploc(c, own)
    return total


def _cyclomatic(s):
    """1 + decision points of a named fn including its closures, excluding nested named fns. MAI unit
    complexity follows SIG, where a unit is a named method (unchanged from schema 1)."""
    total = s["metrics"]["cyclomatic"]["sum"] - _count_desc(s)  # 1 + decision points in whole subtree
    for c in s["spaces"]:
        if c["kind"] == "function" and c["name"] != "<anonymous>":
            total -= (c["metrics"]["cyclomatic"]["sum"] - _count_desc(c)) - 1
    return total


def extract(rca_json, rel, lang):
    """Return (file_sloc, [function dicts], [closure dicts]) for one file."""
    unit = rca_json
    file_sloc = unit["metrics"]["loc"]["sloc"]
    out, clos = [], []
    own = own_ploc_map(unit)
    seen_lines = {}

    def visit(s, parent, owner):
        for c in s["spaces"]:
            named = c["name"] != "<anonymous>"
            m = c["metrics"]
            if c["kind"] == "function" and (named or parent["kind"] in CONTAINERS):
                pn = parent["name"] if parent["kind"] in ("impl", "trait", "class") else ""
                name = (pn + "::" if pn and lang == "rust" else pn + "." if pn else "") + c["name"]
                out.append({
                    "file": rel, "line": int(c["start_line"]), "end_line": int(c["end_line"]),
                    "name": name, "lang": lang,
                    "cognitive": int(_own_cognitive(c)), "cyclomatic": int(_cyclomatic(c)),
                    "sloc": int(m["loc"]["sloc"]), "ploc": int(m["loc"]["ploc"]), "own_ploc": own[id(c)],
                    "unit_ploc": _unit_ploc(c, own),
                    "nargs": int(m["nargs"]["total_functions"]),
                    "nexits": int(m["nexits"]["sum"]),
                    "halstead_volume": rnd(m["halstead"]["volume"], 2),
                    "mi_vs": rnd(m["mi"]["mi_visual_studio"], 2),
                })
                visit(c, c, name)  # nested named fns and closures
            elif c["kind"] == "function":
                ln = int(c["start_line"])
                seen_lines[ln] = seen_lines.get(ln, 0) + 1  # rca has no columns: n-th closure starting on the line
                tag = f"{ln}" if seen_lines[ln] == 1 else f"{ln}#{seen_lines[ln]}"
                cname = f"{owner or '<top>'}::<closure@{tag}>"
                clos.append({
                    "file": rel, "line": int(c["start_line"]), "end_line": int(c["end_line"]),
                    "name": cname, "lang": lang, "parent": owner,
                    "cognitive": int(_own_cognitive(c)), "own_ploc": own[id(c)],
                })
                visit(c, parent, cname)
            else:
                visit(c, c if c["kind"] in CONTAINERS else parent, owner)

    visit(unit, unit, None)
    return int(file_sloc), out, clos
