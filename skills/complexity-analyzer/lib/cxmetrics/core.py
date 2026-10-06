"""File discovery, staging, text normalisation and the rust-code-analysis runner.

Everything here is cross-platform: pathlib, no shell, no locale-dependent ordering
(sorting is done in Python on normalised forward-slash strings), utf-8 everywhere.
"""
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent.parent


def tools_dir():
    """Persistent per-user tools, independent of replaceable plugin installations."""
    override = os.environ.get("SWARM_DATA_DIR")
    if override:
        base = Path(override).expanduser()
    elif os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "swarm"
    else:
        base = Path.home() / ".local" / "share" / "swarm"
    return base / "tools" / "complexity-analyzer"


LANG_BY_EXT = {
    ".rs": "rust",
    ".py": "python",
    ".js": "js", ".mjs": "js", ".cjs": "js", ".jsx": "js",
    ".ts": "js", ".tsx": "js",
}
STAGE_EXT = {".mjs": ".js", ".cjs": ".js"}  # rust-code-analysis does not know these
EXCLUDE_DIRS = {".git", "target", "node_modules", ".venv", "venv", "__pycache__", "dist", "build",
                ".tox", ".mypy_cache", ".pytest_cache", "site-packages", ".next", "coverage", ".idea",
                ".vscode", "vendor"}
TEST_DIRS = {"tests", "test", "__tests__", "benches", "bench", "examples", "spec", "specs", "e2e"}


def norm(p):
    return str(p).replace("\\", "/")


def rnd(x, nd=4):
    if x is None:
        return None
    if isinstance(x, float) and (math.isnan(x) or math.isinf(x)):
        return None
    v = round(float(x), nd)
    return 0.0 if v == 0 else v


def read_text(path):
    """utf-8, undecodable bytes replaced, CRLF/CR normalised to LF, BOM dropped."""
    data = Path(path).read_bytes().decode("utf-8", errors="replace")
    if data.startswith("﻿"):
        data = data[1:]
    return data.replace("\r\n", "\n").replace("\r", "\n")


def percentile(values, p):
    """Nearest-rank percentile; deterministic, no interpolation."""
    if not values:
        return 0.0
    s = sorted(values)
    k = max(0, math.ceil(p / 100.0 * len(s)) - 1)
    return float(s[min(k, len(s) - 1)])


def is_test_path(rel):
    parts = rel.split("/")
    name = parts[-1]
    if any(p in TEST_DIRS for p in parts[:-1]):
        return True
    low = name.lower()
    stem = low.rsplit(".", 1)[0]
    return (stem.endswith("_test") or stem.endswith(".test") or stem.endswith(".spec")
            or stem.startswith("test_") or low == "conftest.py" or stem == "tests")


def tmp_base():
    """Scratch parent dir. /tmp can be RAM on some boxes, so prefer /var/tmp when it exists."""
    env = os.environ.get("CXM_TMPDIR")
    if env:
        Path(env).mkdir(parents=True, exist_ok=True)
        return env
    if os.name != "nt" and Path("/var/tmp").is_dir():
        return "/var/tmp"
    return tempfile.gettempdir()


def discover(root, langs, excludes=()):
    """Return sorted list of dicts {rel, lang, test}."""
    root = Path(root)
    rels = []
    if (root / ".git").exists() and shutil.which("git"):
        try:
            out = subprocess.run(["git", "-C", str(root), "ls-files", "-z", "--cached", "--others",
                                  "--exclude-standard"], capture_output=True, check=True).stdout
            rels = [r for r in out.decode("utf-8", "replace").split("\0") if r]
        except Exception:
            rels = []
    if not rels:
        for dp, dns, fns in os.walk(root):
            dns[:] = sorted(d for d in dns if d not in EXCLUDE_DIRS)
            for f in fns:
                rels.append(norm(Path(dp, f).relative_to(root)))
    import fnmatch
    files = []
    for rel in sorted(set(norm(r) for r in rels)):
        parts = rel.split("/")
        if any(p in EXCLUDE_DIRS for p in parts[:-1]):
            continue
        if not (root / rel).is_file():
            continue
        ext = Path(rel).suffix.lower()
        lang = LANG_BY_EXT.get(ext)
        if not lang or (langs and lang not in langs):
            continue
        if rel.endswith((".min.js", ".d.ts")):
            continue
        if any(fnmatch.fnmatch(rel, pat) for pat in excludes):
            continue
        files.append({"rel": rel, "lang": lang, "test": is_test_path(rel)})
    return files


def strip_directives(text):
    """Blank PJSR-style preprocessor lines (#include, #define, ...) keeping line numbers."""
    out = []
    for ln in text.split("\n"):
        s = ln.lstrip()
        out.append("" if s.startswith("#") and not s.startswith("#!") and not s.startswith("#[") else ln)
    return "\n".join(out)


def stage(root, files, stage_dir):
    """Copy files LF-normalised into stage_dir/{prod,test}/fNNNNN.ext. Returns {stage_name: rel}."""
    mapping = {}
    for i, f in enumerate(files):
        ext = Path(f["rel"]).suffix.lower()
        ext = STAGE_EXT.get(ext, ext)
        sub = "test" if f["test"] else "prod"
        name = f"f{i:05d}{ext}"
        d = Path(stage_dir) / sub
        d.mkdir(parents=True, exist_ok=True)
        text = read_text(Path(root) / f["rel"])
        if f["lang"] == "js":
            text = strip_directives(text)
        (d / name).write_bytes(text.encode("utf-8"))
        f["stage"] = name
        mapping[name] = f["rel"]
    return mapping


def find_rca():
    sys.path.insert(0, str(SKILL_DIR / "bin"))
    import install_tools
    return install_tools.find_rca()


def run_rca(work, rca_bin):
    """Run rust-code-analysis-cli on work/stage, JSON per file into work/rca. Returns {stage_name: json}."""
    out = Path(work) / "rca"
    out.mkdir(exist_ok=True)
    cmd = [str(rca_bin), "-m", "-O", "json", "-p", "stage", "-o", str(out), "-j", "4"]
    r = subprocess.run(cmd, cwd=str(work), capture_output=True)
    if r.returncode != 0:
        raise RuntimeError("rust-code-analysis-cli failed: " + r.stderr.decode("utf-8", "replace")[-500:])
    res = {}
    for p in sorted(out.rglob("*.json")):
        name = p.name[:-5]
        res[name] = json.loads(p.read_text(encoding="utf-8"))
    return res


def rca_version(rca_bin):
    try:
        v = subprocess.run([str(rca_bin), "--version"], capture_output=True, check=True).stdout.decode().strip()
        return v.split()[-1]
    except Exception:
        return "unknown"
