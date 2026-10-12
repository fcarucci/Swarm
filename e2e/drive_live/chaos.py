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
import re
import os
import signal
import subprocess
import sys
import time
import traceback
from pathlib import Path

import qa_lib as q
from rig import Rig, CostCap

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
        if not lock.exists():
            return F, "could not plant the stale lock file: nothing was tested"
        led2 = ledger.load(key, job)
        led2.errors += 1
        led2.save()
        notes.append("a left-over .lock file blocks no save")
        # (2) a planted symlink as the lock: refused (fail closed), never followed
        lock.unlink()
        victim = run.dir / "victim.txt"
        victim.write_text("keep")
        lock.symlink_to(victim)
        if not lock.is_symlink():
            return F, "could not plant the lock symlink: nothing was tested"
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
#
# A fault that did not happen is a FAILED scenario, never a pass: every `_drive` call names the evidence that
# its fault fired (`fired(rig, st)` returns a string, or None = it never happened). A crash barrier that
# does not exist in the checkout under test is a SKIP naming it, never a pass.

def _drive(args, name, *, expect="met", script="met", patch=None, inject=None, fired=None, after=None,
           budget=None, timeout_min=None, activate_extra=None, pass_env=None, systemd=False,
           check_landing=None, harness=None) -> tuple[str, str, str]:
    """One driven scenario: activate, drive with `inject(rig, k, snap, st)` before each pass, then assert: the
    fault fired, the terminal state, no coordinator, every seat supervisor-launched, landing (for MET),
    `after(rig, st)` extras. Skipped as pending until the driver is integrated."""
    try:
        q.require(*q.LIVE_NEEDS, what="driven chaos")
    except q.Pending as exc:
        return (name, S, str(exc))
    try:
        with Rig(f"chaos-{name}", script=script, backend=args.backend, harness=harness or args.harness,
                 budget_usd=budget or args.budget_usd, script_patch=patch, activate_extra=activate_extra,
                 pass_env=pass_env, systemd=systemd) as rig:
            rig.activate()
            st: dict = {}
            hook = (lambda k, snap: inject(rig, k, snap, st)) if inject else None
            bad: list[str] = []
            try:
                rig.drive(timeout_s=(timeout_min or args.timeout_min) * 60,
                          interval_s=args.interval_s or (15 if args.harness == "fake" else 60), on_pass=hook)
            except CostCap as exc:
                rig.write_costs(); rig.export_evidence()
                return (name, F, f"COST CAP: {exc}")
            except TimeoutError as exc:
                rig.write_costs(); rig.export_evidence()
                return (name, F, f"STUCK: {exc}")
            if fired:
                ev = fired(rig, st)
                if ev:
                    st["note"] = ev
                else:
                    bad.append("the injected fault never fired: nothing was tested")
            bad += rig.check_terminal(expect) + rig.check_no_coordinator() + rig.check_every_seat_supervisor_launched()
            if (check_landing if check_landing is not None else expect == "met"):
                bad += rig.check_landing()
            if after:
                bad += after(rig, st) or []
            c = rig.write_costs(); rig.export_evidence()
            note = f"{c['launches']} launches/{c['passes']} passes ${c['spent_usd']}; injected: {st.get('note', '-')}"
            return (name, F if bad else P, ("; ".join(bad) + " | " if bad else "") + note)
    except q.Pending as exc:
        return (name, S, str(exc))
    except Exception as exc:
        traceback.print_exc()
        return (name, F, f"{type(exc).__name__}: {exc}")


def _starts(rig, **match):
    return [e for e in rig.harness_log() if e.get("ev") == "start" and all(e.get(k) == v for k, v in match.items())]


# -- crash barriers ----------------------------------------------------------------------------

# the plan's 13 (Task 16) plus the terminalization barriers that exist in the tree today; where a barrier is
# not in the checkout the row is a SKIP that names it. where = which process the barrier lives in.
BARRIERS = {
    "before_claim": "pass", "after_claim": "pass", "after_run_file": "pass", "after_marker": "pass",
    "after_runner_start": "pass", "runner_mid_session": "seat", "before_finish": "seat",
    "mid_terminalization": "pass", "mid_rollover": "pass", "hook_after_cli_before_receipt": "seat",
    "land_after_first_destination": "pass", "after_intent_before_claim": "pass", "mid_rollover_after_ledger": "pass",
    "terminal_intent_saved": "pass", "terminal_verdict_recorded": "pass", "terminal_closed_before_ledger": "pass",
}


