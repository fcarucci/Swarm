"""an agent can't silently stop its own final transcript archive by making redaction
run out of time. A failed final capture stays pending and is retried (the sweeps, then the
supervisor pass with FINAL_RETRY_SECONDS); one that keeps failing ends as a capture-failed row
(reason and size, no text) that status and doctor show and every reader treats as no body."""
from __future__ import annotations

import datetime as dt
import getpass
import json
import time
from unittest import mock

from support import LIB, posix_only  # noqa: E402
from test_transcripts_capture import SESSION, CaptureEnv, line  # noqa: F401  (sets sys.path)

from swarm import cli as swarm  # noqa: E402
from swarm import hooks as swarm_hooks  # noqa: E402
from swarm import transcripts  # noqa: E402
from swarm.supervisor import command, lost  # noqa: E402

SECRET = "Zq8Zq8Zq8Zq8Zq8"


def slow_text(kb: int) -> str:
    """JSONL whose redaction takes a while (dense key=value secrets: ~0.3 s per MB here) -- the
    adversarial shape, scaled down: the tests scale the budgets down with it."""
    words = " ".join(f"password=Pw{i:08d}xyzXYZ" for i in range(100))
    one = json.dumps({"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": words}]}})
    return "".join(one + "\n" for _ in range(max(1, kb * 1024 // (len(one) + 1))))


class FinalRetryEnv(CaptureEnv):
    def setUp(self):
        super().setUp()
        with self.config.open("a") as fh:
            fh.write("[supervise]\nenabled = true\n")
        from swarm import cli
        self.cfg = cli.load_config(self.config)
        self.cfg["supervise"]["allowed_workdirs"] = [str(self.tmp)]
        # scaled: a hook budget the slow transcript always outlasts, and failures at it count
        # as "at a real budget" (the real hook has 4 s, SLOW_AFTER_SECONDS is 2 s)
        p = mock.patch.object(transcripts, "SLOW_AFTER_SECONDS", 0.01)
        p.start()
        self.addCleanup(p.stop)

    def slow_agent(self, key: str, kb: int = 600):
        p = self.agent_file(key)
        with p.open("a") as fh:
            fh.write(line(f"the end of {key}, token={SECRET}") + slow_text(kb))
        return p

    def stop(self, key: str, budget: float = 0.02):
        with mock.patch.object(swarm_hooks, "TRANSCRIPT_BUDGET_SECONDS", budget):
            self.hook("stop", agent_id=key, session=SESSION, transcript_path=str(self.main))

    def state(self) -> dict:
        p = lost.retry_state_path()
        return json.loads(p.read_text()) if p.exists() else {}

    def row(self, key: str, job: str = "J"):
        rows = self.rows(job=job, agent_key=key)
        return rows[0] if rows else None

    def supervise(self):
        out: list = []
        rc = command.run_pass(self.cfg, start_runner=lambda cfg, run: 4242, which=lambda b: f"/usr/bin/{b}",
                              say=out.append, scope_available=lambda: True)
        self.assertEqual(rc, 0, out)
        return out


class HookOutOfTimeTests(FinalRetryEnv):
    def test_out_of_time_at_the_hook_is_stored_by_the_supervisor_retry(self):
        self.activate()
        self.fresh_stamp()
        self.start("a1")
        self.slow_agent("a1")
        self.stop("a1")                                        # 4 s in real life; outlasted
        self.assertIsNone(self.row("a1"))
        self.assertIn("OutOfTime", self.error_log.read_text())
        entry = self.state()["J\ta1"]
        self.assertEqual((entry["slow"], entry.get("tries", 0), entry["harness"], entry["reason"]),
                         (True, 0, "claude", "ran out of time"))
        with self.board() as b:                                # the sweeps leave it to the pass...
            swarm.sweep_jobs(b, self.cfg)
        self.assertIsNone(self.row("a1"))
        self.supervise()                                       # ...which has 60 s for it
        row = self.row("a1")
        self.assertIsNotNone(row)
        self.assertEqual((row.final, row.failed), (True, None))
        body = self.body("J", "a1")
        self.assertIn("the end of a1", body)
        self.assertNotIn(SECRET, body)
        self.assertNotIn("Pw00000001xyzXYZ", body)
        with self.board() as b:
            swarm.sweep_jobs(b, self.cfg)                      # no longer pending: forgotten
        self.assertNotIn("J\ta1", self.state())

    def test_never_finishing_capture_ends_as_a_capture_failed_row(self):
        self.activate()
        self.fresh_stamp()
        self.start("a1")
        size = self.slow_agent("a1").stat().st_size
        self.stop("a1")
        with mock.patch.object(transcripts, "FINAL_RETRY_SECONDS", 0.02):   # "never finishes", scaled
            for tries in range(1, transcripts.FINAL_RETRY_MAX):
                self.supervise()
                self.assertIsNone(self.row("a1"))
                self.assertEqual(self.state()["J\ta1"]["tries"], tries)
            self.supervise()
        row = self.row("a1")   # no snapshot was ever stored: a bodiless marker
        self.assertEqual((row.final, row.raw_bytes, row.redactions, row.images), (True, 0, 0, ()))
        self.assertIn("ran out of time", row.failed)
        self.assertIn("3 tries", row.failed)
        self.assertIn(f"transcript file {transcripts.human_size(size)}", row.failed)
        with self.board() as b:
            self.assertIsNone(b.transcript_body("J", "a1"))   # no text, redacted or not
            since = b.now() - dt.timedelta(days=1)
            self.assertEqual(b.pending_final_transcripts(transcripts._host(), getpass.getuser(), "claude", since), [])
        self.supervise()
        self.assertNotIn("J\ta1", self.state())
        # surfaced: status --job per agent, and transcript show says why there is no body
        rc, out, _ = self.cli("status", "--job", "J", "--all-agents")
        self.assertEqual(rc, 0)
        self.assertIn("capture failed", out)
        self.assertIn("1 capture failed", out)
        rc, _, err = self.cli("transcript", "show", "--job", "J", "--key", "a1")
        self.assertEqual(rc, 1)
        self.assertIn("capture failed", err)
        self.assertNotIn(SECRET, err)
        # a later full capture (e.g. deactivate, no deadline) replaces the marker
        rc, _, err = self.cli("deactivate", "--job", "J", "--status", "completed", "--force")
        self.assertEqual(rc, 0, err)
        row = self.row("a1")
        self.assertEqual((row.final, row.failed), (True, None))

    def test_a_kill_switch_stops_the_retries(self):
        from swarm.supervisor import settings as st
        self.activate()
        self.fresh_stamp()
        self.start("a1")
        self.slow_agent("a1")
        self.stop("a1")
        st.off_file().parent.mkdir(parents=True, exist_ok=True)
        st.off_file().write_text("")
        with self.board() as b:
            self.assertEqual(lost.retry_slow_finals(b, self.cfg, time.monotonic() + 100), 0)
        self.assertIsNone(self.row("a1"))
        st.off_file().unlink()
        with self.board() as b:
            lost.retry_slow_finals(b, self.cfg, time.monotonic() + 100)
        self.assertTrue(self.row("a1").final)

    def test_the_retry_stays_inside_the_pass_budget(self):
        self.activate()
        self.fresh_stamp()
        self.start("a1")
        self.slow_agent("a1")
        self.stop("a1")
        with self.board() as b:   # less than FINAL_RETRY_SECONDS left: not even started
            self.assertEqual(lost.retry_slow_finals(b, self.cfg, time.monotonic() + 59), 0)
        self.assertIsNone(self.row("a1"))
        self.assertEqual(self.state()["J\ta1"].get("tries", 0), 0)

    def test_a_normally_stopped_agent_whose_stop_capture_failed_is_finalized_by_a_sweep(self):
        # not slow (a failure at a tiny budget, e.g. the hook's budget already spent): the next
        # sweep retries it with its own budget
        self.activate()
        self.fresh_stamp()
        self.start("a1")
        self.agent_file("a1")
        with mock.patch.object(transcripts, "SLOW_AFTER_SECONDS", 2.0):
            self.stop("a1", budget=-1)
        self.assertIsNone(self.row("a1"))
        self.start("a2")                                         # its start hook's sweep
        self.assertTrue(self.row("a1").final)


class CaptureJobIsolationTests(FinalRetryEnv):
    def test_a_slow_agent_does_not_cost_the_others_their_final(self):
        self.activate()
        self.fresh_stamp()
        for key in ("a1", "a2", "a3"):
            self.start(key)
        self.slow_agent("a1", kb=1500)
        self.agent_file("a2")
        self.agent_file("a3")
        with self.board() as b:
            b.close_job("J", "completed", None, forced=True)
            n = transcripts.capture_closed(b, self.cfg, ["J"], time.monotonic() + 0.3)
        self.assertGreaterEqual(n, 2)
        self.assertIsNone(self.row("a1"))
        self.assertTrue(self.row("a2").final)
        self.assertTrue(self.row("a3").final)
        self.assertTrue(self.state()["J\ta1"]["slow"])            # left to the supervisor pass
        self.supervise()
        self.assertTrue(self.row("a1").final)

    def test_one_agent_failing_otherwise_does_not_stop_the_rest(self):
        self.activate()
        self.fresh_stamp()
        for key in ("a1", "a2"):
            self.start(key)
            self.agent_file(key)
        real = transcripts.capture_subagent

        def flaky(board, cfg, job, key, *a, **kw):
            if key == "a1":
                raise RuntimeError("boom")
            return real(board, cfg, job, key, *a, **kw)
        warned: list = []
        with mock.patch.object(transcripts, "capture_subagent", flaky), self.board() as b:
            transcripts.capture_job(b, self.cfg, "J", True, time.monotonic() + 30, warn=warned.append)
        self.assertIsNone(self.row("a1"))
        self.assertTrue(self.row("a2").final)
        self.assertTrue(any("a1" in w and "RuntimeError" in w for w in warned), warned)

    def test_deactivate_reports_one_failing_agent_and_captures_the_rest(self):
        self.activate()
        self.fresh_stamp()
        for key in ("a1", "a2"):
            self.start(key)
            self.agent_file(key)
        real = transcripts.capture_subagent

        def flaky(board, cfg, job, key, *a, **kw):
            if key == "a1":
                raise transcripts.OutOfTime("transcript capture ran out of time")
            return real(board, cfg, job, key, *a, **kw)
        with mock.patch.object(transcripts, "capture_subagent", flaky):
            rc, _, err = self.cli("deactivate", "--job", "J", "--status", "completed", "--force")
        self.assertEqual(rc, 0, err)
        self.assertIn("OutOfTime", err)
        self.assertIsNone(self.row("a1"))
        self.assertTrue(self.row("a2").final)
        self.assertTrue(self.row("orchestrator").final)


class ReadersTests(FinalRetryEnv):
    """A capture-failed row is an audit marker: brief, memory refs and export see no body."""

    def failed(self):
        self.activate()
        self.fresh_stamp()
        self.start("a1")
        with self.board() as b:
            self.assertEqual(lost.mark_failed(b, "J", "a1", self.agent("a1").name, SESSION, "claude",
                                              "ran out of time, 3 tries of 60 s"), "stored")

    def snapshot_then_failed(self):
        """The probe_snap shape: a redacted non-final snapshot, then the final capture gives up."""
        self.activate()
        self.fresh_stamp()
        self.start("a1")
        p = self.agent_file("a1", "snapshot text visible")
        with self.board() as b:
            transcripts.capture_subagent(b, self.cfg, "J", "a1", p, False, "a1", SESSION, None,
                                         harness="claude", use_mtime=False)
            before = b.transcripts(job="J", agent_key="a1")[0]
            self.assertFalse(before.final)
            self.assertEqual(lost._give_up(b, self.cfg, "J", "a1", "a1", SESSION, "claude", p,
                                           "ran out of time", 3), "marked")
        return before

    def test_a_snapshot_is_kept_final_and_labelled(self):
        before = self.snapshot_then_failed()
        row = self.row("a1")
        self.assertEqual((row.final, row.sha256, row.raw_bytes), (True, before.sha256, before.raw_bytes))
        self.assertIn("ran out of time, 3 tries", row.failed)
        body = self.body("J", "a1")
        self.assertIn("snapshot text visible", body)
        self.assertNotIn("Zq8Zq8Zq8Zq8", body)
        rc, out, err = self.cli("transcript", "show", "--job", "J", "--key", "a1")
        self.assertEqual(rc, 0, err)
        self.assertIn("final capture failed (ran out of time", out.splitlines()[0])
        self.assertIn("last redacted snapshot from", out.splitlines()[0])
        self.assertIn("snapshot text visible", out)
        rc, out, err = self.cli("transcript", "show", "--job", "J", "--key", "a1", "--format", "jsonl")
        self.assertEqual(rc, 0, err)
        self.assertIn("last redacted snapshot", err)
        for ln in out.splitlines():
            json.loads(ln)                                     # still JSONL
        rc, out, _ = self.cli("status", "--job", "J", "--all-agents")
        self.assertIn("capture failed", out)
        with self.board() as b:   # the brief labels it instead of PARTIAL
            js = b.job_status("J")
            a = next(x for x in b.agents("J") if x.agent_key == "a1")
            label = command._ensure_final_transcript(b, self.cfg, js, a)
        self.assertIsInstance(label, str)
        self.assertIn("final capture failed (ran out of time", label)
        self.assertIn("last redacted snapshot from", label)

    def test_memory_show_labels_a_kept_snapshot(self):
        from swarm.board import MemoryRef
        self.snapshot_then_failed()
        with self.board() as b:
            b.save_memory_ref(MemoryRef(document_id="d1", bank="notes", job="J", agent_key="a1",
                                        agent_name=self.agent("a1").name, harness="claude", host="h",
                                        session_id=SESSION, tool_call_id="t1", writer="note-tool"))
        rc, out, err = self.cli("transcript", "show", "--memory", "d1")
        self.assertEqual(rc, 0, err)
        self.assertIn("final capture failed (ran out of time", out)
        self.assertIn("last redacted snapshot from", out)

    def test_brief_treats_it_as_nothing_stored(self):
        from swarm.supervisor import brief
        self.failed()
        with self.board() as b:
            self.assertIsNone(brief._stored_body(b, "J", "a1"))
            js = b.job_status("J")
            a = next(x for x in b.agents("J") if x.agent_key == "a1")
            self.assertFalse(command._ensure_final_transcript(b, self.cfg, js, a))   # final: no recapture

    def test_memory_show_says_the_capture_failed(self):
        from swarm.board import MemoryRef
        self.failed()
        with self.board() as b:
            b.save_memory_ref(MemoryRef(document_id="d1", bank="notes", job="J", agent_key="a1",
                                        agent_name=self.agent("a1").name, harness="claude", host="h",
                                        session_id=SESSION, tool_call_id="t1", writer="note-tool"))
        rc, out, err = self.cli("transcript", "show", "--memory", "d1")
        self.assertEqual(rc, 0, err)
        self.assertIn("full transcript: capture failed", out)
        rc, out, err = self.cli("memory", "refs", "--job", "J")
        self.assertEqual(rc, 0, err)

    def test_export_and_list(self):
        self.failed()
        rc, out, err = self.cli("transcript", "list", "--job", "J")
        self.assertEqual(rc, 0, err)
        self.assertIn("capture failed", out)
        target = self.tmp / "out"
        rc, _, err = self.cli("transcript", "export", "--job", "J", str(target))
        self.assertEqual(rc, 0, err)
        exported = [p for p in target.glob("*.jsonl") if "orchestrator" not in p.name]
        self.assertEqual([p.read_bytes() for p in exported], [b""])
        index = (target / "index.tsv").read_text().splitlines()
        cols = index[0].split("\t")
        [row] = [dict(zip(cols, ln.split("\t"))) for ln in index[1:] if "\ta1\t" in ln]
        self.assertEqual(row["final"], "capture failed")

    def test_doctor_counts_failed_captures(self):
        from swarm import bootstrap
        self.failed()
        checks = [c for c in bootstrap._capture_failed_check(self.cfg)]
        self.assertEqual(len(checks), 1)
        self.assertIsNone(checks[0].ok)
        self.assertIn("1 final transcript capture failed", checks[0].detail)


class CodexOrderingTests(FinalRetryEnv):
    """finalize_owned used to take todo[:20] in a fixed order: 20 agents whose capture always
    ran out of time were retried first on every sweep, and the 21st never got its final."""

    def test_codex_finals_no_longer_starve_behind_failing_ones(self):
        from swarm import enrolment, hosts
        from swarm.board.autoinit import store_key
        keys = [f"00000000-0000-4000-8000-{n:012x}" for n in range(0xb00, 0xb00 + 4)]
        with self.board() as b:
            b.ensure_job("CX")
            for key in keys:
                b.allocate_name(key, "CX")
                b.set_agent_runtime(key, "codex", None)
                b.agent_stopped(key)
                enrolment.write(store_key(self.cfg), job="CX", agent_key=key, harness="codex",
                                session_id=None, cwd=str(self.tmp))
        good, bad = keys[-1], set(keys[:-1])
        rollout = self.tmp / "rollout.jsonl"
        rollout.write_text(json.dumps({"timestamp": "2026-09-28T10:00:00Z", "type": "session_meta",
                                       "payload": {"id": good}}) + "\n"
                           + json.dumps({"timestamp": "2026-09-28T10:00:01Z", "type": "event_msg",
                                         "payload": {"type": "agent_message", "message": "codex done"}}) + "\n")
        real = transcripts.capture_subagent
        tried: list = []

        def capture(board, cfg, job, key, path, final, name=None, sid=None, deadline=None, **kw):
            tried.append(key)
            if key in bad:   # adversarial: eats the whole budget, then runs out of time
                time.sleep(max(0.0, (deadline or time.monotonic()) - time.monotonic()) + 0.01)
                raise transcripts.OutOfTime("transcript capture ran out of time")
            return real(board, cfg, job, key, path, final, name, sid, deadline, **kw)
        codex = hosts.get("codex")
        with mock.patch.object(transcripts, "capture_subagent", capture), \
                mock.patch.object(transcripts, "FINALIZE_PER_SWEEP", 3), \
                mock.patch.object(type(codex), "find_agent_transcript", lambda self, main, key: rollout):
            for _ in range(len(keys)):
                with self.board() as b:
                    transcripts.finalize_owned(b, self.cfg, time.monotonic() + 0.5)
                if self.row(good, job="CX") is not None:
                    break
        self.assertTrue(self.row(good, job="CX").final)
        self.assertEqual(sorted(k for k in tried if k in bad), sorted(bad))   # each once in the sweeps
        state = self.state()
        self.assertTrue(all(state[f"CX\t{k}"]["slow"] for k in bad))


class BudgetTests(FinalRetryEnv):
    def test_sweeps_are_never_unbounded(self):
        seen: list = []
        with mock.patch.object(lost, "finalize_pending_owned", lambda b, c, d=None, retry=False: seen.append(d) or 0), \
                mock.patch.object(transcripts, "finalize_owned", lambda b, c, d=None, retry=False: seen.append(d) or 0), \
                self.board() as b:
            before = time.monotonic()
            swarm.sweep_jobs(b, self.cfg)                        # a one-shot CLI command's sweep
        self.assertEqual(len(seen), 2)
        for d in seen:
            self.assertIsNotNone(d)
            self.assertLessEqual(d, before + transcripts.SWEEP_SECONDS + 1)

    def test_the_pass_bounds_its_sweep_and_gives_the_retries_what_is_left(self):
        calls: dict = {}
        real_sweep = swarm.sweep_jobs

        def sweep(b, cfg, deadline=None):
            calls["sweep"] = deadline - time.monotonic()
            return real_sweep(b, cfg, deadline)
        with mock.patch.object(swarm, "sweep_jobs", sweep), \
                mock.patch.object(lost, "retry_slow_finals",
                                  lambda b, c, d: calls.__setitem__("retry", d - time.monotonic()) or 0):
            self.supervise()
        self.assertLessEqual(calls["sweep"], command.PASS_SWEEP_SECONDS)
        self.assertLessEqual(calls["retry"], command.PASS_BUDGET_SECONDS)
        self.assertGreater(calls["retry"], transcripts.FINAL_RETRY_SECONDS)   # room for one retry
        self.assertLess(command.PASS_BUDGET_SECONDS, 110)                      # TimeoutStartSec

    def test_nothing_pending_creates_no_retry_state(self):
        with self.board() as b:
            swarm.sweep_jobs(b, self.cfg)
        self.assertFalse(lost.retry_state_path().exists())


class MixedVersionTests(FinalRetryEnv):
    def test_a_v8_clients_capture_over_a_marker_is_readable(self):
        """A pre-v9 client's upsert (no capture_failed column) over a marker leaves the column
        set; the body it stored is still read (only a bodiless marker hides its body)."""
        import hashlib
        import lzma
        import os
        import sqlite3
        if os.environ.get("SWARM_TEST_BACKEND", "memory") != "sqlite":
            self.skipTest("the v8 SQL is SQLite's")
        self.failed_marker()
        body = b'{"real": "v8 body"}\n'
        blob = lzma.compress(body)
        c = sqlite3.connect(os.path.expanduser(self.cfg["sqlite"]["path"]))
        try:   # the v8 client's statement, as it was
            c.execute(
                "INSERT INTO transcripts (job, agent_key, agent_name, role, host, session_id, captured_at, final, "
                "raw_bytes, stored_bytes, redactions, sha256, body, harness) VALUES ('J','a1','a1','subagent',NULL,"
                "NULL,'2026-09-28T00:00:00',1,?,?,0,?,?,'claude') ON CONFLICT (job, agent_key) DO UPDATE SET "
                "agent_name = excluded.agent_name, role = excluded.role, host = excluded.host, session_id = "
                "excluded.session_id, captured_at = excluded.captured_at, final = excluded.final, raw_bytes = "
                "excluded.raw_bytes, stored_bytes = excluded.stored_bytes, redactions = excluded.redactions, "
                "sha256 = excluded.sha256, body = excluded.body, harness = excluded.harness WHERE "
                "transcripts.sha256 <> excluded.sha256 OR (excluded.final AND NOT transcripts.final)",
                (len(body), len(blob), hashlib.sha256(body).hexdigest(), blob))
            c.commit()
        finally:
            c.close()
        self.assertEqual(self.body("J", "a1"), body.decode())
        self.assertIsNotNone(self.row("a1").failed)            # labelled, never hidden

    def failed_marker(self):
        self.activate()
        self.fresh_stamp()
        self.start("a1")
        with self.board() as b:
            lost.mark_failed(b, "J", "a1", "a1", SESSION, "claude", "ran out of time")


class LockTests(FinalRetryEnv):
    """Any process of this user (or a sandbox that can read the file) can flock the retry
    state's lock through a read-only descriptor: nothing may wait on it for long."""

    def hold_lock(self):
        """A same-user process holding the lock (through a write descriptor: the lock
        file is 0200, so a read-only one can't even be opened)."""
        import subprocess
        import sys
        with lost._Retries():
            pass                                               # creates the private dir and the lock
        lockp = lost.retry_state_path().parent / lost.RETRIES_LOCK
        child = subprocess.Popen([sys.executable, "-c",
                                  "import os,sys,time; sys.path.insert(0, sys.argv[2]); from swarm import compat; "
                                  "fd=os.open(sys.argv[1], os.O_WRONLY); "
                                  "compat.flock(fd, compat.LOCK_EX); print('held', flush=True); time.sleep(60)",
                                  str(lockp), str(LIB)], stdout=subprocess.PIPE, text=True)
        self.addCleanup(child.stdout.close)
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        self.assertEqual(child.stdout.readline().strip(), "held")
        lost._contended[0] = float("-inf")
        self.addCleanup(lost._contended.__setitem__, 0, float("-inf"))

    def test_a_held_lock_blocks_no_sweep_hook_or_pass(self):
        self.activate()
        self.fresh_stamp()
        self.start("a1")
        self.start("a2")
        self.agent_file("a2")
        self.slow_agent("a1")
        self.stop("a1")                                        # pending, slow (state written)
        self.hold_lock()
        limit = lost.RETRIES_LOCK_SECONDS * 3 + 2
        t = time.monotonic()
        with self.board() as b:
            swarm.sweep_jobs(b, self.cfg, time.monotonic() + 1.0)
        self.assertLess(time.monotonic() - t, limit)
        t = time.monotonic()
        self.hook("stop", agent_id="a2", session=SESSION, transcript_path=str(self.main))
        self.assertLess(time.monotonic() - t, limit)
        self.assertTrue(self.row("a2").final)                 # captured all the same
        t = time.monotonic()
        self.supervise()
        self.assertLess(time.monotonic() - t, limit + 5)
        self.assertIn("held by another process", (lost.retry_state_path().parent / "supervise.log").read_text())
        from swarm import bootstrap
        [check] = bootstrap._capture_failed_check(self.cfg)
        self.assertIsNone(check.ok)
        self.assertIn("lock", check.detail)


class OrderingTests(FinalRetryEnv):
    def test_the_pass_takes_slow_finals_of_both_harnesses_oldest_first(self):
        """One list across harnesses: an older Codex slow entry goes before a Claude one."""
        import os
        from swarm import enrolment, hosts
        from swarm.board.autoinit import store_key
        self.activate()
        self.fresh_stamp()
        self.start("a1")
        self.agent_file("a1")
        cx = "00000000-0000-4000-8000-000000000c01"
        with self.board() as b:
            b.allocate_name(cx, "J")
            b.set_agent_runtime(cx, "codex", None)
            b.agent_stopped(cx)
            b.agent_stopped("a1")
        enrolment.write(store_key(self.cfg), job="J", agent_key=cx, harness="codex", session_id=None,
                        cwd=str(self.tmp))
        state = {"J\ta1": {"harness": "claude", "slow": True, "at": time.time() - 10},
                 f"J\t{cx}": {"harness": "codex", "slow": True, "at": time.time() - 100}}
        with lost._Retries() as st_:
            st_.update(state)
        order: list = []

        def capture(board, cfg, job, key, *a, **kw):
            order.append(key)
            return False
        rollout = self.tmp / "r.jsonl"
        rollout.write_text("{}\n")
        codex = hosts.get("codex")
        with mock.patch.object(transcripts, "capture_subagent", capture), \
                mock.patch.object(type(codex), "find_agent_transcript", lambda self, main, key: rollout), \
                self.board() as b:
            lost.retry_slow_finals(b, self.cfg, time.monotonic() + 100)
        self.assertEqual(order[:2], [cx, "a1"])
        self.assertTrue(os.path.exists(lost.retry_state_path()))


class TooLargeTests(FinalRetryEnv):
    def test_over_the_raw_cap_a_final_is_marked_at_once_and_never_read(self):
        self.activate()
        self.fresh_stamp()
        self.start("a1")
        p = self.agent_file("a1")
        forbid = mock.Mock(side_effect=AssertionError("read"))
        with mock.patch.object(transcripts, "RAW_READ_MAX", 10), mock.patch.object(transcripts, "_read", forbid):
            with self.board() as b:
                self.assertFalse(transcripts.capture_subagent(b, self.cfg, "J", "a1", p, False, "a1"))
            self.assertIsNone(self.row("a1"))                   # a snapshot is just skipped
            self.stop("a1", budget=4.0)
        row = self.row("a1")
        self.assertEqual((row.final, row.raw_bytes), (True, 0))
        self.assertIn("too large", row.failed)
        self.assertIn(transcripts.human_size(p.stat().st_size), row.failed)

    def test_past_the_deadline_a_file_is_not_read(self):
        self.activate()
        self.fresh_stamp()
        self.start("a1")
        p = self.agent_file("a1")
        forbid = mock.Mock(side_effect=AssertionError("read"))
        with mock.patch.object(transcripts, "_read", forbid), self.board() as b:
            with self.assertRaises(transcripts.OutOfTime):
                transcripts.capture_subagent(b, self.cfg, "J", "a1", p, True, "a1", None, time.monotonic() - 1)


class SkipAndDoctorTests(FinalRetryEnv):
    def test_a_skip_is_looked_at_again(self):
        self.activate()
        self.fresh_stamp()
        self.start("a1")
        self.agent_file("a1")
        with self.board() as b:
            b.agent_stopped("a1")
        with lost._Retries() as st_:
            st_["J\ta1"] = {"harness": "claude", "skip": True, "at": time.time() - 60}
        with self.board() as b:
            lost.finalize_pending_owned(b, self.cfg, time.monotonic() + 30)
        self.assertIsNone(self.row("a1"))                       # skipped recently: left alone
        with lost._Retries() as st_:
            st_["J\ta1"]["at"] = time.time() - lost.SKIP_RECHECK_SECONDS - 1
        with self.board() as b:
            lost.finalize_pending_owned(b, self.cfg, time.monotonic() + 30)
        self.assertTrue(self.row("a1").final)                   # ours after all: finalized

    def test_doctor_counts_overdue_pending_finals_from_the_board(self):
        from swarm import bootstrap
        self.activate()
        self.fresh_stamp()
        self.start("a1")
        with self.board() as b:
            b.agent_stopped("a1")                               # no transcript file: stays pending
        [check] = bootstrap._capture_failed_check(self.cfg)
        self.assertTrue(check.ok)                               # just ended: not overdue yet
        self.h.backdate_agent("a1", left_at=2 * 3600)
        [check] = bootstrap._capture_failed_check(self.cfg)
        self.assertIsNone(check.ok)
        self.assertIn("1 ended agent of this user without a final transcript", check.detail)


class PassClockTests(FinalRetryEnv):
    def test_the_pass_budget_counts_from_before_the_board_is_opened(self):
        """Time spent opening the board comes out of the pass's budget."""
        seen: dict = {}
        real_open = command._open

        def slow_open(cfg, dry_run):
            time.sleep(0.5)
            return real_open(cfg, dry_run)
        t0 = time.monotonic()
        with mock.patch.object(command, "_open", slow_open), \
                mock.patch.object(lost, "retry_slow_finals", lambda b, c, d: seen.setdefault("d", d) and 0):
            self.supervise()
        self.assertLessEqual(seen["d"], t0 + command.PASS_BUDGET_SECONDS + 0.1)
        self.assertLess(seen["d"] - t0, command.PASS_BUDGET_SECONDS + 0.1)


class LockModeTests(FinalRetryEnv):
    """N1 (a): the lock files are 0200 and locked through a write descriptor, so a process that
    can only read them (a sandbox) can't hold them; an existing looser lock file is tightened."""

    @posix_only("needs POSIX file modes (Windows has ACLs)")
    def test_retry_and_pass_locks_are_write_only(self):
        import os
        import stat as st_
        from swarm.supervisor import settings as st
        with lost._Retries():
            pass
        d = lost.retry_state_path().parent
        (d / "supervise.lock").write_text("")
        os.chmod(d / "supervise.lock", 0o600)                  # as an earlier version left it
        os.chmod(d / lost.RETRIES_LOCK, 0o600)
        with lost._Retries():
            pass
        fd = command._take_pass_lock()
        self.assertIsNotNone(fd)
        os.close(fd)
        for name in (lost.RETRIES_LOCK, "supervise.lock"):
            self.assertEqual(st_.S_IMODE(os.stat(d / name).st_mode), 0o200, name)
            with self.assertRaises(PermissionError):
                os.open(d / name, os.O_RDONLY)
        self.assertEqual(command.lock_path().parent, st.private_dir())


class LockHeldForeverTests(FinalRetryEnv):
    """N1 (b): with the lock held for good, the state is still read (sweeps skip slow entries)
    and the pass writes it back, so the final still ends as capture failed."""

    hold_lock = LockTests.hold_lock

    def test_the_pass_reaches_capture_failed_with_the_lock_held(self):
        self.activate()
        self.fresh_stamp()
        self.start("a1")
        self.slow_agent("a1")
        self.hold_lock()
        self.stop("a1")                                        # the hook can't record it
        with mock.patch.object(transcripts, "FINAL_RETRY_SECONDS", 0.02), \
                mock.patch.object(command, "PASS_SWEEP_SECONDS", 0.02):
            for _ in range(transcripts.FINAL_RETRY_MAX + 1):
                lost._contended[0] = float("-inf")
                self.supervise()
                if self.row("a1") is not None:
                    break
        row = self.row("a1")
        self.assertIsNotNone(row)
        self.assertTrue(row.final)
        self.assertIn("ran out of time", row.failed)
        self.assertIsNone(self.body("J", "a1"))

    def test_sweeps_still_read_the_state_and_skip_slow_entries(self):
        self.activate()
        self.fresh_stamp()
        self.start("a1")
        self.slow_agent("a1")
        self.stop("a1")                                        # recorded slow (no lock held yet)
        self.hold_lock()
        tried: list = []
        with mock.patch.object(transcripts, "capture_subagent", lambda *a, **k: tried.append(a[3]) or False), \
                self.board() as b:
            swarm.sweep_jobs(b, self.cfg, time.monotonic() + 5)
        self.assertNotIn("a1", tried)


class ZstdBombTests(FinalRetryEnv):
    """N2: a small .zst that expands past RAW_READ_MAX is refused, in bounded time and memory,
    by the zstandard package and by the zstd tool fallback; a final capture marks it too large."""

    def bomb(self) -> "Path":
        import zstandard
        meta = json.dumps({"type": "session_meta", "payload": {"id": "x"}}) + "\n"
        one = (json.dumps({"type": "response_item", "payload": {"text": "A" * 4000}}) + "\n").encode()
        p = self.tmp / "rollout-bomb.jsonl.zst"
        with open(p, "wb") as f, zstandard.ZstdCompressor(level=19).stream_writer(f) as w:
            w.write(meta.encode())
            for _ in range(8 * 1024 * 1024 // len(one)):       # 8 MB of text
                w.write(one)
        return p

    def test_zstandard_bomb_is_refused(self):
        from swarm.hosts import codex
        p = self.bomb()
        self.assertLess(p.stat().st_size, 100_000)
        with mock.patch.object(transcripts, "RAW_READ_MAX", 1024 * 1024):
            with self.assertRaises(codex.RolloutTooLarge):
                codex.read_rollout(p)
        self.assertIn("x", codex.read_rollout(p)[:200])        # under the real cap: read

    def test_zstd_tool_fallback_is_capped_and_timed(self):
        import os
        import sys
        from swarm.hosts import codex
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        fake = bin_dir / "zstd"
        fake.write_text("#!/bin/sh\nexec yes AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA\n")   # never ends
        fake.chmod(0o755)
        p = self.tmp / "r.jsonl.zst"
        p.write_bytes(b"not really zstd")
        t = time.monotonic()
        with mock.patch.dict(sys.modules, {"zstandard": None}), \
                mock.patch.dict(os.environ, {"PATH": f"{bin_dir}:{os.environ['PATH']}"}), \
                mock.patch.object(transcripts, "RAW_READ_MAX", 1024 * 1024):
            with self.assertRaises(codex.RolloutTooLarge):
                codex.read_rollout(p)
            fake.write_text("#!/bin/sh\nsleep 30\n")         # never answers: the deadline stops it
            with self.assertRaises((transcripts.OutOfTime, codex.RolloutUnreadable)):
                codex.read_rollout(p, deadline=time.monotonic() + 0.5)
        self.assertLess(time.monotonic() - t, 10)

    def test_a_codex_final_over_the_cap_is_marked_too_large(self):
        from swarm import enrolment, hosts
        from swarm.board.autoinit import store_key
        p = self.bomb()
        key = "00000000-0000-4000-8000-000000000d01"
        with self.board() as b:
            b.ensure_job("CX")
            b.allocate_name(key, "CX")
            b.set_agent_runtime(key, "codex", None)
            b.agent_stopped(key)
        enrolment.write(store_key(self.cfg), job="CX", agent_key=key, harness="codex", session_id=None,
                        cwd=str(self.tmp))
        codex = hosts.get("codex")
        with mock.patch.object(transcripts, "RAW_READ_MAX", 1024 * 1024), \
                mock.patch.object(type(codex), "find_agent_transcript", lambda self, main, k: p), \
                self.board() as b:
            transcripts.finalize_owned(b, self.cfg, time.monotonic() + 30)
        row = self.row(key, job="CX")
        self.assertEqual((row.final, row.raw_bytes), (True, 0))
        self.assertIn("too large", row.failed)


class OrchestratorSliceTests(FinalRetryEnv):
    def test_a_claude_slice_past_the_cap_is_too_large(self):
        import datetime as _d
        p = self.tmp / "session.jsonl"
        p.write_text("".join(line(f"turn {i} " + "x" * 200) for i in range(2000)))
        start = _d.datetime.now(_d.timezone.utc) - _d.timedelta(hours=1)
        with mock.patch.object(transcripts, "TRANSCRIPT_MAX_RAW", 50_000):
            with self.assertRaises(transcripts.TooLarge):
                transcripts.read_slice(p, start, None, time.monotonic() + 30)
        self.assertIn("turn 1999", transcripts.read_slice(p, start, None, time.monotonic() + 30))

    def test_one_huge_line_is_not_read_whole(self):
        import datetime as _d
        p = self.tmp / "session.jsonl"
        p.write_text(line("start") + "x" * 300_000 + "\n")
        start = _d.datetime.now(_d.timezone.utc) - _d.timedelta(hours=1)
        with mock.patch.object(transcripts, "TRANSCRIPT_MAX_RAW", 50_000):
            with self.assertRaises(transcripts.TooLarge):
                transcripts.read_slice(p, start, None, time.monotonic() + 30)
