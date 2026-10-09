"""`swarm events serve`: the listener that turns outside signals into board events.

Core knows nothing about what a signal means. A plugin registers an event source
(plugins.PluginAPI.register_event_source) with URL routes, a `verify(headers, body)`, a
`handle(headers, body, ctx)` that returns EventSpecs and optionally a `poll(ctx)`; this module
serves the routes, runs the polls and posts what the sources return with Board.post_event
(idempotent per job + kind + key).

Security rules, each tested:
  * the signature is verified BEFORE the body is parsed: verify() gets the raw bytes and nothing
    has looked at them; a verify that raises is a refusal;
  * the body is capped ([events] max_body_bytes): a larger Content-Length is refused with 413
    without reading it, a missing one with 411 (no chunked bodies);
  * nothing of a request (body, headers, query) is ever logged, and neither is an exception's
    text: a log line holds the source, the status and a count;
  * the default bind is 127.0.0.1 and [events] enabled is false.

Config: [events] enabled, bind, port, max_body_bytes, and [events.sources.<name>] (the source's own
keys: secret_file, token_file, ...; core only hands the table to the source).
"""
from __future__ import annotations

import dataclasses
import json
import logging
import os
import subprocess
import re
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable

DEFAULTS = {
    "enabled": False, "bind": "127.0.0.1", "port": 8923, "max_body_bytes": 1024 * 1024,
    "request_timeout_s": 10, "min_poll_interval_s": 600, "model": "haiku", "stall_minutes": 60, "unacked_minutes": 30,
    "waiter_stale_seconds": 120, "alert_repeat_minutes": 60, "triage": False,
    "orchestrator_roles": ["orchestrator", "project_manager"], "sources": {},
}
TEXT_MAX = 500
DEBUG = logging.getLogger("swarm.events")
HELPERS_FILE = "events-helpers.json"
LOCAL_TOKEN_FILE = "events-local-token"   # this listener start's local-route token (private dir, 0600)
PROXY_HEADERS = ("x-forwarded-for", "x-forwarded-host", "x-forwarded-proto", "x-forwarded-port",
                 "x-forwarded-server", "forwarded", "via", "x-real-ip", "cf-connecting-ip", "true-client-ip")
BACKOFF = (5, 15, 60, 300)   # seconds before a helper restart; reset after HEALTHY_AFTER s up. The cap
# (300 s) bounds how long a fixed credential or config waits: each restart re-reads the spec.
HEALTHY_AFTER = 60
_KIND = re.compile(r"[A-Za-z0-9_.:-]{1,48}")
_TO = re.compile(r"@?[A-Za-z0-9_. -]{1,64}")
HEALTH = "/healthz"


class EventSettingsError(ValueError):
    """[events] holds a value the listener refuses to run with."""


def settings(cfg: dict) -> dict:
    section = (cfg or {}).get("events")
    if section is None:
        section = {}
    if not isinstance(section, dict):
        raise EventSettingsError(f"[events] must be a table, not {section!r}")
    s = {**DEFAULTS, **section}
    for k in ("enabled", "triage"):
        if not isinstance(s[k], bool):
            raise EventSettingsError(f"[events] {k} must be true or false, not {s[k]!r}")
    if not isinstance(s["bind"], str) or not s["bind"]:
        raise EventSettingsError("[events] bind must be an address")
    for k, lo, hi in (("port", 1, 65535), ("max_body_bytes", 1, 64 * 1024 * 1024), ("request_timeout_s", 1, 300), ("min_poll_interval_s", 1, 10**6),
                      ("stall_minutes", 1, 10**6), ("unacked_minutes", 1, 10**6),
                      ("waiter_stale_seconds", 5, 10**6), ("alert_repeat_minutes", 1, 10**6)):
        v = s[k]
        if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
            raise EventSettingsError(f"[events] {k} must be a whole number from {lo} to {hi}, not {v!r}")
    if not isinstance(s["model"], str) or not re.fullmatch(r"[A-Za-z0-9_.:\[\]/-]{1,64}", s["model"]):
        raise EventSettingsError("[events] model must be a model name such as \"haiku\"")
    if not isinstance(s["sources"], dict) or not all(isinstance(v, dict) for v in s["sources"].values()):
        raise EventSettingsError("[events.sources.<name>] must be tables")
    roles = s["orchestrator_roles"]
    if not isinstance(roles, list) or not all(isinstance(r, str) for r in roles):
        raise EventSettingsError("[events] orchestrator_roles must be a list of role names")
    return s


