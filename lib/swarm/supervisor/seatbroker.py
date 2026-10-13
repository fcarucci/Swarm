"""A jailed drive seat's only way to the board (Task 20). The seat has no board password and no host-private file
in its jail; its `swarm` command and swarm hooks (swarm.seatbroker_client) leave TYPED requests
(swarm.seatproto) in the seat's broker directory, bound into that seat's jail only. This broker, threads of the
seat's runner (outside the jail, alive exactly as long as the session), answers them:

- a cli request is validated against seatproto.SCHEMA (unknown command, unknown or identity-like field: refused)
  and the broker builds the CLI argv itself (seatproto.build_argv): the seat's identity (--key <its session> or
  --as <its name>) comes from the launch record; a file field arrives as content and is handed to the CLI as a
  file the broker wrote in the supervisor's private dir, so the CLI never opens a path the seat chose;
- the CLI runs as `python -I` from the supervisor's own code with cwd "/" and `--config` pinned to the
  supervisor's config (the only one the CLI's plugin discovery reads: swarm.cli._load_plugins honours a leading
  --config only), and refuses to finish when a module from the seat's work tree or seat dir was imported;
- a hook request keeps only seatproto.HOOK_FIELDS of its payload; session_id, transcript_path and cwd are set
  from the launch record. A Codex seat's session is its thread id, known once the runner read thread.started:
  before that its cli requests are refused and its guard hook denies (rr12);
- hooks have their own lane (HOOK_WORKERS, HOOK_TIMEOUT) and cli calls theirs (CLI_WORKERS, at most CLI_QUEUE
  queued), so a seat's long `swarm wait` never starves its guard; a guard hook that cannot run is answered with
  a deny (fail closed);
- grants (person.host_pid) bind to the seat's own session process: each call carries a fresh secret
  (person.BROKER_TOKEN_ENV) naming a record only the broker writes in the supervisor's private dir, so a variable
  an agent sets attests nothing (rr19).

Requests are read through the directory's descriptor without following links, size-capped."""
from __future__ import annotations

import base64
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REQ_MAX = 4 * 1024 * 1024
OUT_MAX = 8 * 1024 * 1024
POLL = 0.05
HOOK_WORKERS, HOOK_TIMEOUT = 4, 20.0
CLI_WORKERS, CLI_QUEUE, CLI_TIMEOUT, CLI_WAIT_TIMEOUT = 2, 8, 300.0, 900.0
SESSION_VARS = ("CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_CHILD_SESSION", "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT",
                "CODEX_SESSION_ID", "CODEX_THREAD_ID", "CODEX_SANDBOX", "CODEX_SANDBOX_NETWORK_DISABLED",
                "SWARM_HOST", "SWARM_SEAT_BROKER", "SWARM_RESUME_TOKEN", "SWARM_CONFIG", "PYTHONPATH",
                "PYTHONHOME", "PYTHONSTARTUP", "PYTHONUSERBASE", "CLAUDE_CONFIG_DIR", "CODEX_HOME",
                "SWARM_BROKER_TOKEN", "SWARM_BROKER_HOST_PID")
AUDIT_EXIT = 97
LIB_DIR = str(Path(__file__).resolve().parents[2])
# The brokered CLI: the supervisor's code only (python -I: no PYTHONPATH, no user site, no script dir), and an
# audit that no module came from the seat's paths.
BOOT = r"""
import os, sys
lib, forbid = sys.argv[1], [p for p in sys.argv[2].split("\n") if p]
argv = sys.argv[3:]
sys.path.insert(0, lib)
def _audit():
    for m in list(sys.modules.values()):
        f = getattr(m, "__file__", None)
        if f and any(os.path.realpath(f).startswith(r.rstrip("/") + "/") for r in forbid):
            os.write(2, b"swarm: refused: a module from the seat's files was imported\n")
            os._exit(97)
import atexit
atexit.register(_audit)
from swarm import cli
_audit()
sys.argv = ["swarm", *argv]
rc = cli.main(argv)
_audit()
sys.exit(rc or 0)
"""


class Refused(ValueError):
    pass


