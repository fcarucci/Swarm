"""The drive-mode chaos suite (plan Task 16, spec 6.2 scenario 3 and 5), against the live rig.

Every scenario has up to two layers, reported as separate rows:
  <name>/today   needs no driver: it exercises the pieces that exist (ledger, receipts, board sweeps)
                 on the isolated home and board. It runs on any checkout.
  <name>         the driven scenario: the fault is injected into a real supervisor-driven run (fake or real
                 harness) and the job must still end MET (or UNREACHABLE) or CANCELLED: never stuck, never
                 auto-closed. Skipped with "pending 8a/8b/8c/11b" until the driver is integrated.
A `today` row that reproduces a known bug the plan fixes (the reactivation auto-close, 8b) reports SKIP
"known bug, pending 8b" while the fix is absent and FAIL once the fix is present and it still happens.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import traceback
from pathlib import Path

import qa_lib as q
from rig import Rig

P, F, S = q.Outcome.PASS, q.Outcome.FAIL, q.Outcome.SKIP


# ---------------------------------------------------------------------------------------------
# helpers

def _env(run: q.Run) -> None:
    run.apply_env()
    sys.path.insert(0, str(run.root / "lib"))


def _pg_sql(rig_or_run, sql: str, params=()):
    """SQL on the run's OWN throwaway cluster (never any other): used only to backdate rows or plant stale
    ones, the way tests/support.py does for its throwaway board."""
    run = getattr(rig_or_run, "run", rig_or_run)
    if not run.pg:
        raise q.Pending([], "needs the postgres backend")
    import psycopg
    assert run.pg.data.is_relative_to(q.QA_ROOT)
    with psycopg.connect(host="127.0.0.1", port=run.pg.port, user=run.pg.user, dbname="qa_board",
                         autocommit=True, connect_timeout=5) as c:
        cur = c.execute(sql, params)
        return cur.fetchall() if cur.description else None


def _drive(args, name, *, expect="met", script="met", patch=None, inject=None, after=None,
           budget=None, timeout_min=None, job_setup=None) -> tuple[str, str, str]:
    """One driven scenario: activate, drive with `inject(rig, k, snap)` before each pass, assert the
    terminal state, no coordinator, every seat supervisor-launched, then `after(rig)` extra checks."""
    try:
        q.require(*q.LIVE_NEEDS, what="driven chaos")
    except q.Pending as exc:
        return (name, S, str(exc))
    try:
        with Rig(f"chaos-{name}", script=script, backend=args.backend, harness=args.harness,
                 budget_usd=budget or args.budget_usd, script_patch=patch) as rig:
            rig.activate()
            state: dict = {}
            hook = (lambda k, snap: inject(rig, k, snap, state)) if inject else None
            try:
                rig.drive(timeout_s=(timeout_min or args.timeout_min) * 60,
                          interval_s=args.interval_s or (15 if args.harness == "fake" else 60), on_pass=hook)
            except TimeoutError as exc:
                rig.write_costs(); rig.export_evidence()
                return (name, F, f"STUCK: {exc}")
            bad = rig.check_terminal(expect) + rig.check_no_coordinator()
            if expect == "met":
                bad += rig.check_landing()
            if after:
                bad += after(rig, state) or []
            c = rig.write_costs(); rig.export_evidence()
            note = f"{c['launches']} launches/{c['passes']} passes ${c['ledger_cost_usd']}; injected: {state.get('note', '-')}"
            return (name, F if bad else P, ("; ".join(bad) + " | " if bad else "") + note)
    except q.Pending as exc:
        return (name, S, str(exc))
    except Exception as exc:
        traceback.print_exc()
        return (name, F, f"{type(exc).__name__}: {exc}")


def _today(name: str, fn) -> tuple[str, str, str]:
    """Run a no-driver check in its own isolated run (sqlite unless it needs postgres)."""
    try:
        outcome, detail = fn()
        return (f"{name}/today", outcome, detail)
    except q.Pending as exc:
        return (f"{name}/today", S, str(exc))
    except Exception as exc:
        traceback.print_exc()
        return (f"{name}/today", F, f"{type(exc).__name__}: {exc}")


def _mk_ledger(run: q.Run, job: str = "chaos-ledger"):
    from swarm.supervisor import ledger
    return ledger.create(run.board_key(), job, budgets={"max_cost_usd": 2.0},
                         activated_at="2026-10-11T00:00:00+00:00")


# ---------------------------------------------------------------------------------------------
# today layers

def today_corrupt_ledger(args):
    with q.Run("chaos-ledger", backend="sqlite", harness="fake") as run:
        _env(run)
        from swarm.supervisor import ledger
        key, job = run.board_key(), "chaos-ledger"
        led = _mk_ledger(run, job)
        path = next(run.ledger_dir().glob("*.json"))
        good = path.read_bytes()
        cases = {"truncated": good[: len(good) // 2], "garbage": b"\x00\xff not json", "wrong-shape": b'{"v": 1}',
                 "other-job": good.replace(job.encode(), b"someone-else"), "oversize": b" " * (5 * 1024 * 1024)}
        silent = []
        for name, blob in cases.items():
            path.write_bytes(blob)
            try:
                got = ledger.load(key, job)
                silent.append(f"{name}: load returned {got!r} instead of raising")
            except ledger.LedgerBadError:
                pass
            bads = [x for x in ledger.unfinished(key) if isinstance(x, ledger.LedgerBad)]
            if not bads:
                silent.append(f"{name}: unfinished() did not report it")
        path.write_bytes(good)
        ok_again = ledger.load(key, job) is not None
        if silent or not ok_again:
            return F, "; ".join(silent) or "restored ledger does not load"
        return P, f"{len(cases)} corruptions all refused loudly (LedgerBadError / LedgerBad), restore loads"


def today_stale_lock(args):
    with q.Run("chaos-lock", backend="sqlite", harness="fake") as run:
        _env(run)
        from swarm.supervisor import ledger
        key, job = run.board_key(), "chaos-lock"
        led = _mk_ledger(run, job)
        d = run.ledger_dir()
        lock = next(d.glob("*.lock"), None)
        notes = []
        # (1) a stale lock FILE left by a dead holder: flock-based, so it must not block anyone
        if lock is None:
            lock = d / (next(d.glob("*.json")).stem + ".lock")
            lock.write_text("")
        led2 = ledger.load(key, job)
        led2.errors += 1
        led2.save()
        notes.append("a left-over .lock file blocks no save")
        # (2) a planted symlink as the lock: refused (fail closed), never followed
        lock.unlink()
        victim = run.dir / "victim.txt"
        victim.write_text("keep")
        lock.symlink_to(victim)
        refused = False
        try:
            ledger.unfinished(key)
        except Exception:
            refused = True
        try:
            ledger.load(key, job)
        except Exception:
            refused = refused or True
        if victim.read_text() != "keep":
            return F, "a planted lock symlink was followed and its target changed"
        if not refused:
            return F, "a planted lock symlink was accepted silently"
        notes.append("a planted lock symlink is refused and never followed")
        return P, "; ".join(notes)


def today_forged_receipt(args):
    with q.Run("chaos-receipt", backend="sqlite", harness="fake") as run:
        _env(run)
        from swarm import receipts
        key = run.board_key()
        dec = {"verdict": "met", "scope": "goal", "generation": 1, "candidate": "main@" + "a" * 40, "req": None}
        r = receipts.Receipt(board_key=key, job="J", generation=1, incarnation="f" * 32, action_id="a1", attempt=1,
                             seat="judge", kind="verdict", record_id="7", rev=1,
                             content_sha=receipts.content_sha("verdict", dec), args_sha=receipts.args_sha(["swarm", "verdict"]),
                             expires_at=time.time() + 600)
        receipts.write(key, r)
        row = {"kind": "verdict", "id": 7, "rev": 1, **dec}
        probs = []
        found = receipts.find(key, "J", "verdict", "7", rev=1, incarnation="f" * 32, generation=1, now=time.time())
        if not (found and receipts.matches(found, row)):
            probs.append("the genuine receipt did not verify")
        forged_row = {**row, "verdict": "unreachable"}                 # a row changed after the hook saw it
        if receipts.matches(found, forged_row):
            probs.append("a modified row still matched its receipt")
        if receipts.matches(found, {**row, "rev": 2}):
            probs.append("a re-revised row still matched")
        if receipts.find(key, "J", "verdict", "7", rev=1, incarnation="0" * 32, generation=1, now=time.time()):
            probs.append("a receipt of another incarnation was accepted")
        if receipts.find(key, "J", "verdict", "7", rev=1, incarnation="f" * 32, generation=2, now=time.time()):
            probs.append("a generation-1 receipt was accepted for generation 2")
        if receipts.find(key, "J", "verdict", "7", rev=1, incarnation="f" * 32, generation=1, now=time.time() + 3600):
            probs.append("an expired receipt was accepted")
        if receipts.find(key, "J", "verdict", "8", rev=1, incarnation="f" * 32, generation=1, now=time.time()):
            probs.append("a receipt was found for a record that has none")
        return (F, "; ".join(probs)) if probs else (P, "forged row, other incarnation/generation, expiry, missing receipt: all refused")


def today_reactivated_long_idle(args):
    """Today's auto-close bug (the 8b fix 924b58c, tests/test_stale_waiting.py): a goal job waiting for its judge,
    silent for 10 hours, is re-activated (`swarm activate` again); the next sweep (`swarm purge`, also run
    by every supervisor pass) closes it at once because its quiet time counts from the OLD activity, not
    from the reactivation. Needs backdating, so postgres."""
    if args.backend != "postgres":
        raise q.Pending([], "needs --backend postgres (backdating)")
    with q.Run("chaos-reactivate", backend="postgres", harness="fake") as run:
        run.apply_env()
        job = "chaos-reactivate"
        run.swarm("activate", "--job", job, "--session", "qa-operator", "--goal", "ship it", check=True)
        who = run.swarm("join", "--job", job, "--key", "w1", check=True).stdout.strip().splitlines()[0]
        run.swarm("post", "--job", job, "--as", who, "hello", check=True)
        run.swarm("leave", "--job", job, "--key", "w1")
        hrs = "10 hours"
        _pg_sql(run, f"UPDATE agents SET joined_at = now() - interval '{hrs}', last_seen = now() - interval '{hrs}', "
                     f"left_at = now() - interval '{hrs}' WHERE job = %s", (job,))
        _pg_sql(run, f"UPDATE messages SET created_at = now() - interval '{hrs}' WHERE job = %s", (job,))
        _pg_sql(run, f"UPDATE jobs SET created_at = now() - interval '{hrs}', activated_at = now() - interval '{hrs}' "
                     "WHERE job = %s", (job,))
        run.swarm("activate", "--job", job, "--session", "qa-operator", "--goal", "ship it", check=True)   # reopened now
        st = ""
        for _ in range(2):
            run.swarm("purge")                        # the sweep
            run.supervise_pass()                      # and the supervisor's
            st = run.swarm("status", "--job", job).stdout
        still_open = "[active]" in st or "[idle]" in st or "[waiting" in st
        if still_open:
            return P, "a reactivated long-silent goal job is not closed by its old silence"
        caps = q.capabilities()
        if not caps["8b-reactivation"].present:
            return S, "KNOWN BUG, pending 8b (quiet_start): the reactivated job was closed by the next sweep"
        return F, f"8b's quiet_start is present but the reactivated job was closed: {st.strip()[:160]}"


TODAY = {
    "corrupt_ledger": today_corrupt_ledger,
    "stale_lock": today_stale_lock,
    "forged_receipt": today_forged_receipt,
    "reactivated_long_idle": today_reactivated_long_idle,
}


# ---------------------------------------------------------------------------------------------
# driven scenarios

def drv_kill_seat_mid_task(args):
    def patch(s):
        s["engineer"] = {**s["engineer"], "hang_once": True}

    def inject(rig, k, snap, st):
        pidf = rig.run.dir / "hang" / "eng-a.pid"
        if pidf.exists() and not st.get("killed"):
            pid = int(pidf.read_text())
            q.kill_tree(pid)
            st["killed"] = True
            st["note"] = f"SIGKILL engineer session pid {pid} mid-task (pass {k})"
            rig.run.event("chaos", st["note"])

    def after(rig, st):
        bad = [] if st.get("killed") else ["the engineer never reached the hang point: nothing was killed"]
        starts = [e for e in rig.harness_log() if e.get("ev") == "start" and e.get("seat") == "eng-a"]
        if st.get("killed") and len(starts) < 2:
            bad.append("the killed engineer seat was not relaunched")
        return bad
    return _drive(args, "kill_seat_mid_task", patch=patch, inject=inject, after=after)


def drv_kill_runner(args):
    def patch(s):
        s["engineer"] = {**s["engineer"], "hang_once": True, "hang_seconds": 900}

    def inject(rig, k, snap, st):
        if (rig.run.dir / "hang" / "eng-a.pid").exists() and not st.get("killed"):
            pids = q.run_procs(rig.run, "supervisor.runner")
            for p in pids:
                os.kill(p, signal.SIGKILL)
            st["killed"] = bool(pids)
            st["note"] = f"SIGKILL runner pid(s) {pids}; the session is orphaned for the next reap"
    return _drive(args, "kill_runner_mid_session", patch=patch, inject=inject,
                  after=lambda rig, st: [] if st.get("killed") else ["no runner process found to kill"])


def _barrier(barrier: str):
    def run(args):
        def inject(rig, k, snap, st):
            if not st.get("killed") and not snap["agents"]:
                r = rig.run.supervise_pass(env={"SWARM_CRASH_AT": barrier})
                st["killed"] = True
                st["note"] = f"pass SIGKILLed at barrier {barrier} (rc {r.returncode})"
                rig.run.event("chaos", st["note"])
        return _drive(args, f"kill_supervisor_{barrier}", inject=inject,
                      after=lambda rig, st: [] if st.get("killed") else ["the barrier pass never ran"])
    return run


def drv_corrupt_ledger(args):
    def inject(rig, k, snap, st):
        files = list(rig.run.ledger_dir().glob("*.json")) if rig.run.ledger_dir().exists() else []
        if files and snap["agents"] and "bak" not in st:
            p = files[0]
            st["bak"], st["path"] = p.read_bytes(), p
            p.write_bytes(b'{"v": 1, "garbage')
            st["note"] = f"ledger corrupted at pass {k}; restored 3 passes later"
            st["at"] = k
        elif "bak" in st and k >= st["at"] + 3 and not st.get("restored"):
            st["path"].write_bytes(st["bak"])
            st["restored"] = True
        elif "bak" in st and not st.get("restored"):
            if snap["status"] in q.TERMINAL:
                st["terminal_while_corrupt"] = snap["status"]

    def after(rig, st):
        bad = [] if "bak" in st else ["the ledger was never corrupted"]
        if st.get("terminal_while_corrupt"):
            bad.append(f"the job went {st['terminal_while_corrupt']} while its ledger was corrupt")
        _, _, _, msgs = rig.state()
        if "bak" in st and not any("LEDGER-BAD" in m.message.upper().replace(" ", "-") for m in msgs):
            bad.append("no DRIVER-LEDGER-BAD post: a bad ledger must hold the job loudly, not silently")
        return bad
    return _drive(args, "corrupt_ledger", inject=inject, after=after)


def drv_stale_lock(args):
    def inject(rig, k, snap, st):
        if st.get("done"):
            return
        st["done"] = True
        d = rig.run.ledger_dir()
        note = []
        if d.exists():
            for f in d.glob("*.json"):
                (d / (f.stem + ".lock")).write_text("")        # left by a dead holder: must block nothing
                note.append("stale .lock file")
        if rig.run.pg:
            _pg_sql(rig, "INSERT INTO drive_locks (key, holder, generation, expires_at) "
                         "VALUES ('land:' || repeat('0', 64) || ':main', 'dead-attempt', 1, now() - interval '1 hour') "
                         "ON CONFLICT DO NOTHING")
            note.append("expired drive_locks row of a dead holder")
        st["note"] = ", ".join(note) or "nothing plantable on this backend"
    return _drive(args, "stale_lock", inject=inject)


def drv_reactivated_long_idle(args):
    """A drive job halted for a long time then unhalted (and reactivated with the same goal) must not be
    auto-closed by any core sweep: the 8b live bug."""
    def inject(rig, k, snap, st):
        if snap["agents"] and not st.get("halted"):
            rig.run.swarm("halt", "--job", rig.job, "--reason", "chaos: long idle")
            st["halted"] = True
            if rig.run.pg:
                _pg_sql(rig, "UPDATE messages SET created_at = created_at - interval '5 hours' WHERE job = %s", (rig.job,))
                _pg_sql(rig, "UPDATE agents SET last_seen = last_seen - interval '5 hours' WHERE job = %s", (rig.job,))
            for _ in range(2):
                rig.run.swarm("status")
                rig.run.supervise_pass()
            r = rig.run.swarm("unhalt", "--job", rig.job)
            st["note"] = f"halted, backdated 5h, swept, unhalted (rc {r.returncode})"
    return _drive(args, "reactivated_long_idle", inject=inject,
                  after=lambda rig, st: [] if st.get("halted") else ["never halted"])


def drv_question_default_expires(args):
    def patch(s):
        s["lead"]["ask"] = {"text": "left or right?", "options": "left,right", "default": "left", "expires": "2m"}
    return _drive(args, "question_default_expires", patch=patch,
                  after=lambda rig, st: [] if any("default" in m.message.lower()
                                                  for m in rig.state()[3]) else ["no post shows the default being applied"])


def drv_forged_closed_by_driver(args):
    def inject(rig, k, snap, st):
        if snap["agents"] and not st.get("forged"):
            rig.run.apply_env()
            with rig.run.board() as b:
                try:
                    ok = b.close_job(rig.job, "completed", "forged by an agent", closed_by="driver")
                    st["note"] = f"forged close_job(closed_by=driver) accepted={ok}"
                except Exception as exc:
                    st["note"] = f"forged close refused: {type(exc).__name__}"
            st["forged"] = True
            st["forged_at"] = k
        elif st.get("forged") and not st.get("checked"):
            st["checked"] = True
            led = rig.run.ledger(rig.job)
            if snap["status"] == "completed" and not (led and led.terminal):
                st["accepted"] = True

    def after(rig, st):
        bad = [] if st.get("forged") else ["never forged"]
        if st.get("accepted"):
            bad.append("a closed_by=driver close with no ledger terminal stayed completed: the forged MET was accepted")
        return bad
    return _drive(args, "forged_closed_by_driver", inject=inject, after=after)


def drv_forged_receipt(args):
    forged = "f" * 40

    def inject(rig, k, snap, st):
        if snap["agents"] and not st.get("forged"):
            rig.run.apply_env()
            with rig.run.board() as b:
                name = next((a.name for a in b.agents(rig.job) if a.role in ("engineer", "worker")), snap["agents"][0][0])
                b.post(rig.job, name, f"DONE feat/forged@{forged} forged hand-off, no receipt")
                b.post(rig.job, name, f"INTEGRATED feat/forged@{forged}")
            st["forged"] = True
            st["note"] = "posted DONE and INTEGRATED rows with no hook receipt"

    def after(rig, st):
        bad = [] if st.get("forged") else ["never forged"]
        for e in rig.harness_log():
            if forged in json.dumps(e):
                bad.append(f"a {e.get('role')} session was launched for the forged artifact")
        return bad
    return _drive(args, "forged_receipt", inject=inject, after=after)


SCENARIOS = {
    "kill_seat_mid_task": (None, drv_kill_seat_mid_task),
    "kill_runner_mid_session": (None, drv_kill_runner),
    "kill_supervisor_after_claim": (None, _barrier("after_claim")),
    "kill_supervisor_after_run_file": (None, _barrier("after_run_file")),
    "kill_supervisor_after_runner_start": (None, _barrier("after_runner_start")),
    "corrupt_ledger": ("corrupt_ledger", drv_corrupt_ledger),
    "stale_lock": ("stale_lock", drv_stale_lock),
    "reactivated_long_idle": ("reactivated_long_idle", drv_reactivated_long_idle),
    "question_default_expires": (None, drv_question_default_expires),
    "forged_closed_by_driver": (None, drv_forged_closed_by_driver),
    "forged_receipt": ("forged_receipt", drv_forged_receipt),
}


def run(args, names: list[str]) -> list[tuple[str, str, str]]:
    names = names or list(SCENARIOS)
    unknown = [n for n in names if n not in SCENARIOS]
    if unknown:
        return [(n, F, f"unknown scenario (have: {', '.join(SCENARIOS)})") for n in unknown]
    out = []
    for n in names:
        today, driven = SCENARIOS[n]
        if today:
            out.append(_today(n, lambda t=today: TODAY[t](args)))
        if not getattr(args, "today_only", False):
            out.append(driven(args))
    return out
