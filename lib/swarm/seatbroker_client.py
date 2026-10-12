"""The swarm command and hooks of a jailed drive seat (Task 20). The seat has no board password and no
host-private file, so it hands each call to its runner's broker (swarm.supervisor.seatbroker) through its broker
directory ($SWARM_SEAT_BROKER, bound into this seat's jail only), as a TYPED request (swarm.seatproto):

- `swarm ...`: parsed here against seatproto.SCHEMA (only seat commands and their fields; --as/--key are dropped,
  the broker injects who the seat is); a file named on the command line is read here, in the jail, and sent as
  its content;
- a hook: its event name and payload; the broker keeps only the payload fields it knows.

A guard hook (PreToolUse) that gets no answer fails CLOSED: the tool call is denied.

Standard library only: run as `python3 -I seatbroker_client.py cli|hook ARG...`; seatproto is loaded from this
file's own directory by path."""
from __future__ import annotations

import base64
import importlib.util
import json
import os
import secrets
import sys
import time

STDIN_MAX = 2 * 1024 * 1024
TIMEOUT = {"cli": 960.0, "hook": 30.0}
POLL = 0.05
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)   # Windows has none
_BINARY = getattr(os, "O_BINARY", 0)


def _proto():
    here = os.path.dirname(os.path.abspath(__file__))
    spec = importlib.util.spec_from_file_location("_swarm_seatproto", os.path.join(here, "seatproto.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def request(body: dict, directory: str, timeout: float, clock=time.monotonic,
            sleep=time.sleep) -> tuple[int, bytes, bytes] | None:
    """Send one request; (rc, stdout, stderr), or None when no answer came in time."""
    rid = secrets.token_hex(16)
    tmp = os.path.join(directory, f"{rid}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW | _BINARY, 0o600)
    try:
        os.write(fd, json.dumps(body).encode())
    finally:
        os.close(fd)
    os.rename(tmp, os.path.join(directory, f"{rid}.req"))
    resp = os.path.join(directory, f"{rid}.resp")
    deadline = clock() + timeout
    while clock() < deadline:
        try:
            fd = os.open(resp, os.O_RDONLY | _NOFOLLOW | _BINARY)
        except FileNotFoundError:
            sleep(POLL)
            continue
        try:
            raw = b""
            while True:
                chunk = os.read(fd, 1 << 20)
                if not chunk:
                    break
                raw += chunk
        finally:
            os.close(fd)
        os.unlink(resp)
        r = json.loads(raw)
        return int(r["rc"]), base64.b64decode(r.get("out") or ""), base64.b64decode(r.get("err") or "")
    try:
        os.unlink(os.path.join(directory, f"{rid}.req"))   # never answered: withdraw it if still queued
    except OSError:
        pass
    return None


def deny_output(event: str, why: str) -> bytes:
    """What a guard hook prints when it cannot run: the tool call is denied (fail closed)."""
    if event != "turn":
        return b""
    return json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                              "permissionDecisionReason": f"[swarm] {why}: this tool call is "
                                                                          "refused; try again"}}).encode() + b"\n"


def run_hook(argv: list[str], directory: str, stdin_bytes: bytes, **kw) -> tuple[int, bytes, bytes]:
    event = argv[-1] if argv else ""
    try:
        payload = json.loads(stdin_bytes or b"{}")
    except ValueError:
        payload = {}
    body = {"v": 2, "kind": "hook", "event": event, "payload": payload if isinstance(payload, dict) else {}}
    if not os.path.isdir(directory):
        return 0, deny_output(event, "the seat broker is missing"), b""
    try:
        r = request(body, directory, kw.pop("timeout", TIMEOUT["hook"]), **kw)
    except OSError:
        r = None
    if r is None:
        return 0, deny_output(event, "the seat broker did not answer"), b""
    if r[0] != 0 and event == "turn" and not r[1].strip():
        return 0, deny_output(event, "the seat's guard hook failed"), r[2]
    return r


def run_cli(argv: list[str], directory: str, **kw) -> tuple[int, bytes, bytes]:
    proto = _proto()
    try:
        body = proto.parse_cli(argv, os.getcwd(), sys.stdin if "-" in argv else None)
    except proto.ProtocolError as exc:
        return 2, b"", f"swarm: refused in a drive seat: {exc}\n".encode()
    if not os.path.isdir(directory):
        return 1, b"", b"swarm: no seat broker here ($SWARM_SEAT_BROKER)\n"
    r = request(body, directory, kw.pop("timeout", TIMEOUT["cli"]), **kw)
    if r is None:
        return 124, b"", b"swarm: the seat broker did not answer in time (is the seat's runner alive?)\n"
    return r


def main(args: list[str]) -> int:
    if not args or args[0] not in TIMEOUT:
        print("usage: seatbroker_client.py cli|hook ARG...", file=sys.stderr)
        return 2
    kind, argv = args[0], args[1:]
    directory = os.environ.get("SWARM_SEAT_BROKER", "")
    if kind == "hook":
        data = sys.stdin.buffer.read(STDIN_MAX)
        rc, out, err = run_hook(argv, directory, data)
    else:
        rc, out, err = run_cli(argv, directory)
    for stream, data in ((sys.stdout, out), (sys.stderr, err)):
        buf = getattr(stream, "buffer", None)
        if buf is not None:
            buf.write(data)
        else:
            stream.write(data.decode("utf-8", "replace"))
        stream.flush()
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