def _under(path: str, root: str) -> bool:
    """Whether `path` is `root` or below it, compared as the platform compares paths (Windows: separators and
    case normalised, so a drive letter or a `/` from a .git file still matches)."""
    p = os.path.normcase(os.path.normpath(str(path)))
    r = os.path.normcase(os.path.normpath(str(root)))
    return p == r or p.startswith(r.rstrip(os.sep) + os.sep)


_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_O_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
_O_BINARY = getattr(os, "O_BINARY", 0)
_DIR_FLAGS = os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | _O_CLOEXEC
# Descriptor-relative file access (Linux, macOS). Windows has no dir_fd: the broker then works by path, refusing a
# link it sees at each step (best effort: the seat there is not jailed anyway).
FD_MODE = bool(_O_DIRECTORY and _O_NOFOLLOW and all(f in os.supports_dir_fd for f in (os.open, os.rename, os.unlink))
               and os.listdir in os.supports_fd and os.scandir in os.supports_fd)


def _no_link(path: str) -> None:
    if os.path.islink(path):
        import errno
        raise OSError(errno.ELOOP, "a link is never followed", path)


def _opendir(name: str, parent=None):
    """A directory handle: a descriptor opened without following a link (FD_MODE), else its checked path."""
    if FD_MODE:
        return os.open(name, _DIR_FLAGS) if parent is None else os.open(name, _DIR_FLAGS, dir_fd=parent)
    full = name if parent is None else os.path.join(parent, name)
    _no_link(full)
    if not os.path.isdir(full):
        raise NotADirectoryError(full)
    return full


def _closedir(d) -> None:
    if FD_MODE:
        os.close(d)


def _open_in(d, name: str, flags: int, mode: int = 0o600) -> int:
    flags |= _O_CLOEXEC | _O_BINARY
    if FD_MODE:
        return os.open(name, flags | _O_NOFOLLOW, mode, dir_fd=d)
    full = os.path.join(d, name)
    if os.path.lexists(full):
        _no_link(full)
    return os.open(full, flags, mode)


def _rename_in(d, src: str, dst: str) -> None:
    if FD_MODE:
        os.rename(src, dst, src_dir_fd=d, dst_dir_fd=d)
    else:
        _no_link(os.path.join(d, src))
        os.rename(os.path.join(d, src), os.path.join(d, dst))


def _unlink_in(d, name: str) -> None:
    if FD_MODE:
        os.unlink(name, dir_fd=d)
    else:
        os.unlink(os.path.join(d, name))


def _read_fd(fd: int, limit: int) -> bytes | None:
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode) or st.st_size > limit:
        return None
    chunks, left = [], limit
    while left > 0:
        c = os.read(fd, min(left, 1 << 20))
        if not c:
            break
        chunks.append(c)
        left -= len(c)
    return b"".join(chunks)


def read_under(root: str, rel: str, limit: int) -> bytes | None:
    """The file `rel` below the directory `root`, reached one component at a time with O_NOFOLLOW (a link
    anywhere on the way, a non-directory component, "..", or a non-regular or oversize file: None)."""
    parts = [p for p in re.split(r"[\\/]", rel) if p not in ("", ".")]
    if not parts or ".." in parts:
        return None
    try:
        d = _opendir(root)
    except OSError:
        return None
    try:
        for part in parts[:-1]:
            nd = _opendir(part, d)
            _closedir(d)
            d = nd
        leaf = _open_in(d, parts[-1], os.O_RDONLY | _O_NONBLOCK)
    except OSError:
        _closedir(d)
        return None
    _closedir(d)
    try:
        return _read_fd(leaf, limit)
    finally:
        os.close(leaf)


def _transcript_shape(harness: str, key: str):
    if harness == "codex":
        return ("codex", "sessions"), 3, (lambda n: n.startswith("rollout-") and n.endswith(f"-{key}.jsonl"))
    return ("claude", "projects"), 1, (lambda n: n == f"{key}.jsonl")


