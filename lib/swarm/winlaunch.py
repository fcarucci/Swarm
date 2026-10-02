"""The Windows launcher: what bin/swarm.cmd (and bin/swarm under Git Bash) runs. Stdlib only, so
it runs with any Python 3.11+ before the venv exists. It does what the sh launcher bin/swarm does:
keep the venv (outside the plugin, so it survives plugin updates) matching requirements.txt, then
run the swarm package from this plugin in it.

  venv:   %SWARM_VENV%, else %USERPROFILE%\\.local\\share\\swarm\\venv  (interpreter: Scripts\\python.exe)
  stamp:  <venv>\\.swarm-requirements holds a CRC-32 of requirements.txt (line endings normalised)
  lock:   the directory <venv>.building, taken with mkdir (atomic), waited for up to 300 s
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
import zlib
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent.parent
MIN_PY = (3, 11)


def venv_dir() -> Path:
    env = os.environ.get("SWARM_VENV")
    return Path(env) if env else Path(os.path.expanduser("~")) / ".local" / "share" / "swarm" / "venv"


def venv_python(v: Path) -> Path:
    if os.name == "nt":
        for cand in (v / "Scripts" / "python.exe", v / "bin" / "python.exe", v / "bin" / "python"):
            if cand.exists():
                return cand
        return v / "Scripts" / "python.exe"
    return v / "bin" / "python"


def requirements_stamp(root: Path = PLUGIN_ROOT) -> str:
    data = (root / "requirements.txt").read_bytes().replace(b"\r\n", b"\n")
    return str(zlib.crc32(data))


def _stamp_ok(v: Path, want: str) -> bool:
    try:
        return venv_python(v).exists() and (v / ".swarm-requirements").read_text().strip() == want
    except OSError:
        return False


def ensure_venv(v: Path | None = None, root: Path = PLUGIN_ROOT, timeout: int = 300) -> Path:
    """The venv's interpreter, creating or refreshing the venv first when needed. SystemExit(1)
    with a message on stderr when it can't be set up."""
    v = v or venv_dir()
    want = requirements_stamp(root)
    if _stamp_ok(v, want):
        return venv_python(v)
    if sys.version_info < MIN_PY:
        sys.exit(f"swarm: Python {MIN_PY[0]}.{MIN_PY[1]} or newer is needed to set up {v}; this is "
                 f"{sys.version_info[0]}.{sys.version_info[1]} ({sys.executable})")
    v.parent.mkdir(parents=True, exist_ok=True)
    lock = Path(str(v) + ".building")
    waited = 0
    while True:
        try:
            lock.mkdir()
            break
        except FileExistsError:
            waited += 1
            if waited > timeout:
                sys.exit(f"swarm: {lock} is stuck; remove it and retry")
            time.sleep(1)
    try:
        if not _stamp_ok(v, want):
            print(f"swarm: setting up {v} (first run or new requirements)...", file=sys.stderr)
            env = {**os.environ, "PIP_DISABLE_PIP_VERSION_CHECK": "1"}
            ok = venv_python(v).exists() or subprocess.run([sys.executable, "-m", "venv", str(v)]).returncode == 0
            ok = ok and subprocess.run([str(venv_python(v)), "-m", "pip", "install", "-q", "-r",
                                        str(root / "requirements.txt")], env=env,
                                       stdout=sys.stderr).returncode == 0
            if not ok:
                sys.exit(f"swarm: could not set up {v} (need python >= 3.11 with venv and pip)")
            (v / ".swarm-requirements").write_text(want + "\n")
    finally:
        try:
            lock.rmdir()
        except OSError:
            pass
    return venv_python(v)


def run_env(root: Path = PLUGIN_ROOT) -> dict:
    env = dict(os.environ)
    lib = str(root / "lib")
    env["PYTHONPATH"] = lib + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    return env


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    py = ensure_venv()
    try:
        return subprocess.run([str(py), "-B", "-m", "swarm.cli", *argv], env=run_env()).returncode
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