# ---- what a source's handle()/poll() returns, and how it is posted --------------------------------

class SpecError(ValueError):
    pass


def check_spec(spec) -> dict:
    """A well-formed EventSpec or SpecError. Core never looks at what kind or text mean."""
    if not isinstance(spec, dict):
        raise SpecError("an event spec must be a dict")
    job, kind, key, text, to = (spec.get("job"), spec.get("kind"), spec.get("key"), spec.get("text"),
                                spec.get("to"))
    if not isinstance(job, str) or not job:
        raise SpecError("event spec needs a job")
    if not isinstance(kind, str) or not _KIND.fullmatch(kind):
        raise SpecError("event spec kind must be 1-48 of A-Za-z0-9_.:-")
    if not isinstance(key, str) or not 1 <= len(key) <= 200:
        raise SpecError("event spec key must be 1-200 characters")
    if not isinstance(text, str):
        raise SpecError("event spec text must be a string")
    if to is not None and (not isinstance(to, str) or not _TO.fullmatch(to)):
        raise SpecError("event spec to must be a role (@pm) or a name, or None")
    return {"job": job, "kind": kind, "key": key, "text": " ".join(text.split())[:TEXT_MAX], "to": to}


@dataclasses.dataclass
class EventContext:
    """What a source's handle() and poll() get: the board, its own config table, post and log."""
    source: str
    board: object
    config: dict
    log: Callable[[str], None]
    posted: list = dataclasses.field(default_factory=list)

    def post(self, job: str, kind: str, key: str, text: str, to: str | None = None) -> tuple:
        spec = check_spec({"job": job, "kind": kind, "key": key, "text": text, "to": to})
        res = self.board.post_event(spec["job"], spec["kind"], spec["key"], spec["text"],
                                    to=spec["to"], source=self.source)
        self.posted.append(res)
        return res


def post_specs(ctx: EventContext, specs) -> int:
    """Post every spec the source returned (None is none). Returns how many were new. One bad spec is
    logged (never its content) and skipped; the others still go in."""
    new = 0
    for spec in specs or ():
        try:
            _, created = ctx.post(**{k: v for k, v in check_spec(spec).items()})
            new += bool(created)
        except SpecError as exc:
            ctx.log(f"{ctx.source}: dropped a malformed event ({exc})")
    return new


# ---- the HTTP side --------------------------------------------------------------------------------

def _make_handler(listener: "Listener"):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"
        server_version = "swarm-events"
        sys_version = ""

        def setup(self):
            self.timeout = listener.cfg["request_timeout_s"]
            super().setup()

        def log_message(self, *a):   # never the request line: it may carry a query with a token
            pass

        def _reply(self, code: int, body: bytes = b""):
            try:
                self.send_response(code)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if body:
                    self.wfile.write(body)
            except OSError:   # the peer went away (a liveness probe, a timeout)
                pass

        def do_GET(self):
            self._reply(200, b"ok\n") if self.path == HEALTH else self._reply(404)

        def do_POST(self):
            listener.handle_post(self)

        do_PUT = do_DELETE = do_PATCH = lambda self: self._reply(405)
    return Handler


def _proc_addr(ip: str, port: int, v6: bool) -> str:
    """An address as /proc/net/tcp{,6} prints it: each 32-bit word of the address in host byte
    order, as hex, then the port in hex (an IPv4 peer in tcp6 is ::ffff:a.b.c.d)."""
    import ipaddress
    a = ipaddress.ip_address(ip.split("%", 1)[0])
    if v6 and a.version == 4:
        a = ipaddress.IPv6Address(f"::ffff:{a}")
    if (a.version == 6) != v6:
        raise ValueError("other family")
    b = a.packed
    words = [b[i:i + 4] if sys.byteorder == "big" else b[i:i + 4][::-1] for i in range(0, len(b), 4)]
    return "".join(w.hex() for w in words).upper() + f":{int(port):04X}"