def _find_transcript(seat_dir: str, harness: str, key: str | None) -> str | None:
    """The seat's own transcript as a path relative to its seat directory, found through directory descriptors
    without following a link (Claude: claude/projects/<p>/<session>.jsonl; Codex: codex/sessions/Y/M/D/
    rollout-*-<thread>.jsonl), or None."""
    if not key or not re.fullmatch(r"[A-Za-z0-9-]{8,80}", key):
        return None
    base, depth, want = _transcript_shape(harness, key)
    found: list = []

    def walk(d, rel: list, left: int) -> None:
        try:
            with os.scandir(d) as it:
                entries = [(e.name, e.is_dir(follow_symlinks=False), e.is_file(follow_symlinks=False)) for e in it]
        except OSError:
            return
        for name, is_dir, is_file in entries:
            if left == 0:
                if is_file and want(name):
                    found.append("/".join([*rel, name]))
            elif is_dir:
                try:
                    sub = _opendir(name, d)
                except OSError:
                    continue
                try:
                    walk(sub, [*rel, name], left - 1)
                finally:
                    _closedir(sub)
    try:
        d = _opendir(seat_dir)
    except OSError:
        return None
    try:
        for part in base:
            nd = _opendir(part, d)
            _closedir(d)
            d = nd
        walk(d, list(base), depth)
    except OSError:
        return None
    finally:
        _closedir(d)
    return sorted(found)[0] if found else None


def seat_transcript(seat_dir: str, harness: str, key: str | None) -> str | None:
    """The path of the seat's own transcript in its seat directory (see _find_transcript), or None."""
    rel = _find_transcript(seat_dir, harness, key)
    return os.path.join(seat_dir, *rel.split("/")) if rel else None   # native separators


def read_seat_transcript(seat_dir: str, harness: str, key: str | None, limit: int) -> bytes | None:
    """The seat's own transcript, found and read through descriptors, no link followed anywhere (rr18)."""
    rel = _find_transcript(seat_dir, harness, key)
    return read_under(seat_dir, rel, limit) if rel else None


