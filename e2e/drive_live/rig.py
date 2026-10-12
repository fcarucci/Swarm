"""The drive rig: one isolated run with a goal job on it, driven by supervisor passes and nothing else.

`Rig.prepare()` works on any checkout (throwaway board, fake hub, scratch repo, harness shims, team config).
`Rig.activate()` needs 6a/6b and `Rig.drive()` needs 8a/8b/8c/11b: without them they raise qa_lib.Pending.
Zero coordinator turns by construction: no Claude Code session is started and nothing but
`swarm activate` (once, plus scripted re-activations) and `swarm supervise` (the timer's command) is run against the job.

Nothing here may pass vacuously: every injected fault reports whether it fired (`rig.fired`), and the
scenario fails when the fault it names never happened.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import qa_lib as q

HERE = Path(__file__).resolve().parent
TEMPLATE = HERE / "toy-repo-template"
SCRIPTS = HERE / "scripts"
HUB_PY = HERE / "hub.py"
ARTIFACT_GOAL = "write a report"
SHA40 = re.compile(r"[0-9a-f]{40}")
# [drive.models] defaults (plan Task 6 / PM ruling [P1]); a role's model must contain this word
MODELS = {"lead": "opus", "judge": "opus", "engineer": "haiku", "reviewer": "sonnet", "qa": "sonnet",
          "integrator": "haiku", "security": "opus"}
KILLED = -signal.SIGKILL if hasattr(signal, "SIGKILL") else -9



def is_predecessor(a) -> bool:
    """The placeholder row the driver allocates for each launch (key `launch:<id>`, closed at once): the name the
    launched session takes over via claim_resume. It is never a session, so it has no model and no launch."""
    return (a.agent_key or "").startswith("launch:") and a.ended_at is not None

class CostCap(RuntimeError):
    pass


class Rig:
    def __init__(self, name: str, *, script: str = "met", backend: str = "postgres", harness: str = "fake",
                 job: str | None = None, budget_usd: float = 10.0, stamp: str | None = None,
                 script_patch=None, systemd: bool | None = None, pass_env: list | None = None,
                 activate_extra: list | None = None, stall_passes: int = 10):
        self.run = q.Run(name, backend=backend, harness=harness, stamp=stamp, systemd=systemd, pass_env=pass_env)
        self.script_name = script
        self.job = q.check_job_name(job or f"qa-{name}")
        self.budget_usd = budget_usd
        self.hub = self.run.hub_dir
        self.initial = ""
        self.urls: dict[str, str] = {}
        self.workdir = self.run.work / "toy"
        self.passes = 0
        self.script_patch = script_patch
        self.activate_extra = list(activate_extra or [])
        self.pass_env: dict = {}          # extra environment for the NEXT supervisor passes (crash barriers)
        self.rcs: list[int] = []          # exit code of every pass; -9 = died by SIGKILL
        self.fired: dict = {}             # fault name -> evidence that it really happened
        self.invariant_violations: list[str] = []
        self.stall_passes = stall_passes
        self.lead_brief: Path | None = None
        self._last_change = 0
        self._last_sig = None

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
        if self._idle is not None:
            self._idle.kill()
        self.run.__exit__(*exc)

    # -- setup that works today
    def prepare(self) -> None:
        r = self.run
        out = subprocess.run([sys.executable, str(HUB_PY), "init", "--hub", str(self.hub),
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
                                                 "hub": str(self.hub), "job": self.job,
                                                 "initial_head": self.initial}))
        evidence = f"{sys.executable} {HUB_PY} ci-check --hub {self.hub} --run {{sha}}"
        team = ('[pipeline]\nintegrate = true\ntarget_branch = "main"\ndelete_branch = true\n\n'
                f'[ci]\nkind = "none"\nevidence_command = {json.dumps(evidence)}\n')
        (r.home / ".config" / "swarm" / "team.toml").write_text(team)
        for name in ("origin", "backup"):
            ls = subprocess.run(["git", "-C", str(self.workdir), "ls-remote", self.urls[name]], capture_output=True,
                                text=True, env=r.env())
            if ls.returncode or "refs/heads/main" not in ls.stdout:
                raise RuntimeError(f"destination {name} ({self.urls[name]}) does not answer ls-remote from the work tree: {ls.stderr[-200:]}")
        res = r.swarm("doctor")
        r.event("prepare", "board and hub ready", pg_port=getattr(r.pg, "port", None),
                initial=self.initial, doctor_rc=res.returncode)

    # -- the goal job
    def goal_text(self) -> str:
        return {"met": "Add a --version flag to the toy CLI (prints `toy 0.1.0`) with a unit test; make test passes.",
                "unreachable": "Add a test asserting 1 == 2 that passes, without changing the assertion."
                }[self.script_name.split("-")[0]]

    def _remote(self, name: str) -> str:
        """The URL pinned at activation: file:// (the spec's fixture; SWARM_QA_REMOTE_STYLE=path pins the plain
        path instead, for checkouts where 6a still treats file:// as ssh and demands SSH_AUTH_SOCK)."""
        url = self.urls[name]
        return url[len("file://"):] if os.environ.get("SWARM_QA_REMOTE_STYLE") == "path" else url

    def activate_args(self, goal: str | None = None, extra: list[str] | None = None) -> list[str]:
        if self.recipe == "artifact":
            lead = self.run.dir / "LEAD.md"
            lead.write_text("# lead\nWrite REPORT.md and post goal-ready.\n")
            return ["activate", "--job", self.job, "--goal", goal or ARTIFACT_GOAL, "--recipe", "artifact",
                    "--workdir", str(self.workdir), "--session", "qa-operator", "--lead-brief", str(lead)]
        lead = self.lead_brief or HERE / ("LEAD-unreachable.md" if self.script_name.startswith("unreachable") else "LEAD-met.md")
        return ["activate", "--job", self.job, "--goal", goal or self.goal_text(), "--lead-brief", str(lead),
                "--workdir", str(self.workdir), "--allow-workdir", str(self.run.work), "--session", "qa-operator",
                "--remote", f"origin={self._remote('origin')}", "--remote", f"backup={self._remote('backup')}",
                "--budget", f"max_cost_usd={self.budget_usd:g}", *self.activate_extra, *(extra or [])]

    session_driver = False   # True: no supervise pass is ever run (the session driver drives; Task 21)
    recipe = None            # "artifact": the target's drive_fake_seat.py recipe (REPORT.md goal, no remotes)
    _idle = None

    def start_session_driver(self) -> str:
        """Task 21: what a session's hooks do after `swarm activate`: start the detached session driver, watching an idle
        orchestrator process that never makes a hook call. No pass, no timer."""
        self._idle = subprocess.Popen(["sleep", "10800"], start_new_session=True)
        code = ("import sys, swarm.cli as c; from swarm.supervisor import session_driver as sd; import pathlib\n"
                "cfg = c.load_config(pathlib.Path(sys.argv[1])); pid = sd.start(cfg, sys.argv[2], int(sys.argv[3]), "
                "config_path=sys.argv[1]); print('driver pid', pid)\n")
        r = subprocess.run([str(q.VENV / "bin" / "python"), "-c", code, str(self.run.config), "qa-operator", str(self._idle.pid)],
                           capture_output=True, text=True, env={**self.run.env(), "PYTHONPATH": str(self.run.root / "lib")})
        self.run.event("session-driver", (r.stdout + r.stderr).strip()[-200:])
        if "driver pid None" in r.stdout or r.returncode:
            raise RuntimeError(f"session driver not started: {(r.stdout + r.stderr)[-300:]}")
        return r.stdout.strip()

    def activate(self, extra: list[str] | None = None, goal: str | None = None) -> None:
        q.require("6a", "6b", what="swarm activate --drive and seats")
        if not self.session_driver:
            self.run.supervise_pass()    # activation refuses unless the supervisor has run in the last 2 minutes
        res = self.run.swarm(*self.activate_args(goal, extra))
        self.run.event("activate", "goal job activated", rc=res.returncode, out=(res.stdout + res.stderr)[-300:])
        if res.returncode:
            raise RuntimeError(f"activate failed: {res.stderr.strip()[:500]}")
        if self.session_driver:
            self.start_session_driver()
        if os.environ.get("SWARM_QA_WORKAROUND") == "lead-receipt":
            self.run.event("workaround", self.workaround_lead_receipt())

    def run_now(self):
        import datetime as _dt
        return _dt.datetime.now(_dt.timezone.utc)

    def workaround_lead_receipt(self) -> str:
        """QA WORKAROUND for driver bug 2 (activate from a plain shell leaves the lead seat without a seat_request
        receipt, so the snapshot refuses it). Enabled only by SWARM_QA_WORKAROUND=lead-receipt; it writes the receipt
        the person-path hook would have written. Remove when the driver receipts the lead itself."""
        import time as _t
        self.run.apply_env()
        sys.path.insert(0, str(self.run.root / "lib"))
        from swarm import receipts
        key = self.run.board_key()
        led = self.run.ledger(self.job)
        with self.run.board() as b:
            lead = next(x for x in b.seats(self.job, led.generation, include_closed=True) if x.seat == "lead")
        from swarm.supervisor import driver_exec
        driver_exec.authorize_seat(led, lead, self.run_now())
        return f"wrote seat_request receipt for lead (id {lead.id} rev {lead.revision})"

    # -- driving
    def state(self):
        """(JobStatus|None, agents, restarts, messages) read in-process on the isolated board."""
        self.run.apply_env()
        with self.run.board() as b:
            js = b.job_status(self.job)
            return js, b.agents(self.job), b.restarts(self.job), b.messages_after(0, self.job)

    def spent_usd(self) -> float:
        """Cost so far: the larger of the ledger's charge and the restart rows' recorded cost."""
        js, agents, restarts, _ = self.state()
        rows = sum(r.cost_usd or 0 for r in restarts)
        try:
            led = self.run.ledger(self.job)
            return max(rows, led.charged_usd() if led else 0.0)
        except Exception:
            return rows

    def snapshot_line(self) -> dict:
        js, agents, restarts, msgs = self.state()
        d = {"status": getattr(js, "status", None), "verdict": getattr(js, "verdict", None),
             "closed_by": getattr(js, "closed_by", None), "agents": [(a.name, a.role, a.status) for a in agents],
             "restarts": len(restarts), "messages": len(msgs)}
        try:
            led = self.run.ledger(self.job)
            d["charged_usd"] = led.charged_usd() if led else None
            d["halted"] = bool(led and led.halted)
        except Exception as exc:   # a corrupt ledger is a scenario, not a rig error
            d["ledger"] = f"{type(exc).__name__}"
        return d

    def _watch(self, snap: dict) -> None:
        """The 'never stuck' invariant, as far as it is observable from outside (spec 3.3): a non-terminal job
        always has something that will move it: a live agent, a launch in flight, a halt a person owns, or
        recent change. A job that shows no change at all for `stall_passes` passes with no live agent and no halt
        is a violation, recorded here and failed at the end (never silently). An approximation of the spec's
        invariant, which needs the driver's own snapshot; replace with `driver.invariant(snapshot)` when landed."""
        live = any(st in ("started", "running", "idle") for _, _, st in snap["agents"])
        sig = (snap["status"], snap["messages"], snap["restarts"], tuple(snap["agents"]), snap.get("charged_usd"))
        if sig != self._last_sig:
            self._last_sig, self._last_change = sig, self.passes
        elif not live and not snap.get("halted") and self.passes - self._last_change >= self.stall_passes:
            self.invariant_violations.append(
                f"pass {self.passes}: no change for {self.passes - self._last_change} passes, no live agent, not halted")
            self._last_change = self.passes   # report once per window

    def one_pass(self) -> subprocess.CompletedProcess:
        self.passes += 1
        res = self.run.supervise_pass(env=dict(self.pass_env) or None)
        self.rcs.append(res.returncode)
        self.run.event("pass", f"#{self.passes}", rc=res.returncode, armed=self.pass_env.get("SWARM_CRASH_AT"))
        return res

    def drive(self, *, timeout_s: float, interval_s: float = 20.0, on_pass=None, max_passes: int | None = None) -> dict:
        """Run supervisor passes until the job is terminal. Returns the final snapshot line.
        `on_pass(k, snap)` runs before each pass (chaos injects there). TimeoutError when not terminal;
        CostCap (after halting the job) when spending passes the budget."""
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
            self._watch(snap)
            spent = self.spent_usd()
            if spent > self.budget_usd + 1e-6:
                self.run.swarm("halt", "--job", self.job, "--reason", f"QA cost cap: ${spent:.2f} > ${self.budget_usd:g}")
                self.run.event("cost", "cap exceeded; job halted", spent=spent)
                raise CostCap(f"spent ${spent:.2f} over the ${self.budget_usd:g} cap; job halted")
            if time.monotonic() > end or (max_passes is not None and self.passes >= max_passes):
                raise TimeoutError(f"job {self.job} not terminal after {self.passes} passes: {snap}")
            if on_pass:
                on_pass(self.passes, snap)
            if self.session_driver:
                time.sleep(interval_s)   # nothing but the session driver moves the job
            else:
                self.one_pass()
                time.sleep(interval_s)

    # -- fault helpers
    def arm_barrier(self, name: str) -> None:
        """Arm SWARM_CRASH_AT=name for every following pass until it fires (disarm() then)."""
        self.pass_env["SWARM_CRASH_AT"] = name

    def disarm(self) -> None:
        self.pass_env.pop("SWARM_CRASH_AT", None)

    def barrier_fired(self, name: str) -> str | None:
        """Evidence that the named barrier killed something: a pass that died by SIGKILL while armed, or a seat
        hook that was killed (harness log), or a session that started and never ended. None = it never fired."""
        if KILLED in self.rcs:
            return f"supervisor pass #{self.rcs.index(KILLED) + 1} died by SIGKILL (rc {KILLED})"
        log = self.harness_log()
        hk = [e for e in log if e.get("ev") == "hook_killed"]
        if hk:
            return f"a seat hook was SIGKILLed ({hk[0].get('role')}/{hk[0].get('event')})"
        started = {e["sid"] for e in log if e.get("ev") == "start"}
        ended = {e["sid"] for e in log if e.get("ev") == "end"}
        if name in ("runner_mid_session", "before_finish") and started - ended:
            return f"session(s) {sorted(started - ended)[:2]} started and never ended"
        return None

    # -- assertions (each returns a list of problems; empty = fine)
    def check_terminal(self, expect: str) -> list[str]:
        """expect: met | unreachable | cancelled | terminal (met or unreachable). Never stuck, never
        auto-closed, within budget, no duplicate attempts."""
        js, agents, restarts, msgs = self.state()
        bad: list[str] = []
        status = getattr(js, "status", None)
        verdict = getattr(js, "verdict", None)
        if expect == "terminal":
            expect = "met" if verdict == "met" else "unreachable"
        want = {"met": ("completed", "met"), "unreachable": ("failed", "unreachable"),
                "cancelled": ("cancelled", None)}[expect]
        if js is None or status != want[0]:
            bad.append(f"status {status!r}, want {want[0]!r}")
        if want[1] and verdict != want[1]:
            bad.append(f"verdict {verdict!r}, want {want[1]!r}")
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
        bad += [f"invariant: {v}" for v in self.invariant_violations]
        return bad

    def check_no_coordinator(self) -> list[str]:
        js, agents, restarts, msgs = self.state()
        bad = [f"restart row {r.id} reason {r.reason!r}" for r in restarts if r.reason.startswith("orphan-coordinator")]
        bad += [f"orchestrator-role agent {a.name} ({a.role})" for a in agents
                if (a.role or "") in ("orchestrator", "project_manager")]
        return bad

    def check_every_seat_supervisor_launched(self) -> list[str]:
        """Every agent of the job was started by a supervisor launch (a restart row) and has ended; in fake
        mode the harness log has one session per launch; no process of this run is left."""
        js, agents, restarts, msgs = self.state()
        bad = []
        launched = {r.new_agent_key for r in restarts if r.new_agent_key}
        for a in agents:
            if is_predecessor(a):
                continue
            if a.agent_key not in launched and not (a.resume_of and a.resume_of in launched):
                bad.append(f"agent {a.name} ({a.role}) was not launched by a supervisor restart row")
            if a.ended_at is None:
                bad.append(f"agent {a.name} still active after the job ended")
        if self.run.harness == "fake":
            starts = [e for e in self.harness_log() if e.get("ev") == "start"]
            if len(starts) < len(restarts):
                bad.append(f"{len(restarts)} launches but {len(starts)} harness sessions")
        left = q.run_procs(self.run, "claude|codex|fake_harness|supervisor.runner")
        if left:
            bad.append(f"processes of this run still alive: {left}")
        return bad

    def landed(self) -> dict:
        out = subprocess.run([sys.executable, str(HUB_PY), "audit", "--hub", str(self.hub), "--since", self.initial],
                             capture_output=True, text=True, check=True).stdout
        return json.loads(out)

    def judged_candidate(self) -> str | None:
        """The 40-hex SHA of the goal verdict's candidate: the verdict record when the board exposes it, else the
        newest `main@<sha>` the job's posts carry (GOAL-READY / the verdict post)."""
        self.run.apply_env()
        with self.run.board() as b:
            for name in ("verdict_details", "latest_verdict"):
                fn = getattr(b, name, None)
                if fn:
                    try:
                        d = fn(self.job)
                        cand = getattr(d, "candidate", None) or (d.get("candidate") if isinstance(d, dict) else None)
                        if cand and SHA40.search(str(cand)):
                            return SHA40.search(str(cand)).group(0)
                    except Exception:
                        pass
            for m in reversed(b.messages_after(0, self.job)):
                hit = re.search(r"main@([0-9a-f]{40})", m.message)
                if hit:
                    return hit.group(1)
        return None

    def check_landing(self) -> list[str]:
        """Plan Task 19 / spec 6.3: the landed head equals the judged candidate on EVERY destination, history is
        rebase-only (linear), and CI was green on that exact SHA and was consulted for it."""
        a = self.landed()
        bad = []
        if not a["linear"]:
            bad.append("merge commit on the target")
        if not a["destinations_equal"]:
            bad.append(f"destinations differ: {a['heads']}")
        if not a["commits"]:
            bad.append("nothing landed")
            return bad
        head = next(iter(a["heads"].values()))
        _, _, _, msgs = self.state()
        if not any(m.message.startswith("INTEGRATED") for m in msgs):
            bad.append("no INTEGRATED post: the landing was not recorded by swarm land")
        cand = self.judged_candidate()
        if cand is None:
            bad.append("could not find the judged candidate SHA to compare the landed head with")
        elif cand != head:
            bad.append(f"landed head {head[:10]} != judged candidate {cand[:10]}")
        ci = self.hub / "ci"
        res = ci / f"{head}.json"
        if not res.exists() or json.loads(res.read_text()).get("state") != "green":
            bad.append(f"no green CI result recorded for the landed head {head[:10]}")
        elif not (ci / f"{head}.checks").exists():
            bad.append("CI on the landed head was never consulted (no ci-check for that exact SHA)")
        return bad

    def check_models(self) -> list[str]:
        """Spec 6.3: the per-role model table is what the seats were launched on. The product records a seat's model on
        its SEAT row (board.seats(): model, set at the seat request / by the driver from [drive.models]) and passes
        it as `--model` at launch; the agent row's `model` stays empty for a headless `claude -p` session because the
        hook payload carries none. So the check reads the seat rows."""
        self.run.apply_env()
        with self.run.board() as b:
            seats = b.seats(self.job, None, include_closed=True)
        bad, seen = [], set()
        for st in seats:
            role = (st.role or "").lower()
            key = next((k for k in MODELS if k in role), None)
            if not key:
                continue
            seen.add(key)
            if not st.model:
                bad.append(f"seat {st.seat} ({role}) has no model")
            elif MODELS[key] not in st.model.lower():
                bad.append(f"seat {st.seat} ({role}) is on {st.model!r}, want a {MODELS[key]} model")
        for need in ("lead", "judge") + (("engineer",) if self.script_name.startswith("met") else ()):
            if need not in seen:
                bad.append(f"no {need} seat found to check its model")
        return bad

    def check_unreachable_details(self) -> list[str]:
        js, *_ = self.state()
        bad = []
        scope = getattr(js, "verdict_scope", None)
        if scope is not None and scope not in ("ruling", "mechanical"):
            bad.append(f"unreachable verdict scope {scope!r}, want ruling (or labelled mechanical)")
        shown = self.run.swarm("verdict", "show", "--job", self.job)   # prints the latest full report ("--details" there takes a path)
        if shown.returncode or len(shown.stdout.strip()) < 20:
            bad.append("no verdict details are recorded for the unreachable verdict")
        return bad

    def check_codex_engineer(self) -> list[str]:
        _, agents, _, _ = self.state()
        if not any((a.harness or "") == "codex" for a in agents):
            return ["no codex seat ran ([F18]: the live MET run includes one real Codex engineer)"]
        return []

    # -- reports
    def harness_log(self) -> list[dict]:
        p = self.run.logs / "harness.jsonl"
        return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []

    def write_costs(self) -> dict:
        _, _, restarts, _ = self.state()
        h = self.harness_log()
        sessions = [e for e in h if e.get("ev") == "end"]
        by_role: dict = {}
        for e in sessions:
            by_role[e["role"]] = round(by_role.get(e["role"], 0) + float(e.get("cost", 0)), 4)
        out = {"sessions": len(sessions) if self.run.harness == "fake" else len(restarts),
               "harness_cost_usd": round(sum(float(e.get("cost", 0)) for e in sessions), 4),
               "by_role": by_role, "ledger_cost_usd": round(sum(r.cost_usd or 0 for r in restarts), 4),
               "launches": len(restarts), "passes": self.passes, "budget_usd": self.budget_usd,
               "spent_usd": round(self.spent_usd(), 4), "wall_s": round(time.monotonic() - self.run.t0, 1),
               "pass_exit_codes": self.rcs}
        (self.run.logs / "costs.json").write_text(json.dumps(out, indent=2))
        return out

    def export_evidence(self) -> Path:
        """What the judge gets: transcript export, board snapshot, supervise log excerpt, timeline, costs."""
        ev = self.run.logs / "evidence"
        ev.mkdir(exist_ok=True)
        for name, args in (("transcript.txt", ["transcript", "export", "--job", self.job]),
                           ("snapshot.json", ["snapshot", "--json", "--job", self.job, "--messages", "200"]),
                           ("status.txt", ["status", "--job", self.job]),
                           ("questions.txt", ["questions", "--job", self.job, "--all"])):
            r = self.run.swarm(*args)
            (ev / name).write_text(r.stdout + (("\n[stderr]\n" + r.stderr) if r.returncode else ""))
        # the seats' own session transcripts (claude -p keeps them under the isolated HOME) and the runners' kept tails
        proj = self.run.home / ".claude" / "projects"
        if proj.exists():
            shutil.copytree(proj, ev / "claude-projects", dirs_exist_ok=True)
        for t in (self.run.home / ".local/share/swarm/supervisor").rglob("*.tail.txt"):
            shutil.copy(t, ev / f"runner-{t.parent.name}-{t.name}")
        sup = self.run.logs / "supervise.log"
        if sup.exists():
            (ev / "supervise-tail.log").write_text("\n".join(sup.read_text().splitlines()[-400:]))
        for f in ("timeline.jsonl", "costs.json", "harness.jsonl"):
            if (self.run.logs / f).exists():
                shutil.copy(self.run.logs / f, ev / f)
        return ev
