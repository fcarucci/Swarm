"""Rust structural facts via tree-sitter-rust (syntax only, name based, no type resolution)."""
import re
from collections import Counter

import tree_sitter_rust
from tree_sitter import Language, Parser

_PARSER = Parser(Language(tree_sitter_rust.language()))
IDENT_TYPES = {"identifier", "type_identifier", "field_identifier"}
TRIVIAL_WRAPPERS = {"clone", "into", "as_ref", "as_str", "to_string", "to_owned", "borrow", "as_deref", "as_mut"}
ITEM_KINDS = {"function_item": "fn", "struct_item": "struct", "enum_item": "enum", "trait_item": "trait",
              "type_item": "type", "const_item": "const", "static_item": "static", "union_item": "struct"}


def _t(n):
    return n.text.decode("utf-8", "replace")


def _last_ident(n):
    if n is None:
        return None
    if n.type == "generic_type":
        return _last_ident(n.child_by_field_name("type"))
    if n.type in ("scoped_type_identifier", "scoped_identifier"):
        return _t(n.child_by_field_name("name"))
    if n.type == "reference_type":
        return _last_ident(n.child_by_field_name("type"))
    return _t(n)


def _is_pub(n):
    for c in n.children:
        if c.type == "visibility_modifier":
            return _t(c) == "pub"
    return False


def _has_vis(n):
    return any(c.type == "visibility_modifier" for c in n.children)


def _generic_count(n):
    tp = n.child_by_field_name("type_parameters")
    k = len([c for c in tp.named_children if c.type != "line_comment"]) if tp else 0
    wc = next((c for c in n.children if c.type == "where_clause"), None)
    w = len([c for c in wc.named_children if c.type == "where_predicate"]) if wc else 0
    return k, w


def flatten_use(n, prefix=()):
    """Yield (segments tuple, is_glob) for a use-tree node."""
    t = n.type
    if t in ("identifier", "crate", "self", "super", "metavariable"):
        yield prefix + (_t(n),), False
    elif t == "scoped_identifier":
        yield prefix + _path(n), False
    elif t == "use_as_clause":
        yield from flatten_use(n.child_by_field_name("path"), prefix)
    elif t == "use_wildcard":
        kids = [c for c in n.named_children]
        base = _path(kids[0]) if kids else ()
        yield prefix + base, True
    elif t == "scoped_use_list":
        p = n.child_by_field_name("path")
        base = prefix + (_path(p) if p is not None else ())
        lst = n.child_by_field_name("list")
        for c in lst.named_children:
            yield from flatten_use(c, base)
    elif t == "use_list":
        for c in n.named_children:
            yield from flatten_use(c, prefix)


def _path(n):
    if n.type == "scoped_identifier":
        p = n.child_by_field_name("path")
        return (_path(p) if p is not None else ()) + (_t(n.child_by_field_name("name")),)
    return (_t(n),)


def _unwrap_expr(n):
    while n is not None and n.type in ("await_expression", "try_expression", "return_expression",
                                       "expression_statement", "parenthesized_expression"):
        kids = n.named_children
        if not kids:
            return None
        n = kids[0]
    return n


def _arg_ident(n):
    """Identifier an argument trivially forwards, or None."""
    while n is not None:
        if n.type == "identifier":
            return _t(n)
        if n.type == "self":
            return "self"
        if n.type == "reference_expression":
            n = n.child_by_field_name("value")
        elif n.type == "call_expression":
            f = n.child_by_field_name("function")
            args = n.child_by_field_name("arguments")
            if f is not None and f.type == "field_expression" and not args.named_children \
                    and _t(f.child_by_field_name("field")) in TRIVIAL_WRAPPERS:
                n = f.child_by_field_name("value")
            else:
                return None
        elif n.type == "try_expression":
            n = n.named_children[0]
        else:
            return None
    return None


def _is_self_chain(n):
    while n is not None and n.type == "field_expression":
        n = n.child_by_field_name("value")
    return n is not None and n.type == "self"


def _pass_through(fn, params, fname):
    body = fn.child_by_field_name("body")
    if body is None:
        return False
    kids = [c for c in body.named_children if c.type not in ("line_comment", "block_comment")]
    if len(kids) != 1:
        return False
    e = _unwrap_expr(kids[0])
    if e is None or e.type != "call_expression":
        return False
    f = e.child_by_field_name("function")
    args = e.child_by_field_name("arguments")
    arg_ids = []
    for a in args.named_children:
        if a.type in ("line_comment", "block_comment"):
            continue
        i = _arg_ident(a)
        if i is None:
            return False
        arg_ids.append(i)
    plain = set(params)
    if not all(a in plain or a == "self" for a in arg_ids):
        return False
    if not plain.issubset(set(arg_ids)):
        return False
    if f.type == "field_expression":  # method call
        recv = f.child_by_field_name("value")
        mname = _t(f.child_by_field_name("field"))
        recv_ok = _is_self_chain(recv) or (recv.type == "identifier" and _t(recv) in plain)
        return recv_ok and (len(arg_ids) >= 1 or mname == fname) and mname != "clone"
    if f.type in ("identifier", "scoped_identifier"):
        callee = _t(f.child_by_field_name("name")) if f.type == "scoped_identifier" else _t(f)
        return len(arg_ids) >= 1 and callee != fname
    return False


def _allow_marker(lines, line):
    cand = [lines[i] for i in (line - 2, line - 1) if 0 <= i < len(lines)]
    return any("oei: allow" in c for c in cand)