class SeatBroker:
    """The broker of one running seat. `run` is the runner's run dict (harness, session_id, agent_key, name),
    read live: a Codex thread id the runner binds later is used from then on."""

    def __init__(self, run: dict, directory: str, *, workdir: str, forbid: list[str], seat_dir: str,
                 config: str, host_pid: int | None, token: str | None = None, base_env: dict | None = None,
                 python: str | None = None, runner=subprocess.run, file_dir: str | None = None,
                 cfg: dict | None = None, attest_dir: str | None = None):
        self.run, self.dir, self.workdir = run, directory, workdir
        self.cfg = cfg
        if attest_dir is None:
            from swarm import person
            from swarm.supervisor.settings import private_dir
            attest_dir = str(private_dir() / person.BROKER_ATTEST_DIR)
        self.attest_dir = attest_dir
        self.forbid = [os.path.realpath(p) for p in forbid if p]
        self.seat_dir, self.config = seat_dir.rstrip("/"), config
        self.host_pid, self.token = host_pid, token
        self.base_env = dict(os.environ if base_env is None else base_env)
        self.python = python or sys.executable
        self._run = runner
        self.file_dir = file_dir
        self._stop = threading.Event()
        self._hooks = ThreadPoolExecutor(HOOK_WORKERS)
        self._cli = ThreadPoolExecutor(CLI_WORKERS)
        self._cli_pending = 0
        self._lock = threading.Lock()
        self._thread = None
        self.handled = 0

    # --- identity ---------------------------------------------------------------------------------

    def session(self) -> str | None:
        """The seat's session as the launch record knows it: a Claude seat's session id (set at launch), a
        Codex seat's thread id (once the runner read it); None until then."""
        if self.run.get("harness") == "codex":
            return self.run.get("agent_key") or None
        return self.run.get("session_id") or self.run.get("agent_key") or None

    def env(self) -> dict:
        from swarm import person
        env = {k: v for k, v in self.base_env.items() if k not in SESSION_VARS}
        harness, sid = self.run.get("harness"), self.session()
        env["SWARM_HOST"] = harness
        env["SWARM_CONFIG"] = self.config
        if harness == "codex":
            env["CODEX_SESSION_ID"] = env["CODEX_THREAD_ID"] = sid or ""
            if self.token:
                env["SWARM_RESUME_TOKEN"] = self.token
        else:
            env["CLAUDE_CODE_SESSION_ID"] = sid or ""
        return env

    def _attest(self) -> tuple[str | None, str | None]:
        """A per-call secret and the record it names (the seat's session pid, an expiry), written where only the
        supervisor writes. (token, record name) or (None, None) when there is no pid to attest."""
        import secrets
        import time
        from swarm import person, safefs
        if not self.host_pid:
            return None, None
        token = secrets.token_hex(32)
        name = person.broker_attest_name(token)
        fd = safefs.open_base(self.attest_dir, create=True, strict_mode=0o700)
        try:
            safefs.write_atomic(fd, name, json.dumps({"pid": int(self.host_pid),
                                                      "expires_at": time.time() + CLI_WAIT_TIMEOUT + 60}))
        finally:
            os.close(fd)
        return token, name

    def _unattest(self, name: str | None) -> None:
        if not name:
            return
        from swarm import safefs
        try:
            fd = safefs.open_base(self.attest_dir, create=False, strict_mode=0o700)
        except Exception:
            return
        try:
            safefs.unlink(fd, name)
        finally:
            os.close(fd)

    def payload(self, raw) -> bytes:
        """The hook payload as the broker passes it on: only HOOK_FIELDS, then session_id, transcript_path
        and cwd from the launch record."""
        from swarm import seatproto
        src = raw if isinstance(raw, dict) else {}
        out = {k: src[k] for k in seatproto.HOOK_FIELDS if k in src}
        out["session_id"] = self.session()
        t = seat_transcript(self.seat_dir, self.run.get("harness") or "claude", self.session())
        if t:
            out["transcript_path"] = t
        out["cwd"] = self.workdir
        return json.dumps(out).encode()

    # --- running ----------------------------------------------------------------------------------

    def _store(self, content, made: list) -> str:
        fd, path = tempfile.mkstemp(prefix="seat-file-", dir=self.file_dir)
        with os.fdopen(fd, "wb") as fh:
            fh.write(content if isinstance(content, bytes) else content.encode("utf-8"))
        made.append(path)
        return path

    def _exec(self, argv: list[str], stdin: bytes | None, timeout: float, extra_env=None) -> tuple[int, bytes, bytes]:
        cmd = [self.python, "-I", "-c", BOOT, LIB_DIR, "\n".join([self.workdir, self.seat_dir, *self.forbid]),
               "--config", self.config, *argv]
        from swarm import person
        env = {k: v for k, v in {**self.env(), **(extra_env or {})}.items() if k != person.BROKER_TOKEN_ENV}
        token, name = self._attest()
        if token:
            env[person.BROKER_TOKEN_ENV] = token
        try:
            res = self._run(cmd, input=stdin, capture_output=True, cwd=os.path.abspath(os.sep), env=env, timeout=timeout)
        finally:
            self._unattest(name)
        return res.returncode, res.stdout or b"", res.stderr or b""

    def _requirement(self, path: str, made: list) -> list[str]:
        """A requirement the seat names in its work tree: read here through descriptors (no link followed),
        pinned by the CLI at that content (--add-requirements-pinned PATH\\nFILE)."""
        from swarm import seatproto
        rel = os.path.relpath(path, self.workdir)
        if not _under(path, self.workdir) or rel.startswith(".."):
            raise Refused(f"requirement {path} is not in the seat's work tree")
        data = read_under(self.workdir, rel, seatproto.REQUIREMENT_MAX)
        if data is None:
            raise Refused(f"requirement {path} is not a regular file of the work tree (a link, or too large)")
        return [f"--add-requirements-pinned={path}\n{self._store(data, made)}"]

    def _grant(self, argv: list[str]) -> tuple:
        """For a control command that needs its caller attested (drivegate.GRANT_SUBS): the broker writes the
        grant itself, for the seat its launch record names. (board key, job, grant id) or Nones."""
        from swarm import drivegate
        if not argv or argv[0] not in drivegate.GRANT_SUBS or self.cfg is None:
            return None, None, None
        from swarm.board.autoinit import store_key
        bk = store_key(self.cfg)
        ctx = drivegate.seat_context(self.cfg, bk, {"session_id": self.session()})
        full = ["--config", self.config, *argv]
        rid = drivegate.write_grant_argv(self.cfg, full, ctx, self.host_pid)
        ns = drivegate.parse_swarm(full) if rid else None
        return bk, getattr(ns, "job", None), rid

    def do_cli(self, req) -> tuple[int, bytes, bytes]:
        from swarm import receipts, seatproto
        command, fields = seatproto.validate(req, self.run.get("job") or None)   # fills/checks --job (own job)
        if self.session() is None:
            raise Refused("the seat's session is not known yet (a Codex seat before its thread started)")
        made: list = []
        grant = (None, None, None)
        try:
            argv = seatproto.build_argv(
                command, fields, key=self.session(), name=self.run.get("name"),
                file_path=lambda c: self._store(c, made),
                report=lambda path, content: [f"--report={path}", f"--report-content={self._store(content, made)}"],
                requirement=lambda path: self._requirement(path, made))
            if command == "seat request" and "workdir" not in fields:
                argv[2:2] = [f"--workdir={self.workdir}"]
            limit = CLI_WAIT_TIMEOUT if command == "wait" else CLI_TIMEOUT
            grant = self._grant(argv)
            return self._exec(argv, None, limit)
        finally:
            bk, job, rid = grant
            if rid:
                try:
                    receipts.drop(bk, job, "grant", rid)   # used up by the command, or never: none outlives it
                except Exception:
                    pass
            for p in made:
                try:
                    os.unlink(p)
                except OSError:
                    pass

    def do_hook(self, req) -> tuple[int, bytes, bytes]:
        from swarm import seatbroker_client, seatproto
        event = req.get("event") if isinstance(req, dict) else None
        if event not in seatproto.HOOK_EVENTS or set(req) - {"v", "kind", "event", "payload"}:
            raise Refused("not a hook request")
        if self.session() is None:
            return 0, seatbroker_client.deny_output(event, "the seat's session is not known yet"), b""
        rc, out, err = self._exec(["hook", "--host", self.run.get("harness") or "claude", event],
                                  self.payload(req.get("payload")), HOOK_TIMEOUT,
                                  {"SWARM_HOOK_EVENT": event, "SWARM_HOOK_FASTPATH": "1"})
        if rc != 0 and event in seatproto.GUARD_EVENTS and not out.strip():
            out = seatbroker_client.deny_output(event, "the seat's guard hook failed")
        return 0 if event in seatproto.GUARD_EVENTS else rc, out, err

    # --- the loop ---------------------------------------------------------------------------------

    def _open_dir(self):
        d = _opendir(self.dir)
        if FD_MODE and hasattr(os, "getuid") and os.fstat(d).st_uid != os.getuid():
            _closedir(d)
            raise OSError("the broker directory is not this user's")
        return d

    def _read(self, d, name: str) -> bytes | None:
        try:
            fd = _open_in(d, name, os.O_RDONLY | _O_NONBLOCK)
        except OSError:
            return None
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_size > REQ_MAX:
                return None
            return os.read(fd, REQ_MAX + 1)
        finally:
            os.close(fd)

    def _answer(self, d: int, rid: str, rc: int, out: bytes, err: bytes) -> None:
        body = json.dumps({"rc": rc, "out": base64.b64encode(out[:OUT_MAX]).decode(),
                           "err": base64.b64encode(err[:OUT_MAX]).decode()}).encode()
        tmp = f"{rid}.answer"
        try:
            _unlink_in(d, tmp)
        except FileNotFoundError:
            pass
        fd = _open_in(d, tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, body)
        finally:
            os.close(fd)
        _rename_in(d, tmp, f"{rid}.resp")

    def handle(self, d: int, rid: str, req) -> None:
        from swarm import seatbroker_client, seatproto
        event = req.get("event") if isinstance(req, dict) else ""
        try:
            if isinstance(req, dict) and req.get("kind") == "hook":
                rc, out, err = self.do_hook(req)
            else:
                rc, out, err = self.do_cli(req)
        except (Refused, seatproto.ProtocolError) as exc:
            rc, out, err = 2, b"", f"swarm: refused in a drive seat: {exc}\n".encode()
            if event in seatproto.GUARD_EVENTS:
                rc, out = 0, seatbroker_client.deny_output(event, f"refused ({exc})")
        except subprocess.TimeoutExpired:
            rc, out, err = 124, b"", b"swarm: the command ran out of time in the seat broker\n"
            if event in seatproto.GUARD_EVENTS:
                rc, out = 0, seatbroker_client.deny_output(event, "the seat's guard hook ran out of time")
        except Exception as exc:   # never let one request stop the broker
            rc, out, err = 1, b"", f"swarm: seat broker error ({type(exc).__name__})\n".encode()
            if event in seatproto.GUARD_EVENTS:
                rc, out = 0, seatbroker_client.deny_output(event, "the seat broker failed")
        try:
            self._answer(d, rid, rc, out, err)
            self.handled += 1
        except OSError:
            pass

    def _cli_done(self, *_):
        with self._lock:
            self._cli_pending -= 1

    def poll_once(self, d: int, inline: bool = False) -> int:
        n = 0
        for name in sorted(os.listdir(d)):
            if not name.endswith(".req"):
                continue
            rid = name[:-4]
            if not re.fullmatch(r"[0-9a-f]{32}", rid):
                continue
            try:
                _rename_in(d, name, f"{rid}.work")
            except OSError:
                continue
            raw = self._read(d, f"{rid}.work")
            try:
                _unlink_in(d, f"{rid}.work")
            except OSError:
                pass
            try:
                req = json.loads(raw) if raw else None
            except ValueError:
                req = None
            n += 1
            if inline:
                self.handle(d, rid, req)
            elif isinstance(req, dict) and req.get("kind") == "hook":
                self._hooks.submit(self.handle, d, rid, req)
            else:
                with self._lock:
                    busy = self._cli_pending >= CLI_QUEUE
                    if not busy:
                        self._cli_pending += 1
                if busy:
                    self._answer(d, rid, 75, b"", b"swarm: too many swarm commands of this seat at once; retry\n")
                    continue
                self._cli.submit(self.handle, d, rid, req).add_done_callback(self._cli_done)
        return n

    def _loop(self) -> None:
        try:
            d = self._open_dir()
        except OSError:
            return
        try:
            while not self._stop.is_set():
                try:
                    self.poll_once(d)
                except OSError:
                    pass
                self._stop.wait(POLL)
        finally:
            self._hooks.shutdown(wait=True, cancel_futures=True)
            self._cli.shutdown(wait=True, cancel_futures=True)
            _closedir(d)

    def start(self) -> "SeatBroker":
        self._thread = threading.Thread(target=self._loop, name="seat-broker", daemon=True)
        self._thread.start()
        return self

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)


