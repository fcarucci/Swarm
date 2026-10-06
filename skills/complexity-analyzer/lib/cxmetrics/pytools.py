"""Python tool adapters: radon (cc, mi, raw, Halstead) and vulture.

Roles (see docs/validation-langtools.md):
* radon cc      -> the Python function inventory and per-function cyclomatic complexity.
* radon hal/mi/raw -> per-file Halstead totals, maintainability index and raw LOC (report columns).
* rust-code-analysis -> kept as cross-check, and as the source of the Sonar-scale cognitive complexity,
  SLOC and the Visual Studio MI of each function (radon has no cognitive complexity; `radon hal -f -j`
  keys functions by bare name, so same-named methods collide and per-function Halstead is not used).
* vulture       -> unused functions/classes/methods/properties (confidence >= VULTURE_MIN_CONFIDENCE) for OEI.

The parsers are pure functions over the tools' JSON/text output so they can be unit tested.
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from .core import rnd

VULTURE_MIN_CONFIDENCE = 60
# Methods and properties are reported (dead_code) but kept out of the OEI ratio: vulture sees no dynamic
# dispatch, so overrides called by frameworks (do_GET, run, __enter__ users, ...) are mostly false positives.
VULTURE_KINDS_IN_RATIO = ("function", "class")
_VULTURE_RE = re.compile(r"^(?P<file>.+?):(?P<line>\d+): (?P<msg>.+?) \((?P<conf>\d+)% confidence(?:, \d+ lines?)?\)\s*$")
_VULTURE_MSG = re.compile(r"^unused (?P<kind>[a-z ]+?) '(?P<name>[^']+)'$")


def _run(args, cwd, timeout=900):
    env = dict(os.environ, PYTHONUTF8="1")  # same decoding of sources on Windows and POSIX
    r = subprocess.run([sys.executable, "-m"] + args, cwd=str(cwd), capture_output=True, timeout=timeout, env=env)
    return r.returncode, r.stdout.decode("utf-8", "replace"), r.stderr.decode("utf-8", "replace")


def available():
    """(ok, reason): radon and vulture importable in the running interpreter."""
    try:
        import importlib.metadata as md
        return True, {"radon": md.version("radon"), "vulture": md.version("vulture")}
    except Exception as exc:  # PackageNotFoundError or broken metadata
        return False, f"radon/vulture not importable ({exc}); run through bin/metrics (uv run --with) or install_tools.py --venv"


# ------------------------------------------------------------------ parsers
def parse_radon_cc(obj):
    """radon cc -j output -> {basename: {"error": str} | {"blocks": [..sorted..]}}.

    radon lists methods twice (inside their class and again at top level) and nests closures, so blocks are
    flattened, classes dropped and duplicates removed by (lineno, name)."""
    out = {}
    for path, items in obj.items():
        key = path.replace("\\", "/").rsplit("/", 1)[-1]
        if isinstance(items, dict):  # {"error": "..."}
            out[key] = {"error": str(items.get("error", "unparsable"))}
            continue
        seen = {}

        def visit(b, cls=""):
            if b.get("type") == "class":
                for m in b.get("methods", []):
                    visit(m, b["name"])
                return
            c = b.get("classname") or cls
            rec = {"name": (c + "." if c else "") + b["name"], "line": int(b["lineno"]), "end_line": int(b["endline"]),
                   "cyclomatic": int(b["complexity"])}
            seen.setdefault((rec["line"], rec["name"]), rec)
            for cl in b.get("closures", []):
                visit(cl, "")

        for b in items:
            visit(b)
        out[key] = {"blocks": [seen[k] for k in sorted(seen)]}
    return out


def parse_radon_file_metrics(mi_obj, hal_obj, raw_obj):
    """radon mi/hal/raw -j -> {basename: {mi, volume, difficulty, effort, sloc, comments}} (rounded)."""
    def k(p):
        return p.replace("\\", "/").rsplit("/", 1)[-1]

    out = {}
    for p, v in mi_obj.items():
        if isinstance(v, dict) and "mi" in v:
            out.setdefault(k(p), {})["mi"] = rnd(v["mi"], 2)
    for p, v in hal_obj.items():
        t = v.get("total") if isinstance(v, dict) else None
        if t:
            d = out.setdefault(k(p), {})
            d["volume"], d["difficulty"], d["effort"] = rnd(t["volume"], 2), rnd(t["difficulty"], 2), rnd(t["effort"], 1)
    for p, v in raw_obj.items():
        if isinstance(v, dict) and "sloc" in v:
            d = out.setdefault(k(p), {})
            d["sloc"], d["comments"] = int(v["sloc"]), int(v["comments"])
    return out


def parse_vulture(text):
    """vulture text output -> sorted [{file, line, kind, name, confidence}] (unreachable code etc. get kind 'other')."""
    items = []
    for ln in text.splitlines():
        m = _VULTURE_RE.match(ln.strip())
        if not m:
            continue
        mm = _VULTURE_MSG.match(m["msg"])
        kind, name = (mm["kind"], mm["name"]) if mm else ("other", m["msg"])
        items.append({"file": m["file"].replace("\\", "/"), "line": int(m["line"]), "kind": kind, "name": name,
                      "confidence": int(m["conf"])})
    items.sort(key=lambda x: (x["file"], x["line"], x["kind"], x["name"]))
    return items


# ------------------------------------------------------------------ runners
def run(work, mapping):
    """Never raises: timeouts, undecodable tool output and OS errors come back as available=False with a reason."""
    try:
        return _run_tools(work, mapping)
    except subprocess.TimeoutExpired as exc:
        why = f"{exc.cmd[2] if len(exc.cmd) > 2 else 'tool'} timed out"
    except (ValueError, OSError, KeyError) as exc:
        why = f"unusable tool output ({type(exc).__name__}: {str(exc)[:120]})"
    return {"available": False, "reason": why, "functions": {}, "files": {}, "vulture": [], "errors": []}


def _run_tools(work, mapping):
    """Run radon + vulture over work/stage/{prod,test}. mapping: stage name -> repo-relative path.

    Returns {"available": bool, "versions": {...}, "functions": {rel: {"blocks"|"error"}}, "files": {rel: {...}},
             "vulture": [{file: rel, ...}], "reason": str}. Everything single-threaded, sorted."""
    ok, info = available()
    if not ok:
        return {"available": False, "reason": info, "functions": {}, "files": {}, "vulture": []}
    dirs = [d for d in ("stage/prod", "stage/test") if any((Path(work) / d).glob("*.py"))]
    if not dirs:
        return {"available": True, "versions": info, "functions": {}, "files": {}, "vulture": []}
    outs = {}
    for sub in ("cc", "mi", "hal", "raw"):
        rc, so, se = _run(["radon", sub, "-j"] + dirs, work)
        if rc != 0:
            return {"available": False, "reason": f"radon {sub} failed: {se.strip()[-200:]}",
                    "functions": {}, "files": {}, "vulture": []}
        outs[sub] = json.loads(so or "{}")
    # vulture exits 3 when it finds something, 0 when clean, 1 when some file could not be parsed (the other files
    # are still analysed); anything else is a tool failure
    rc, so, se = _run(["vulture"] + dirs + ["--min-confidence", str(VULTURE_MIN_CONFIDENCE)], work)
    if rc not in (0, 1, 3):
        return {"available": False, "reason": f"vulture failed: {se.strip()[-200:]}",
                "functions": {}, "files": {}, "vulture": []}
    funcs, errors = {}, []
    for key, v in parse_radon_cc(outs["cc"]).items():
        if key in mapping:
            funcs[mapping[key]] = v
            if "error" in v:
                errors.append(f"{mapping[key]} (radon: {v['error'][:80]})")
    for ln in (so + "\n" + se).splitlines():  # e.g. "stage/prod/f00003.py: invalid syntax at line 3"
        m = re.match(r"^(?P<f>[^:]+\.py):.*(invalid syntax|syntax error|Error)", ln.strip().replace("\\", "/"))
        if m and m["f"].rsplit("/", 1)[-1] in mapping and not _VULTURE_RE.match(ln.strip()):
            errors.append(f"{mapping[m['f'].rsplit('/', 1)[-1]]} (vulture: {re.sub(r'^\d+:\s*', '', ln.split(':', 1)[1].strip())[:80]})")
    files = {}
    for key, v in parse_radon_file_metrics(outs["mi"], outs["hal"], outs["raw"]).items():
        if key in mapping:
            files[mapping[key]] = v
    vult = []
    for it in parse_vulture(so):
        base = it["file"].rsplit("/", 1)[-1]
        if base in mapping:
            it["file"] = mapping[base]
            vult.append(it)
    vult.sort(key=lambda x: (x["file"], x["line"], x["kind"], x["name"]))
    return {"available": True, "versions": info, "functions": funcs, "files": files, "vulture": vult,
            "errors": sorted(set(errors))}
