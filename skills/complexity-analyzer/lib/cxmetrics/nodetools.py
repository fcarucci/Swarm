"""JS/TS tool adapters, all run from the persistent npm prefix (cache tools/complexity-analyzer/node, see bin/install_tools.py).

* ESLint 9 + eslint-plugin-sonarjs + @typescript-eslint/parser: per-function SonarJS cognitive complexity,
  cyclomatic complexity (ESLint `complexity`), parameter count and exported names (js/cx-eslint.config.mjs).
* dependency-cruiser (JSON): module graph with tsconfig path aliases, re-exports and type-only imports.
* knip (JSON): unused exports, unused exported types and unused files (OEI).
* jscpd is driven by dup.py from the same prefix.

Fallback when node or the pinned packages are missing: the caller keeps the regex import graph (jssrc /
coupling.js_edges) and rust-code-analysis functions, and the report header says "degraded". Parsers are pure
functions over the tools' JSON so they can be unit tested without node.
"""
import json
import os
import posixpath
import re
import shutil
import subprocess
from pathlib import Path

from .core import SKILL_DIR, norm, tmp_base, tools_dir

NODE_DIR = tools_dir() / "node"
CONFIG_SRC = SKILL_DIR / "js"
NO_NODE_REASON = "node not found on PATH"
JS_EXT = (".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts")


def _manifest():
    return json.loads((SKILL_DIR / "tool-manifest.json").read_text(encoding="utf-8"))


def pkg_version(name):
    try:
        return json.loads((NODE_DIR / "node_modules" / name / "package.json").read_text(encoding="utf-8"))["version"]
    except Exception:
        return None


def available():
    """(ok, info): node on PATH and every pinned package installed at its pinned version."""
    node = shutil.which("node")
    if not node:
        return False, NO_NODE_REASON
    man = _manifest()
    try:  # eslint 9, knip and dependency-cruiser need a recent node (see node-install.min_node in the manifest)
        out = subprocess.run([node, "--version"], capture_output=True, timeout=30).stdout.decode().strip().lstrip("v")
        have = tuple(int(x) for x in out.split(".")[:2])
        need = tuple(int(x) for x in man["node-install"]["min_node"].split(".")[:2])
        if have < need:
            return False, f"node {out} is older than the required {man['node-install']['min_node']}"
    except (OSError, ValueError, subprocess.SubprocessError):
        return False, "node --version failed"
    pins = man["node-packages"]
    bad = [f"{k} {pkg_version(k)} != {v}" for k, v in sorted(pins.items()) if pkg_version(k) != v]
    if bad:
        return False, "pinned node packages missing (run bin/install_tools.py): " + "; ".join(bad)
    return True, {"node": node, "versions": dict(sorted(pins.items()))}


def scratch_env():
    """Environment for node subprocesses: temp files go to tmp_base() (never RAM /tmp); jiti, which knip uses to load
    the project's TS/ESM config files, would otherwise cache under os.tmpdir()."""
    env = dict(os.environ)
    scratch = tmp_base()
    env.update({"TMPDIR": scratch, "TEMP": scratch, "TMP": scratch, "JITI_FS_CACHE": "false", "JITI_CACHE": "false"})
    return env


def _node(args, cwd, timeout=1800):
    node = shutil.which("node")
    r = subprocess.run([node] + [str(a) for a in args], cwd=str(cwd), capture_output=True, timeout=timeout,
                       env=scratch_env())
    return r.returncode, r.stdout.decode("utf-8", "replace"), r.stderr.decode("utf-8", "replace")


def _bin(pkg, rel):
    return NODE_DIR / "node_modules" / pkg / rel


def ensure_eslint_config():
    """The config must sit next to node_modules so its imports resolve; copy it when stale."""
    src = CONFIG_SRC / "cx-eslint.config.mjs"
    dst = NODE_DIR / "cx-eslint.config.mjs"
    if not dst.exists() or dst.read_bytes() != src.read_bytes():
        shutil.copyfile(src, dst)
    return dst


