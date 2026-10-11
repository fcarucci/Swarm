#!/usr/bin/env python3
"""Entry of e2e/drive_live.sh: preflight, MET run, UNREACHABLE run, chaos, pending report."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import traceback
from pathlib import Path

import qa_lib as q
from rig import Rig

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
            out.append(("preflight/isolation", q.Outcome.FAIL if same else q.Outcome.PASS,
                        f"HOME={r.home} config={r.config}"))
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
            out.append(("preflight/hub-refuses-rewind", q.Outcome.PASS if ff.returncode else q.Outcome.FAIL,
                        "a non-fast-forward push to main is refused"))
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


def run_goal(args, expect: str) -> tuple[str, str, str]:
    name = f"live-{expect}"
    try:
        q.require(*q.LIVE_NEEDS, what="supervisor-driven goal run")
    except q.Pending as exc:
        return (name, q.Outcome.SKIP, str(exc))
    script = "met" if expect == "met" else "unreachable"
    if args.harness == "real":
        script = script   # the real harness ignores the script; kept for the rig's bookkeeping
    try:
        with Rig(name, script=script, backend=args.backend, harness=args.harness, budget_usd=args.budget_usd) as rig:
            killed = []

            def on_pass(k, snap):
                if args.kill_supervisor_once and not killed and snap["agents"]:
                    killed.append(k)
                    p = subprocess.Popen([str(rig.run.bin / "swarm"), "supervise"], env=rig.run.env(),
                                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    time.sleep(1.0)
                    q.kill_tree(p.pid)
                    p.wait()
                    rig.run.event("chaos", "supervisor pass SIGKILLed mid-pass", pass_no=k)

            rig.activate()
            interval = args.interval_s or (20 if args.harness == "fake" else 60)
            try:
                rig.drive(timeout_s=args.timeout_min * 60, interval_s=interval, on_pass=on_pass)
            except TimeoutError as exc:
                rig.write_costs(); rig.export_evidence()
                return (name, q.Outcome.FAIL, f"STUCK: {exc}")
            problems = rig.check_terminal(expect) + rig.check_no_coordinator() + rig.check_every_seat_supervisor_launched()
            if expect == "met":
                problems += rig.check_landing()
            costs = rig.write_costs()
            ev = rig.export_evidence()
            detail = (f"{costs['launches']} launches in {costs['passes']} passes, {costs['wall_s']}s, "
                      f"${costs['ledger_cost_usd']} (harness ${costs['harness_cost_usd']}); evidence {ev}")
            if problems:
                return (name, q.Outcome.FAIL, "; ".join(problems) + " | " + detail)
            return (name, q.Outcome.PASS, detail)
    except q.Pending as exc:
        return (name, q.Outcome.SKIP, str(exc))
    except Exception as exc:
        traceback.print_exc()
        return (name, q.Outcome.FAIL, f"{type(exc).__name__}: {exc}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="drive_live.sh")
    ap.add_argument("cmd", choices=["preflight", "met", "unreachable", "chaos", "all", "pending"])
    ap.add_argument("names", nargs="*", help="chaos scenario names")
    ap.add_argument("--backend", default="postgres", choices=["postgres", "sqlite", "file"])
    ap.add_argument("--harness", default="fake", choices=["fake", "real"])
    ap.add_argument("--budget-usd", type=float, default=None)
    ap.add_argument("--timeout-min", type=float, default=None)
    ap.add_argument("--interval-s", type=float, default=None)
    ap.add_argument("--kill-supervisor-once", action="store_true")
    ap.add_argument("--list", action="store_true", help="chaos: list scenario names")
    ap.add_argument("--today-only", action="store_true",
                    help="chaos: only the no-driver `<name>/today` layers (fast; what the unit tests run)")
    a = ap.parse_intermixed_args(argv)
    if a.budget_usd is None:
        a.budget_usd = 10.0 if a.harness == "real" else 2.0
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
        results.append(run_goal(a, "met"))
    if a.cmd in ("unreachable", "all"):
        results.append(run_goal(a, "unreachable"))
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
