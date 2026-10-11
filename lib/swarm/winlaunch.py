"""The Windows launcher: what bin/swarm.cmd (and bin/swarm under Git Bash) runs. Stdlib only, so
it runs with any Python 3.11+ before the venv exists. It does what the sh launcher bin/swarm does:
keep the venv (outside the plugin, so it survives plugin updates) matching requirements.txt, then
run the swarm package from this plugin in it.

  venv:   %SWARM_VENV%, else %USERPROFILE%\\.local\\share\\swarm\\venv-<stamp> (one venv per requirements
          stamp, so plugin versions with other requirements do not rebuild it in turn; an old
          install's venv\\ with a matching stamp is adopted)  (interpreter: Scripts\\python.exe)
  stamp:  <venv>\\.swarm-requirements holds a CRC-32 of the requirements files (line endings
          normalised); the venv also needs zstandard (and psycopg for a Postgres board), else it is
          refreshed. Installs are hash-checked and wheels only (--require-hashes --only-binary=:all:)
  lock:   the directory <venv>.building, taken with mkdir (atomic), waited for up to 300 s; it holds
          the owner's pid and host, and one older than LOCK_STALE_MINUTES (SWARM_LOCK_STALE_MINUTES,
          default 20) is stale (a killed build): removed, then taken again
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
import zlib
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent.parent
MIN_PY = (3, 11)


def venv_dir() -> Path:
    env = os.environ.get("SWARM_VENV")
    if env:
        return Path(env)
    share = Path(os.path.expanduser("~")) / ".local" / "share" / "swarm"
    stamp = requirements_stamp()
    per_stamp, legacy = share / f"venv-{stamp}", share / "venv"
    if not venv_python(per_stamp).exists() and venv_python(legacy).exists():
        try:
            if (legacy / ".swarm-requirements").read_text().strip() == stamp:
                return legacy
        except OSError:
            pass
    return per_stamp


def venv_python(v: Path) -> Path:
    if os.name == "nt":
        for cand in (v / "Scripts" / "python.exe", v / "bin" / "python.exe", v / "bin" / "python"):
            if cand.exists():
                return cand
        return v / "Scripts" / "python.exe"
    return v / "bin" / "python"


REQUIREMENTS = ("requirements.txt", "requirements-postgres.txt")


def requirements_stamp(root: Path = PLUGIN_ROOT) -> str:
    data = b"".join((root / rel).read_bytes().replace(b"\r\n", b"\n") for rel in REQUIREMENTS
                    if (root / rel).exists())
    return str(zlib.crc32(data))


PACKAGES = ("psycopg", "zstandard")   # what a full (Postgres-capable) venv has


def _has(v: Path, pkg: str) -> bool:
    sites = [v / "Lib" / "site-packages", *v.glob("lib/python3*/site-packages")]
    return any((s / pkg / "__init__.py").exists() for s in sites)


def want_postgres(v: Path, argv=None) -> bool:
    """psycopg is installed only for a Postgres board: the config says so (swarm.pgwant, the rule
    bin/swarm uses: cli.load_config's, honouring --config in argv and SWARM_CONFIG), or the venv
    already has it (an existing install keeps it)."""
    if _has(v, "psycopg"):
        return True
    try:
        from swarm import pgwant
    except ImportError:          # run as a script: its own folder is on sys.path
        import pgwant
    arg = pgwant.config_arg(sys.argv[1:] if argv is None else argv)
    cfg = Path(arg).expanduser() if arg else Path(os.environ.get("SWARM_CONFIG")
                                                  or Path(os.path.expanduser("~")) / ".config" / "swarm" / "config.toml")
    return pgwant.wanted(cfg)


def _packages_ok(v: Path, want_pg: bool) -> bool:
    return _has(v, "zstandard") and (not want_pg or _has(v, "psycopg"))


def _stamp_ok(v: Path, want: str, want_pg: bool = False) -> bool:
    try:
        return (venv_python(v).exists() and (v / ".swarm-requirements").read_text().strip() == want
                and _packages_ok(v, want_pg))
    except OSError:
        return False


def pip_args(root: Path, want_pg: bool) -> list[str]:
    """Hash-checked, wheels-only install of the pinned requirements."""
    args = ["-m", "pip", "install", "-q", "--require-hashes", "--only-binary=:all:",
            "-r", str(root / "requirements.txt")]
    if want_pg:
        args += ["-r", str(root / "requirements-postgres.txt")]
    return args


LOCK_STALE_MINUTES = 20


def lock_is_stale(lock: Path, now: float | None = None) -> bool:
    """A build lock left by a killed build: older than SWARM_LOCK_STALE_MINUTES. (Windows has no
    safe pid probe from the stdlib: os.kill would terminate the process, so age alone decides.)"""
    try:
        minutes = float(os.environ.get("SWARM_LOCK_STALE_MINUTES") or LOCK_STALE_MINUTES)
        return (time.time() if now is None else now) - lock.stat().st_mtime > minutes * 60
    except (OSError, ValueError):
        return False


def take_lock(lock: Path, timeout: int = 300) -> None:
    """Create the lock directory (atomic), removing a stale one first; SystemExit when it stays held."""
    waited = 0
    while True:
        try:
            lock.mkdir()
            (lock / "owner").write_text(f"{os.getpid()}\n")
            return
        except FileExistsError:
            pass
        if lock_is_stale(lock):
            gone = lock.with_name(f"{lock.name}.stale.{os.getpid()}")
            try:
                lock.rename(gone)         # of several waiters only one wins the rename
            except OSError:
                pass
            else:
                print(f"swarm: removed a stale build lock ({lock})", file=sys.stderr)
                shutil.rmtree(gone, ignore_errors=True)
                continue
        waited += 1
        if waited > timeout:
            sys.exit(f"swarm: {lock} is held (younger than the stale limit); remove it if you are sure no "
                     f"build is running, then retry")
        time.sleep(1)


def ensure_venv(v: Path | None = None, root: Path = PLUGIN_ROOT, timeout: int = 300) -> Path:
    """The venv's interpreter, creating or refreshing the venv first when needed. SystemExit(1)
    with a message on stderr when it can't be set up."""
    v = v or venv_dir()
    want_pg = want_postgres(v)
    want = requirements_stamp(root)
    if _stamp_ok(v, want, want_pg):
        return venv_python(v)
    if sys.version_info < MIN_PY:
        sys.exit(f"swarm: Python {MIN_PY[0]}.{MIN_PY[1]} or newer is needed to set up {v}; this is "
                 f"{sys.version_info[0]}.{sys.version_info[1]} ({sys.executable})")
    v.parent.mkdir(parents=True, exist_ok=True)
    lock = Path(str(v) + ".building")
    take_lock(lock, timeout)
    try:
        if not _stamp_ok(v, want, want_pg):
            print(f"swarm: setting up {v} (first run or new requirements)...", file=sys.stderr)
            env = {**os.environ, "PIP_DISABLE_PIP_VERSION_CHECK": "1"}
            ok = venv_python(v).exists() or subprocess.run([sys.executable, "-m", "venv", str(v)]).returncode == 0
            ok = ok and subprocess.run([str(venv_python(v)), *pip_args(root, want_pg)], env=env,
                                       stdout=sys.stderr).returncode == 0
            if not ok:
                sys.exit(f"swarm: could not set up {v}. Needs python >= 3.11 with venv and pip. Packages are installed as "
                         f"hash-checked wheels only (pip --require-hashes --only-binary=:all:), so a platform or "
                         f"Python version with no published wheel, or a hash mismatch, fails here: see pip's error above. "
                         f"Use Python 3.11-3.14 on a supported platform, or install the requirements files by hand.")
            (v / ".swarm-requirements").write_text(want + "\n")
    finally:
        shutil.rmtree(lock, ignore_errors=True)
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
