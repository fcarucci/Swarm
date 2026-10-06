#!/usr/bin/env python3
"""Fetch pinned external tools into the persistent per-user Swarm cache (never system-wide).

Cross-platform (Linux, Windows, macOS). Usage: python3 bin/install_tools.py [--check]
"""
import hashlib
import io
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path

SKILL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL / "lib"))
from cxmetrics.core import tools_dir, tmp_base

TOOLS = tools_dir()


def platform_key():
    sysname = platform.system().lower()
    mach = platform.machine().lower()
    if mach in ("amd64", "x64"):
        mach = "x86_64"
    if mach == "arm64":
        mach = "aarch64"
    return {"linux": "linux", "windows": "windows", "darwin": "macos"}.get(sysname, sysname) + "-" + mach


def sha256_bytes(b):
    return hashlib.sha256(b).hexdigest()


def sha256_file(p):
    return sha256_bytes(Path(p).read_bytes())


def rca_binary_name():
    return "rust-code-analysis-cli.exe" if os.name == "nt" else "rust-code-analysis-cli"


def find_rca():
    """Return the rust-code-analysis-cli path to use, or None. Checks the persistent cache then PATH."""
    local = TOOLS / rca_binary_name()
    if local.exists():
        return local
    on_path = shutil.which("rust-code-analysis-cli")
    return Path(on_path) if on_path else None


def download(url_tag_repo, asset):
    repo, tag = url_tag_repo
    url = f"https://github.com/{repo}/releases/download/{tag}/{asset}"
    try:
        with urllib.request.urlopen(url, timeout=120) as r:
            return r.read()
    except Exception as exc:  # fall back to gh when direct download fails
        gh = shutil.which("gh")
        if not gh:
            raise SystemExit(f"download failed ({exc}) and gh is not available")
        with tempfile.TemporaryDirectory(dir=tmp_base()) as td:
            subprocess.run([gh, "release", "download", tag, "-R", repo, "-p", asset, "-D", td], check=True)
            return (Path(td) / asset).read_bytes()


def install_rca(manifest, check_only=False):
    m = manifest["rust-code-analysis"]
    key = platform_key()
    entry = m["assets"].get(key)
    dest = TOOLS / rca_binary_name()
    if entry:
        if dest.exists() and sha256_file(dest) == entry["binary_sha256"]:
            print(f"rust-code-analysis-cli {m['version']} already installed and verified ({key})")
            return 0
        if check_only:
            print("rust-code-analysis-cli missing or hash mismatch", file=sys.stderr)
            return 1
        data = download((m["repo"], m["tag"]), entry["asset"])
        if sha256_bytes(data) != entry["asset_sha256"]:
            print("asset sha256 mismatch; refusing to install", file=sys.stderr)
            return 1
        if entry["asset"].endswith(".zip"):
            z = zipfile.ZipFile(io.BytesIO(data))
            names = [n for n in z.namelist() if n.endswith(entry["member"])]
            blob = z.read(names[0])
        else:
            t = tarfile.open(fileobj=io.BytesIO(data), mode="r:gz")
            names = [n for n in t.getnames() if n.endswith(entry["member"])]
            blob = t.extractfile(names[0]).read()
        if sha256_bytes(blob) != entry["binary_sha256"]:
            print("binary sha256 mismatch; refusing to install", file=sys.stderr)
            return 1
        TOOLS.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(blob)
        dest.chmod(0o755)
        print(f"installed rust-code-analysis-cli {m['version']} -> {dest} (sha256 ok)")
        return 0
    # no prebuilt asset for this platform
    existing = shutil.which("rust-code-analysis-cli")
    if existing:
        print(f"using rust-code-analysis-cli from PATH: {existing} (version not hash-pinned)")
        return 0
    cargo = shutil.which("cargo")
    if not cargo:
        print(f"No prebuilt rust-code-analysis-cli for {key}. Install Rust (https://rustup.rs) and re-run, or run:\n"
              f"  cargo install rust-code-analysis-cli --version {m['version']} --locked", file=sys.stderr)
        return 1
    if check_only:
        return 1
    TOOLS.mkdir(parents=True, exist_ok=True)
    root = TOOLS / "cargo-root"
    subprocess.run([cargo, "install", "rust-code-analysis-cli", "--version", m["version"], "--locked",
                    "--root", str(root)], check=True)
    built = root / "bin" / rca_binary_name()
    shutil.copy2(built, dest)
    print(f"built rust-code-analysis-cli {m['version']} via cargo -> {dest} (not hash-pinned)")
    return 0


NODE_DIR = TOOLS / "node"


def node_bin(name):
    """Path of a binary in the persistent npm prefix (npm writes .cmd shims on Windows)."""
    return NODE_DIR / "node_modules" / ".bin" / (name + (".cmd" if os.name == "nt" else ""))


