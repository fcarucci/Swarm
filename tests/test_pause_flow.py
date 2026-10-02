"""swarm.pause: pausing a job and resuming it (here, with the host launch faked): the manifest, the
final-transcript report, names and cursors kept, the restart rows and markers that let the hooks
enrol the new sessions, dry run, retry, the briefing fallback, and the hooks' pause behaviour."""
from __future__ import annotations

import copy
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from support import SMALL_POOL, MemoryHarness, SqliteHarness  # noqa: F401  (sets sys.path)

from swarm import pause, transcripts as T  # noqa: E402
from swarm.board import PAUSE_WRITER, JobPaused  # noqa: E402
from swarm.supervisor import markers  # noqa: E402

SECRET = "sk-ant-api03-AbCdEfGhIjKlMnOpQrStUv_wx-yz0123"
SID = "11111111-2222-3333-4444-555555555555"


def transcript(text="work", sid=SID):
    base = {"sessionId": sid, "cwd": "/old/box/work", "isSidechain": False, "userType": "external"}
    rows = [{**base, "type": "user", "uuid": "u1", "parentUuid": None,
             "message": {"role": "user", "content": f"{text} key {SECRET}"}},
            {**base, "type": "assistant", "uuid": "a1", "parentUuid": "u1",
             "message": {"role": "assistant", "content": [{"type": "text", "text": "on it"}]}}]
    return "\n".join(json.dumps(r) for r in rows) + "\n"