# ------------------------------------------------------------------ ESLint
def _contains(f, line, col):
    return (f["sl"], f["sc"]) <= (line, col) <= (f["el"], f["ec"])


def parse_eslint(results, mapping):
    """ESLint -f json results -> {rel: {"functions": [...], "exports": [...], "error": str|None}}.

    results: list of {filePath, messages}; mapping: stage basename -> rel. Every function-like node is reported
    by rule cx/fn (JSON message); `complexity` and `sonarjs/cognitive-complexity` messages (threshold 0) are
    attributed to the innermost function whose span contains the message position."""
    out = {}
    for res in sorted(results, key=lambda r: r["filePath"]):
        base = res["filePath"].replace("\\", "/").rsplit("/", 1)[-1]
        rel = mapping.get(base)
        if rel is None:
            continue
        fns, exports, cog, cyc, error = [], [], [], [], None
        for m in res["messages"]:
            rule = m.get("ruleId")
            if rule is None:
                if m.get("fatal"):
                    error = m["message"]
            elif rule == "cx/fn":
                fns.append(json.loads(m["message"]))
            elif rule == "cx/exports":
                exports += json.loads(m["message"])["exports"]
            elif rule == "complexity":
                cyc.append((m["line"], m["column"], _num(m["message"], _CYC_RE)))
            elif rule == "sonarjs/cognitive-complexity":
                cog.append((m["line"], m["column"], _num(m["message"], _COG_RE)))
        fns.sort(key=lambda f: (f["sl"], f["sc"], -f["el"], -f["ec"], f["name"]))
        for f in fns:
            f["cog"], f["cyc"] = 0, 1

        def owner(line, col):
            best = None
            for f in fns:
                if _contains(f, line, col) and (best is None or (f["sl"], f["sc"]) >= (best["sl"], best["sc"])):
                    best = f
            return best

        for line, col, v in cyc:
            f = owner(line, col)
            if f:
                f["cyc"] = v
        for line, col, v in cog:
            f = owner(line, col)
            if f:
                f["cog"] = v
        out[rel] = {"functions": fns, "exports": sorted(set(exports)), "error": error}
    return out


_COG_RE = re.compile(r"Cognitive Complexity from (\d+)")
_CYC_RE = re.compile(r"complexity of (\d+)")


def _num(msg, rx):
    m = rx.search(msg)
    if not m:  # a plugin message format change must not silently read as complexity 0
        raise ValueError("unrecognised ESLint message: " + msg[:100])
    return int(m.group(1))


