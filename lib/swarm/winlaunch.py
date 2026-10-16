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
          its owner's pid, host and a token: a dead owner (same host) or, where the pid cannot be
          checked, a lock older than SWARM_LOCK_STALE_MINUTES (default 20) is stale (a killed build):
          removed, then taken again; a live owner never is. Release checks the token
"""
from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
import sys
import time
import uuid
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


def _stale_minutes() -> float:
    try:
        v = os.environ.get("SWARM_LOCK_STALE_MINUTES", "")
        return float(int(v)) if v.isdigit() else float(LOCK_STALE_MINUTES)
    except ValueError:
        return float(LOCK_STALE_MINUTES)


LOCK_CAP_MINUTES = 360       # a running owner's lock older than this is stale anyway (a build never takes hours)


def _boot_id() -> str:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip() or "-"
    except OSError:
        return "-"


def _start_time(pid: int) -> str:
    """Field 22 of /proc/<pid>/stat (the process start, in clock ticks since boot), "-" without /proc."""
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError):
        return "-"


def _read_owner(lock: Path) -> tuple[int, str, str, str, str] | None:
    """(pid, host, token, boot id, start time) from the lock's owner file; the last two "-" when it
    does not carry them; None when there is none or it is unreadable."""
    try:
        f = (lock / "owner").read_text().split()
        return int(f[0]), f[1], f[2], (f[3] if len(f) > 3 else "-"), (f[4] if len(f) > 4 else "-")
    except (OSError, ValueError, IndexError):
        return None


def _pid_alive(pid: int) -> bool | None:
    """Whether a process of this host exists; None when the stdlib cannot tell (Windows: os.kill
    would terminate it). A process of another user (PermissionError) is alive."""
    if os.name == "nt":
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return None
    return True


def _owner_gone(owner: tuple[int, str, str, str, str]) -> bool | None:
    """Whether the owning process is not running: dead, from another boot, or its pid reused by a
    process that started later. None when that cannot be told."""
    pid, _, _, boot, start = owner
    if boot != "-" and _boot_id() not in ("-", boot):
        return True
    alive = _pid_alive(pid)
    if alive is None:
        return None
    if not alive:
        return True
    return start != "-" and _start_time(pid) not in ("-", start)


def lock_is_stale(lock: Path, now: float | None = None) -> bool:
    """A build lock left by a killed build. A dead owner on this host (or one from before a reboot, or
    whose pid was reused) always is stale; a running one is stale only past LOCK_CAP_MINUTES. Where the
    owner cannot be checked (another host, no owner record, Windows) the lock's age decides: at least
    SWARM_LOCK_STALE_MINUTES (default 20; 0 = always)."""
    owner = _read_owner(lock)
    try:
        age = (time.time() if now is None else now) - lock.stat().st_mtime
    except OSError:
        return False
    if owner is not None and owner[1] == socket.gethostname():
        gone = _owner_gone(owner)
        if gone is not None:
            return gone or age >= LOCK_CAP_MINUTES * 60
    return age >= _stale_minutes() * 60


def _steal(lock: Path) -> bool:
    """Remove the stale lock; False when it was not ours to remove (left or put back)."""
    gone = lock.with_name(f"{lock.name}.stale.{os.getpid()}")
    try:
        lock.rename(gone)         # of several waiters only one wins the rename
    except OSError:
        return False
    if lock_is_stale(gone):       # judge what we hold: it may be a fresh lock another waiter just took
        print(f"swarm: removed a stale build lock ({lock})", file=sys.stderr)
        shutil.rmtree(gone, ignore_errors=True)
        return True
    try:                          # not stale: put it back
        lock.mkdir()
        shutil.copy2(gone / "owner", lock / "owner")
        st = gone.stat()
        os.utime(lock, (st.st_atime, st.st_mtime))
    except OSError:
        pass
    shutil.rmtree(gone, ignore_errors=True)
    return False


def take_lock(lock: Path, timeout: int = 300) -> str:
    """Create the lock directory (atomic), removing a stale one first; SystemExit when it stays held.
    Returns the owner token release_lock needs."""
    token = uuid.uuid4().hex
    waited = 0
    while True:
        try:
            lock.mkdir()
            (lock / "owner").write_text(f"{os.getpid()} {socket.gethostname()} {token} {_boot_id()} "
                                        f"{_start_time(os.getpid())}\n")
            return token
        except FileExistsError:
            pass
        if lock_is_stale(lock) and _steal(lock):
            continue
        waited += 1
        if waited > timeout:
            sys.exit(f"swarm: {lock} is held (its owner is running, or it is younger than the stale limit); "
                     f"remove it if you are sure no build is running, then retry")
        time.sleep(1)


def release_lock(lock: Path, token: str) -> None:
    """Remove the lock only while it is still ours (it may have been taken over by age meanwhile)."""
    owner = _read_owner(lock)
    if owner is not None and owner[2] == token:
        shutil.rmtree(lock, ignore_errors=True)


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
    token = take_lock(lock, timeout)
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
        release_lock(lock, token)
    return venv_python(v)


def run_env(root: Path = PLUGIN_ROOT) -> dict:
    env = dict(os.environ)
    lib = str(root / "lib")
    env["PYTHONPATH"] = lib + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    return env


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if os.environ.get("SWARM_LAUNCH_BROKER"):   # a drive launch (Task 20): its broker runs this outside the launch
        return _launch_broker("cli", list(argv))
    py = ensure_venv()
    try:
        return subprocess.run([str(py), "-B", "-m", "swarm.cli", *argv], env=run_env()).returncode
    except KeyboardInterrupt:
        return 130


def _launch_broker(kind: str, argv: list) -> int:
    """Hand the call to the launch's broker (swarm.launchbroker_client, loaded by path: no import from elsewhere)."""
    import importlib.util
    here = os.path.dirname(os.path.abspath(__file__))
    spec = importlib.util.spec_from_file_location("_swarm_launchbroker_client", os.path.join(here, "launchbroker_client.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.main([kind, *argv])


if __name__ == "__main__":
    sys.exit(main())