def analyze(rel, text):
    """Parse one Rust file. Returns a facts dict (no paths other than rel)."""
    lines = text.split("\n")
    src = text.encode("utf-8")
    tree = _PARSER.parse(src)
    root = tree.root_node
    facts = {"file": rel, "items": [], "impls": [], "uses": [], "mods": [], "fns": [], "markers": [],
             "idents": Counter(), "test_ranges": [], "parse_errors": bool(root.has_error)}

    def in_test_range(line):
        return any(a <= line <= b for a, b in facts["test_ranges"])

    # identifier counts + macro markers: single full traversal
    stack = [root]
    while stack:
        n = stack.pop()
        if n.type in IDENT_TYPES:
            facts["idents"][_t(n)] += 1
        elif n.type == "macro_invocation":
            m = n.child_by_field_name("macro")
            if m is not None and _last_ident(m) in ("todo", "unimplemented"):
                facts["markers"].append({"kind": "todo_macro", "line": n.start_point[0] + 1})
        elif n.type == "inner_attribute_item" and re.search(r"allow\(.*(dead_code|unused)", _t(n)):
            facts["markers"].append({"kind": "allow_unused", "line": n.start_point[0] + 1})
        stack.extend(n.children)

    def walk(container, inline, in_test, in_trait_impl):
        pending = []
        for n in container.children:
            if n.type == "attribute_item":
                pending.append(_t(n))
                continue
            if n.type in ("line_comment", "block_comment"):
                continue
            attrs, pending = pending, []
            attr_txt = " ".join(attrs)
            line = n.start_point[0] + 1
            end = n.end_point[0] + 1
            is_test = in_test or bool(re.search(r"cfg\(\s*test|#\[\s*(\w+::)*test\b|\btest\]", attr_txt))
            if is_test and not in_test:
                facts["test_ranges"].append((line, end))
            if re.search(r"allow\(.*(dead_code|unused)", attr_txt) and not is_test:
                facts["markers"].append({"kind": "allow_unused", "line": line})
            t = n.type
            if t == "use_declaration":
                arg = n.child_by_field_name("argument")
                for segs, glob in flatten_use(arg):
                    facts["uses"].append({"path": list(segs), "glob": glob, "reexport": _has_vis(n),
                                          "inline": list(inline), "test": is_test, "line": line})
            elif t == "mod_item":
                name = _t(n.child_by_field_name("name"))
                body = n.child_by_field_name("body")
                pm = re.search(r'path\s*=\s*"([^"]+)"', attr_txt)
                facts["mods"].append({"name": name, "inline": list(inline), "body": body is not None,
                                      "path": pm.group(1) if pm else None, "test": is_test})
                if body is not None:
                    walk(body, inline + (name,), is_test, False)
            elif t == "impl_item":
                tr = n.child_by_field_name("trait")
                ty = n.child_by_field_name("type")
                gc, wc = _generic_count(n)
                facts["impls"].append({"trait": _last_ident(tr) if tr is not None else None,
                                       "type": _last_ident(ty), "line": line, "test": is_test,
                                       "generics": gc, "where": wc})
                body = n.child_by_field_name("body")
                if body is not None:
                    walk(body, inline, is_test, tr is not None)
            elif t in ITEM_KINDS:
                kind = ITEM_KINDS[t]
                nm = n.child_by_field_name("name")
                name = _t(nm) if nm is not None else ""
                gc, wc = _generic_count(n)
                it = {"kind": kind, "name": name, "line": line, "pub": _is_pub(n), "test": is_test,
                      "generics": gc, "where": wc, "top": not in_trait_impl and container.parent is not None
                      and container.parent.type in ("mod_item",) or container.type == "source_file",
                      "allow": _allow_marker(lines, line)}
                facts["items"].append(it)
                if t == "trait_item":
                    body = n.child_by_field_name("body")
                    if body is not None:
                        walk(body, inline, is_test, False)
                if t == "function_item":
                    params = []
                    has_self = False
                    underscore = 0
                    ps = n.child_by_field_name("parameters")
                    for p in (ps.named_children if ps is not None else []):
                        if p.type == "self_parameter":
                            has_self = True
                        elif p.type == "parameter":
                            pat = p.child_by_field_name("pattern")
                            if pat is not None and pat.type == "identifier":
                                params.append(_t(pat))
                                if _t(pat).startswith("_") and len(_t(pat)) > 1:
                                    underscore += 1
                            elif pat is not None and pat.type == "mut_pattern":
                                ids = [c for c in pat.named_children if c.type == "identifier"]
                                if ids:
                                    params.append(_t(ids[0]))
                    has_body = n.child_by_field_name("body") is not None
                    pt = (has_body and not in_trait_impl and not is_test and name != "main"
                          and (params or has_self) and _pass_through(n, params, name))
                    facts["fns"].append({"name": name, "line": line, "end_line": end, "has_self": has_self,
                                         "nparams": len(params), "test": is_test,
                                         "trait_impl": in_trait_impl, "has_body": has_body,
                                         "pass_through": bool(pt)})
                    if underscore and not in_trait_impl and not is_test:
                        for _ in range(underscore):
                            facts["markers"].append({"kind": "underscore_param", "line": line})
            elif t == "macro_definition":
                facts["items"].append({"kind": "macro", "name": _t(n.child_by_field_name("name")),
                                       "line": line, "pub": False, "test": is_test, "generics": 0,
                                       "where": 0, "top": True, "allow": False})

    walk(root, (), False, False)
    # markers that landed in test ranges are dropped
    facts["markers"] = [m for m in facts["markers"] if not in_test_range(m["line"])]
    facts["idents"] = dict(facts["idents"])
    return facts