def peer_uid(peer, local, proc="/proc/net") -> int | None:
    """The OS user owning the client end of a local TCP connection (peer -> local, both
    (host, port, ...)), from the kernel's socket table; None when not found or unreadable. Another
    user can't fake this: the uid column is the socket owner's, as the kernel records it."""
    for name, v6 in (("tcp", False), ("tcp6", True)):
        try:
            want_l, want_r = _proc_addr(peer[0], peer[1], v6), _proc_addr(local[0], local[1], v6)
            with open(os.path.join(proc, name), encoding="ascii") as fh:
                next(fh, None)
                for line in fh:
                    f = line.split()
                    # ESTABLISHED (01) with a live inode only: a client that sent and closed
                    # leaves a TIME_WAIT/FIN_WAIT row whose uid is 0 (root's), inode 0
                    if len(f) > 9 and f[1] == want_l and f[2] == want_r and f[3] == "01" and f[9] != "0":
                        return int(f[7])
        except (OSError, ValueError, IndexError):
            continue
    return None


def own_peer(peer, local, proc="/proc/net") -> bool:
    """A loopback peer whose socket belongs to this process's OS user (Linux only: elsewhere the
    owner can't be told, so no)."""
    import ipaddress
    try:
        if not ipaddress.ip_address(str(peer[0]).split("%", 1)[0]).is_loopback:
            return False
    except ValueError:
        return False
    return hasattr(os, "getuid") and peer_uid(peer, local, proc) == os.getuid()


