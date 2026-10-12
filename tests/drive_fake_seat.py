#!/usr/bin/env python3
"""A scripted stand-in for `claude -p`, started by the real runner in the session driver's end-to-end tests
(tests/test_drive_session_driver.py). Stdlib only. It goes through the REAL hooks (`swarm hook --host claude
turn|done`, with the session id the runner bound to its marker) around every `swarm` command it runs, like a
Claude session, and prints a Claude-style result line with `total_cost_usd`.

The lead posts GOAL-READY for a report file in its workdir; the goal judge records `met` for the candidate and
requirements digest its prompt names. Everything it does is appended to $FAKE_SEAT_LOG (JSON lines)."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import uuid


def opt(args, flag):
    return args[args.index(flag) + 1] if flag in args and args.index(flag) + 1 < len(args) else None


def log(**kw) -> None:
    path = os.environ.get("FAKE_SEAT_LOG")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"pid": os.getpid(), "ppid": os.getppid(), **kw}) + "\n")


class Seat:
    def __init__(self, sid: str) -> None:
        self.sid = sid
        self.swarm = os.environ["FAKE_SWARM"]
        # the CLI on PATH for the hooks (their shell guard resolves the bare word) and for the commands
        os.environ["PATH"] = os.path.dirname(self.swarm) + os.pathsep + os.environ.get("PATH", "")

    def hook(self, event: str, **extra):
        payload = {"session_id": self.sid, "cwd": os.getcwd(), **extra}
        r = subprocess.run([sys.executable, "-m", "swarm.cli", "hook", "--host", "claude", event],
                           input=json.dumps(payload), capture_output=True, text=True, timeout=120)
        try:
            return json.loads(r.stdout) if r.stdout.strip() else None
        except ValueError:
            return None

    def run(self, *args: str) -> subprocess.CompletedProcess:
        argv = ["swarm", *args]            # the CLI on PATH, as an agent types it (FAKE_SWARM's directory)
        if os.name == "nt":                # cmd.exe runs it: double quotes
            cmd = subprocess.list2cmdline(argv)
        else:
            import shlex
            cmd = shlex.join(argv)
        out = self.hook("turn", tool_name="Bash", tool_input={"command": cmd})
        spec = (out or {}).get("hookSpecificOutput") or {}
        if spec.get("permissionDecision") == "deny":
            log(ev="denied", cmd=cmd, why=spec.get("permissionDecisionReason"))
            return subprocess.CompletedProcess(argv, 126, "", "denied")
        cmd = ((spec.get("updatedInput") or {}).get("command")) or cmd
        line = cmd
        if os.name == "nt":
            # Claude Code runs its Bash tool in Git Bash on Windows, and the hook's rewrites (`--as 'Name'`)
            # are POSIX shell words: split them as bash would, then hand cmd.exe the same words in its own
            # quoting (cmd.exe resolves `swarm` to swarm.cmd on PATH)
            import shlex
            line = subprocess.list2cmdline(shlex.split(cmd))
        r = subprocess.run(line, shell=True, capture_output=True, text=True, timeout=120)
        self.hook("done", tool_name="Bash", tool_input={"command": cmd},
                  tool_response={"stdout": r.stdout, "stderr": r.stderr, "interrupted": False})
        log(ev="cmd", cmd=cmd, rc=r.returncode, out=r.stdout[-400:], err=r.stderr[-400:])
        return r


def main() -> int:
    args = sys.argv[1:]
    prompt = sys.stdin.read()
    sid = opt(args, "--session-id") or opt(args, "--resume") or str(uuid.uuid4())
    m = re.search(r"^\[swarm job: ([^\]]+)\]", prompt, re.M)
    job = m.group(1) if m else "?"
    seat = Seat(sid)
    log(ev="start", sid=sid, job=job, head=prompt[:300])
    try:   # a root session: no SubagentStart; its first tool call's hook enrols it in its predecessor's seat
        seat.run("read", "--job", job)
        if "You are the goal judge" in prompt:
            # inline, or in its fenced block (which comes before the board's posts)
            found = re.findall(r"(?<![=\w:])(?:report:)?[A-Za-z0-9._/-]+@[0-9a-f]{7,64}", prompt)
            cand = found[0] if found else re.search(r"Judge candidate (\S+) against", prompt).group(1)
            req = re.search(r"--req ([0-9a-f]+)", prompt).group(1)
            seat.run("verdict", "--job", job, "--scope", "goal", "--candidate", cand, "--req", req, "met",
                     "the report meets the goal")
        elif "You are the lead" in prompt:
            report = os.path.join(os.getcwd(), "REPORT.md")
            with open(report, "w", encoding="utf-8") as fh:
                fh.write("# Report\nThe goal is met.\n")
            data = open(report, "rb").read()
            import hashlib
            blob = hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()
            seat.run("goal-ready", "--job", job, "--candidate", f"report:REPORT.md@{blob}")
        else:
            log(ev="idle-role", sid=sid)
    finally:
        seat.hook("session-stop")
        log(ev="end", sid=sid)
    print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "session_id": sid,
                      "total_cost_usd": 0.01, "num_turns": 2, "result": "done"}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
