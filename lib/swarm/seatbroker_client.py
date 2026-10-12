"""The swarm command and hooks of a jailed drive seat (Task 20, rr4): the seat has no board password and no
host-private file, so every `swarm ...` and every swarm hook it runs is handed, as a request file, to its
runner's broker (swarm.supervisor.seatbroker) through the seat's broker directory ($SWARM_SEAT_BROKER, bound
into this seat's jail only). The broker runs it outside the jail as this seat (who the seat is comes from
the seat's launch record, never from the request) and returns its output and exit status.

Standard library only: run as `python3 -I seatbroker_client.py cli|hook ARG...`."""
from __future__ import annotations

import base64
import json
import os
import secrets
import sys
import time

STDIN_MAX = 2 * 1024 * 1024
TIMEOUT = {"cli": 960.0, "hook": 60.0}
POLL = 0.05


def _stdin(kind: str, argv: list[str]) -> bytes | None:
    if kind == "hook" or "-" in argv:
        data = sys.stdin.buffer.read(STDIN_MAX + 1)
        if len(data) > STDIN_MAX:
            raise SystemExit("swarm: input too large for the seat broker")
        return data
    return None


def request(kind: str, argv: list[str], directory: str, stdin: bytes | None = None,
            timeout: float | None = None, clock=time.monotonic, sleep=time.sleep) -> tuple[int, bytes, bytes]:
    rid = secrets.token_hex(16)
    body = json.dumps({"v": 1, "kind": kind, "argv": argv, "cwd": os.getcwd(),
                       "stdin": None if stdin is None else base64.b64encode(stdin).decode()}).encode()
    tmp = os.path.join(directory, f"{rid}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        os.write(fd, body)
    finally:
        os.close(fd)
    os.rename(tmp, os.path.join(directory, f"{rid}.req"))
    resp = os.path.join(directory, f"{rid}.resp")
    deadline = clock() + (TIMEOUT[kind] if timeout is None else timeout)
    while clock() < deadline:
        try:
            fd = os.open(resp, os.O_RDONLY | os.O_NOFOLLOW)
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
    return 124, b"", b"swarm: the seat broker did not answer in time (is the seat's runner alive?)\n"


def main(args: list[str]) -> int:
    if not args or args[0] not in TIMEOUT:
        print("usage: seatbroker_client.py cli|hook ARG...", file=sys.stderr)
        return 2
    kind, argv = args[0], args[1:]
    directory = os.environ.get("SWARM_SEAT_BROKER", "")
    if not os.path.isdir(directory):
        print("swarm: no seat broker here ($SWARM_SEAT_BROKER)", file=sys.stderr)
        return 0 if kind == "hook" else 1
    rc, out, err = request(kind, argv, directory, _stdin(kind, argv))
    sys.stdout.buffer.write(out)
    sys.stdout.flush()
    sys.stderr.buffer.write(err)
    sys.stderr.flush()
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