def _barrier(barrier: str):
    where = BARRIERS[barrier]

    def run(args):
        name = f"barrier_{barrier}"
        if not q.barrier_present(barrier):
            return (name, S, f"pending barrier: no hit(\"{barrier}\") in lib/ of this checkout (owner of that code "
                             f"path must add it; plan Task 16 names it)")

        def inject(rig, k, snap, st):
            if not st.get("fired"):
                ev = rig.barrier_fired(barrier)
                if ev:
                    st["fired"] = ev
                    rig.disarm()
                elif k < 40:
                    rig.arm_barrier(barrier)

        def fired(rig, st):
            ev = st.get("fired") or rig.barrier_fired(barrier)
            rig.disarm()
            return f"barrier {barrier}: {ev}" if ev else None
        return _drive(args, name, inject=inject, fired=fired, pass_env=["SWARM_CRASH_AT"] if where == "seat" else None)
    return run


# -- kills -------------------------------------------------------------------------------------

def _kill_seat(args, name, key, role, script_key):
    def patch(s):
        if script_key == "lead":
            s["lead"] = {**s["lead"], "hang_once": True}
        else:
            s[script_key] = {**s[script_key], "hang_once": True}

    def inject(rig, k, snap, st):
        pidf = rig.run.dir / "hang" / f"{key}.pid"
        if pidf.exists() and not st.get("killed"):
            pid = int(pidf.read_text())
            q.kill_tree(pid)
            st["killed"] = f"SIGKILL {role} session pid {pid} mid-task (pass {k})"
            rig.run.event("chaos", st["killed"])

    def after(rig, st):
        starts = [e for e in rig.harness_log() if e.get("ev") == "start" and (e.get("seat") == key or e.get("role") == role)]
        return [f"the killed {role} was not relaunched"] if len(starts) < 2 else []
    return _drive(args, name, patch=patch, inject=inject, fired=lambda rig, st: st.get("killed"), after=after)


def drv_kill_seat_mid_task(args):
    return _kill_seat(args, "kill_seat_mid_task", "eng-a", "engineer", "engineer")


def drv_kill_lead_mid_turn(args):
    return _kill_seat(args, "kill_lead_mid_turn", "lead", "lead", "lead")


def drv_kill_runner(args):
    def patch(s):
        s["engineer"] = {**s["engineer"], "hang_once": True, "hang_seconds": 900}

    def inject(rig, k, snap, st):
        if (rig.run.dir / "hang" / "eng-a.pid").exists() and not st.get("killed"):
            pids = q.run_procs(rig.run, "supervisor.runner")
            for p in pids:
                os.kill(p, signal.SIGKILL)
            if pids:
                st["killed"] = f"SIGKILL runner pid(s) {pids}; the session is orphaned for the next reap"
    return _drive(args, "kill_runner_mid_session", patch=patch, inject=inject, fired=lambda rig, st: st.get("killed"))


