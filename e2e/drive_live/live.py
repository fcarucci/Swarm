#!/usr/bin/env python3
"""Entry of e2e/drive_live.sh: preflight, MET run, UNREACHABLE run, chaos, pending report."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import traceback
from pathlib import Path

import qa_lib as q
from rig import CostCap, Rig
import chaos
HERE = Path(__file__).resolve().parent

PENDING_EXIT = 3


def preflight(args) -> list[tuple[str, str, str]]:
    """What runs on any checkout: the throwaway board comes up, the isolated config does not reach the
    shared board, the hub refuses non-fast-forward, the scratch repo's CI runs on an exact SHA, the
    harness shims exist. No driver needed."""
    out = []
    t = time.monotonic()
    try:
        with Rig("preflight", backend=args.backend, harness=args.harness, budget_usd=args.budget_usd) as rig:
            r = rig.run
            act = r.swarm("activate", "--job", "preflight", "--session", "qa-operator")
            st = r.swarm("status", "--job", "preflight")
            up = act.returncode == 0 and "[active]" in st.stdout
            out.append(("preflight/board", q.Outcome.PASS if up else q.Outcome.FAIL,
                        f"{args.backend} board up: activate rc={act.returncode}, job shown active={'[active]' in st.stdout}"
                        + ("" if up else f" ({(act.stderr + st.stderr).strip()[:200]})")))
            real = Path.home() / ".config" / "swarm" / "config.toml"
            same = real.exists() and real.resolve() == r.config.resolve()
            seen = r.swarm("status").stdout
            leaked = [j for j in ("swarm-autonomy",) if j in seen]
            port_ok = (r.pg is None) or str(r.pg.port) in r.config.read_text()
            out.append(("preflight/isolation", q.Outcome.FAIL if same or leaked or not port_ok else q.Outcome.PASS,
                        f"HOME={r.home}; status lists only the run's jobs (shared job names seen: {leaked or 'none'}); "
                        f"own database port {getattr(r.pg, 'port', '-')}"))
            sha = rig.initial
            ci = subprocess.run([sys.executable, str(Path(__file__).parent / "hub.py"), "ci-check", "--hub",
                                 str(rig.hub), "--run", sha], capture_output=True, text=True)
            out.append(("preflight/hub-ci", q.Outcome.PASS if ci.returncode == 0 else q.Outcome.FAIL,
                        f"toy `make test` on {sha[:8]}: {ci.stdout.strip()}"))
            subprocess.run([sys.executable, str(Path(__file__).parent / "hub.py"), "advance", "--hub",
                            str(rig.hub)], capture_output=True, check=True)
            subprocess.run(["git", "-C", str(rig.workdir), "-c", "user.email=q@q", "-c", "user.name=q",
                            "commit", "-q", "--allow-empty", "-m", "diverged"], capture_output=True)
            ff = subprocess.run(["git", "-C", str(rig.workdir), "push", "-f", rig.urls["origin"], "HEAD:main"],
                                capture_output=True, text=True)
            out.append(("preflight/hub-refuses-rewind",
                        q.Outcome.PASS if ff.returncode and "non-fast-forward" in ff.stderr else q.Outcome.FAIL,
                        "the hub itself refuses a non-fast-forward push to main" if ff.returncode else "push was accepted"))
            if args.harness == "fake":
                ok = all((r.bin / n).exists() for n in ("swarm", "claude", "codex"))
                out.append(("preflight/shims", q.Outcome.PASS if ok else q.Outcome.FAIL, f"{r.bin}"))
            caps = q.capabilities()
            miss = [k for k in q.LIVE_NEEDS if not caps[k].present]
            out.append(("drive/pending", q.Outcome.SKIP if miss else q.Outcome.PASS,
                        f"pending {'/'.join(miss)}" if miss else "all drive pieces present"))
            r.event("preflight", "done", took=round(time.monotonic() - t, 1))
    except Exception as exc:
        out.append(("preflight", q.Outcome.FAIL, f"{type(exc).__name__}: {exc}"))
        traceback.print_exc()
    return out


def run_goal(args, expect: str) -> list[tuple[str, str, str]]:
    """The live run. Returns one row for the run itself plus one per spec-6.3 / Task 19 assertion, so a missing
    capability (no codex binary, no user manager) is a visible SKIP and never a silent omission."""
    name = f"live-{expect}"
    try:
        q.require(*q.LIVE_NEEDS, what="supervisor-driven goal run")
    except q.Pending as exc:
        return [(name, q.Outcome.SKIP, str(exc))]
    if args.harness == "real" and not (os.environ.get("SWARM_QA_CLAUDE") or shutil.which("claude")):
        return [(name, q.Outcome.SKIP, "not run (--harness real: no `claude` on PATH; set SWARM_QA_CLAUDE)")]
    if args.systemd is None:
        args.systemd = q.user_manager_up()
    if args.systemd and not q.user_manager_up():
        return [(name, q.Outcome.SKIP, "not run (--systemd: no systemd user manager answers)")]
    have_codex = bool(os.environ.get("SWARM_QA_CODEX") or shutil.which("codex"))
    script = "met" if expect == "met" else "unreachable"
    try:
        with Rig(name, script=script, backend=args.backend, harness=args.harness, budget_usd=args.budget_usd,
                 systemd=args.systemd) as rig:
            rig.session_driver = args.driver == "session"
            rig.run.session_driver_env = rig.session_driver
            if args.harness == "fakeseat":
                rig.recipe = "artifact"
            if args.harness == "real" and expect == "met" and have_codex:
                rig.lead_brief = HERE / "LEAD-met-codex.md"      # asks for one engineer seat on the codex harness
            st: dict = {}

            def on_pass(k, snap):
                if args.kill_supervisor_once and not st.get("killed_pass") and snap["agents"] and st.get("kill_miss", 0) < 8:
                    chaos.kill_pass_midway(rig, st)

            rig.activate()
            interval = args.interval_s or (20 if args.harness == "fake" else 60)
            try:
                rig.drive(timeout_s=args.timeout_min * 60, interval_s=interval, on_pass=on_pass)
            except CostCap as exc:
                rig.write_costs(); rig.export_evidence()
                return [(name, q.Outcome.FAIL, f"COST CAP: {exc}")]
            except TimeoutError as exc:
                rig.write_costs(); rig.export_evidence()
                return [(name, q.Outcome.FAIL, f"STUCK: {exc}")]
            problems = rig.check_terminal(expect) + rig.check_no_coordinator() + rig.check_every_seat_supervisor_launched()
            if args.kill_supervisor_once and not st.get("killed_pass"):
                problems.append("--kill-supervisor-once: the pass was never killed in flight: nothing was tested")
            costs = rig.write_costs()
            ev = rig.export_evidence()
            if args.harness == "fakeseat":
                return [(name, q.Outcome.FAIL if problems else q.Outcome.PASS,
                         ("; ".join(problems) + " | " if problems else "") + f"{costs['launches']} launches, {costs['wall_s']}s; evidence {ev}")]
            detail = (f"{costs['launches']} launches in {costs['passes']} passes, {costs['wall_s']}s, "
                      f"spent ${costs['spent_usd']} of ${costs['budget_usd']:g}; "
                      f"{st.get('killed_pass') or 'no fault injected'}; evidence {ev}")
            rows = [(name, q.Outcome.FAIL if problems else q.Outcome.PASS,
                     ("; ".join(problems) + " | " if problems else "") + detail)]
            def row(n, probs, ok):
                rows.append((f"{name}/{n}", q.Outcome.FAIL if probs else q.Outcome.PASS, "; ".join(probs) or ok))
            row("models", rig.check_models(), "lead/judge opus, engineer haiku, reviewer sonnet (agent rows)")
            row("invariant", [f"invariant: {v}" for v in rig.invariant_violations],
                f"no stall window in {rig.passes} passes (observable approximation; see rig._watch)")
            if expect == "met":
                row("landed-head", rig.check_landing(),
                    "landed head == judged candidate on both destinations; linear; green CI on that SHA, consulted")
                if args.harness == "real":
                    if have_codex:
                        row("codex-engineer", rig.check_codex_engineer(), "a codex engineer seat ran")
                    else:
                        rows.append((f"{name}/codex-engineer", q.Outcome.SKIP,
                                     "not run (no codex binary on this box; [F18] is unchecked)"))
                else:
                    rows.append((f"{name}/codex-engineer", q.Outcome.SKIP,
                                 "not run in fake mode (chaos codex_parity covers the fake codex harness)"))
            else:
                row("unreachable-details", rig.check_unreachable_details(), "verdict ruling/mechanical with details")
            rows.append((f"{name}/real-systemd", q.Outcome.PASS if args.systemd and not problems else q.Outcome.SKIP,
                         "passes ran with real scopes" if args.systemd else
                         "not run (add --systemd; the harness never touches the shared swarm-supervise unit; "
                         "Task 17 tests/test_drive_systemd.py covers containment)"))
            return rows
    except q.Pending as exc:
        return [(name, q.Outcome.SKIP, str(exc))]
    except Exception as exc:
        traceback.print_exc()
        return [(name, q.Outcome.FAIL, f"{type(exc).__name__}: {exc}")]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="drive_live.sh")
    ap.add_argument("cmd", choices=["preflight", "met", "unreachable", "chaos", "all", "pending"])
    ap.add_argument("names", nargs="*", help="chaos scenario names")
    ap.add_argument("--backend", default="postgres", choices=["postgres", "sqlite", "file"])
    ap.add_argument("--harness", default="fake", choices=["fake", "fakeseat", "real"])
    ap.add_argument("--budget-usd", type=float, default=None)
    ap.add_argument("--timeout-min", type=float, default=None)
    ap.add_argument("--interval-s", type=float, default=None)
    ap.add_argument("--driver", choices=["pass", "session"], default="pass",
                    help="pass: the rig runs `swarm supervise` (4318921); session: activate in an idle session and run NO "
                         "pass and no timer (int/rc, Task 21 session driver)")
    ap.add_argument("--kill-supervisor-once", action="store_true")
    ap.add_argument("--systemd", action="store_true", default=None, help="require real systemd scopes (default: on when a user manager answers; drive seats need them)")
    ap.add_argument("--no-systemd", dest="systemd", action="store_false", help="SWARM_NO_SYSTEMD=1: the driver will refuse launches")
    ap.add_argument("--list", action="store_true", help="chaos: list scenario names")
    ap.add_argument("--today-only", action="store_true",
                    help="chaos: only the no-driver `<name>/today` layers (fast; what the unit tests run)")
    a = ap.parse_intermixed_args(argv)
    if a.budget_usd is None:
        a.budget_usd = 50.0   # cap - ruling_reserve (5) must stay above the open reservations (5 per live session); a fake run spends cents
    if a.timeout_min is None:
        a.timeout_min = 180.0 if a.harness == "real" else 25.0
    if a.cmd == "pending":
        caps = q.capabilities()
        for k, c in caps.items():
            print(f"{'ok     ' if c.present else 'PENDING'} {k:16} {c.name}")
        return 0
    results: list[tuple[str, str, str]] = []
    if a.cmd in ("preflight", "all"):
        results += preflight(a)
    if a.cmd in ("met", "all"):
        results += run_goal(a, "met")
    if a.cmd in ("unreachable", "all"):
        results += run_goal(a, "unreachable")
    if a.cmd in ("chaos", "all"):
        import chaos
        if a.list:
            print("\n".join(chaos.SCENARIOS))
            return 0
        results += chaos.run(a, a.names)
    rc = q.report(results, q.QA_ROOT / "runs" / "last-report.txt")
    if rc == 0 and a.cmd != "preflight" and any(st == q.Outcome.SKIP for _, st, _ in results):
        print("incomplete: some scenarios are skipped because drive pieces are pending (exit 3)")
        return PENDING_EXIT
    return rc


if __name__ == "__main__":
    sys.exit(main())