def build_units(fns, lines=None):
    """Raw cx/fn records of one file -> units, in the schema-2 convention of funcs.py: every named function
    (incl. methods and `const f = () => ...`) is a function unit, every anonymous callback is a closure unit with
    its own cognitive complexity (SonarJS already reports each function on its own). Cyclomatic of a named
    function includes the decisions of its anonymous callbacks (not of nested named functions).
    Returns [{"kind": "fn"|"closure", name, line, end_line, cognitive, cyclomatic, nargs, ploc, own_ploc, parent}]."""
    fns = sorted(fns, key=lambda f: (f["sl"], f["sc"], -f["el"], -f["ec"]))

    def encl(f):
        best = None
        for g in fns:
            if g is f:
                continue
            if (g["sl"], g["sc"]) <= (f["sl"], f["sc"]) and (f["el"], f["ec"]) <= (g["el"], g["ec"]):
                if best is None or (g["sl"], g["sc"]) >= (best["sl"], best["sc"]):
                    best = g
        return best

    parent = {id(f): encl(f) for f in fns}

    def qual(f):
        return (f["cls"] + "." if f.get("cls") else "") + f["name"]

    def named_owner(f):
        g = parent[id(f)]
        while g is not None and g["anon"]:
            g = parent[id(g)]
        return g

    cyc = {id(f): f["cyc"] for f in fns}
    for f in fns:
        if f["anon"]:
            g = named_owner(f)
            if g is not None:
                cyc[id(g)] += f["cyc"] - 1
    ploc = {id(f): _sloc(lines, f["sl"], f["el"]) for f in fns}
    children = {id(f): [] for f in fns}
    tops = []
    for f in fns:  # fns is in source order, so children lists are too
        g = parent[id(f)]
        (children[id(g)] if g is not None else tops).append(f)
    own = {}

    def allocate(f, budget):
        """Exact partition of code lines over the unit tree (same rule as funcs._allocate for rca spaces): a child
        gets its ploc minus 1 if it starts on the line where the previous sibling ends, capped by what is left."""
        used, prev_end = 0, None
        for c in children[id(f)]:
            p = ploc[id(c)] - (1 if prev_end is not None and c["sl"] <= prev_end else 0)
            a = max(0, min(p, budget - used))
            used += a
            allocate(c, a)
            prev_end = max(prev_end or 0, c["el"])
        own[id(f)] = budget - used

    for f in tops:
        allocate(f, ploc[id(f)])

    def unit_ploc(f):  # own lines plus those of closures (any depth), excluding nested named functions
        return own[id(f)] + sum(unit_ploc(c) for c in children[id(f)] if c["anon"])

    seen = {}
    out = []
    for f in fns:
        if f["anon"]:
            g = named_owner(f)
            owner = qual(g) if g is not None else "<top>"
            seen[f["sl"]] = seen.get(f["sl"], 0) + 1
            tag = f"{f['sl']}" if seen[f["sl"]] == 1 else f"{f['sl']}#{seen[f['sl']]}"
            out.append({"kind": "closure", "name": f"{owner}::<closure@{tag}>", "line": f["sl"], "end_line": f["el"],
                        "cognitive": f["cog"], "cyclomatic": f["cyc"], "nargs": f["params"], "ploc": ploc[id(f)],
                        "own_ploc": own[id(f)], "parent": None if g is None else qual(g)})
        else:
            out.append({"kind": "fn", "name": qual(f), "line": f["sl"], "end_line": f["el"], "cognitive": f["cog"],
                        "cyclomatic": cyc[id(f)], "nargs": f["params"], "ploc": ploc[id(f)], "own_ploc": own[id(f)],
                        "unit_ploc": unit_ploc(f), "parent": None})
    return out


def _sloc(lines, a, b):
    """Code lines (non-blank, not comment-only) in [a, b]; rust-code-analysis calls this ploc."""
    if not lines:
        return b - a + 1
    n = 0
    for ln in lines[a - 1:b]:
        s = ln.strip()
        if s and not s.startswith(("//", "/*", "*", "*/")):
            n += 1
    return n


def run_eslint(work):
    """ESLint over work/stage (single process). Returns (results, error)."""
    cfg = ensure_eslint_config()
    out = Path(work) / "eslint.json"
    # ESLint errors on a pattern that matches no file, so only name directories that hold JS/TS (stage also has .py/.rs)
    dirs = [d + "/*" for d in ("stage/prod", "stage/test")
            if any(p.suffix in JS_EXT for p in (Path(work) / d).glob("*"))]
    try:
        rc, so, se = _node([_bin("eslint", "bin/eslint.js"), "--no-ignore", "--no-warn-ignored", "-c", cfg, "-f", "json",
                            "-o", out] + dirs, work)
        if not out.exists():
            return None, "eslint produced no report: " + (se.strip()[-300:] or so.strip()[-300:])
        return json.loads(out.read_text(encoding="utf-8")), None
    except subprocess.TimeoutExpired:
        return None, "eslint timed out"
    except (ValueError, OSError) as exc:
        return None, f"eslint output unusable ({type(exc).__name__})"


