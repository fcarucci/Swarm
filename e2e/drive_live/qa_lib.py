"""Shared plumbing for the drive-mode live-run and chaos harnesses (stdlib only).

Everything a run touches lives under ONE run directory below QA_ROOT (default
~/work/swarm-autonomy/qa; never /tmp, never the shared board, never a real repository):

    <run>/home/        the HOME of every process the harness starts (config, markers, spool, supervisor
                       private dir and ledger, mirrors): the shared ~/.local/share/swarm is not reachable
    <run>/pg/          a throwaway Postgres cluster (127.0.0.1, its own random port, trust auth); the
                       sqlite and file backends need none
    <run>/hub/       the fake hub: bare repositories reached by file:// URLs only
    <run>/work/        the job's work tree(s) and the seats' work trees
    <run>/bin/         `swarm`, `claude`, `codex` shims (the fake harness in fake mode)
    <run>/logs/        supervisor passes, harness logs, the timeline and the cost report

`capabilities()` says which drive pieces the checkout under test has; `Pending` carries the message
"pending 8a/8b/8c/11b" so a scenario skips with a clear reason instead of failing on a missing command.
"""
from __future__ import annotations

import atexit
import contextlib
import datetime as dt
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = Path(os.environ.get("SWARM_QA_REPO", str(HERE.parent.parent)))   # the checkout under test
REAL_HOME = os.path.expanduser("~")
REAL_PATH = os.environ.get("PATH", "")
QA_ROOT = Path(os.environ.get("SWARM_QA_ROOT", "~/work/swarm-autonomy/qa")).expanduser()
VENV = Path(os.environ.get("SWARM_QA_VENV", str(QA_ROOT / "venv"))).expanduser()


def runtime_dir() -> str | None:
    xdg = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    return xdg if os.path.isdir(xdg) else None