def kill_pass_midway(rig, st) -> bool:
    """Plan Task 19 addition, as far as a non-systemd harness can: with a runner alive, SIGKILL the supervisor pass in
    flight, then SIGKILL that runner. `st["killed_pass"]` is recorded ONLY when a runner was seen and killed; otherwise
    nothing is recorded and the miss is counted (the scenario then fails: nothing was tested).
    (`systemctl --user kill swarm-supervise.service` is the real-timer variant: never run here, it names the
    shared unit; Task 17 covers it on the box.)"""
    p = subprocess.Popen([str(rig.run.swarm_bin), "supervise"], env=rig.run.env(),
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    t0 = time.monotonic()
    runners: list[int] = []
    while time.monotonic() - t0 < 45 and p.poll() is None:
        runners = q.run_procs(rig.run, "supervisor.runner")
        if runners:
            break
        time.sleep(0.5)
    if p.poll() is not None or not runners:     # the pass ended first, or never started a runner
        if p.poll() is None:
            p.kill()
        p.wait()
        st["kill_miss"] = st.get("kill_miss", 0) + 1
        return False
    os.kill(p.pid, signal.SIGKILL)               # the pass only: its runner is what we kill next, on purpose
    p.wait()
    victim = runners[0]
    try:
        os.kill(victim, signal.SIGKILL)
    except ProcessLookupError:
        st["kill_miss"] = st.get("kill_miss", 0) + 1
        return False
    st["killed_pass"] = f"pass SIGKILLed in flight (rc {p.returncode}); runner {victim} SIGKILLed ({len(runners)} alive)"
    rig.run.event("chaos", st["killed_pass"])
    return True


def drv_kill_supervisor_and_runner(args):
    def inject(rig, k, snap, st):
        if snap["agents"] and not st.get("killed_pass") and st.get("kill_miss", 0) < 8:
            kill_pass_midway(rig, st)
    return _drive(args, "kill_supervisor_and_runner_once", inject=inject,
                  fired=lambda rig, st: st.get("killed_pass"))


# -- ledger, lock, reactivation -----------------------------------------------------------------

def drv_corrupt_ledger(args):
    def inject(rig, k, snap, st):
        d = rig.run.ledger_dir()
        files = list(d.glob("*.json")) if d.exists() else []
        if files and snap["agents"] and "bak" not in st:
            p = files[0]
            st["bak"], st["path"], st["at"] = p.read_bytes(), p, k
            p.write_bytes(b'{"v": 1, "garbage')
            st["note"] = f"ledger corrupted at pass {k}; restored 3 passes later"
        elif "bak" in st and k >= st["at"] + 3 and not st.get("restored"):
            st["path"].write_bytes(st["bak"])
            st["restored"] = True

    def after(rig, st):
        _, _, _, msgs = rig.state()
        bad = []
        if not any("LEDGER-BAD" in m.message.upper().replace(" ", "-") for m in msgs):
            bad.append("no DRIVER-LEDGER-BAD post: a bad ledger must hold the job loudly, not silently")
        if not st.get("restored"):
            bad.append("the job ended before the ledger was restored: it was closed while corrupt")
        return bad
    return _drive(args, "corrupt_ledger", inject=inject,
                  fired=lambda rig, st: f"ledger corrupted at pass {st['at']}" if "bak" in st else None, after=after)


# every NOT NULL column of drive_locks (lib/swarm/board/seats.py): key, holder, job, generation, launch_id, host, expires_at
STALE_LOCK_COLUMNS = ("key", "holder", "job", "generation", "launch_id", "host", "expires_at")
STALE_LOCK_SQL = ("INSERT INTO drive_locks (key, holder, job, generation, launch_id, host, expires_at) "
                  "VALUES ('land:' || repeat('0', 64) || ':main', 'dead-attempt', 'qa-dead-job', 1, 'dead-launch', "
                  "'dead-host', now() - interval '1 hour') ON CONFLICT DO NOTHING")


def drv_stale_lock(args):
    """Plant a stale ledger lock file and (postgres) an expired drive_locks row of a dead holder, PROVE both exist,
    then require the job to end MET. Nothing planted = FAIL."""
    def inject(rig, k, snap, st):
        if st.get("planted"):
            return
        d = rig.run.ledger_dir()
        ledgers = list(d.glob("*.json")) if d.exists() else []
        if not ledgers:
            return                    # wait until the ledger exists: planting a lock for nothing proves nothing
        planted = []
        for f in ledgers:
            lock = d / (f.stem + ".lock")
            lock.write_text("")
            if lock.exists():
                planted.append(f"ledger lock {lock.name[:12]}")
        if rig.run.pg:
            _pg_sql(rig, STALE_LOCK_SQL)
            rows = _pg_sql(rig, "SELECT holder FROM drive_locks WHERE holder = 'dead-attempt'")
            if rows:
                planted.append("expired drive_locks row of a dead holder")
        st["planted"] = planted

    return _drive(args, "stale_lock", inject=inject,
                  fired=lambda rig, st: ", ".join(st["planted"]) if st.get("planted") else None)


def drv_reactivated_long_idle(args):
    """A drive job halted, silent for hours (backdated), swept, then unhalted must not be auto-closed by any core
    sweep: the 8b live bug. Needs postgres (backdating)."""
    if args.backend != "postgres":
        return ("reactivated_long_idle", S, "not run (needs --backend postgres for backdating)")

    def inject(rig, k, snap, st):
        if snap["agents"] and not st.get("halted"):
            h = rig.run.swarm("halt", "--job", rig.job, "--reason", "chaos: long idle")
            st["halted"] = h.returncode == 0
            _pg_sql(rig, "UPDATE messages SET created_at = created_at - interval '10 hours' WHERE job = %s", (rig.job,))
            _pg_sql(rig, "UPDATE agents SET last_seen = last_seen - interval '10 hours' WHERE job = %s", (rig.job,))
            sweeps = [rig.run.swarm("purge").returncode, rig.run.swarm("status").returncode]
            sweeps.append(rig.run.supervise_pass().returncode)
            js = rig.state()[0]
            st["still_open"] = js is not None and js.status not in q.TERMINAL
            u = rig.run.swarm("unhalt", "--job", rig.job)
            st["note"] = f"halted rc{h.returncode}, backdated 10h, swept {sweeps}, still open={st['still_open']}, unhalt rc{u.returncode}"

    def after(rig, st):
        return [] if st.get("still_open") else ["the halted, long-silent job was closed by a sweep"]
    return _drive(args, "reactivated_long_idle", inject=inject,
                  fired=lambda rig, st: st.get("note") if st.get("halted") else None, after=after)


# -- questions ---------------------------------------------------------------------------------

def question_default_applied(text: str) -> bool:
    """`swarm questions --all` shows the asked question WITH a 'default applied' event (spec 3.10)."""
    return bool(re.search(r"(?i)left or right\?[\s\S]*default applied|default applied[\s\S]*left or right\?", text))


def question_companions(log: list[dict], expires_s: float) -> list[str]:
    """Real events, tied to the expiry: the ask command ran at t0; (a) some seat's `done` succeeded between t0 and
    t0+expires (progress WHILE the question was open); (b) a lead session STARTED after t0+expires (woken by the default)."""
    asks = [e for e in log if e.get("ev") == "cmd" and re.search(r"swarm\S* ask ", e.get("cmd", ""))]
    if not asks:
        return ["no `swarm ask` command was run by the lead"]
    t0 = min(e["at"] for e in asks)
    bad = []
    if not any(e.get("ev") == "cmd" and re.search(r"swarm\S* done ", e.get("cmd", "")) and e.get("rc") == 0
               and t0 <= e["at"] < t0 + expires_s for e in log):
        bad.append("no other seat finished work (`done` rc 0) while the question was open")
    if not any(e.get("ev") == "start" and e.get("role") == "lead" and e["at"] >= t0 + expires_s for e in log):
        bad.append("no lead session started after the default expired: the asker was not woken")
    return bad


def drv_question_default_expires(args):
    def patch(s):
        s["lead"]["ask"] = {"text": "left or right?", "options": "left,right", "default": "left", "expires": "2m"}

    def after(rig, st):
        qs = rig.run.swarm("questions", "--job", rig.job, "--all").stdout
        bad = [] if question_default_applied(qs) else \
            ["the board shows no 'default applied' event for the question (swarm questions --all)"]
        if rig.run.harness == "fake":
            bad += question_companions(rig.harness_log(), 120.0)
        return bad
    return _drive(args, "question_default_expires", patch=patch, after=after,
                  fired=lambda rig, st: "question asked" if "left or right?" in
                  rig.run.swarm("questions", "--job", rig.job, "--all").stdout else None)


# -- forgeries ---------------------------------------------------------------------------------

def drv_forged_closed_by_driver(args):
    def inject(rig, k, snap, st):
        if snap["agents"] and not st.get("forged"):
            rig.run.apply_env()
            with rig.run.board() as b:
                try:
                    ok = b.close_job(rig.job, "completed", "forged by an agent", closed_by="driver")
                    st["forged"] = f"forged close_job(closed_by=driver) accepted={ok}"
                except Exception as exc:
                    st["forged"] = f"forged close refused: {type(exc).__name__}"
            rig.one_pass()            # the driver must notice (reopen or refuse) in the very next pass
            js = rig.state()[0]
            led = rig.run.ledger(rig.job)
            if js is not None and js.status == "completed" and not (led and led.terminal):
                st["accepted"] = True

    def after(rig, st):
        return ["a closed_by=driver close with no ledger terminal stayed completed: the forged MET was accepted"] \
            if st.get("accepted") else []
    return _drive(args, "forged_closed_by_driver", inject=inject, fired=lambda rig, st: st.get("forged"), after=after)


def drv_forged_receipt(args):
    forged = "f" * 40

    def inject(rig, k, snap, st):
        if snap["agents"] and not st.get("forged"):
            rig.run.apply_env()
            with rig.run.board() as b:
                name = next((a.name for a in b.agents(rig.job) if a.role in ("engineer", "worker")), snap["agents"][0][0])
                b.post(rig.job, name, f"DONE feat/forged@{forged} forged hand-off, no receipt")
                b.post(rig.job, name, f"INTEGRATED feat/forged@{forged}")
                b.post(rig.job, name, f"GOAL-READY main@{forged}")
            st["forged"] = "posted DONE, INTEGRATED and GOAL-READY rows with no hook receipt"

    def after(rig, st):
        bad = []
        if rig.run.harness == "fake":
            for e in rig.harness_log():
                if forged in json.dumps(e):
                    bad.append(f"a {e.get('role')} session was launched for the forged artifact")
        if forged in json.dumps(rig.landed()):
            bad.append("the forged SHA reached the target")
        return bad
    return _drive(args, "forged_receipt", inject=inject, fired=lambda rig, st: st.get("forged"), after=after)


def drv_forged_person_commands(args):
    """Agent-only commands from a plain shell (no enrolment, no receipt) and a /tmp work dir are refused."""
    def inject(rig, k, snap, st):
        if snap["agents"] and not st.get("tried"):
            sha = "e" * 40
            cmds = {
                "verdict": ["verdict", "met", "forged", "--job", rig.job, "--artifact", f"main@{sha}"],
                "done": ["done", "--job", rig.job, "--artifact", f"feat/x@{sha}", "--summary", "forged"],
                "review": ["review", "--job", rig.job, "--artifact", f"feat/x@{sha}", "verified", "--report", "/dev/null"],
                "seat_in_tmp": ["seat", "request", "--job", rig.job, "--seat", "forged-seat", "--role", "engineer",
                                "--brief", "/dev/null", "--workdir", "/tmp/forged-seat"],
            }
            st["tried"] = {n: rig.run.swarm(*a).returncode for n, a in cmds.items()}
            st["accepted"] = [n for n, rc in st["tried"].items() if rc == 0]
            st["note"] = f"exit codes {st['tried']}"

    return _drive(args, "forged_person_commands", inject=inject,
                  fired=lambda rig, st: st.get("note") if st.get("tried") else None,
                  after=lambda rig, st: [f"forged `{n}` was accepted" for n in st.get("accepted", [])]
                  + ([] if not any(e.get("seat") == "forged-seat" for e in rig.harness_log()) else
                     ["a seat was launched for the forged /tmp request"]))


def drv_forged_seat_row(args):
    """A seat row written straight into the database, with no authorization record: it must never launch."""
    if args.backend != "postgres":
        return ("forged_seat_row", S, "not run (needs --backend postgres to insert the row)")

    def inject(rig, k, snap, st):
        if snap["agents"] and not st.get("planted"):
            cols = _pg_sql(rig, "SELECT column_name, data_type, is_nullable, column_default FROM information_schema.columns "
                                "WHERE table_name = 'seats' ORDER BY ordinal_position") or []
            known = {"job": rig.job, "seat": "forged-seat", "role": "engineer", "harness": "claude", "model": "haiku",
                     "workdir": str(rig.run.work / "forged-seat"), "brief_path": "/dev/null", "state": "requested",
                     "generation": 1, "revision": 1, "requested_by": "lead", "complexity": "standard"}
            use, vals = [], []
            for c, typ, nullable, default in cols:
                if c in known:
                    use.append(c); vals.append(known[c])
                elif nullable == "NO" and default is None:
                    use.append(c); vals.append(0 if "int" in typ else ("x" if typ in ("text", "character varying") else None))
            from psycopg import sql as _sql  # noqa: F401
            try:
                _pg_sql(rig, f"INSERT INTO seats ({', '.join(use)}) VALUES ({', '.join(['%s'] * len(use))})", tuple(vals))
                rows = _pg_sql(rig, "SELECT count(*) FROM seats WHERE seat = 'forged-seat'")
                st["planted"] = bool(rows and rows[0][0])
            except Exception as exc:
                st["plant_error"] = f"{type(exc).__name__}: {exc}"[:200]
                st["planted"] = False
            st["note"] = "forged seat row inserted" if st["planted"] else f"could not insert: {st.get('plant_error')}"

    return _drive(args, "forged_seat_row", inject=inject,
                  fired=lambda rig, st: st.get("note") if st.get("planted") else None,
                  after=lambda rig, st: ["a seat was launched for the forged row"]
                  if any(e.get("seat") == "forged-seat" for e in rig.harness_log()) else [])


def drv_forged_unhalt_by_seat(args):
    return ("forged_unhalt_by_seat", S, "not automated: needs an enrolled seat shell running while the job is "
            "halted (the fake harness cannot be started by hand during a halt); covered by tests/test_drive_hooks")


# -- pipeline faults ----------------------------------------------------------------------------

def drv_ci_red_twice(args):
    def patch(s):
        s["integrator"] = {"action": "land", "ci_red": 2}

    def fired(rig, st):
        reds = [p for p in (rig.hub / "ci").glob("*.json") if json.loads(p.read_text()).get("state") == "failed"]
        return f"CI recorded red on {len(reds)} rebased SHA(s)" if len(reds) >= 2 else None
    return _drive(args, "ci_red_twice", expect="terminal", patch=patch, fired=fired, check_landing=False)


def drv_budget_ruling_max_sessions(args):
    def patch(s):
        s["reviewer"] = [{"outcome": "changes"}] * 7 + [{"outcome": "verified"}]
    return _drive(args, "budget_ruling_max_sessions_8", expect="terminal", patch=patch, check_landing=False,
                  activate_extra=["--budget", "max_sessions=8"],
                  fired=lambda rig, st: f"{len(rig.state()[2])} launches (cap 8)" if len(rig.state()[2]) >= 8 else None,
                  after=lambda rig, st: [f"{len(rig.state()[2])} launches exceed max_sessions=8 by more than the "
                                         f"ruling reserve allows"] if len(rig.state()[2]) > 8 + 3 else [])


def drv_ruling_judge_fails_twice(args):
    def patch(s):
        s["judge"]["ruling"] = [{"fail": True}, {"fail": True}]

    def after(rig, st):
        js = rig.state()[0]
        scope = getattr(js, "verdict_scope", None)
        return [] if scope in (None, "mechanical") else [f"verdict scope {scope!r}, want the labelled mechanical one"]
    return _drive(args, "ruling_judge_fails_twice", expect="unreachable", script="unreachable", patch=patch, after=after,
                  fired=lambda rig, st: f"{len([e for e in rig.harness_log() if e.get('ev') == 'judge_fail'])} ruling-judge failures"
                  if len([e for e in rig.harness_log() if e.get("ev") == "judge_fail"]) >= 2 else None)


def drv_provider_outage(args):
    def patch(s):
        s["outage_sessions"] = 2

    def fired(rig, st):
        n = len([e for e in rig.harness_log() if e.get("ev") == "outage"])
        return f"{n} sessions failed like a provider outage" if n >= 2 else None
    return _drive(args, "provider_outage", patch=patch, fired=fired, harness="fake")


def drv_bus_down(args):
    """The user bus is down for a few passes (a `systemctl` stub errors); the driver must hold, then recover. Needs the
    real scope mode, so it only runs with SWARM_QA_SYSTEMD=1 (Task 17 covers the box-only containment checks)."""
    if os.environ.get("SWARM_QA_SYSTEMD") != "1":
        return ("bus_down", S, "not run (needs real scope mode: SWARM_QA_SYSTEMD=1 and a user manager; Task 17)")

    def inject(rig, k, snap, st):
        flag = rig.run.dir / "bus-down"
        if k == 0:
            stub = rig.run.bin / "systemctl"
            stub.write_text('#!/bin/sh\n[ -e "%s" ] && { echo "Failed to connect to bus: No medium found" >&2; exit 1; }\n'
                            'exec /usr/bin/systemctl "$@"\n' % flag)
            stub.chmod(0o755)
            flag.write_text("1")
            st["down_from"] = k
        elif k == st.get("down_from", 0) + 4 and flag.exists():
            flag.unlink()
            st["note"] = "bus down for 4 passes, then back"
    return _drive(args, "bus_down", inject=inject, systemd=True,
                  fired=lambda rig, st: st.get("note"))


def drv_stale_met(args):
    """The target moves after the work landed but before the close: the stale `met` must be rejected and the goal judge
    rerun (spec 6.2 scenario 7)."""
    def inject(rig, k, snap, st):
        if not st.get("advanced") and rig.landed()["commits"] and snap["status"] not in q.TERMINAL:
            subprocess.run([sys.executable, str(Path(__file__).parent / "hub.py"), "advance", "--hub", str(rig.hub)],
                           check=True, capture_output=True)
            st["advanced"] = f"target advanced after landing (pass {k})"

    def after(rig, st):
        goal_judges = [e for e in rig.harness_log() if e.get("ev") == "start" and e.get("scope") == "goal"]
        return [] if len(goal_judges) >= 2 or rig.run.harness != "fake" else \
            [f"the goal judge ran {len(goal_judges)}x: the stale met was not re-judged"]
    return _drive(args, "stale_met", expect="terminal", inject=inject, check_landing=False,
                  fired=lambda rig, st: st.get("advanced"), after=after)


def drv_rollover(args):
    """Re-activation with a changed goal mid-run: generation-1 sessions stop before generation 2 launches; a
    generation-1 verdict cannot close the job (spec 6.2 scenario 6)."""
    def inject(rig, k, snap, st):
        if snap["agents"] and not st.get("rolled"):
            r = rig.run.swarm(*rig.activate_args(goal=rig.goal_text() + " (revised: also document it in the README)"))
            st["rolled"] = f"re-activated with a changed goal at pass {k} (rc {r.returncode})" if r.returncode == 0 else None
            st["rc"] = r.returncode

    def after(rig, st):
        led = rig.run.ledger(rig.job)
        gen = max(getattr(led, "generation", 1), getattr(led, "generation_requested", 1)) if led else 0
        return [] if gen >= 2 else [f"ledger generation is {gen}: no rollover happened"]
    return _drive(args, "rollover", expect="terminal", inject=inject, check_landing=False,
                  fired=lambda rig, st: st.get("rolled"), after=after)


def drv_codex_parity(args):
    def patch(s):
        s["lead"]["seats"][0]["harness"] = "codex"

    def after(rig, st):
        codex = [e for e in rig.harness_log() if e.get("ev") == "start" and e.get("harness") == "codex"]
        return [] if codex else ["no session ran on the codex harness"]
    return _drive(args, "codex_parity", patch=patch, activate_extra=["--lead-harness", "codex"], after=after,
                  fired=lambda rig, st: "lead and eng-a on the codex harness", harness="fake")


SCENARIOS = {
    "kill_seat_mid_task": (None, drv_kill_seat_mid_task),
    "kill_lead_mid_turn": (None, drv_kill_lead_mid_turn),
    "kill_runner_mid_session": (None, drv_kill_runner),
    "kill_supervisor_and_runner_once": (None, drv_kill_supervisor_and_runner),
    "corrupt_ledger": ("corrupt_ledger", drv_corrupt_ledger),
    "stale_lock": ("stale_lock", drv_stale_lock),
    "reactivated_long_idle": ("reactivated_long_idle", drv_reactivated_long_idle),
    "question_default_expires": (None, drv_question_default_expires),
    "forged_closed_by_driver": (None, drv_forged_closed_by_driver),
    "forged_receipt": ("forged_receipt", drv_forged_receipt),
    "forged_person_commands": (None, drv_forged_person_commands),
    "forged_seat_row": (None, drv_forged_seat_row),
    "forged_unhalt_by_seat": (None, drv_forged_unhalt_by_seat),
    "ci_red_twice": (None, drv_ci_red_twice),
    "budget_ruling_max_sessions_8": (None, drv_budget_ruling_max_sessions),
    "ruling_judge_fails_twice": (None, drv_ruling_judge_fails_twice),
    "provider_outage": (None, drv_provider_outage),
    "bus_down": (None, drv_bus_down),
    "stale_met": (None, drv_stale_met),
    "rollover": (None, drv_rollover),
    "codex_parity": (None, drv_codex_parity),
}
for _b in BARRIERS:
    SCENARIOS[f"barrier_{_b}"] = (None, _barrier(_b))


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