# ------------------------------------------------------------------ dependency-cruiser
def parse_depcruise(obj, known):
    """dependency-cruiser JSON -> (edges {src: sorted [dst]}, stats). Only edges between files in `known`
    count (known = production and test JS/TS files discovered by the pipeline, repo-relative)."""
    edges, flags = {}, {"edges": 0, "type_only": 0, "reexport": 0, "aliased": 0, "dynamic": 0, "unresolved": 0,
                        "circular": 0}
    per_pair = {}
    for m in sorted(obj["modules"], key=lambda x: x["source"]):
        src = norm(m["source"])
        if src not in known:
            continue
        for d in m.get("dependencies", []):
            if d.get("couldNotResolve"):
                flags["unresolved"] += 1
                continue
            dst = norm(d.get("resolved", ""))
            if dst not in known or dst == src:
                continue
            t = set(d.get("dependencyTypes", []))
            rec = per_pair.setdefault((src, dst), {"type_only": True, "reexport": False, "aliased": False,
                                                   "dynamic": False, "circular": False})
            rec["type_only"] &= "type-only" in t
            rec["reexport"] |= "export" in t
            rec["aliased"] |= "aliased" in t
            rec["dynamic"] |= "dynamic-import" in t
            rec["circular"] |= bool(d.get("circular"))
    for (src, dst), r in sorted(per_pair.items()):
        edges.setdefault(src, []).append(dst)
        flags["edges"] += 1
        for k in ("type_only", "reexport", "aliased", "dynamic", "circular"):
            flags[k] += 1 if r[k] else 0
    return {k: sorted(v) for k, v in edges.items()}, flags


def load_jsonc(path):
    """tsconfig files allow comments and trailing commas; strip them outside of strings."""
    text = Path(path).read_text(encoding="utf-8-sig", errors="replace")
    out, i, n, in_str = [], 0, len(text), False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 1
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
            out.append(c)
        elif text.startswith("//", i):
            while i < n and text[i] != "\n":
                i += 1
            continue
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        else:
            out.append(c)
        i += 1
    return json.loads(re.sub(r",(\s*[}\]])", r"\1", "".join(out)))


def _solution_style(root, rel):
    """A solution-style tsconfig (files: [] + references) has no `paths`: use the first referenced config that
    does, because dependency-cruiser takes a single tsconfig and does not follow references."""
    try:
        cfg = load_jsonc(Path(root) / rel)
        if "paths" in cfg.get("compilerOptions", {}) or not cfg.get("references"):
            return rel
        base = posixpath.dirname(rel)
        for ref in sorted(r["path"] for r in cfg["references"] if "path" in r):
            cand = posixpath.normpath(posixpath.join(base, ref))
            if not cand.endswith(".json"):
                cand = posixpath.join(cand, "tsconfig.json")
            try:
                if "paths" in load_jsonc(Path(root) / cand).get("compilerOptions", {}):
                    return cand
            except (OSError, ValueError):
                continue
    except (OSError, ValueError):
        pass
    return rel


def pick_tsconfig(root, files):
    """Root tsconfig.json, else the shallowest one above the JS/TS sources (documented limitation: one config
    per run). Solution-style configs are resolved to the referenced config that defines `paths`."""
    root = Path(root)
    if (root / "tsconfig.json").is_file():
        return _solution_style(root, "tsconfig.json")
    dirs = set()
    for f in files:
        d = posixpath.dirname(f)
        while d:
            dirs.add(d)
            d = posixpath.dirname(d)
    for d in sorted(dirs, key=lambda d: (d.count("/"), d)):
        if (root / d / "tsconfig.json").is_file():
            return _solution_style(root, d + "/tsconfig.json")
    return None