class Listener:
    def __init__(self, cfg: dict, registry, board_factory: Callable, log: Callable[[str], None] | None = None,
                 write_token: Callable[[str], None] | None = None):
        import secrets
        self.cfg = settings(cfg)
        # Local routes are served at <route>/<token>: a fresh random token per listener start, kept
        # in the private dir for the helpers' plugin (local_token()). Local users can read it in a
        # helper's argv, but the uid check stops them; it stops a remote sender behind a proxy.
        self.local_token = secrets.token_urlsafe(24)
        self.write_token = write_token or _write_local_token
        self.registry = registry
        self.board_factory = board_factory
        self.log = log or (lambda m: None)
        self.routes = {r: src for src in registry.event_sources.values()
                       for r in src.routes + tuple(getattr(src, "local_routes", ()) or ())}
        self.local_paths = {r for src in registry.event_sources.values()
                            for r in tuple(getattr(src, "local_routes", ()) or ())}
        self.server: ThreadingHTTPServer | None = None
        self.stop = threading.Event()
        self._next_poll: dict[str, float] = {}

    def source_config(self, name: str) -> dict:
        return dict(self.cfg["sources"].get(name) or {})

    def handle_post(self, h: BaseHTTPRequestHandler) -> None:
        path = h.path.split("?", 1)[0]
        local = self._local_route(path)
        src = self.routes.get(local or path)
        if src is None:
            return h._reply(404)
        if local is not None or path in self.local_paths:   # unsigned by design; checked before the body
            why = self._local_refusal(h, path, local)
            if why:
                self.log(f"{src.name}: refused a local delivery ({why})")
                return h._reply(403)
        if h.headers.get("Transfer-Encoding"):
            return h._reply(411)
        raw = h.headers.get("Content-Length")
        if raw is None or not raw.isdigit():
            return h._reply(411)
        n = int(raw)
        if n > self.cfg["max_body_bytes"]:
            self.log(f"{src.name}: refused an oversized body")
            return h._reply(413)
        try:
            body = h.rfile.read(n)
        except (OSError, ValueError):
            return h._reply(400)
        if len(body) != n:
            return h._reply(400)
        if local is None:
            try:
                ok = bool(src.verify(h.headers, body))   # before anything parses the body
            except Exception:
                ok = False
            if not ok:
                self.log(f"{src.name}: refused a request (bad signature)")
                return h._reply(403)
        try:
            with self.board_factory() as board:
                ctx = EventContext(src.name, board, self.source_config(src.name), self.log)
                new = post_specs(ctx, src.handle(h.headers, body, ctx))
        except Exception as exc:   # the type only: its text could quote the body
            self.log(f"{src.name}: handler failed ({type(exc).__name__})")
            return h._reply(500)
        self.log(f"{src.name}: ok, {new} new event(s)")
        h._reply(204)

    def _local_route(self, path: str) -> str | None:
        """The local route `path` is `<route>/<token>` of (whatever the token), else None."""
        base, sep, _ = path.rpartition("/")
        return base if sep and base in self.local_paths else None

    def _local_refusal(self, h, path: str, local: str | None) -> str | None:
        """Why a delivery to a local route is refused (None: served). Only this listener start's
        token, never through a proxy, only from this OS user's own established loopback socket."""
        import hmac as _hmac
        if any(h.headers.get(name) is not None for name in PROXY_HEADERS):
            return "proxy headers: local routes are never served through a proxy"
        token = path.rpartition("/")[2] if local is not None else ""
        if not token or not _hmac.compare_digest(token.encode(), self.local_token.encode()):
            return "wrong or missing route token"
        if not own_peer(h.client_address, h.connection.getsockname()):
            return "not an established loopback socket of this OS user"
        return None

    # ---- polls

    def poll_interval(self, src) -> int:
        """The source's interval, never below [events] min_poll_interval_s unless the operator's
        [events.sources.<name>] poll_interval_s says so explicitly (callbacks, not polling)."""
        own = self.source_config(src.name).get("poll_interval_s")
        if isinstance(own, int) and not isinstance(own, bool) and own >= 1:
            return own
        return max(src.poll_interval_s, self.cfg["min_poll_interval_s"])

    def poll_due(self, now: float | None = None) -> int:
        """Run every source's poll that is due (a failing one is logged and retried at its next interval)."""
        now = time.monotonic() if now is None else now
        ran = 0
        for src in self.registry.event_sources.values():
            if src.poll is None or self._next_poll.get(src.name, 0) > now:
                continue
            self._next_poll[src.name] = now + self.poll_interval(src)
            ran += 1
            DEBUG.debug("%s: polling (calls its API)", src.name)
            try:
                with self.board_factory() as board:
                    ctx = EventContext(src.name, board, self.source_config(src.name), self.log)
                    post_specs(ctx, src.poll(ctx))
            except Exception as exc:
                self.log(f"{src.name}: poll failed ({type(exc).__name__})")
        return ran

    def _poll_loop(self):
        while not self.stop.wait(1.0):
            self.poll_due()

    # ---- running

    def bind(self) -> tuple[str, int]:
        self.server = ThreadingHTTPServer((self.cfg["bind"], self.cfg["port"]), _make_handler(self))
        self.server.daemon_threads = True
        return self.server.server_address[:2]

    def serve_forever(self) -> None:
        if self.server is None:
            self.bind()
        threading.Thread(target=self._poll_loop, daemon=True).start()
        if self.local_paths:   # before any helper starts: they build their URL from it
            try:
                self.write_token(self.local_token)
            except Exception as exc:
                self.log(f"cannot keep the local-route token ({type(exc).__name__}): local routes refuse all")
                self.local_token = ""
        self.helpers = HelperSupervisor(self, self.log)
        threading.Thread(target=self.helpers.run, daemon=True).start()
        try:
            self.server.serve_forever(poll_interval=0.5)
        finally:
            self.stop.set()
            if getattr(self, "helpers", None):
                self.helpers.stop_all()
            self.server.server_close()

    def shutdown(self) -> None:
        self.stop.set()
        if self.server is not None:
            self.server.shutdown()


# ---- per-source helper processes ------------------------------------------------------------------