class Base(unittest.TestCase):
    harness = staticmethod(lambda: MemoryHarness("pause-flow"))

    def setUp(self):
        self.h = self.harness()
        self.h.reset()
        self.tmp = Path(tempfile.mkdtemp(prefix="swarm-pf-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.cfg = copy.deepcopy(self.h.cfg)
        self.cfg["hook"]["marker_dir"] = str(self.tmp / "markers")
        self.work = self.tmp / "work"
        self.work.mkdir()
        self.root = self.tmp / "claude-home"
        self.b = self.h.board()
        self.addCleanup(self.b.close)
        self.b.open_job("j", "desc", "build the thing", None, "tester", goal="done")
        self.n1 = self.b.allocate_name("k1", "j", "engineer")
        self.n2 = self.b.allocate_name("k2", "j", "qa")
        for k, model in (("k1", "sonnet"), ("k2", "haiku")):
            self.b.set_agent_runtime(k, "claude", model)
        self.b.post("j", self.n1, "hello team")
        self.b.read_new(agent_key="k2")
        self.launched = []

    def store(self, key, name, final=True, harness="claude"):
        self.b.save_transcript(T.make_row("j", key, name, "subagent", transcript(name), final=final, harness=harness))

    def fake_start(self, restored, env):
        self.launched.append((restored, env))

    def resume(self, **kw):
        kw.setdefault("workdir", str(self.work))
        kw.setdefault("dest_root", self.root)
        kw.setdefault("label", "box-b")
        kw.setdefault("start", self.fake_start)
        kw.setdefault("enrol_wait", 0)
        return pause.resume(self.b, self.cfg, "j", **kw)


class PauseTest(Base):
    def test_pause_records_agents_posts_notice_and_reports_transcripts(self):
        self.store("k1", self.n1)
        rep = pause.pause(self.b, self.cfg, "j", "moving box", "francesco", wait=0)
        self.assertEqual(self.b.job_status("j").status, "paused")
        self.assertEqual({e["agent_name"] for e in rep.record.manifest["agents"]}, {self.n1, self.n2})
        self.assertEqual(rep.transcripts, {"k1": "final", "k2": "missing"})
        last = self.b.recent_messages(5, "j")[-1]
        self.assertEqual(last.agent_name, PAUSE_WRITER)
        self.assertIn("paused by francesco: moving box", last.message)
        text = "\n".join(rep.lines())
        self.assertIn(self.n1, text)
        self.assertIn("no final transcript yet for " + self.n2, text)
        self.assertNotIn(SECRET, text)

    def test_pause_is_idempotent_and_refuses_unknown_or_closed_jobs(self):
        first = pause.pause(self.b, self.cfg, "j", "r", "u", wait=0)
        again = pause.pause(self.b, self.cfg, "j", "other", "v", wait=0)
        self.assertTrue(again.already)
        self.assertEqual(again.record.id, first.record.id)
        self.assertEqual(len(self.b.recent_messages(10, "j")), 2)    # one notice only
        self.assertIsNone(pause.pause(self.b, self.cfg, "nope", None, "u", wait=0))
        self.b.close_job("j", "cancelled", None)
        self.assertIsNone(pause.pause(self.b, self.cfg, "j", None, "u", wait=0))

    def test_pause_waits_for_other_boxes_final_transcripts(self):
        clock = [0.0]
        calls = []

        def sleep(s):
            clock[0] += s
            calls.append(s)
            if len(calls) == 2:   # another box's hooks store theirs
                self.store("k1", self.n1)
                self.store("k2", self.n2)
        rep = pause.pause(self.b, self.cfg, "j", None, "u", wait=10, sleep=sleep, clock=lambda: clock[0])
        self.assertEqual(rep.transcripts, {"k1": "final", "k2": "final"})
        self.assertEqual(len(calls), 2)

    def test_join_and_post_are_refused_with_a_clear_message(self):
        pause.pause(self.b, self.cfg, "j", "lunch", "u", wait=0)
        with self.assertRaisesRegex(JobPaused, r"job j is paused.*lunch.*swarm resume --job j"):
            self.b.allocate_name("new", "j")
        with self.assertRaises(JobPaused):
            self.b.post("j", self.n1, "x")

    def test_status_listing_shows_paused_jobs(self):
        from swarm import cli
        pause.pause(self.b, self.cfg, "j", None, "u", wait=0)
        self.assertEqual([j.job for j in cli.open_and_paused(self.b, False)], ["j"])
        self.assertEqual(self.b.jobs(False), [])      # the sweeps and the supervisor never see it
        self.assertIn("paused", cli.jobs_overview(self.b, False, False))


class ResumeTest(Base):
    def paused(self, store=True):
        if store:
            self.store("k1", self.n1)
            self.store("k2", self.n2)
        return pause.pause(self.b, self.cfg, "j", "move", "u", wait=0)

    def test_resume_keeps_names_cursors_and_binds_the_new_sessions(self):
        pz = self.paused()
        cursors = {e["agent_key"]: e["cursor"] for e in pz.record.manifest["agents"]}
        rep = self.resume()
        self.assertFalse(rep.failed)
        self.assertEqual({r.name: (r.status, r.mode) for r in rep.results},
                         {self.n1: ("launched", "resume"), self.n2: ("launched", "resume")})
        self.assertEqual(self.b.job_status("j").status, "active")
        active = {a.name: a for a in self.b.agents("j", include_departed=False)}
        self.assertEqual(set(active), {self.n1, self.n2})
        for key in ("k1", "k2"):
            new = next(a for a in active.values() if a.resume_of == key)
            self.assertEqual(self.b.sync_state(new.agent_key).__class__.__name__, "SyncState")
        self.assertEqual(len(self.launched), 2)
        for restored, env in self.launched:
            self.assertEqual(restored.mode, "resume")
            self.assertEqual(restored.cwd, str(self.work.resolve()))
            self.assertIn("box-b", restored.stdin)
            self.assertNotIn(SECRET, restored.stdin)
            self.assertTrue((self.root).exists())
        # restart rows and markers: what hooks._enrol_resumed checks
        rows = self.b.restarts(job="j")
        self.assertEqual({r.old_agent_key for r in rows}, {"k1", "k2"})
        for r in rows:
            self.assertTrue(r.reason.startswith(pause.PAUSE_RESUME_REASON))
            self.assertIsNone(r.ended_at)
            marker = json.loads(markers.resume_marker_path(self.cfg, "j", r.id).read_text())
            self.assertEqual(marker["resume"]["resume_of"], r.old_agent_key)
            self.assertEqual(marker["resume"]["restart_id"], r.id)
        done = self.b.pauses("j")[0]
        self.assertEqual(done.resumed_host, "box-b")
        self.assertEqual({v["status"] for v in done.outcome.values()}, {"launched"})
        notices = [m.message for m in self.b.recent_messages(10, "j") if m.agent_name == PAUSE_WRITER]
        self.assertTrue(any("resumed by" in m and "box-b" in m for m in notices))
        # the cursor is the paused one: the new agent sees what its predecessor hadn't read
        read = self.b.read_new(agent_key=next(a.agent_key for a in active.values() if a.resume_of == "k2"))
        self.assertNotIn("hello team", [m.message for m in read])    # read before the pause: not shown again
        self.assertEqual(cursors["k2"], max(m.id for m in self.b.recent_messages(10, "j") if m.agent_name == self.n1))

    def test_a_resumed_hook_enrolment_accepts_the_restart_without_the_supervisor_switch(self):
        from swarm import hooks
        self.paused()
        self.resume()
        row = next(r for r in self.b.restarts(job="j") if r.old_agent_key == "k1")
        sid = next(a.agent_key for a in self.b.agents("j", include_departed=False) if a.resume_of == "k1")
        marker = markers.resume_binding(self.cfg, sid)
        self.assertEqual(marker["resume"]["restart_id"], row.id)
        self.assertIsNone(hooks._resume_problem(self.b, sid, marker))
        self.assertFalse(self.cfg["supervise"].get("enabled", False))   # supervisor off: still allowed

    def test_dry_run_changes_nothing(self):
        self.paused()
        rep = self.resume(dry_run=True)
        self.assertEqual({r.status for r in rep.results}, {"planned"})
        self.assertEqual(self.b.job_status("j").status, "paused")
        self.assertEqual(self.b.restarts(job="j"), [])
        self.assertEqual(self.launched, [])
        self.assertFalse(self.root.exists())

    def test_only_resumes_the_named_agents(self):
        self.paused()
        rep = self.resume(only=[self.n2.lower()])
        self.assertEqual([r.name for r in rep.results], [self.n2])
        self.assertEqual(len(self.launched), 1)

    def test_missing_transcript_falls_back_to_a_briefing(self):
        self.paused(store=False)
        rep = self.resume()
        self.assertEqual({r.mode for r in rep.results}, {"briefing"})
        self.assertFalse(rep.failed)
        self.assertIn("could not be restored", self.launched[0][0].stdin)

    def test_another_host_resumes_from_a_briefing(self):
        self.paused()
        rep = self.resume(host="codex")
        self.assertEqual({(r.harness, r.mode) for r in rep.results}, {("codex", "briefing")})

    def test_orchestrator_entries_are_hinted_not_launched(self):
        self.store("k1", self.n1)
        self.b.save_transcript(T.make_row("j", "k2", self.n2, "orchestrator", transcript("orch"), harness="claude"))
        self.b.record_route("k2", SID, "final", "j")
        self.paused(store=False)
        rep = self.resume()
        self.assertEqual({r.name: r.status for r in rep.results}, {self.n1: "launched", self.n2: "skipped"})
        self.assertTrue(any(f"claude --resume {SID}" in h for h in rep.hints))
        self.assertEqual(len(self.launched), 1)

    def test_second_resume_loses_and_a_resumed_job_has_nothing_to_resume(self):
        self.paused()
        self.resume()
        self.assertIsNone(self.resume())
        self.assertIsNone(pause.find_pause(self.b, "j", False)[0])

    def test_failed_launch_is_reported_and_retried(self):
        self.paused()
        boom = {"left": 1}

        def start(restored, env):
            if restored.stdin.count(self.n2) and boom["left"]:
                boom["left"] -= 1
                raise OSError("claude not found")
            self.launched.append((restored, env))
        rep = self.resume(start=start)
        self.assertTrue(rep.failed)
        st = {r.name: r.status for r in rep.results}
        self.assertEqual(st, {self.n1: "launched", self.n2: "failed"})
        self.assertEqual(self.b.job_status("j").status, "active")
        self.assertEqual({a.name for a in self.b.agents("j", include_departed=False)}, {self.n1})   # seat freed
        rep2 = self.resume(start=start, retry=True)
        self.assertEqual([(r.name, r.status) for r in rep2.results], [(self.n2, "launched")])
        self.assertEqual({a.name for a in self.b.agents("j", include_departed=False)}, {self.n1, self.n2})
        out = self.b.pauses("j")[0].outcome
        self.assertEqual({v["status"] for v in out.values()}, {"launched"})
        self.assertIsNone(self.resume(start=start, retry=True))   # nothing failed any more

    def test_agent_seen_on_the_board_closes_its_restart_row(self):
        self.paused()
        clock = [0.0]

        def sleep(s):
            clock[0] += s
            for a in self.b.agents("j", include_departed=False):
                self.b.tool_started(a.agent_key, "Bash")
        rep = self.resume(enrol_wait=30, sleep=sleep, clock=lambda: clock[0])
        self.assertTrue(all("back on the board" in (r.note or "") for r in rep.results))
        self.assertEqual({r.outcome for r in self.b.restarts(job="j")}, {"completed"})

    def test_manifest_and_report_never_hold_the_secret(self):
        pz = self.paused()
        rep = self.resume()
        blob = json.dumps(pz.record.manifest) + "\n".join(rep.lines()) + json.dumps(self.b.pauses("j")[0].outcome)
        self.assertNotIn(SECRET, blob)


class SqliteResumeTest(ResumeTest):
    harness = staticmethod(lambda: SqliteHarness())


class HookTest(Base):
    def test_agent_of_a_paused_job_is_told_to_stop(self):
        from swarm import hooks
        pause.pause(self.b, self.cfg, "j", "lunch", "francesco", wait=0)
        self.b.record_route("k1", "sess", "final", "j")
        text = hooks._paused_stop(self.b, "k1")
        self.assertIn("paused by francesco (lunch)", text)
        self.assertIn("Stop now", text)
        self.assertIsNone(hooks._paused_stop(self.b, "unknown"))
        rec = self.b.open_pause("j")
        self.b.begin_resume("j", rec.id, "u", "h")
        self.assertIsNone(hooks._paused_stop(self.b, "k1"))


if __name__ == "__main__":
    unittest.main()
