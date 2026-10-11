#!/usr/bin/env python3
"""A scripted stand-in for `claude -p` and `codex exec`, started by the real supervisor runner.

It behaves like an agent session as far as the board can tell: it reads its prompt on stdin, finds its
`[swarm job: J]` / `[swarm role: R]` / seat tags, goes through the REAL hooks (`bin/swarm-hook turn|done|stop`,
with a synthetic session id, as tests/test_pause_e2e.py does) around every `swarm` command it runs, does
the role's work with real git against the fake forge, and prints a Claude-style result JSON with
`total_cost_usd` (or Codex-style JSONL). Behaviour per role comes from the scenario script (scripts/*.json):
the n-th session of a role (or of a seat) takes the n-th entry of a list.

Stdlib only; it finds its run directory from $HOME (<run>/home) and reads <run>/harness.json
({"repo": <checkout under test>, "script": <script path>, "forge": <forge dir>, "job": <job>}).
Role commands are in the ROLE_* functions: if the integrated drive prompts or CLI differ from the plan
(plan Tasks 6b, 10, 11), this is the one place to adapt.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

HOME = Path(os.environ["HOME"])
RUN = HOME.parent
CONF = json.loads((RUN / "harness.json").read_text())
REPO = Path(CONF["repo"])
FORGE = Path(CONF["forge"])
SCRIPT = json.loads(Path(CONF["script"]).read_text())
LOGS = RUN / "logs"
SWARM = str(RUN / "bin" / "swarm")
HOOK = str(REPO / "bin" / "swarm-hook")
FORGE_PY = str(Path(__file__).resolve().parent / "forge.py")


def tag(prompt: str, name: str) -> str | None:
    import re
    m = re.search(r"^\[swarm %s: ([^\]]+)\]" % name, prompt, re.M)
    return m.group(1).strip() if m else None


def log(**kw) -> None:
    LOGS.mkdir(parents=True, exist_ok=True)
    with open(LOGS / "harness.jsonl", "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        fh.write(json.dumps({"at": time.time(), "pid": os.getpid(), **kw}) + "\n")


def counter(key: str) -> int:
    """How many sessions of `key` started before this one (persisted, locked)."""
    p = RUN / "harness-state.json"
    with open(RUN / "harness-state.lock", "a") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        st = json.loads(p.read_text()) if p.exists() else {}
        n = st.get(key, 0)
        st[key] = n + 1
        p.write_text(json.dumps(st))
    return n


def nth(entries, n: int, default=None):
    if not isinstance(entries, list):
        return entries if entries is not None else default
    return entries[min(n, len(entries) - 1)] if entries else default


class Session:
    def __init__(self, sid: str, job: str, role: str, seat: str | None, model: str | None):
        self.sid, self.job, self.role, self.seat, self.model = sid, job, role, seat, model
        self.denied: str | None = None

    # -- a tool call, the way Claude does it: PreToolUse hook, the command, PostToolUse hook
    def hook(self, event: str, **extra) -> dict | None:
        payload = {"session_id": self.sid, "agent_id": None, "cwd": str(RUN / "work"), **extra}
        r = subprocess.run([HOOK, event], input=json.dumps(payload), capture_output=True, text=True,
                           env=os.environ, timeout=60)
        try:
            return json.loads(r.stdout) if r.stdout.strip() else None
        except ValueError:
            return None

    def sh(self, argv: list[str], *, input: str | None = None, cwd: str | None = None,
           timeout: float = 300) -> subprocess.CompletedProcess:
        cmd = " ".join(argv)
        out = self.hook("turn", tool_name="Bash", tool_input={"command": cmd})
        spec = (out or {}).get("hookSpecificOutput") or {}
        if spec.get("permissionDecision") == "deny":
            self.denied = spec.get("permissionDecisionReason", "denied")
            log(ev="denied", sid=self.sid, role=self.role, cmd=cmd[:160], why=self.denied[:200])
            return subprocess.CompletedProcess(argv, 126, "", self.denied)
        r = subprocess.run(argv, input=input, capture_output=True, text=True, cwd=cwd, env=os.environ,
                           timeout=timeout)
        self.hook("done", tool_name="Bash", tool_input={"command": cmd},
                  tool_response={"stdout": r.stdout[-2000:], "stderr": r.stderr[-500:], "exit_code": r.returncode})
        log(ev="cmd", sid=self.sid, role=self.role, seat=self.seat, cmd=cmd[:200], rc=r.returncode)
        return r

    def swarm(self, *a, **kw):
        return self.sh([SWARM, *map(str, a)], **kw)

    def git(self, cwd: Path, *a):
        return subprocess.run(["git", *a], cwd=cwd, capture_output=True, text=True, env=os.environ)


# --- the work an engineer does to the toy repo -----------------------------------------------

CHANGES = {
    "version": ("toy.py", None),
    "impossible": ("tests/test_impossible.py", None),
}


def edit_toy(w: Path, change: str, round_no: int) -> None:
    if change == "impossible":
        (w / "tests").mkdir(exist_ok=True)
        (w / "tests" / "test_impossible.py").write_text(
            "import unittest\n\n\nclass Impossible(unittest.TestCase):\n"
            "    def test_both(self):\n        self.assertEqual(1, 2)   # the goal wants this to pass AND stay as is\n")
        return
    src = (w / "toy.py").read_text()
    if "def version" not in src:
        src = src.replace("def main(", 'def version() -> str:\n    return "toy 0.1.0"\n\n\ndef main(')
        src = src.replace("    argv = list(sys.argv[1:] if argv is None else argv)\n",
                          "    argv = list(sys.argv[1:] if argv is None else argv)\n"
                          "    if argv == [\"--version\"]:\n        print(version())\n        return 0\n")
        (w / "toy.py").write_text(src)
        t = w / "tests" / "test_toy.py"
        t.write_text(t.read_text().replace("\n\nif __name__", "\n\nclass Version(unittest.TestCase):\n"
                                           "    def test_version(self):\n"
                                           "        self.assertEqual(toy.version(), \"toy 0.1.0\")\n\n\nif __name__"))
    if round_no:   # a fix round: a further, real test
        t = w / "tests" / "test_toy.py"
        t.write_text(t.read_text().replace("\n\nif __name__", f"\n\nclass Round{round_no}(unittest.TestCase):\n"
                                           f"    def test_main_version(self):\n"
                                           f"        self.assertEqual(toy.main([\"--version\"]), 0)\n\n\nif __name__"))


# --- roles ------------------------------------------------------------------------------------

def hang_if_asked(s: Session, spec: dict, key: str) -> None:
    """Chaos hook: `"hang_once": true` makes the FIRST session of this seat write its pid and sleep, so a
    test can SIGKILL it mid-task; the relaunch (second session) continues normally."""
    if spec.get("hang_once") and counter("hang:" + key) == 0:
        hd = RUN / "hang"
        hd.mkdir(exist_ok=True)
        (hd / f"{key}.pid").write_text(str(os.getpid()))
        log(ev="hang", sid=s.sid, role=s.role, key=key)
        time.sleep(float(spec.get("hang_seconds", 600)))


def role_lead(s: Session, n: int) -> None:
    lead = nth(SCRIPT.get("lead"), n, {}) or {}
    if n == 0:
        ask = lead.get("ask")
        if ask:   # a human question with a default that expires (spec 3.10); the lead keeps working
            s.swarm("ask", "--job", s.job, "--to", "human", ask["text"], "--options", ask.get("options", "left,right"),
                    "--default", ask.get("default", "left"), "--expires", ask.get("expires", "2m"),
                    "--blocks", "nothing: the seats go on")
        for seat in lead.get("seats", []):
            brief = RUN / "briefs" / f"{seat['seat']}.md"
            brief.parent.mkdir(exist_ok=True)
            brief.write_text(f"# {seat['seat']}\nDo the {seat['role']} work for the goal. Branch feat/{seat['seat']}.\n")
            args = ["seat", "request", "--job", s.job, "--seat", seat["seat"], "--role", seat["role"],
                    "--brief", str(brief)]
            if seat.get("complexity"):
                args += ["--complexity", seat["complexity"]]
            if seat["role"] == "engineer":
                args += ["--workdir", str(RUN / "work" / seat["seat"])]
            s.swarm(*args)
        return
    head = subprocess.run(["git", "--git-dir", str(FORGE / "origin.git"), "rev-parse", "main"],
                          capture_output=True, text=True).stdout.strip()
    landed = (RUN / "landed.json").exists() or head != CONF.get("initial_head")
    if landed:
        s.swarm("goal-ready", "--job", s.job, "--candidate", f"main@{head}")


def role_engineer(s: Session, n: int) -> None:
    spec = nth(SCRIPT.get("engineer"), 0, {}) or {}
    hang_if_asked(s, spec, s.seat or "engineer")
    w = RUN / "work" / (s.seat or "engineer")
    if not (w / ".git").exists():
        w.parent.mkdir(parents=True, exist_ok=True)
        s.git(w.parent, "clone", "-q", "file://" + str(FORGE / "origin.git"), str(w))
        s.git(w, "checkout", "-q", "-b", f"feat/{s.seat}")
    edit_toy(w, spec.get("change", "version"), n)
    s.git(w, "add", "-A")
    s.git(w, "commit", "-q", "-m", f"{s.seat}: round {n}")
    sha = s.git(w, "rev-parse", "HEAD").stdout.strip()
    s.git(w, "push", "-q", "-f", "origin", f"feat/{s.seat}")
    ref = f"feat/{s.seat}@{sha}"
    (RUN / "handoff.json").write_text(json.dumps({"artifact": ref, "seat": s.seat, "round": n}))
    s.swarm("done", "--job", s.job, "--artifact", ref, "--summary", f"round {n}: toy change + test")


def current_artifact(prompt: str) -> str | None:
    import re
    m = re.findall(r"[A-Za-z0-9._/-]+@[0-9a-f]{40}", prompt)
    if m:
        return m[-1]
    p = RUN / "handoff.json"
    return json.loads(p.read_text())["artifact"] if p.exists() else None


def role_reviewer(s: Session, n: int, prompt: str) -> None:
    art = current_artifact(prompt)
    outcome = (nth(SCRIPT.get("reviewer"), n, {}) or {}).get("outcome", "verified")
    rep = RUN / "reports" / f"review-{s.sid[:8]}.md"
    rep.parent.mkdir(exist_ok=True)
    rep.write_text(f"# review {outcome}\nline-by-line: ok. mutants: none surviving.\n")
    s.swarm("review", "--job", s.job, "--artifact", art, outcome, "--report", str(rep))


def role_judge(s: Session, n: int, prompt: str) -> None:
    ent = nth(SCRIPT.get("judge"), n, {"verdict": "met"}) or {}
    art = current_artifact(prompt)
    v = ent.get("verdict", "met")
    extra = ["--artifact", art] if art else []
    if v == "met":
        s.swarm("verdict", "met", ent.get("reason", "criteria met"), "--job", s.job, *extra)
    elif v == "not_met":
        s.swarm("verdict", "not_met", "--job", s.job, "--reason", ent.get("reason", "criteria open"),
                "--open", ent.get("open", 1), *extra)
    else:
        s.swarm("verdict", "unreachable", "--job", s.job, "--reason", ent.get("reason", "cannot be met"),
                "--details", "-", *extra, input=f"# unreachable\n{ent.get('reason', '')}\n")


def role_integrator(s: Session, n: int, prompt: str) -> None:
    art = current_artifact(prompt)
    branch = art.split("@")[0]
    w = RUN / "work" / "integrator"
    if not (w / ".git").exists():
        s.git(w.parent, "clone", "-q", "file://" + str(FORGE / "origin.git"), str(w))
    s.git(w, "fetch", "-q", "--all")
    s.git(w, "checkout", "-q", "-B", branch, f"origin/{branch}")
    s.git(w, "rebase", "origin/main")
    sha = s.git(w, "rev-parse", "HEAD").stdout.strip()
    s.git(w, "push", "-q", "-f", "origin", f"{branch}:refs/heads/{branch}")
    r = s.sh([sys.executable, FORGE_PY, "ci-check", "--forge", str(FORGE), "--run", sha])
    if r.returncode == 0:
        s.swarm("land", "--job", s.job, "--artifact", f"{branch}@{sha}")
        (RUN / "landed.json").write_text(json.dumps({"sha": sha}))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--harness", default="claude")
    a, rest = ap.parse_known_args()
    opt = lambda f: rest[rest.index(f) + 1] if f in rest and rest.index(f) + 1 < len(rest) else None  # noqa: E731
    prompt = sys.stdin.read()
    sid = opt("--session-id") or opt("--resume") or str(uuid.uuid4())
    model = opt("--model") or opt("-m")
    job, role = tag(prompt, "job") or CONF.get("job"), (tag(prompt, "role") or "worker").split(":")[0]
    seat = tag(prompt, "seat") or tag(prompt, "title")
    if "engineer" in role and not seat:
        seat = "eng-a"
    key = f"{role}:{seat or ''}"
    n = counter(key)
    cost = float(SCRIPT.get("cost_usd", 0.05))
    s = Session(sid, job, role, seat, model)
    log(ev="start", sid=sid, role=role, seat=seat, job=job, model=model, harness=a.harness, nth=n,
        budget=opt("--max-budget-usd"), artifact=current_artifact(prompt) if role not in ("lead", "engineer") else None)
    s.hook("start")
    try:
        if "lead" in role or role in ("engineering_lead", "coordinator"):
            role_lead(s, n)
        elif "engineer" in role:
            role_engineer(s, n)
        elif "review" in role or role == "qa":
            role_reviewer(s, n, prompt)
        elif "judge" in role:
            role_judge(s, n, prompt)
        elif "integrat" in role:
            role_integrator(s, n, prompt)
    finally:
        s.hook("stop")
        log(ev="end", sid=sid, role=role, seat=seat, cost=cost, denied=s.denied)
    if a.harness == "codex":
        print(json.dumps({"type": "thread.started", "thread_id": sid}))
        print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 1000, "output_tokens": 200}}))
    else:
        print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "session_id": sid,
                          "total_cost_usd": cost, "num_turns": 3, "result": f"{role} done"}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