def user_manager_up() -> bool:
    """A systemd user manager answers (drive seats REQUIRE a real scope: the driver refuses to launch without one)."""
    xd = runtime_dir()
    if not xd:
        return False
    env = {**os.environ, "XDG_RUNTIME_DIR": xd, "DBUS_SESSION_BUS_ADDRESS": f"unix:path={xd}/bus"}
    try:
        r = subprocess.run(["systemctl", "--user", "is-system-running"], capture_output=True, text=True, env=env, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.stdout.strip() in ("running", "degraded")


def _pick_pg_bin() -> Path:
    """A Postgres bin dir whose `postgres` actually runs (the pg0 install needs libicuuc.so.70, which this
    box no longer has; QA_ROOT/pgdist is a copy of the pgserver wheel (install + its libs))."""
    cands = [os.environ.get("SWARM_QA_PG_BIN"), str(QA_ROOT / "pgdist" / "pgserver" / "pginstall" / "bin"),
             "~/.pg0/installation/18.1.0/bin"]
    for c in filter(None, cands):
        p = Path(c).expanduser()
        try:
            if subprocess.run([str(p / "postgres"), "-V"], capture_output=True).returncode == 0:
                return p
        except OSError:
            continue
    return Path(cands[1])


PG_BIN = _pick_pg_bin()
TERMINAL = ("completed", "failed", "cancelled")

# --- guards ------------------------------------------------------------------------------------


class Refused(RuntimeError):
    """The harness refuses to touch something outside its own run directory."""


def under(path: Path, root: Path) -> bool:
    try:
        Path(os.path.realpath(path)).relative_to(os.path.realpath(root))
        return True
    except ValueError:
        return False


def check_run_dir(path: Path) -> Path:
    path = Path(path).expanduser().absolute()
    if str(path).startswith(("/tmp/", "/var/tmp/", "/dev/shm/")):
        raise Refused(f"{path}: never /tmp (tmpfs is RAM on this box)")
    if not under(path, QA_ROOT):
        raise Refused(f"{path} is not under SWARM_QA_ROOT={QA_ROOT}")
    return path


# --- which drive pieces exist -----------------------------------------------------------------

@dataclass(frozen=True)
class Cap:
    name: str          # the plan task that provides it
    present: bool
    detail: str


def _has(root: Path, rel: str, *needles: str) -> bool:
    p = root / rel
    if not p.is_file():
        return False
    if not needles:
        return True
    text = p.read_text(errors="replace")
    return all(n in text for n in needles)


def capabilities(root: Path = REPO) -> dict[str, Cap]:
    """What the checkout at `root` has. Source inspection only (no import, no board): cheap, and it
    works on any branch. Each probe names the plan task that delivers the piece."""
    c = {
        "6a": Cap("6a activate --drive/--no-drive, budgets, --lead-brief, --remote",
                  _has(root, "lib/swarm/drive_activate.py", "--no-drive", "--lead-brief")
                  or _has(root, "lib/swarm/cli.py", "--no-drive", "--lead-brief"), "lib/swarm/drive_activate.py"),
        "6b": Cap("6b swarm seat|halt|unhalt|review|goal-ready",
                  _has(root, "lib/swarm/drive_cli.py", '"seat"', '"halt"', '"goal-ready"'), "lib/swarm/drive_cli.py"),
        "7": Cap("7 pure next_actions", _has(root, "lib/swarm/supervisor/driver_model.py", "next_actions")
                 or _has(root, "lib/swarm/supervisor/next_actions.py"), "driver_model / next_actions"),
        "8a": Cap("8a driver execution (claims, reconciliation)",
                  _has(root, "lib/swarm/supervisor/driver_exec.py", "def execute", "claim_drive_launch"),
                  "supervisor/driver_exec.py def execute + claim_drive_launch"),
        "8b": Cap("8b deadlines, terminalization, rollover, core-sweep skip",
                  _has(root, "lib/swarm/supervisor/driver_life.py", "def terminalize", "def enforce_deadlines")
                  and _has(root, "lib/swarm/board/base.py", "drive_job_claimed"),
                  "driver_life.py terminalize/enforce_deadlines + base.drive_job_claimed"),
        "8b-reactivation": Cap("8b reactivation quiet clock (today's auto-close bug)",
                               _has(root, "lib/swarm/board/base.py", "quiet_start"), "board/base.py quiet_start"),
        "8c": Cap("8c pass integration, tamper handling",
                  _has(root, "lib/swarm/supervisor/driver.py") and _has(root, "lib/swarm/supervisor/command.py", "driver"),
                  "supervisor/driver.py + command.py runs the driver"),
        "11b": Cap("11b swarm land (driver-executed landing)",
                   _has(root, "lib/swarm/land.py") and _has(root, "lib/swarm/cli.py", '"land"')
                   or _has(root, "lib/swarm/drive_cli.py", '"land"'), "lib/swarm/land.py + swarm land"),
        "ledger": Cap("4 host-private ledger", _has(root, "lib/swarm/supervisor/ledger.py"), "supervisor/ledger.py"),
        "receipts": Cap("4 receipts", _has(root, "lib/swarm/receipts.py"), "receipts.py"),
    }
    return c


def barrier_present(name: str, root: Path = REPO) -> bool:
    """Whether the checkout has a crash barrier of this name: a `hit("name")` call under lib/. A scenario that
    arms an absent barrier would kill nothing and pass vacuously, so it is skipped instead."""
    import re as _re
    pat = _re.compile(r"""hit\(\s*["']%s["']\s*\)""" % _re.escape(name))
    for f in (root / "lib").rglob("*.py"):
        try:
            if pat.search(f.read_text(errors="replace")):
                return True
        except OSError:
            continue
    return False


LIVE_NEEDS = ("6a", "6b", "8a", "8b", "8c", "11b")


class Pending(Exception):
    """A piece the scenario needs is not integrated yet: the harness skips, never fails."""

    def __init__(self, missing: list[str], what: str = ""):
        self.missing = missing
        super().__init__((f"pending {'/'.join(missing)}" if missing else "not run") + (f" ({what})" if what else ""))


def require(*names: str, root: Path = REPO, what: str = "") -> None:
    caps = capabilities(root)
    missing = [n for n in names if not caps[n].present]
    if missing:
        raise Pending(missing, what)


# --- a throwaway Postgres ---------------------------------------------------------------------

def free_port() -> int:
    with contextlib.closing(socket.socket()) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Pg:
    """A private cluster in <run>/pg/data on 127.0.0.1:<port>, TCP only, trust auth, fsync off.
    Only this object starts and stops it, and only by its own data directory: it never touches another
    cluster or connection."""

    def __init__(self, base: Path):
        self.base = check_run_dir(base)
        self.data = self.base / "data"
        self.port = free_port()
        self.user = "swarm"

    def _pg(self, tool: str) -> str:
        p = PG_BIN / tool
        if not p.exists():
            raise Refused(f"{p} missing: set SWARM_QA_PG_BIN to a Postgres bin dir")
        return str(p)

    def start(self) -> None:
        self.base.mkdir(parents=True, exist_ok=True)
        if not (self.data / "PG_VERSION").exists():
            subprocess.run([self._pg("initdb"), "-D", str(self.data), "-U", self.user, "--auth=trust",
                            "-E", "UTF8", "--no-locale"], check=True, capture_output=True, text=True)
        opts = (f"-p {self.port} -c listen_addresses=127.0.0.1 -c unix_socket_directories= "
                f"-c fsync=off -c synchronous_commit=off -c max_connections=60")
        subprocess.run([self._pg("pg_ctl"), "-D", str(self.data), "-l", str(self.base / "pg.log"), "-o", opts,
                        "-w", "-t", "60", "start"], check=True, capture_output=True, text=True)
        atexit.register(self.stop)
        for sig in (signal.SIGTERM, signal.SIGINT, getattr(signal, "SIGHUP", signal.SIGTERM)):
            try:
                signal.signal(sig, self._on_signal)
            except ValueError:   # not the main thread
                pass

    def _on_signal(self, signum, frame):
        self.stop()
        raise SystemExit(128 + signum)

    def stop(self) -> None:
        if (self.data / "postmaster.pid").exists():
            subprocess.run([self._pg("pg_ctl"), "-D", str(self.data), "-m", "fast", "-w", "-t", "30", "stop"],
                           capture_output=True, text=True)

    def running(self) -> bool:
        r = subprocess.run([self._pg("pg_ctl"), "-D", str(self.data), "status"], capture_output=True, text=True)
        return r.returncode == 0


# --- the run ------------------------------------------------------------------------------------

def toml_str(v) -> str:
    return json.dumps(str(v))


class Run:
    """One isolated run: home, board, hub, shims, logs, timeline. `with Run(...) as run:` stops the
    throwaway Postgres on the way out, whatever happens."""

    def __init__(self, name: str, *, backend: str = "postgres", harness: str = "fake",
                 root: Path = REPO, stamp: str | None = None, keep: bool = True, systemd: bool | None = None,
                 pass_env: list | None = None):
        # drive seats need real scopes: default to them whenever a user manager answers
        self.systemd = user_manager_up() if systemd is None else systemd
        self.pass_env = list(pass_env or [])
        self.session_driver_env = False
        self.name, self.backend, self.harness, self.root, self.keep = name, backend, harness, root, keep
        stamp = stamp or dt.datetime.now().strftime("%Y%m%dT%H%M%S") + f"-{os.getpid() % 10000}"
        self.dir = check_run_dir(QA_ROOT / "runs" / f"{stamp}-{name}")
        self.home = self.dir / "home"
        # The fake hub's bare repositories are local destinations: the driver trusts a local destination only
        # inside the supervisor's private directory (supervisor/mirror.py _check_local), so the hub lives there.
        self.hub_dir = self.home / ".local" / "share" / "swarm" / "supervisor" / "qa-hub"
        self.work = self.dir / "work"
        self.bin = self.dir / "bin"
        self.logs = self.dir / "logs"
        self.pg = Pg(self.dir / "pg") if backend == "postgres" else None
        self.config = self.home / ".config" / "swarm" / "config.toml"
        self.t0 = time.monotonic()
        self._timeline = None

    # -- lifecycle
    def __enter__(self) -> "Run":
        self.setup()
        return self

    def __exit__(self, *exc) -> None:
        self.teardown()

    def setup(self) -> None:
        if not (VENV / "bin" / "python").exists():
            raise Refused(f"{VENV}: no venv; run e2e/drive_live/setup_venv.sh once")
        for d in (self.home, self.hub_dir, self.work, self.bin, self.logs):
            d.mkdir(parents=True, exist_ok=True)
        os.chmod(self.home, 0o700)
        for d in (self.hub_dir.parent.parent, self.hub_dir.parent):   # share/swarm and its supervisor dir: private
            os.chmod(d, 0o700)
        if self.pg:
            self.pg.start()
        self._stamp_venv()
        self._write_shims()
        self._write_config()
        self._timeline = open(self.logs / "timeline.jsonl", "a", buffering=1)
        self.event("run", "start", name=self.name, backend=self.backend, harness=self.harness,
                   head=self.git_head())

    def teardown(self) -> None:
        self.event("run", "end")
        if self._timeline:
            self._timeline.close()
            self._timeline = None
        if self.pg:
            self.pg.stop()
        if not self.keep:
            shutil.rmtree(self.dir, ignore_errors=True)

    def git_head(self) -> str:
        r = subprocess.run(["git", "-C", str(self.root), "rev-parse", "HEAD"], capture_output=True, text=True)
        return r.stdout.strip() or "?"

    # -- files
    def _stamp_venv(self) -> None:
        """bin/swarm rebuilds a venv whose requirements stamp differs from the checkout's. The QA venv was built from
        the same pinned requirement files, so record the checkout's stamp instead of letting a seat trigger a rebuild."""
        files = [self.root / "requirements.txt", self.root / "requirements-postgres.txt"]
        if all(f.exists() for f in files):
            r = subprocess.run(["cksum"], input=b"".join(f.read_bytes() for f in files), capture_output=True)
            if r.returncode == 0:
                (VENV / ".swarm-requirements").write_text(r.stdout.split()[0].decode() + "\n")

    def _write_shims(self) -> None:
        py = VENV / "bin" / "python"
        swarm = self.bin / "swarm"
        swarm.write_text(f'#!/bin/sh\nPYTHONPATH="{self.root}/lib" exec "{py}" "{HERE}/qa_swarm.py" "$@"\n')
        swarm.chmod(0o755)
        for harness in ("claude", "codex"):
            shim = self.bin / harness
            if self.harness == "fakeseat" and harness == "claude":
                # the target checkout's own scripted seat (tests/drive_fake_seat.py, Task 21): real hooks, no tags
                seat = self.root / "tests" / "drive_fake_seat.py"
                shim.write_text(f'#!/bin/sh\nexport FAKE_SWARM="{self.root}/bin/swarm" FAKE_SEAT_LOG="{self.logs}/fakeseat.jsonl"\n'
                                f'PYTHONPATH="{self.root}/lib" exec "{py}" "{seat}" "$@"\n')
                shim.chmod(0o755)
            elif self.harness == "fakeseat":
                if shim.exists() or shim.is_symlink():
                    shim.unlink()
            elif self.harness == "fake":
                shim.write_text(f'#!/bin/sh\nexec "{py}" "{HERE}/fake_harness.py" --harness {harness} "$@"\n')
                shim.chmod(0o755)
            elif shim.exists() or shim.is_symlink():
                shim.unlink()

    def _write_config(self) -> None:
        self.config.parent.mkdir(parents=True, exist_ok=True)
        L = ["[board]", f'backend = "{self.backend}"', "message_max_chars = 400", ""]
        if self.pg:
            L += ["[database]", 'host = "127.0.0.1"', f"port = {self.pg.port}", f'user = "{self.pg.user}"',
                  'dbname = "qa_board"', 'admin_dbname = "postgres"', 'sslmode = "disable"',
                  "password_env_file = \"\"", ""]
        elif self.backend == "sqlite":
            L += ["[sqlite]", f"path = {toml_str(self.dir / 'board.sqlite3')}", ""]
        else:
            L += ["[file]", f"path = {toml_str(self.dir / 'board')}", ""]
        sup = ["[supervise]", "enabled = true", f"allowed_workdirs = [{toml_str(self.dir)}]",
               "timer_minutes = 1", "orphan_minutes = 1", "enrol_minutes = 2", "max_minutes = 20"]
        if self.pass_env:
            sup.append(f"pass_env = {json.dumps(self.pass_env)}")
        if self.harness in ("fake", "fakeseat"):
            sup += [f"claude_bin = {toml_str(self.bin / 'claude')}", f"codex_bin = {toml_str(self.bin / 'codex')}"]
        else:
            # the user's own binaries (the isolated HOME has no ~/.local/bin); SWARM_QA_CLAUDE/_CODEX override
            for h in ("claude", "codex"):
                found = os.environ.get(f"SWARM_QA_{h.upper()}") or shutil.which(h, path=REAL_PATH)
                if found:
                    sup.append(f"{h}_bin = {toml_str(found)}")
            sup += [f"claude_plugin_dir = {toml_str(self.root)}"]
            creds = Path(REAL_HOME) / ".claude" / ".credentials.json"
            if creds.exists():   # a private copy: the run's HOME is isolated
                dst = self.home / ".claude"
                dst.mkdir(mode=0o700, exist_ok=True)
                shutil.copy(creds, dst / ".credentials.json")
                os.chmod(dst / ".credentials.json", 0o600)
        L += sup + [""]
        L += ["[hook]", f"marker_dir = {toml_str(self.home / '.local/state/swarm/active')}", ""]
        self.config.write_text("\n".join(L))
        self.config.chmod(0o600)
        self._guard_not_shared()

    def _guard_not_shared(self) -> None:
        real = Path(os.path.expanduser("~")) / ".config" / "swarm" / "config.toml"
        if os.path.realpath(self.config) == os.path.realpath(real):
            raise Refused("the run config is the shared swarm config")

    # -- environment and commands
    def env(self, **extra) -> dict:
        keep = {k: os.environ[k] for k in ("LANG", "LC_ALL", "TERM", "USER", "LOGNAME", "ANTHROPIC_API_KEY",
                                           "OPENAI_API_KEY") if k in os.environ}
        e = {**keep, "HOME": str(self.home), "SWARM_CONFIG": str(self.config), "SWARM_VENV": str(VENV),
             "SWARM_PYCACHE": str(self.home / ".cache" / "pyc"),
             "PATH": f"{self.bin}:" + (REAL_PATH if self.harness == "real" else "/usr/local/bin:/usr/bin:/bin"),
             "SWARM_QA_RUN": str(self.dir),
             "SWARM_QA_ROOT": str(QA_ROOT), "SWARM_QA_REPO": str(self.root), "SWARM_QA_HARNESS": self.harness,
             "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0",
             "GIT_AUTHOR_NAME": "QA", "GIT_AUTHOR_EMAIL": "qa@example.invalid",
             "GIT_COMMITTER_NAME": "QA", "GIT_COMMITTER_EMAIL": "qa@example.invalid",
             "GIT_CONFIG_GLOBAL": os.devnull}
        if self.session_driver_env:
            e["SWARM_SESSION_DRIVER"] = "0"   # started explicitly by the rig, as tests/test_drive_session_driver.py does
        if not self.systemd:
            e["SWARM_NO_SYSTEMD"] = "1"
        elif runtime_dir():   # real scopes: the user manager's bus; units are transient and named by the runner
            e["XDG_RUNTIME_DIR"] = runtime_dir()
            e["DBUS_SESSION_BUS_ADDRESS"] = os.environ.get("DBUS_SESSION_BUS_ADDRESS") or f"unix:path={runtime_dir()}/bus"
        e.update({k: str(v) for k, v in extra.items()})
        return e

    def apply_env(self) -> None:
        """Make this process (and every child) the isolated one, for in-process board access."""
        os.environ.clear()
        os.environ.update(self.env())

    def swarm(self, *args, check: bool = False, timeout: float = 120, input: str | None = None,
              env: dict | None = None) -> subprocess.CompletedProcess:
        r = subprocess.run([str(self.bin / "swarm"), *map(str, args)], capture_output=True, text=True,
                           timeout=timeout, input=input, env={**self.env(), **(env or {})})
        self.event("cmd", " ".join(["swarm", *map(str, args)])[:200], rc=r.returncode)
        if check and r.returncode:
            raise RuntimeError(f"swarm {' '.join(map(str, args))} -> {r.returncode}: {r.stderr.strip()[:400]}")
        return r

    def supervise_pass(self, *, timeout: float = 300, env: dict | None = None) -> subprocess.CompletedProcess:
        """One supervisor pass, as the timer would run it: `swarm supervise`."""
        r = self.swarm("supervise", timeout=timeout, env=env)
        with open(self.logs / "supervise.log", "a") as fh:
            fh.write(f"--- pass rc={r.returncode} {dt.datetime.now().isoformat(timespec='seconds')}\n"
                     f"{r.stdout}{r.stderr}")
        return r

    def board(self):
        """An in-process board on the run's own config (this process must run under apply_env())."""
        sys.path.insert(0, str(self.root / "lib"))
        import swarm.cli as cli
        from swarm.board import open_board
        assert os.environ.get("SWARM_CONFIG") == str(self.config), "call run.apply_env() first"
        return open_board(cli.load_config(self.config))

    def board_key(self) -> str:
        sys.path.insert(0, str(self.root / "lib"))
        import swarm.cli as cli
        from swarm.board.autoinit import store_key
        return store_key(cli.load_config(self.config))

    def ledger(self, job: str):
        """The job's host-private ledger (None when there is none; LedgerBadError when it is corrupt).
        Needs run.apply_env() first: the ledger lives under $HOME/.local/share/swarm/supervisor."""
        sys.path.insert(0, str(self.root / "lib"))
        from swarm.supervisor import ledger
        return ledger.load(self.board_key(), job)

    def ledger_dir(self) -> Path:
        return self.home / ".local" / "share" / "swarm" / "supervisor" / "drive"

    def job_state(self, job: str) -> dict:
        r = self.swarm("snapshot", "--json", "--job", job, "--messages", "200")
        try:
            snap = json.loads(r.stdout)
        except ValueError:
            return {"status": "unknown", "error": (r.stderr or r.stdout)[:300]}
        jobs = snap.get("jobs") or []
        j = jobs[0] if isinstance(jobs, list) and jobs else (jobs.get(job) if isinstance(jobs, dict) else {})
        return {"raw": snap, **(j or {})}

    # -- timeline
    def event(self, kind: str, what: str, **kw) -> None:
        if self._timeline:
            self._timeline.write(json.dumps({"t": round(time.monotonic() - self.t0, 2),
                                             "at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                                             "kind": kind, "what": what, **kw}) + "\n")

    def write_text(self, rel: str, text: str) -> Path:
        p = self.dir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        return p


# --- result reporting --------------------------------------------------------------------------

class Outcome:
    PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"


def report(results: list[tuple[str, str, str]], out: Path | None = None) -> int:
    """results: (name, PASS|FAIL|SKIP, detail). Prints a table; exit code 1 on any FAIL."""
    width = max((len(n) for n, _, _ in results), default=10)
    lines = [f"{s:4}  {n:<{width}}  {d}" for n, s, d in results]
    text = "\n".join(lines) + "\n" + "\n" + ", ".join(
        f"{k}={sum(1 for _, s, _ in results if s == k)}" for k in ("PASS", "FAIL", "SKIP")) + "\n"
    print(text)
    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text)
    return 1 if any(s == "FAIL" for _, s, _ in results) else 0


def wait_until(pred, *, timeout: float, interval: float = 2.0, what: str = "condition"):
    end = time.monotonic() + timeout
    while True:
        v = pred()
        if v:
            return v
        if time.monotonic() > end:
            raise TimeoutError(f"timed out after {timeout:.0f}s waiting for {what}")
        time.sleep(interval)


def kill_tree(pid: int, sig=getattr(signal, "SIGKILL", signal.SIGTERM)) -> None:
    """SIGKILL a process and its descendants (a fake session and what it forked), not its group: the
    runner that started it may share the group and must be killed only on purpose."""
    kids = subprocess.run(["pgrep", "-P", str(pid)], capture_output=True, text=True).stdout.split()
    for k in kids:
        kill_tree(int(k), sig)
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.kill(pid, sig)


def run_procs(run: "Run", pattern: str) -> list[int]:
    """Pids whose command line matches `pattern` AND whose HOME is this run's home (so never a process of
    another run, the shared swarm, or this harness)."""
    out = []
    for pid in subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True).stdout.split():
        try:
            env = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
        except OSError:
            continue
        if f"HOME={run.home}".encode() in env and int(pid) != os.getpid():
            out.append(int(pid))
    return out


_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def check_job_name(job: str) -> str:
    if not _ID.match(job):
        raise Refused(f"bad job name {job!r}")
    return job
