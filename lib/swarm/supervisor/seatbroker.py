"""A jailed drive seat's only way to the board (Task 20, rr4). The seat has no board password and no
host-private file in its jail; its `swarm` command and its swarm hooks (swarm.seatbroker_client) leave
request files in the seat's broker directory, which is bound into that seat's jail only. This broker, a
thread of the seat's runner (outside the jail, alive exactly as long as the session), runs each request as
the seat and writes the answer back:

- who the seat is comes from the runner's launch record, never from the request: the harness session id
  (CLAUDE_CODE_SESSION_ID / CODEX_SESSION_ID and CODEX_THREAD_ID), SWARM_HOST and the resume token are
  stamped into the command's environment, a hook payload's session_id is overwritten with it, and its
  transcript paths are mapped to the seat's own transcript dir (anything else dropped);
- a command is one of SEAT_COMMANDS (post, read, verdict, done, seat, review, ...): never bg, config,
  supervise, transcript, activate or a person's command, and no global option before it;
- an argument naming an existing file outside the seat's work tree, git dir and seat directory is refused
  (the command runs outside the jail and could read it for the seat);
- grants (person.host_pid) bind to the seat's own session process (BROKER_PID_ENV), so a grant written by a
  seat's hook is used up only by that seat's command.

Requests are read through the directory's descriptor without following links, size-capped, at most
MAX_PARALLEL at once; each command has a wall clock (TIMEOUT)."""
from __future__ import annotations

import base64
import json
import os
import re
import stat
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REQ_MAX = 4 * 1024 * 1024
OUT_MAX = 8 * 1024 * 1024
MAX_PARALLEL = 4
POLL = 0.05
TIMEOUT = {"cli": 900.0, "hook": 50.0}
SEAT_COMMANDS = frozenset(("post", "read", "who", "verdict", "done", "wait", "status", "snapshot", "tail",
                           "leave", "seat", "review", "goal-ready", "ask", "answer", "land", "-h", "--help"))
_EVENT = re.compile(r"^[a-z][a-z-]{0,30}$")
_PATH_KEYS = ("transcript_path", "agent_transcript_path")
SESSION_VARS = ("CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_CHILD_SESSION", "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT",
                "CODEX_SESSION_ID", "CODEX_THREAD_ID", "CODEX_SANDBOX", "CODEX_SANDBOX_NETWORK_DISABLED",
                "SWARM_HOST", "SWARM_SEAT_BROKER", "SWARM_RESUME_TOKEN")


class Refused(ValueError):
    pass


