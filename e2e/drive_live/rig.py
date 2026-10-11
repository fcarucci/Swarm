"""The drive rig: one isolated run with a goal job on it, driven by supervisor passes and nothing else.

`Rig.prepare()` works on any checkout (throwaway board, fake forge, scratch repo, harness shims, team config).
`Rig.activate()` needs 6a and `Rig.drive()` needs 8a/8b/8c/11b: without them they raise qa_lib.Pending.
Zero coordinator turns by construction: no Claude Code session is started and nothing but
`swarm activate` (once) and `swarm supervise` (the timer's command) is ever run against the job.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import qa_lib as q

HERE = Path(__file__).resolve().parent
TEMPLATE = HERE / "toy-repo-template"
SCRIPTS = HERE / "scripts"
FORGE_PY = HERE / "forge.py"
SHA40 = "0123456789abcdef"


class Rig:
    def __init__(self, name: str, *, script: str = "met", backend: str = "postgres", harness: str = "fake",
                 job: str | None = None, budget_usd: float = 10.0, stamp: str | None = None,
                 script_patch=None):
        self.run = q.Run(name, backend=backend, harness=harness, stamp=stamp)
        self.script_name = script
        self.job = q.check_job_name(job or f"qa-{name}")
        self.budget_usd = budget_usd
        self.forge = self.run.forge_dir
        self.initial = ""
        self.urls: dict[str, str] = {}
        self.workdir = self.run.work / "toy"
        self.passes = 0
        self.script_patch = script_patch

    # -- lifecycle
    def __enter__(self) -> "Rig":
        self.run.__enter__()
        try:
            self.prepare()
        except BaseException:
            self.run.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, *exc) -> None:
        self.run.__exit__(*exc)

    # -- setup that works today
    def prepare(self) -> None:
        r = self.run
        out = subprocess.run([sys.executable, str(FORGE_PY), "init", "--forge", str(self.forge),
                              "--template", str(TEMPLATE), "--dest", "origin", "--dest", "backup"],
                             capture_output=True, text=True, check=True).stdout
        info = json.loads(out)
        self.initial = info.pop("initial")
        self.urls = info
        subprocess.run(["git", "clone", "-q", self.urls["origin"], str(self.workdir)], check=True,
                       capture_output=True, env=r.env())
        script = json.loads((SCRIPTS / f"{self.script_name}.json").read_text())
        if self.script_patch:
            self.script_patch(script)
        script_path = r.write_text("script.json", json.dumps(script))
        r.write_text("harness.json", json.dumps({"repo": str(r.root), "script": str(script_path),
                                                 "forge": str(self.forge), "job": self.job,
                                                 "initial_head": self.initial}))
        evidence = f"{sys.executable} {FORGE_PY} ci-check --forge {self.forge} --run {{sha}}"
        team = ('[pipeline]\nintegrate = true\ntarget_branch = "main"\ndelete_branch = true\n\n'
                f'[ci]\nkind = "none"\nevidence_command = {json.dumps(evidence)}\n')
        (r.home / ".config" / "swarm" / "team.toml").write_text(team)
        # setup the board now so a broken throwaway cluster fails here, not mid-run
        res = r.swarm("doctor")
        r.event("prepare", "board and forge ready", pg_port=getattr(r.pg, "port", None),
                initial=self.initial, doctor_rc=res.returncode)

    # -- the goal job
    def goal_text(self) -> str:
        return {"met": "Add a --version flag to the toy CLI (prints `toy 0.1.0`) with a unit test; make test passes.",
                "unreachable": "Add a test asserting 1 == 2 that passes, without changing the assertion."
                }[self.script_name.split("-")[0]]

    def activate(self, extra: list[str] | None = None) -> None:
        q.require("6a", "6b", what="swarm activate --drive and seats")
        self.run.supervise_pass()    # activation refuses unless the supervisor has run in the last 2 minutes
        lead = HERE / ("LEAD-unreachable.md" if self.script_name.startswith("unreachable") else "LEAD-met.md")
        args = ["activate", "--job", self.job, "--goal", self.goal_text(), "--lead-brief", str(lead),
                "--workdir", str(self.workdir), "--session", "qa-operator",
                "--remote", f"origin={self._remote('origin')}", "--remote", f"backup={self._remote('backup')}",
                "--budget", f"max_cost_usd={self.budget_usd:g}", *(extra or [])]
        res = self.run.swarm(*args)
        self.run.event("activate", "goal job activated", rc=res.returncode, out=(res.stdout + res.stderr)[-300:])
        if res.returncode:
            raise RuntimeError(f"activate failed: {res.stderr.strip()[:500]}")

    def _remote(self, name: str) -> str:
        """The URL pinned at activation: file:// (the spec's fixture; SWARM_QA_REMOTE_STYLE=path pins the plain
        path instead, for checkouts where 6a still treats file:// as ssh and demands SSH_AUTH_SOCK)."""
        url = self.urls[name]
        return url[len("file://"):] if os.environ.get("SWARM_QA_REMOTE_STYLE") == "path" else url

    # -- driving
    def state(self):
        """(JobStatus|None, agents, restarts, messages) read in-process on the isolated board."""
        self.run.apply_env()
        with self.run.board() as b:
            js = b.job_status(self.job)
            return js, b.agents(self.job), b.restarts(self.job), b.messages_after(0, self.job)

    def snapshot_line(self) -> dict:
        js, agents, restarts, msgs = self.state()
        d = {"status": getattr(js, "status", None), "verdict": getattr(js, "verdict", None),
             "closed_by": getattr(js, "closed_by", None), "agents": [(a.name, a.role, a.status) for a in agents],
             "restarts": len(restarts), "messages": len(msgs)}
        try:
            led = self.run.ledger(self.job)
            d["charged_usd"] = led.charged_usd() if led else None
        except Exception as exc:   # a corrupt ledger is a scenario, not a rig error
            d["ledger"] = f"{type(exc).__name__}"
        return d

    def drive(self, *, timeout_s: float, interval_s: float = 20.0, on_pass=None, max_passes: int | None = None) -> dict:
        """Run supervisor passes until the job is terminal. Returns the final snapshot line.
        `on_pass(k, snap)` runs before each pass (chaos injects there). TimeoutError when not terminal."""
        q.require(*q.LIVE_NEEDS, what="the supervisor driver")
        end = time.monotonic() + timeout_s
        last = None
        while True:
            snap = self.snapshot_line()
            if snap != last:
                self.run.event("job", "state", **snap)
                last = snap
            if snap["status"] in q.TERMINAL:
                return snap
            if time.monotonic() > end or (max_passes is not None and self.passes >= max_passes):
                raise TimeoutError(f"job {self.job} not terminal after {self.passes} passes: {snap}")
            if on_pass:
                on_pass(self.passes, snap)
            self.passes += 1
            res = self.run.supervise_pass()
            self.run.event("pass", f"#{self.passes}", rc=res.returncode)
            time.sleep(interval_s)

    # -- assertions (each returns a list of problems; empty = fine)
    def check_terminal(self, expect: str) -> list[str]:
        """expect: met | unreachable | cancelled. Never stuck, never auto-closed, within budget."""
        js, agents, restarts, msgs = self.state()
        bad: list[str] = []
        want = {"met": ("completed", "met"), "unreachable": ("failed", "unreachable"),
                "cancelled": ("cancelled", None)}[expect]
        if js is None or js.status != want[0]:
            bad.append(f"status {getattr(js, 'status', None)!r}, want {want[0]!r}")
        if want[1] and getattr(js, "verdict", None) != want[1]:
            bad.append(f"verdict {getattr(js, 'verdict', None)!r}, want {want[1]!r}")
        if js is not None and js.closed_by == "auto":
            bad.append("closed_by auto: the auto-close sweep closed a drive job")
        if js is not None and js.completion_forced:
            bad.append("completion was forced")
        try:
            led = self.run.ledger(self.job)
        except Exception as exc:
            bad.append(f"ledger unreadable at the end: {type(exc).__name__}")
            led = None
        if led is not None:
            if led.charged_usd() > self.budget_usd + 1e-6:
                bad.append(f"charged ${led.charged_usd():.2f} over the budget ${self.budget_usd:g}")
            seen = [(i.action_id, i.attempt) for i in led.intents]
            if len(seen) != len(set(seen)):
                bad.append("duplicate launch attempts in the ledger")
            if expect != "cancelled" and (led.terminal or {}).get("verdict") != want[1]:
                bad.append(f"ledger terminal {led.terminal!r}")
        return bad

    def check_no_coordinator(self) -> list[str]:
        js, agents, restarts, msgs = self.state()
        bad = [f"restart row {r.id} reason {r.reason!r}" for r in restarts if r.reason.startswith("orphan-coordinator")]
        bad += [f"orchestrator-role agent {a.name} ({a.role})" for a in agents
                if (a.role or "") in ("orchestrator", "project_manager")]
        return bad

    def check_every_seat_supervisor_launched(self) -> list[str]:
        """Every agent of the job was started by a supervisor launch (a restart row with a drive launch
        id), and every harness session in the log was a child of the runner (the shim only runs there)."""
        js, agents, restarts, msgs = self.state()
        bad = []
        launched = {r.new_agent_key for r in restarts if r.new_agent_key}
        for a in agents:
            if a.agent_key not in launched and not (a.resume_of and a.resume_of in launched):
                bad.append(f"agent {a.name} ({a.role}) was not launched by a supervisor restart row")
            if a.ended_at is None:
                bad.append(f"agent {a.name} still active after the job ended")
        starts = [e for e in self.harness_log() if e.get("ev") == "start"]
        if len(starts) < len(restarts):
            bad.append(f"{len(restarts)} launches but {len(starts)} harness sessions")
        live = subprocess.run(["pgrep", "-f", str(self.run.dir)], capture_output=True, text=True).stdout.split()
        live = [p for p in live if p != str(os.getpid())]
        if live and not self.run.pg:
            bad.append(f"processes of this run still alive: {live}")
        return bad

    def check_landing(self) -> list[str]:
        out = subprocess.run([sys.executable, str(FORGE_PY), "audit", "--forge", str(self.forge),
                              "--since", self.initial], capture_output=True, text=True, check=True).stdout
        a = json.loads(out)
        bad = []
        if not a["linear"]:
            bad.append("merge commit on the target")
        if not a["destinations_equal"]:
            bad.append(f"destinations differ: {a['heads']}")
        if not a["commits"]:
            bad.append("nothing landed")
        return bad

    # -- reports
    def harness_log(self) -> list[dict]:
        p = self.run.logs / "harness.jsonl"
        return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []

    def write_costs(self) -> dict:
        h = self.harness_log()
        sessions = [e for e in h if e.get("ev") == "end"]
        by_role: dict = {}
        for e in sessions:
            by_role[e["role"]] = round(by_role.get(e["role"], 0) + float(e.get("cost", 0)), 4)
        _, _, restarts, _ = self.state()
        out = {"sessions": len(sessions), "harness_cost_usd": round(sum(float(e.get("cost", 0)) for e in sessions), 4),
               "by_role": by_role, "ledger_cost_usd": round(sum(r.cost_usd or 0 for r in restarts), 4),
               "launches": len(restarts), "passes": self.passes,
               "wall_s": round(time.monotonic() - self.run.t0, 1)}
        (self.run.logs / "costs.json").write_text(json.dumps(out, indent=2))
        return out

    def export_evidence(self) -> Path:
        """What the judge gets: transcript export, board snapshot, supervise log excerpt, timeline, costs."""
        ev = self.run.logs / "evidence"
        ev.mkdir(exist_ok=True)
        for name, args in (("transcript.txt", ["transcript", "export", "--job", self.job]),
                           ("snapshot.json", ["snapshot", "--json", "--job", self.job, "--messages", "200"]),
                           ("status.txt", ["status", "--job", self.job])):
            r = self.run.swarm(*args)
            (ev / name).write_text(r.stdout + (("\n[stderr]\n" + r.stderr) if r.returncode else ""))
        sup = self.run.logs / "supervise.log"
        if sup.exists():
            (ev / "supervise-tail.log").write_text("\n".join(sup.read_text().splitlines()[-400:]))
        for f in ("timeline.jsonl", "costs.json", "harness.jsonl"):
            if (self.run.logs / f).exists():
                shutil.copy(self.run.logs / f, ev / f)
        return ev
