"""The drive-mode QA harness itself (e2e/drive_live/): the parts that run today, so a broken harness is
caught here and not on the day the driver lands. Covers the pending-probe, the run-dir guards, the fake
hub (file:// bare repos: exact-SHA CI, fast-forward-only main, no merge commits), the scripted engineer's
toy change, and the no-driver chaos layers (corrupt ledger, stale lock, forged receipt) in a subprocess.

Run: cd tests && /var/tmp/test-slots/test-slot -- python3 -B -m unittest test_drive_qa_harness
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import support  # noqa: F401  (puts lib/ on sys.path)

if sys.platform == "win32":
    raise unittest.SkipTest("the drive QA harness is POSIX only (sh hooks, O_NOFOLLOW ledger, pgrep)")

HARNESS = Path(__file__).resolve().parent.parent / "e2e" / "drive_live"
sys.path.insert(0, str(HARNESS))

POSIX = sys.platform != "win32"
HAVE_MAKE = shutil.which("make") is not None and shutil.which("git") is not None


def git(*a, cwd=None):
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e"}
    r = subprocess.run(["git", *a], cwd=cwd, capture_output=True, text=True, env=env)
    return r


class Tmp(unittest.TestCase):
    def setUp(self):
        # under the home directory, not /tmp: the harness refuses /tmp (tmpfs is RAM on the coder box)
        base = Path.home() / ".cache" / "swarm-qa-harness-tests"
        base.mkdir(parents=True, exist_ok=True)
        self.tmp = Path(tempfile.mkdtemp(prefix="qa-harness-", dir=base))
        self.addCleanup(shutil.rmtree, self.tmp, True)


class NoVacuousPass(Tmp):
    """A fault that did not happen must never read as a pass (review of feat/drive-qa @ 7c652e4)."""

    def test_barrier_presence_is_a_hit_call_in_lib(self):
        import qa_lib as q
        d = self.tmp / "lib/swarm"
        d.mkdir(parents=True)
        (d / "x.py").write_text('barrier.hit("after_claim")\n# hit("only_a_comment_name") is still a hit call\n')
        self.assertTrue(q.barrier_present("after_claim", self.tmp))
        self.assertFalse(q.barrier_present("after_run_file", self.tmp))
        self.assertFalse(q.barrier_present("after_cla", self.tmp))

    def test_a_barrier_that_is_absent_is_a_skip_naming_it(self):
        import argparse
        import chaos
        from unittest import mock
        with mock.patch.object(chaos.q, "barrier_present", lambda name, *a, **k: False):   # as if not built yet
            row = chaos.SCENARIOS["barrier_after_run_file"][1](argparse.Namespace(backend="sqlite", harness="fake"))
        self.assertEqual(row[1], "SKIP")
        self.assertIn("after_run_file", row[2])

    def test_barrier_fired_needs_evidence(self):
        import rig as rigmod
        r = rigmod.Rig.__new__(rigmod.Rig)
        r.rcs, r.harness_log = [0, 0], lambda: []
        self.assertIsNone(r.barrier_fired("after_claim"))                  # two clean passes: nothing was killed
        r.rcs = [0, rigmod.KILLED]
        self.assertIn("SIGKILL", r.barrier_fired("after_claim"))
        r.rcs, r.harness_log = [0], lambda: [{"ev": "hook_killed", "role": "engineer", "event": "done"}]
        self.assertIn("hook", r.barrier_fired("hook_after_cli_before_receipt"))
        r.harness_log = lambda: [{"ev": "start", "sid": "a"}]
        self.assertIsNotNone(r.barrier_fired("runner_mid_session"))        # started, never ended
        self.assertIsNone(r.barrier_fired("after_claim"))

    def test_every_planned_barrier_and_required_scenario_has_a_row(self):
        import chaos
        plan = ["before_claim", "after_claim", "after_run_file", "after_marker", "after_runner_start", "runner_mid_session",
                "before_finish", "mid_terminalization", "hook_after_cli_before_receipt", "land_after_first_destination",
                "after_intent_before_claim", "mid_rollover", "mid_rollover_after_ledger"]
        for b in plan:
            self.assertIn(f"barrier_{b}", chaos.SCENARIOS)
        for n in ("kill_seat_mid_task", "kill_lead_mid_turn", "ci_red_twice", "budget_ruling_max_sessions_8",
                  "ruling_judge_fails_twice", "provider_outage", "bus_down", "stale_met", "rollover", "codex_parity",
                  "forged_closed_by_driver", "forged_receipt", "forged_person_commands", "forged_seat_row",
                  "forged_unhalt_by_seat", "question_default_expires", "stale_lock", "corrupt_ledger",
                  "reactivated_long_idle", "kill_supervisor_and_runner_once"):
            self.assertIn(n, chaos.SCENARIOS)

    def test_pending_without_pieces_says_not_run(self):
        import qa_lib as q
        self.assertTrue(str(q.Pending([], "needs postgres")).startswith("not run"))


class Mutants(Tmp):
    """Regression tests for the QA harness's own guards (re-review of feat/drive-qa @ ba945c7): each of these
    fails if the guard it names is removed."""

    def test_stale_lock_plants_every_not_null_column_of_drive_locks(self):
        import re
        import chaos
        ddl = (Path(__file__).resolve().parent.parent / "lib/swarm/board/launches.py").read_text()
        body = re.search(r"CREATE TABLE IF NOT EXISTS drive_locks \((.*?)\);", ddl, re.S).group(1)
        required = {m.group(1) for m in re.finditer(r"(\w+) [^,]*NOT NULL", body.replace("{big}", "BIG").replace("{ts}", "TS"))}
        required.add("key")                                   # the PRIMARY KEY
        self.assertTrue({"key", "holder", "job", "generation", "launch_id", "host", "expires_at"} <= required, required)
        cols = re.search(r"INSERT INTO drive_locks \(([^)]*)\)", chaos.STALE_LOCK_SQL).group(1)
        self.assertLessEqual(required, {c.strip() for c in cols.split(",")}, "the planted row omits a NOT NULL column")
        self.assertEqual(set(chaos.STALE_LOCK_COLUMNS), {c.strip() for c in cols.split(",")})

    def fake_rig(self, **kw):
        from types import SimpleNamespace as NS
        calls = self.calls = []
        run = NS(harness="fake", pg=None, ledger_dir=lambda: self.tmp / "ledgers", dir=self.tmp,
                 swarm=lambda *a, **k: calls.append(a) or NS(returncode=0, stdout="", stderr=""),
                 event=lambda *a, **k: None, bin=self.tmp, env=lambda: {})
        rig = NS(run=run, job="J", calls=calls, budget_usd=1.0, **kw)
        return rig

    def test_cost_cap_halts_the_job_and_raises(self):
        from unittest import mock
        import qa_lib as q
        import rig as rigmod
        r = rigmod.Rig.__new__(rigmod.Rig)
        r.run = self.fake_rig().run
        r.job, r.budget_usd, r.passes, r.invariant_violations = "J", 1.0, 0, []
        r.stall_passes, r._last_change, r._last_sig, r.pass_env, r.rcs = 10, 0, None, {}, []
        r.snapshot_line = lambda: {"status": "active", "agents": [], "messages": 0, "restarts": 1, "verdict": None,
                                   "closed_by": None}
        r.spent_usd = lambda: 1.5
        r.one_pass = lambda: self.fail("a pass ran after the cost cap was exceeded")
        with mock.patch.object(q, "require"), self.assertRaises(rigmod.CostCap):
            r.drive(timeout_s=60, interval_s=0)
        self.assertTrue(any(c[0] == "halt" for c in self.calls), "the job was not halted")

    def test_under_budget_keeps_driving(self):
        from unittest import mock
        import qa_lib as q
        import rig as rigmod
        r = rigmod.Rig.__new__(rigmod.Rig)
        r.run = self.fake_rig().run
        r.job, r.budget_usd, r.passes, r.invariant_violations = "J", 1.0, 0, []
        r.stall_passes, r._last_change, r._last_sig, r.pass_env, r.rcs = 10, 0, None, {}, []
        seq = iter([{"status": "active", "agents": [], "messages": 0, "restarts": 0, "verdict": None, "closed_by": None},
                    {"status": "completed", "agents": [], "messages": 1, "restarts": 1, "verdict": "met", "closed_by": "x"}])
        r.snapshot_line = lambda: next(seq)
        r.spent_usd = lambda: 0.5
        r.one_pass = lambda: None
        with mock.patch.object(q, "require"):
            self.assertEqual(r.drive(timeout_s=60, interval_s=0)["status"], "completed")

    def test_session_mode_watches_swarm_drive_run_and_runs_no_pass(self):
        """[T21]: in the default mode `swarm drive run` drives the job; the rig only watches, then stops it."""
        from unittest import mock
        from types import SimpleNamespace as NS
        import qa_lib as q
        import rig as rigmod
        r = rigmod.Rig.__new__(rigmod.Rig)
        r.run = self.fake_rig().run
        proc = mock.Mock()
        proc.poll.return_value = None
        r.run.drive_run = lambda: proc
        r.driver = "session"
        r.job, r.budget_usd, r.passes, r.invariant_violations = "J", 1.0, 0, []
        r.stall_passes, r._last_change, r._last_sig, r.pass_env, r.rcs = 10, 0, None, {}, []
        seq = iter([{"status": "active", "agents": [], "messages": 0, "restarts": 0, "verdict": None, "closed_by": None},
                    {"status": "completed", "agents": [], "messages": 1, "restarts": 1, "verdict": "met", "closed_by": "x"}])
        r.snapshot_line = lambda: next(seq)
        r.spent_usd = lambda: 0.5
        r.one_pass = lambda: self.fail("a supervisor pass ran in session-driver mode")
        with mock.patch.object(q, "require"):
            self.assertEqual(r.drive(timeout_s=60, interval_s=0)["status"], "completed")
        proc.terminate.assert_called_once()
        self.assertEqual(NS(calls=self.calls).calls, [])     # no `swarm supervise` either

    def test_real_mode_resolves_claude_from_the_users_path_inside_the_isolated_home(self):
        from unittest import mock
        import qa_lib as q
        bindir = self.tmp / "userbin"
        bindir.mkdir()
        (bindir / "claude").write_text("#!/bin/sh\n")
        (bindir / "claude").chmod(0o755)
        (self.tmp / "realhome" / ".claude").mkdir(parents=True)
        (self.tmp / "realhome" / ".claude" / ".credentials.json").write_text("{}")
        with mock.patch.object(q, "QA_ROOT", self.tmp), mock.patch.object(q, "REAL_PATH", f"{bindir}:/usr/bin"), \
                mock.patch.object(q, "REAL_HOME", str(self.tmp / "realhome")), \
                mock.patch.dict(os.environ, {"SWARM_QA_CLAUDE": ""}):
            run = q.Run("realmode", backend="sqlite", harness="real", stamp="t")
            run.home.mkdir(parents=True)
            run.bin.mkdir(parents=True)
            run._write_config()
            self.assertIn(str(bindir), run.env()["PATH"])
            self.assertIn(f'claude_bin = "{bindir / "claude"}"', run.config.read_text())
            self.assertTrue((run.home / ".claude" / ".credentials.json").exists())
            fake = q.Run("fakemode", backend="sqlite", harness="fake", stamp="t")
            self.assertNotIn(str(bindir), fake.env()["PATH"])

    def run_harness(self, scope, seat, prompt_extra=""):
        """One fake judge session as a subprocess; returns the `swarm` arguments it ran."""
        run = self.tmp / "hrun"
        (run / "home").mkdir(parents=True, exist_ok=True)
        (run / "bin").mkdir(exist_ok=True)
        (run / "repo" / "bin").mkdir(parents=True, exist_ok=True)
        (run / "repo" / "bin" / "swarm-hook").write_text("#!/bin/sh\ncat >/dev/null\n")
        (run / "repo" / "bin" / "swarm-hook").chmod(0o755)
        (run / "bin" / "swarm").write_text(f'#!/bin/sh\necho "$@" >> {run}/swarm-calls\n')
        (run / "bin" / "swarm").chmod(0o755)
        (run / "script.json").write_text(json.dumps({"judge": {
            "artifact": [{"verdict": "not_met", "open": 1}, {"verdict": "met"}],
            "goal": [{"verdict": "met"}], "ruling": [{"verdict": "unreachable", "reason": "r"}]}}))
        (run / "harness.json").write_text(json.dumps({"repo": str(run / "repo"), "script": str(run / "script.json"),
                                                      "hub": str(run / "hub"), "job": "J"}))
        before = (run / "swarm-calls").read_text().splitlines() if (run / "swarm-calls").exists() else []
        prompt = f"[swarm job: J]\n[swarm role: judge]\n[swarm seat: {seat}]\n[swarm scope: {scope}]\n{prompt_extra}"
        r = subprocess.run([sys.executable, "-B", str(HARNESS / "fake_harness.py"), "--harness", "claude", "-p"],
                           input=prompt, capture_output=True, text=True,
                           env={"HOME": str(run / "home"), "PATH": os.environ["PATH"]})
        self.assertEqual(r.returncode, 0, r.stderr)
        return (run / "swarm-calls").read_text().splitlines()[len(before):]

    def test_judge_sessions_follow_the_script_by_scope_not_by_seat(self):
        first = self.run_harness("goal", "judge-1")
        self.assertTrue(first and first[0].startswith("verdict met "), first)             # goal: first entry is met
        art1 = self.run_harness("artifact", "judge-2")
        self.assertTrue(art1 and art1[0].startswith("verdict not_met"), art1)             # artifact: first entry
        art2 = self.run_harness("artifact", "judge-OTHER-SEAT")                           # a different seat, same scope
        self.assertTrue(art2 and art2[0].startswith("verdict met "), art2)                # ...gets the SECOND entry
        ruling = self.run_harness("ruling", "judge-1")
        self.assertTrue(ruling and ruling[0].startswith("verdict unreachable"), ruling)

    def test_drive_fails_when_the_fault_never_fired_and_passes_when_it_did(self):
        import argparse
        from types import SimpleNamespace as NS
        from unittest import mock
        import chaos
        import qa_lib as q

        class FakeRig:
            def __init__(self, *a, **k):
                self.run = NS(harness="fake", event=lambda *a, **k: None)
            def __enter__(self): return self
            def __exit__(self, *e): pass
            def activate(self): pass
            def drive(self, **k): return {}
            def check_terminal(self, expect): return []
            def check_no_coordinator(self): return []
            def check_every_seat_supervisor_launched(self): return []
            def check_landing(self): return []
            def write_costs(self): return {"launches": 1, "passes": 1, "spent_usd": 0}
            def export_evidence(self): pass
        args = argparse.Namespace(backend="sqlite", harness="fake", budget_usd=1, timeout_min=1, interval_s=0)
        with mock.patch.object(chaos, "Rig", FakeRig), mock.patch.object(q, "require"):
            never = chaos._drive(args, "x", fired=lambda rig, st: None)
            did = chaos._drive(args, "x", fired=lambda rig, st: "it happened")
            nofn = chaos._drive(args, "x")
        self.assertEqual(never[1], "FAIL")
        self.assertIn("never fired", never[2])
        self.assertEqual(did[1], "PASS")
        self.assertEqual(nofn[1], "PASS")

    def test_stale_lock_inject_plants_only_once_a_ledger_exists_and_fired_needs_the_proof(self):
        from unittest import mock
        import chaos
        rig = self.fake_rig()
        captured = {}
        with mock.patch.object(chaos, "_drive", lambda args, name, **kw: captured.update(kw) or ("x", "PASS", "")):
            chaos.drv_stale_lock(None)
        st: dict = {}
        captured["inject"](rig, 0, {}, st)                                  # no ledger directory yet: nothing planted
        self.assertFalse(st.get("planted"))
        self.assertIsNone(captured["fired"](rig, st))
        (self.tmp / "ledgers").mkdir()
        (self.tmp / "ledgers" / ("a" * 64 + ".json")).write_text("{}")
        captured["inject"](rig, 1, {}, st)
        self.assertTrue((self.tmp / "ledgers" / ("a" * 64 + ".lock")).exists())
        self.assertIn("ledger lock", captured["fired"](rig, st))
        self.assertIsNone(captured["fired"](rig, {"planted": []}))          # an empty plant is not proof

    def test_the_question_check_wants_the_event_and_real_companions(self):
        import chaos
        asked = "Q1 left or right?"
        self.assertTrue(chaos.question_default_applied(f"{asked}\n  default applied: left"))
        self.assertFalse(chaos.question_default_applied(f"{asked}\n  default: left (--default left)"))   # only the word default
        self.assertFalse(chaos.question_default_applied("Q1 other question\n default applied"))
        cmd = lambda t, c, rc=0: {"ev": "cmd", "at": t, "cmd": f"/r/bin/swarm {c}", "rc": rc}   # noqa: E731
        start = lambda t, role: {"ev": "start", "at": t, "role": role}                         # noqa: E731
        ok = [cmd(100, "ask --job J"), cmd(150, "done --job J"), start(260, "lead")]
        self.assertEqual(chaos.question_companions(ok, 120), [])
        no_progress = [cmd(100, "ask --job J"), cmd(300, "done --job J"), start(260, "lead")]   # done only AFTER expiry
        self.assertTrue(any("while the question was open" in b for b in chaos.question_companions(no_progress, 120)))
        no_wake = [cmd(100, "ask --job J"), cmd(150, "done --job J"), start(110, "lead")]       # lead started before expiry
        self.assertTrue(any("not woken" in b for b in chaos.question_companions(no_wake, 120)))
        self.assertTrue(chaos.question_companions([start(1, "lead")], 120))                     # no ask at all

    def test_a_nonzero_exit_that_is_not_sigkill_is_not_a_barrier(self):
        import rig as rigmod
        r = rigmod.Rig.__new__(rigmod.Rig)
        r.harness_log = lambda: []
        for rcs in ([1], [0, 2], [-15], [255]):
            r.rcs = rcs
            self.assertIsNone(r.barrier_fired("after_claim"), rcs)

    def test_kill_pass_midway_needs_a_live_runner_and_kills_it(self):
        from types import SimpleNamespace as NS
        from unittest import mock
        import chaos
        import qa_lib as q
        sh = self.tmp / "swarm"
        sh.write_text("#!/bin/sh\nsleep 30\n")
        sh.chmod(0o755)
        run = NS(bin=self.tmp, env=lambda: dict(os.environ), event=lambda *a, **k: None)
        rig = NS(run=run)
        st: dict = {}
        with mock.patch.object(q, "run_procs", lambda *a, **k: []), mock.patch.object(chaos.time, "sleep", lambda s: None), \
                mock.patch.object(chaos.time, "monotonic", iter([0, 1, 100, 100, 100]).__next__):
            self.assertFalse(chaos.kill_pass_midway(rig, st))                # the pass never had a runner
        self.assertNotIn("killed_pass", st)
        self.assertEqual(st["kill_miss"], 1)
        runner = subprocess.Popen(["sleep", "30"])
        self.addCleanup(runner.kill)
        st = {}
        with mock.patch.object(q, "run_procs", lambda *a, **k: [runner.pid]), mock.patch.object(chaos.time, "sleep", lambda s: None):
            self.assertTrue(chaos.kill_pass_midway(rig, st))
        self.assertEqual(runner.wait(timeout=10), -9)                        # the runner really was SIGKILLed
        self.assertIn("killed_pass", st)


@unittest.skipUnless(POSIX, "POSIX only")
class PgSignals(Tmp):
    def test_sigterm_stops_the_throwaway_cluster(self):
        import qa_lib as q
        if not (q.PG_BIN / "postgres").exists() or subprocess.run([str(q.PG_BIN / "postgres"), "-V"],
                                                                   capture_output=True).returncode:
            self.skipTest("no runnable Postgres for the QA harness here")
        import signal
        import time
        code = ("import sys, time; sys.path.insert(0, %r); import qa_lib as q; from pathlib import Path\n"
                "pg = q.Pg(q.QA_ROOT / 'runs' / 'sigterm-test' / 'pg'); pg.start(); print('up', flush=True); time.sleep(120)\n"
                % str(HARNESS))
        env = {**os.environ, "SWARM_QA_ROOT": str(self.tmp / "qa"), "SWARM_QA_PG_BIN": str(q.PG_BIN)}
        p = subprocess.Popen([sys.executable, "-B", "-c", code], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, env=env)
        self.addCleanup(p.stdout.close)
        data = self.tmp / "qa" / "runs" / "sigterm-test" / "pg" / "data"
        self.addCleanup(lambda: subprocess.run([str(q.PG_BIN / "pg_ctl"), "-D", str(data), "-m", "immediate", "stop"],
                                               capture_output=True))
        line = p.stdout.readline().strip()
        self.assertEqual(line, "up", line)
        self.assertTrue((data / "postmaster.pid").exists())
        p.send_signal(signal.SIGTERM)
        p.wait(timeout=60)
        for _ in range(50):
            if not (data / "postmaster.pid").exists():
                break
            time.sleep(0.2)
        self.assertFalse((data / "postmaster.pid").exists(), "the cluster was left running after SIGTERM")
        self.assertEqual(p.returncode, 128 + signal.SIGTERM)


class Guards(Tmp):
    def test_run_dir_must_be_under_the_qa_root_and_never_tmp(self):
        import qa_lib as q
        with self.assertRaises(q.Refused):
            q.check_run_dir(Path("/tmp/x"))
        with self.assertRaises(q.Refused):
            q.check_run_dir(Path("/var/tmp/x"))
        with self.assertRaises(q.Refused):
            q.check_run_dir(Path(os.path.expanduser("~")) / "elsewhere")
        self.assertEqual(q.check_run_dir(q.QA_ROOT / "runs" / "x"), q.QA_ROOT / "runs" / "x")

    def test_job_names_are_plain(self):
        import qa_lib as q
        for bad in ("a b", "../x", "", "x" * 80, "a;b"):
            with self.assertRaises(q.Refused):
                q.check_job_name(bad)
        self.assertEqual(q.check_job_name("qa-live.met_1"), "qa-live.met_1")


@unittest.skipUnless(POSIX and HAVE_MAKE, "the fake hub needs git, make and POSIX sh hooks")
class Hub(Tmp):
    def setUp(self):
        super().setUp()
        import hub
        self.hub = hub
        self.f = self.tmp / "hub"
        info = hub.init(self.f, HARNESS / "toy-repo-template", ["origin", "backup"])
        self.initial = info["initial"]
        self.urls = {k: v for k, v in info.items() if k != "initial"}
        self.w = self.tmp / "w"
        git("clone", "-q", self.urls["origin"], str(self.w))

    def commit(self, name="x.txt", text="x\n", msg="change"):
        (self.w / name).write_text(text)
        git("add", "-A", cwd=self.w)
        return git("commit", "-q", "-m", msg, cwd=self.w)

    def test_urls_are_file_urls_and_both_destinations_start_equal(self):
        self.assertTrue(all(u.startswith("file:///") for u in self.urls.values()))
        a = self.hub.audit(self.f, None)
        self.assertTrue(a["destinations_equal"] and a["linear"])

    def test_ci_is_green_only_for_the_exact_sha_that_was_run(self):
        f = self.hub
        self.assertEqual(f.ci_state(self.f, self.initial), "none")
        self.assertEqual(f.ci_run(self.f, self.initial)["state"], "green")
        self.assertEqual(f.ci_state(self.f, self.initial), "green")
        self.assertEqual(f.ci_state(self.f, "0" * 40), "none")
        self.assertEqual(f.ci_state(self.f, "abc"), "none")            # not an exact SHA

    def test_a_red_toy_change_makes_ci_red_and_forced_red_sticks(self):
        f = self.hub
        self.commit("tests/test_bad.py", "import unittest\nclass T(unittest.TestCase):\n"
                    "    def test_x(self):\n        self.assertEqual(1, 2)\n")
        sha = git("rev-parse", "HEAD", cwd=self.w).stdout.strip()
        self.assertEqual(git("push", "-q", self.urls["origin"], "HEAD:refs/heads/feat/bad", cwd=self.w).returncode, 0)
        self.assertEqual(f.ci_run(self.f, sha)["state"], "failed")
        f.ci_run(self.f, self.initial)
        (f.results_dir(self.f) / f"{self.initial}.json").write_text(json.dumps({"state": "failed"}))
        self.assertEqual(f.ci_state(self.f, self.initial), "failed")

    def test_main_takes_fast_forwards_only(self):
        self.commit()
        ok = git("push", self.urls["origin"], "HEAD:main", cwd=self.w)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.hub.advance(self.f, "origin")                       # the target moves under us
        self.commit("y.txt", "y\n", "second")
        git("reset", "-q", "--hard", "HEAD", cwd=self.w)
        self.assertNotEqual(git("push", self.urls["origin"], "HEAD:main", cwd=self.w).returncode, 0)   # client side
        bad = git("push", "-f", self.urls["origin"], "HEAD:main", cwd=self.w)                         # the hub itself
        self.assertNotEqual(bad.returncode, 0)
        self.assertRegex(bad.stderr, r"non-fast-forward")

    def test_a_merge_commit_on_main_is_refused(self):
        git("checkout", "-q", "-b", "side", cwd=self.w)
        self.commit("s.txt", "s\n", "side")
        git("checkout", "-q", "main", cwd=self.w)
        self.commit("m.txt", "m\n", "main")
        m = git("merge", "--no-ff", "-q", "-m", "merge", "side", cwd=self.w)
        self.assertEqual(m.returncode, 0, m.stderr)
        bad = git("push", self.urls["origin"], "HEAD:main", cwd=self.w)
        self.assertNotEqual(bad.returncode, 0)
        self.assertIn("merge commit", bad.stderr)

    def test_audit_reports_a_merge_commit_that_got_onto_main_by_other_means(self):
        git("checkout", "-q", "-b", "side", cwd=self.w)
        self.commit("s.txt", "s\n", "side")
        git("checkout", "-q", "main", cwd=self.w)
        self.commit("m.txt", "m\n", "main")
        git("merge", "--no-ff", "-q", "-m", "merge", "side", cwd=self.w)
        git("push", "-q", self.urls["origin"], "HEAD:refs/heads/staged", cwd=self.w)   # not main: the hook lets it by
        sha = git("rev-parse", "HEAD", cwd=self.w).stdout.strip()
        git("--git-dir", str(self.f / "origin.git"), "update-ref", "refs/heads/main", sha)   # as an admin would
        self.assertFalse(self.hub.audit(self.f, self.initial)["linear"])

    def test_the_update_hook_alone_refuses_a_rewind_of_main(self):
        git("config", "receive.denyNonFastForwards", "false", cwd=self.f / "origin.git")   # leave only the hook
        self.hub.advance(self.f, "origin")                      # main moves on; our commit now diverges from it
        self.commit()
        bad = git("push", "-f", self.urls["origin"], "HEAD:main", cwd=self.w)
        self.assertNotEqual(bad.returncode, 0)
        self.assertIn("hub: non-fast-forward refused", bad.stderr)

    def test_every_destination_is_configured_to_deny_non_fast_forwards(self):
        for d in ("origin", "backup"):
            r = git("config", "receive.denyNonFastForwards", cwd=self.f / f"{d}.git")
            self.assertEqual(r.stdout.strip(), "true", d)

    def test_branch_deletion_is_allowed_for_the_integrator(self):
        self.commit()
        git("push", "-q", self.urls["origin"], "HEAD:refs/heads/feat/a", cwd=self.w)
        gone = git("push", self.urls["origin"], ":refs/heads/feat/a", cwd=self.w)
        self.assertEqual(gone.returncode, 0, gone.stderr)

    def test_advance_moves_every_destination_to_one_commit_and_audit_sees_it(self):
        moved = self.hub.advance(self.f, None)
        self.assertEqual(len(set(moved.values())), 1)
        a = self.hub.audit(self.f, self.initial)
        self.assertTrue(a["destinations_equal"] and a["linear"])
        self.assertEqual(a["commits"], list(set(moved.values())))

    def test_the_cli_ci_check_is_an_evidence_command(self):
        script = HARNESS / "hub.py"
        r = subprocess.run([sys.executable, str(script), "ci-check", "--hub", str(self.f), "--run", self.initial],
                           capture_output=True, text=True)
        self.assertEqual((r.returncode, r.stdout.strip()), (0, "green"))
        r = subprocess.run([sys.executable, str(script), "ci-check", "--hub", str(self.f), "f" * 40],
                           capture_output=True, text=True)
        self.assertEqual((r.returncode, r.stdout.strip()), (1, "none"))


@unittest.skipUnless(POSIX and HAVE_MAKE, "needs git, make and POSIX")
class ScriptedEngineer(Tmp):
    """The fake engineer's change is real work: the version change passes the toy's tests, the impossible
    one cannot."""

    def load_harness(self):
        run = self.tmp / "run"
        (run / "home").mkdir(parents=True)
        script = run / "s.json"
        script.write_text("{}")
        (run / "harness.json").write_text(json.dumps({"repo": str(HARNESS), "script": str(script),
                                                      "hub": str(run / "hub")}))
        old = os.environ.get("HOME")
        os.environ["HOME"] = str(run / "home")
        self.addCleanup(lambda: os.environ.__setitem__("HOME", old) if old else os.environ.pop("HOME", None))
        sys.modules.pop("fake_harness", None)
        import fake_harness
        return fake_harness

    def toy(self):
        w = self.tmp / "toy"
        shutil.copytree(HARNESS / "toy-repo-template", w)
        return w

    def make_test(self, w):
        return subprocess.run(["make", "test"], cwd=w, capture_output=True, text=True,
                              env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})

    def test_version_change_passes_make_test_and_adds_a_real_test(self):
        fh, w = self.load_harness(), self.toy()
        self.assertEqual(self.make_test(w).returncode, 0)
        fh.edit_toy(w, "version", 0)
        r = self.make_test(w)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("test_version", (w / "tests" / "test_toy.py").read_text())
        self.assertEqual(subprocess.run([sys.executable, "-B", "toy.py", "--version"], cwd=w, capture_output=True,
                                        text=True).stdout.strip(), "toy 0.1.0")

    def test_a_fix_round_adds_another_test_that_still_passes(self):
        fh, w = self.load_harness(), self.toy()
        fh.edit_toy(w, "version", 0)
        fh.edit_toy(w, "version", 1)
        self.assertEqual(self.make_test(w).returncode, 0)
        self.assertIn("Round1", (w / "tests" / "test_toy.py").read_text())

    def test_the_impossible_change_fails_make_test(self):
        fh, w = self.load_harness(), self.toy()
        fh.edit_toy(w, "impossible", 0)
        self.assertNotEqual(self.make_test(w).returncode, 0)

    def test_judge_scope_and_script_are_keyed_by_scope_not_seat(self):
        fh = self.load_harness()
        self.assertEqual(fh.judge_scope("You are the goal judge"), "goal")
        self.assertEqual(fh.judge_scope("[swarm scope: ruling]\nx"), "ruling")
        self.assertEqual(fh.judge_scope("judge the artifact feat/x@" + "a" * 40), "artifact")
        met = json.loads((HARNESS / "scripts" / "met.json").read_text())["judge"]
        unr = json.loads((HARNESS / "scripts" / "unreachable.json").read_text())["judge"]
        self.assertEqual(fh.nth(met["goal"], 0)["verdict"], "met")                   # the goal judge's FIRST run is met
        self.assertEqual(fh.nth(unr["ruling"], 0)["verdict"], "unreachable")         # the ruling judge's first is unreachable
        self.assertEqual([e["verdict"] for e in met["artifact"]], ["not_met", "met"])

    def test_the_nth_script_entry_sticks_at_the_last(self):
        fh = self.load_harness()
        self.assertEqual(fh.nth([{"a": 1}, {"a": 2}], 0), {"a": 1})
        self.assertEqual(fh.nth([{"a": 1}, {"a": 2}], 5), {"a": 2})
        self.assertEqual(fh.nth({"a": 9}, 3), {"a": 9})
        self.assertEqual(fh.nth(None, 0, "d"), "d")


@unittest.skipUnless(POSIX, "POSIX only (O_NOFOLLOW ledger)")
class NoDriverChaosLayers(Tmp):
    """The `<name>/today` rows of the chaos suite, in a subprocess (they replace $HOME and the environment
    of their process) on a throwaway SQLite board under a temp QA root. No Postgres, no driver
    (`--today-only` leaves the driven rows to e2e/drive_live.sh)."""

    def venv(self):
        import qa_lib as q
        return str(q.VENV if (q.VENV / "bin" / "python").exists() else sys.prefix)

    def run_chaos(self, *names):
        env = {k: v for k, v in os.environ.items() if k in ("PATH", "LANG", "LC_ALL")}
        env.update(SWARM_QA_ROOT=str(self.tmp / "qa"), SWARM_QA_VENV=self.venv(),
                   SWARM_QA_REPO=str(Path(__file__).resolve().parent.parent))
        return subprocess.run([sys.executable, "-B", str(HARNESS / "live.py"), "chaos", "--today-only", "--backend", "sqlite", *names],
                              capture_output=True, text=True, env=env, cwd=HARNESS, timeout=240)

    def test_corrupt_ledger_stale_lock_and_forged_receipt_layers_pass(self):
        if not (Path(self.venv()) / "bin" / "python").exists():
            self.skipTest("no venv python under sys.prefix for the run shims")
        r = self.run_chaos("corrupt_ledger", "stale_lock", "forged_receipt")
        out = r.stdout
        for name in ("corrupt_ledger/today", "stale_lock/today", "forged_receipt/today"):
            self.assertRegex(out, rf"PASS\s+{name}\b", out + r.stderr[-1500:])
        self.assertNotRegex(out, r"(?m)^FAIL\s")
        self.assertEqual(r.returncode, 0, out)

    def test_an_unknown_scenario_is_a_failure_not_a_silent_skip(self):
        r = self.run_chaos("no_such_scenario")
        self.assertEqual(r.returncode, 1)
        self.assertIn("unknown scenario", r.stdout)


if __name__ == "__main__":
    unittest.main()