def _under(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")


class SeatBroker:
    """The broker of one running seat. `run` is the runner's run dict (harness, session_id, agent_key);
    `roots` the host paths a command argument may name (work tree, git dir, seat directory); `jail_home`
    and `seat_claude` map the seat's ~/.claude to its own directory on the host."""

    def __init__(self, run: dict, directory: str, *, workdir: str, roots: list[str], jail_home: str,
                 seat_dir: str, host_pid: int | None, token: str | None = None, bin_dir: str | None = None,
                 base_env: dict | None = None, runner=subprocess.run):
        self.run, self.dir, self.workdir = run, directory, workdir
        self.roots = [os.path.realpath(r) for r in roots]
        self.jail_home, self.seat_dir = jail_home.rstrip("/"), seat_dir.rstrip("/")
        self.host_pid, self.token = host_pid, token
        self.bin = bin_dir or str(Path(__file__).resolve().parents[3] / "bin")
        self.base_env = dict(os.environ if base_env is None else base_env)
        self._run = runner
        self._stop = threading.Event()
        self._pool = ThreadPoolExecutor(MAX_PARALLEL)
        self._thread = None
        self.handled = 0

    # --- identity ---------------------------------------------------------------------------------

    def session(self) -> str | None:
        return self.run.get("agent_key") or self.run.get("session_id")

    def env(self) -> dict:
        from swarm import person
        env = {k: v for k, v in self.base_env.items() if k not in SESSION_VARS}
        harness, sid = self.run.get("harness"), self.session()
        env["SWARM_HOST"] = harness
        if harness == "codex":
            if sid:
                env["CODEX_SESSION_ID"] = env["CODEX_THREAD_ID"] = sid
            if self.token:
                env["SWARM_RESUME_TOKEN"] = self.token
        elif sid:
            env["CLAUDE_CODE_SESSION_ID"] = sid
        if self.host_pid:
            env[person.BROKER_PID_ENV] = str(self.host_pid)
        return env

    def _map_path(self, value) -> str | None:
        """A path from a hook payload (a jail path), as the host path of the seat's own file, or None."""
        if not isinstance(value, str):
            return None
        claude = f"{self.jail_home}/.claude/"
        codex = f"{self.jail_home}/.codex/"
        for prefix, sub in ((claude, "claude"), (codex, "codex")):
            if value.startswith(prefix) and ".." not in value.split("/"):
                return f"{self.seat_dir}/{sub}/{value[len(prefix):]}"
        return None

    def stamp_payload(self, raw: bytes) -> bytes:
        try:
            payload = json.loads(raw or b"{}")
        except ValueError:
            raise Refused("the hook payload is not JSON") from None
        if not isinstance(payload, dict):
            raise Refused("the hook payload is not an object")
        sid = self.session()
        if sid:
            payload["session_id"] = sid
        for key in _PATH_KEYS:
            if key in payload:
                mapped = self._map_path(payload[key])
                if mapped is None:
                    payload.pop(key)
                else:
                    payload[key] = mapped
        cwd = payload.get("cwd")
        if not (isinstance(cwd, str) and _under(os.path.realpath(cwd), self.workdir)):
            payload["cwd"] = self.workdir
        return json.dumps(payload).encode()

    # --- what may run -----------------------------------------------------------------------------

    def check_args(self, argv: list[str], cwd: str) -> None:
        for arg in argv:
            values = [arg] + ([arg.split("=", 1)[1]] if arg.startswith("-") and "=" in arg else [])
            for v in values:
                v = v[1:] if v.startswith("@") else v
                if not v or not ("/" in v or v.startswith(("~", "."))):
                    continue
                p = os.path.realpath(os.path.join(cwd, os.path.expanduser(v)))
                if os.path.lexists(p) and not any(_under(p, r) for r in self.roots):
                    raise Refused(f"argument {arg[:80]!r} names a file outside the seat's work tree")

    def check_identity(self, args: list[str]) -> None:
        """Who a command is from is the seat's: --key/--session must name its own session, --as its own name."""
        own = {"--key": self.session(), "--session": self.session(), "--as": self.run.get("name")}
        for i, arg in enumerate(args):
            if arg == "--":
                return
            opt, eq, val = arg.partition("=")
            if opt in own:
                val = val if eq else (args[i + 1] if i + 1 < len(args) else "")
                if not own[opt] or val != own[opt]:
                    raise Refused(f"{opt} must name this seat itself")

    def command(self, req: dict) -> tuple[list[str], bytes | None, str, float]:
        kind, argv = req.get("kind"), req.get("argv")
        if kind not in TIMEOUT or not isinstance(argv, list) or not all(isinstance(a, str) for a in argv):
            raise Refused("malformed request")
        stdin = req.get("stdin")
        data = base64.b64decode(stdin) if isinstance(stdin, str) else None
        cwd = req.get("cwd") if isinstance(req.get("cwd"), str) else self.workdir
        cwd = os.path.realpath(cwd) if _under(os.path.realpath(cwd), self.workdir) else self.workdir
        if kind == "hook":
            if len(argv) != 3 or argv[0] != "--host" or argv[1] != self.run.get("harness") \
                    or not _EVENT.match(argv[2]):
                raise Refused("not a hook call of this seat's harness")
            return [os.path.join(self.bin, "swarm-hook"), *argv], self.stamp_payload(data or b""), cwd, TIMEOUT[kind]
        if not argv or argv[0] not in SEAT_COMMANDS:
            raise Refused(f"`swarm {argv[0] if argv else ''}` is not a command a drive seat runs")
        self.check_identity(argv[1:])
        self.check_args(argv[1:], cwd)
        return [os.path.join(self.bin, "swarm"), *argv], data, cwd, TIMEOUT[kind]

    # --- the loop ---------------------------------------------------------------------------------

    def _open_dir(self) -> int:
        fd = os.open(self.dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        if os.fstat(fd).st_uid != os.getuid():
            os.close(fd)
            raise OSError("the broker directory is not this user's")
        return fd

    def _read(self, d: int, name: str) -> bytes | None:
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=d)
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
            os.unlink(tmp, dir_fd=d)
        except FileNotFoundError:
            pass
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=d)
        try:
            os.write(fd, body)
        finally:
            os.close(fd)
        os.rename(tmp, f"{rid}.resp", src_dir_fd=d, dst_dir_fd=d)

    def handle(self, d: int, rid: str) -> None:
        work = f"{rid}.work"
        raw = self._read(d, work)
        try:
            os.unlink(work, dir_fd=d)
        except OSError:
            pass
        try:
            req = json.loads(raw) if raw else None
            if not isinstance(req, dict):
                raise Refused("unreadable request")
            argv, data, cwd, limit = self.command(req)
            res = self._run(argv, input=data, capture_output=True, cwd=cwd, env=self.env(), timeout=limit)
            rc, out, err = res.returncode, res.stdout or b"", res.stderr or b""
        except Refused as exc:
            rc, out, err = 2, b"", f"swarm: refused in a drive seat: {exc}\n".encode()
        except subprocess.TimeoutExpired:
            rc, out, err = 124, b"", b"swarm: the command ran out of time in the seat broker\n"
        except Exception as exc:   # never let one request stop the broker
            rc, out, err = 1, b"", f"swarm: seat broker error ({type(exc).__name__})\n".encode()
        try:
            self._answer(d, rid, rc, out, err)
            self.handled += 1
        except OSError:
            pass

    def poll_once(self, d: int, inline: bool = False) -> int:
        n = 0
        for name in sorted(os.listdir(d)):
            if not name.endswith(".req"):
                continue
            rid = name[:-4]
            if not re.fullmatch(r"[0-9a-f]{32}", rid):
                continue
            try:
                os.rename(name, f"{rid}.work", src_dir_fd=d, dst_dir_fd=d)
            except OSError:
                continue
            n += 1
            if inline:
                self.handle(d, rid)
            else:
                self._pool.submit(self.handle, d, rid)
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
            self._pool.shutdown(wait=True, cancel_futures=True)
            os.close(d)

    def start(self) -> "SeatBroker":
        self._thread = threading.Thread(target=self._loop, name="seat-broker", daemon=True)
        self._thread.start()
        return self

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)


def for_run(cfg: dict, run: dict, *, host_pid: int | None, token: str | None) -> SeatBroker | None:
    """The broker of a jailed drive run (run["seat_home"] set by the runner when it jailed it), or None."""
    from swarm import paths
    from swarm.supervisor import seathome
    sh = run.get("seat_home")
    if not sh:
        return None
    workdir = os.path.realpath(run["cwd"])
    roots = [workdir, sh]
    try:
        _, common = seathome.git_dirs(cfg, workdir)
        if common:
            roots.append(common)
    except seathome.SeatIsolationError:
        pass
    return SeatBroker(run, os.path.join(sh, "broker"), workdir=workdir, roots=roots,
                      jail_home=str(paths.home()), seat_dir=sh, host_pid=host_pid, token=token)