def node_pkg_version(name):
    p = NODE_DIR / "node_modules" / name / "package.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))["version"]
    except Exception:
        return None


def node_tools_state(manifest):
    """Return (ok, problems): node + npm present and every pinned package installed at the pinned version."""
    problems = []
    node = shutil.which("node")
    if not node:
        problems.append("node not found on PATH")
    else:
        try:
            have = tuple(int(x) for x in subprocess.run([node, "--version"], capture_output=True).stdout.decode().strip().lstrip("v").split(".")[:2])
            need = tuple(int(x) for x in manifest["node-install"]["min_node"].split(".")[:2])
            if have < need:
                problems.append(f"node {'.'.join(map(str, have))} is older than the required {manifest['node-install']['min_node']}")
        except (OSError, ValueError):
            problems.append("node --version failed")
    for pkg, ver in sorted(manifest["node-packages"].items()):
        got = node_pkg_version(pkg)
        if got != ver:
            problems.append(f"{pkg}: installed {got}, pinned {ver}")
    return not problems, problems


def check_manifest_consistency(manifest):
    """node-tools/package.json must pin exactly node-packages."""
    pj = json.loads((SKILL / manifest["node-install"]["dir"] / "package.json").read_text(encoding="utf-8"))
    return pj["dependencies"] == manifest["node-packages"]


def install_node(manifest, check_only=False):
    ok, problems = node_tools_state(manifest)
    if ok:
        print("node tools already installed at pinned versions: " +
              ", ".join(f"{k} {v}" for k, v in sorted(manifest["node-packages"].items())))
        return 0
    if check_only:
        print("NODE TOOLS NOT READY (JS/TS analysis and jscpd will run degraded): " + "; ".join(problems) +
              "\n  fix: python bin/install_tools.py", file=sys.stderr)
        return 1
    node, npm = shutil.which("node"), shutil.which("npm")
    if not node or not npm:
        print("node/npm not found: JS/TS analysis will run in degraded mode (regex import graph, rust-code-analysis "
              "functions, no knip/dependency-cruiser/jscpd). Install Node >= " + manifest["node-install"]["min_node"] +
              " and re-run to enable it.", file=sys.stderr)
        return 1
    if not check_manifest_consistency(manifest):
        print("node-tools/package.json does not match tool-manifest.json node-packages", file=sys.stderr)
        return 1
    NODE_DIR.mkdir(parents=True, exist_ok=True)
    src = SKILL / manifest["node-install"]["dir"]
    for f in ("package.json", "package-lock.json"):
        shutil.copy2(src / f, NODE_DIR / f)
    r = subprocess.run([npm, "ci", "--ignore-scripts", "--no-audit", "--no-fund", "--prefix", str(NODE_DIR)],
                       cwd=str(NODE_DIR))
    if r.returncode != 0:
        print("npm ci failed", file=sys.stderr)
        return 1
    ok, problems = node_tools_state(manifest)
    if not ok:
        print("after install: " + "; ".join(problems), file=sys.stderr)
        return 1
    print("installed node tools into " + str(NODE_DIR))
    return 0


def python_tools_state(manifest):
    """Pinned Python packages importable in this interpreter at the pinned versions."""
    import importlib.metadata as md
    problems = []
    for pkg, ver in sorted(manifest["python-packages"].items()):
        try:
            got = md.version(pkg)
        except md.PackageNotFoundError:
            got = None
        if got != ver:
            problems.append(f"{pkg}: installed {got}, pinned {ver}")
    return not problems, problems


def install_venv(manifest):
    """Optional persistent venv (alternative to `uv run --with`): tools/venv with the pinned packages."""
    venv = TOOLS / "venv"
    subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
    py = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    reqs = [f"{k}=={v}" for k, v in sorted(manifest["python-packages"].items())]
    subprocess.run([str(py), "-m", "pip", "install", "--quiet", *reqs], check=True)
    print(f"venv ready: {py}  (run: {py} bin/metrics.py REPO)")
    return 0


def main():
    manifest = json.loads((SKILL / "tool-manifest.json").read_text(encoding="utf-8"))
    check = "--check" in sys.argv
    if "--venv" in sys.argv:
        return install_venv(manifest)
    rc = install_rca(manifest, check)
    if "--no-node" not in sys.argv:
        # node tools are optional: failure leaves rc unchanged and the pipeline degrades visibly
        nrc = install_node(manifest, check)
        if check and nrc:
            rc = rc or nrc
    ok, problems = python_tools_state(manifest)
    if not ok:
        print("python packages not importable here (use bin/metrics, which runs through `uv run --with`, or "
              "`install_tools.py --venv`): " + "; ".join(problems))
    return rc


if __name__ == "__main__":
    sys.exit(main())