class HelperSupervisor:
    """Runs each source's helpers (`helpers(config)` -> [dict(name, argv, env=None)]), restarts one that
    exits with backoff (BACKOFF; reset once it has been up HEALTHY_AFTER s) and keeps a health file in
    the supervisor's private directory for the safety nets: {"beat": epoch, "helpers": {"src/name":
    {"up": bool, "pid": int, "restarts": int, "since": epoch}}}. Output is discarded: it may carry tokens."""

    def __init__(self, listener: "Listener", log, popen=subprocess.Popen, clock=time.time, write=None):
        self.l, self.log, self.popen, self.clock = listener, log, popen, clock
        self.write = write or _write_health
        self.state: dict[str, dict] = {}
        self.procs: dict[str, object] = {}
        self.specs: dict[str, dict] = {}
        self.retry_at: dict[str, float] = {}
        self.stop = threading.Event()

    def _specs_of(self, src) -> dict[str, dict] | None:
        """{key: spec} of one source's helpers, or None when helpers() fails."""
        try:
            specs = src.helpers(self.l.source_config(src.name)) or []
        except Exception as exc:
            self.log(f"{src.name}: helpers() failed ({type(exc).__name__})")
            return None
        out = {}
        for h in specs:
            if (isinstance(h, dict) and isinstance(h.get("name"), str) and isinstance(h.get("argv"), list)
                    and h["argv"] and all(isinstance(a, str) for a in h["argv"])):
                out[f"{src.name}/{h['name']}"] = h
            else:
                self.log(f"{src.name}: ignored a malformed helper spec")
        return out

    def load(self) -> None:
        for src in self.l.registry.event_sources.values():
            if src.helpers is not None:
                self.specs.update(self._specs_of(src) or {})

    def _fresh(self, key: str, h: dict) -> dict:
        """The spec of `key` as its source gives it now (a restart picks up a rotated secret, a new
        config or credential), else the one loaded before."""
        src = self.l.registry.event_sources.get(key.split("/", 1)[0])
        if src is None or src.helpers is None:
            return h
        new = (self._specs_of(src) or {}).get(key)
        if new is not None:
            self.specs[key] = new
        return new or h

    def step(self) -> None:
        """One supervision tick: start what is due, notice exits, write the health file."""
        now = self.clock()
        for key, h in self.specs.items():
            st = self.state.setdefault(key, {"up": False, "pid": 0, "restarts": 0, "since": now, "fails": 0})
            proc = self.procs.get(key)
            if proc is not None and proc.poll() is not None:
                st["fails"] = 0 if now - st["since"] >= HEALTHY_AFTER else st["fails"] + 1
                st.update(up=False, pid=0)
                self.procs.pop(key)
                self.retry_at[key] = now + BACKOFF[min(max(st["fails"], 1), len(BACKOFF)) - 1]
                self.log(f"{key}: helper exited (rc {proc.returncode}), restart in "
                         f"{int(self.retry_at[key] - now)} s")
            if key not in self.procs and self.retry_at.get(key, 0) <= now:
                if key in self.retry_at:   # a restart: the source's spec as it is now
                    h = self._fresh(key, h)
                try:
                    p = self.popen(h["argv"], env={**os.environ, **(h.get("env") or {})},
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)
                    self.procs[key] = p
                    st.update(up=True, pid=p.pid, since=now, restarts=st["restarts"] + (1 if st["restarts"] or st["fails"] else 0))
                    self.log(f"{key}: helper started")
                except OSError as exc:
                    st["fails"] += 1
                    self.retry_at[key] = now + BACKOFF[min(max(st["fails"], 1), len(BACKOFF)) - 1]
                    self.log(f"{key}: helper failed to start ({type(exc).__name__})")
        try:
            self.write({"beat": now, "helpers": {k: {**{f: v[f] for f in ("up", "pid", "restarts", "since")},
                                                     **({} if v["up"] or k not in self.retry_at
                                                        else {"retry_at": self.retry_at[k]})}
                                                 for k, v in self.state.items()}})
        except Exception:
            pass

    def run(self) -> None:
        self.load()
        if not self.specs:
            return
        while not self.stop.is_set():
            self.step()
            self.stop.wait(2.0)

    def stop_all(self) -> None:
        self.stop.set()
        for p in self.procs.values():
            try:
                p.terminate()
            except Exception:
                pass


def _write_local_token(token: str) -> None:
    from swarm.supervisor import settings as sup
    sup.write_private(LOCAL_TOKEN_FILE, token)


def local_token() -> str | None:
    """The running listener's local-route token (for a plugin building its helper's URL), or None."""
    from swarm.supervisor import settings as sup
    raw = sup.read_private(LOCAL_TOKEN_FILE)
    token = raw.decode("ascii", "replace").strip() if raw else ""
    return token if re.fullmatch(r"[A-Za-z0-9_-]{16,128}", token) else None