def config_path(cfg: dict, run: dict) -> str:
    """The supervisor's own config file: the run's, else the one cfg was loaded from, else the default."""
    p = run.get("config") or (cfg or {}).get("_config_path")
    if p:
        return str(p)
    from swarm import cli
    return str(cli.DEFAULT_CONFIG)


def for_run(cfg: dict, run: dict, *, host_pid: int | None, token: str | None) -> SeatBroker | None:
    """The broker of a jailed drive run (run["seat_home"] set by the runner when it jailed it), or None."""
    from swarm.supervisor import seathome
    from swarm.supervisor.settings import private_dir
    sh = run.get("seat_home")
    if not sh:
        return None
    workdir = os.path.realpath(run["cwd"])
    forbid = [workdir, sh]
    try:
        gitdir, common = seathome.git_dirs(cfg, workdir)
        forbid += [p for p in (gitdir, common) if p]
    except seathome.SeatIsolationError:
        pass
    files = private_dir() / "broker-files"
    files.mkdir(mode=0o700, exist_ok=True)
    return SeatBroker(run, os.path.join(sh, "broker"), workdir=workdir, forbid=forbid, seat_dir=sh,
                      config=config_path(cfg, run), host_pid=host_pid, token=token, file_dir=str(files), cfg=cfg)