def run_depcruise(root, files):
    """Module graph of the real repo tree (read only, no cache). Returns (obj, error, tsconfig)."""
    roots = sorted({f.split("/")[0] if "/" in f else f for f in files})
    tsconfig = pick_tsconfig(root, files)
    args = [_bin("dependency-cruiser", "bin/dependency-cruise.mjs"), "--config", CONFIG_SRC / "cx-depcruise.json",
            "--no-cache", "-T", "json"]
    if tsconfig:
        args += ["--ts-config", tsconfig]
    try:
        rc, so, se = _node(args + roots, root)
    except subprocess.TimeoutExpired:
        return None, "dependency-cruiser timed out", tsconfig
    except OSError as exc:
        return None, f"dependency-cruiser could not start ({type(exc).__name__})", tsconfig
    try:
        return json.loads(so), None, tsconfig
    except ValueError:
        return None, "dependency-cruiser failed: " + (se.strip()[-300:] or so.strip()[-300:] or f"rc={rc}"), tsconfig


# ------------------------------------------------------------------ knip
def parse_knip(obj):
    """knip --reporter json -> {"files": sorted unused files, "exports": {file: [names]}, "types": {file: [names]}}."""
    res = {"files": sorted(norm(f) for f in obj.get("files", [])), "exports": {}, "types": {}}
    for iss in obj.get("issues", []):
        f = norm(iss["file"])
        for key in ("exports", "types"):
            names = sorted({x["name"] for x in iss.get(key, [])})
            if names:
                res[key][f] = names
    return res


def run_knip(root):
    """knip needs the repository's package.json; returns (parsed|None, reason)."""
    if not (Path(root) / "package.json").is_file():
        return None, "no package.json at the repository root"
    try:
        rc, so, se = _node([_bin("knip", "bin/knip.js"), "--reporter", "json", "--no-progress", "--no-config-hints",
                            "--no-exit-code"], root)
    except subprocess.TimeoutExpired:
        return None, "knip timed out"
    except OSError as exc:
        return None, f"knip could not start ({type(exc).__name__})"
    try:
        return parse_knip(json.loads(so)), None
    except (ValueError, KeyError):
        return None, "knip failed: " + (se.strip()[-300:] or so.strip()[-300:] or f"rc={rc}")


# ------------------------------------------------------------------ orchestration
def run(root, work, files, mapping):
    """files: [{rel, lang, test}] JS/TS only. Returns dict(available, reason, functions{rel:[..]},
    exports{rel:[..]}, edges, edge_stats, knip, knip_reason, tsconfig, versions, errors[])."""
    ok, info = available()
    if not ok:
        return {"available": False, "reason": info}
    res = {"available": True, "versions": info["versions"], "errors": []}
    tests = {f["rel"]: f["test"] for f in files}
    results, err = run_eslint(work)
    if err:
        return {"available": False, "reason": err}
    try:
        parsed = parse_eslint(results, mapping)
    except (ValueError, KeyError) as exc:
        return {"available": False, "reason": f"eslint output not understood ({str(exc)[:100]})"}
    res["functions"], res["exports"] = {}, {}
    stage_of = {rel: name for name, rel in mapping.items()}
    for rel, p in sorted(parsed.items()):
        if p["error"]:
            res["errors"].append(f"{rel} (eslint parse error: {p['error']})")
            continue
        sp = Path(work) / "stage" / ("test" if tests.get(rel) else "prod") / stage_of[rel]
        lines = sp.read_text(encoding="utf-8", errors="replace").split("\n") if sp.exists() else None
        res["functions"][rel] = build_units(p["functions"], lines)
        res["exports"][rel] = p["exports"]
    rels = sorted(f["rel"] for f in files)
    obj, err, tsconfig = run_depcruise(root, rels)
    res["tsconfig"] = tsconfig
    if obj is None:
        res["edges"], res["edge_stats"], res["graph_reason"] = None, None, err
    else:
        try:
            res["edges"], res["edge_stats"] = parse_depcruise(obj, set(rels))
            res["graph_reason"] = None
        except (KeyError, TypeError):
            res["edges"], res["edge_stats"], res["graph_reason"] = None, None, "dependency-cruiser output not understood"
    res["knip"], res["knip_reason"] = run_knip(root)
    return res