def _write_health(data: dict) -> None:
    from swarm.supervisor import settings as sup
    sup.write_private(HELPERS_FILE, json.dumps(data, sort_keys=True))


def read_health() -> dict | None:
    from swarm.supervisor import settings as sup
    try:
        d = json.loads(sup.read_private(HELPERS_FILE) or b"")
        return d if isinstance(d, dict) else None
    except ValueError:
        return None


# ---- liveness, start, supervision -----------------------------------------------------------------

def probe(cfg: dict, timeout: float = 2.0) -> bool:
    """Is a listener answering on the configured address?"""
    s = settings(cfg)
    host = "127.0.0.1" if s["bind"] in ("0.0.0.0", "") else "::1" if s["bind"] == "::" else s["bind"]
    try:
        with socket.create_connection((host, s["port"]), timeout=timeout) as c:
            c.sendall(f"GET {HEALTH} HTTP/1.0\r\n\r\n".encode())
            return c.recv(64).startswith(b"HTTP/")
    except OSError:
        return False


def _lock_path():
    from swarm.supervisor import settings as sup
    return sup.private_dir() / "events-listener.lock"


def take_lock():
    """The single-listener lock (flock on a file of the private dir), or None when held."""
    import fcntl
    from swarm.supervisor import settings as sup
    sup.ensure_private_dir()
    fd = os.open(_lock_path(), os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


def lock_held() -> bool:
    fd = take_lock()
    if fd is None:
        return True
    os.close(fd)
    return False


def launch_env(environ, lib) -> dict:
    """The environment bin/swarm gives `python -m swarm.cli`: PYTHONPATH with the plugin's lib, and
    its bytecode cache (paths.pycache_env: PYTHONPYCACHEPREFIX in the private cache dir, else
    PYTHONDONTWRITEBYTECODE=1), so processes the supervisor starts compile nothing on every start
    (no `-B`) and write nothing into the plugin tree."""
    from swarm import paths
    env = {**environ, "PYTHONPATH": str(lib)}
    env.update(paths.pycache_env(environ))
    return env


def start_detached(cfg: dict, popen=None) -> int:
    """Start `swarm events serve` detached (the supervisor pass does this when the listener is down)."""
    import subprocess
    from swarm import paths
    popen = popen or subprocess.Popen
    env = launch_env(os.environ, paths.LIB_DIR)
    cmd = [sys.executable, "-m", "swarm.cli"]
    if cfg.get("_config_path"):
        cmd += ["--config", str(cfg["_config_path"])]
    proc = popen(cmd + ["events", "serve"], env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                 stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)
    return proc.pid


def ensure_running(cfg: dict, say=print, *, probe_fn=probe, lock_fn=lock_held, start=start_detached) -> str:
    """The supervisor's keep-alive: "off", "up", "started" or "stuck" (lock held, not answering: left alone)."""
    s = settings(cfg)
    if not s["enabled"]:
        return "off"
    if probe_fn(cfg):
        return "up"
    if lock_fn():
        say("events listener holds its lock but does not answer: not starting a second one")
        return "stuck"
    pid = start(cfg)
    say(f"events listener was down: started it (pid {pid})")
    return "started"


def serve(cfg: dict, registry, board_factory, log=None) -> int:
    """`swarm events serve`: run until stopped."""
    s = settings(cfg)
    if not s["enabled"]:
        print("swarm events serve: [events] enabled = false: nothing to serve", file=sys.stderr)
        return 1
    if not registry.event_sources:
        print("swarm events serve: no plugin registered an event source", file=sys.stderr)
        return 1
    lock = take_lock()
    if lock is None:
        print("swarm events serve: another listener is running", file=sys.stderr)
        return 1
    try:
        lst = Listener(cfg, registry, board_factory, log=log)
        host, port = lst.bind()
        print(f"swarm events: serving {', '.join(sorted(lst.routes))} on {host}:{port}", flush=True)
        try:
            lst.serve_forever()
        except KeyboardInterrupt:
            pass
        return 0
    finally:
        os.close(lock)
