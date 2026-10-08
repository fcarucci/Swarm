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
BACKOFF = (5, 15, 60, 300)   # seconds before a helper restart; reset after HEALTHY_AFTER s up
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


class Listener:
    def __init__(self, cfg: dict, registry, board_factory: Callable, log: Callable[[str], None] | None = None):
        self.cfg = settings(cfg)
        self.registry = registry
        self.board_factory = board_factory
        self.log = log or (lambda m: None)
        self.routes = {r: src for src in registry.event_sources.values() for r in src.routes}
        self.server: ThreadingHTTPServer | None = None
        self.stop = threading.Event()
        self._next_poll: dict[str, float] = {}

    def source_config(self, name: str) -> dict:
        return dict(self.cfg["sources"].get(name) or {})

    def handle_post(self, h: BaseHTTPRequestHandler) -> None:
        path = h.path.split("?", 1)[0]
        src = self.routes.get(path)
        if src is None:
            return h._reply(404)
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

    def load(self) -> None:
        for src in self.l.registry.event_sources.values():
            if src.helpers is None:
                continue
            try:
                specs = src.helpers(self.l.source_config(src.name)) or []
            except Exception as exc:
                self.log(f"{src.name}: helpers() failed ({type(exc).__name__})")
                continue
            for h in specs:
                if (isinstance(h, dict) and isinstance(h.get("name"), str) and isinstance(h.get("argv"), list)
                        and h["argv"] and all(isinstance(a, str) for a in h["argv"])):
                    self.specs[f"{src.name}/{h['name']}"] = h
                else:
                    self.log(f"{src.name}: ignored a malformed helper spec")

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
            self.write({"beat": now, "helpers": {k: {f: v[f] for f in ("up", "pid", "restarts", "since")}
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


def start_detached(cfg: dict, popen=None) -> int:
    """Start `swarm events serve` detached (the supervisor pass does this when the listener is down)."""
    import subprocess
    from swarm import paths
    popen = popen or subprocess.Popen
    env = {**os.environ, "PYTHONPATH": str(paths.LIB_DIR)}
    cmd = [sys.executable, "-B", "-m", "swarm.cli"]
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
