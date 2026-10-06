"""The sweep closes stuck agents (owner-only, kill switches) and stores their final transcript."""
from __future__ import annotations

import datetime as dt
import getpass
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from support import home_env, posix_only  # noqa: E402
from test_hooks_cli import Env  # the private installation used by the hook/CLI tests

from swarm import cli as swarm, transcripts
from swarm.board import AgentStatus, JobStatus
from swarm.supervisor import lost, settings as st, stuck

UTC = dt.timezone.utc
SID = "0b9f6c1e-2d3a-4e5f-8a7b-9c0d1e2f3a4b"          # the orchestrator's session (a UUID)
OTHER_SID = "5e6f7a8b-9c0d-4e1f-8a2b-3c4d5e6f7a8b"    # an attached session


def tline(text: str, kind: str = "assistant") -> str:
    stamp = dt.datetime.now(UTC).isoformat().replace("+00:00", "Z")
    return json.dumps({"type": kind, "timestamp": stamp,
                       "message": {"role": kind, "content": [{"type": "text", "text": text}]}}) + "\n"


class CloseStuckTests(Env):
    def setUp(self):
        super().setUp()
        self.config.write_text(self.config.read_text() + '\n[supervise]\nenabled = true\n')
        self.cfg = swarm.load_config(self.config)
        rc, _, _ = self.cli("activate", "--job", "J", "--session", SID)
        self.assertEqual(rc, 0)
        self.hook("start", agent_id="a1", session=SID)
        self.name = self.agent("a1").name

    def _age(self, key="a1", minutes=31, tool=None):
        self.h.backdate_agent(key, last_seen=minutes * 60)
        if tool:
            self.h.update_agent(key, current_tool=tool)
            self.h.backdate_agent(key, tool_started_at=minutes * 60)

    def sweep(self):
        with self.board() as b:
            return stuck.close_stuck_owned(b, self.cfg)

    def test_dead_agent_closed_with_reason_post_and_wait(self):
        self._age()
        closed = self.sweep()
        self.assertEqual([(c.name, c.reason) for c in closed], [(self.name, "dead")])
        a = self.agent("a1")
        self.assertEqual((a.status, a.left_reason), ("left", "stuck:dead"))
        with self.board() as b:
            posts = [m.message for m in b.recent_messages(10, job="J")]
            js = b.job_status("J")
        self.assertTrue(any(p.startswith(f"closed {self.name}: stuck (dead)") for p in posts), posts)
        self.assertEqual(js.waiting_on, stuck.WAITING_PREFIX + self.name)
        self.assertIn("stuck:dead", st.log_path().read_text())

    def test_live_agent_untouched(self):
        self.assertEqual(self.sweep(), [])
        self.assertIsNone(self.agent("a1").ended_at)

    def test_disabled_or_off_file_or_no_supervise_closes_nothing(self):
        self._age()
        with mock.patch.dict(self.cfg, {"supervise": {"enabled": False}}):
            self.assertEqual(self.sweep(), [])
        st.off_file().parent.mkdir(parents=True, exist_ok=True)
        st.off_file().write_text("")
        self.assertEqual(self.sweep(), [])
        st.off_file().unlink()
        with self.board() as b:
            b.set_job_supervise("J", False)
        self.assertEqual(self.sweep(), [])
        self.assertIsNone(self.agent("a1").ended_at)

    def test_other_owner_is_left_alone(self):
        # "ours" is this host user's local enrolment record, never the row's os_user/host
        from swarm import enrolment
        from swarm.board.autoinit import store_key
        self._age()
        enrolment.remove(store_key(self.cfg), "a1")           # enrolled elsewhere: no record here
        self.h.update_agent("a1", os_user=getpass.getuser(), host=transcripts._host())  # the row claims us
        self.assertEqual(self.sweep(), [])
        self.assertIsNone(self.agent("a1").ended_at)

    def test_a_forged_row_owner_does_not_disown_an_enrolled_agent(self):
        self._age()
        self.h.update_agent("a1", os_user="someone-else", host="other-machine")
        self.assertEqual([c.reason for c in self.sweep()], ["dead"])

    def test_contact_during_the_sweep_keeps_it(self):
        self._age()
        real = stuck.stuck_reason

        def touch_then_decide(a, *args, **kw):
            with self.board() as b:
                b.tool_finished("a1")          # a hook contact after the agents() read
            return real(a, *args, **kw)
        with mock.patch.object(stuck, "stuck_reason", touch_then_decide):
            self.assertEqual(self.sweep(), [])
        self.assertIsNone(self.agent("a1").ended_at)

    def test_orphaned_when_orchestrator_seen_is_stale(self):
        self._age()
        marker = self.markers / "J.json"
        os.utime(marker, (0, 0))                     # activated long ago, never seen
        closed = self.sweep()
        self.assertEqual([c.reason for c in closed], ["orphaned"])
        self.assertEqual(self.agent("a1").left_reason, "stuck:orphaned")

    def test_tool_stuck_agent_with_stale_seen_is_not_orphaned(self):
        """The e2e shape: the orchestrator waits in one foreground Agent call (no heartbeat)
        while its only subagent hangs in a tool call. A process in a tool call is alive."""
        self._age(minutes=61, tool="Bash")
        os.utime(self.markers / "J.json", (0, 0))
        closed = self.sweep()
        self.assertEqual([c.reason for c in closed], ["tool"])
        self.assertEqual(self.agent("a1").left_reason, "stuck:tool")

    def test_one_dead_one_tool_stale_seen_keeps_each_reason(self):
        self.hook("start", agent_id="a2", session=SID)
        self._age("a1")
        self._age("a2", minutes=61, tool="Bash")
        os.utime(self.markers / "J.json", (0, 0))
        self.assertEqual(sorted(c.reason for c in self.sweep()), ["dead", "tool"])
        self.assertEqual(self.agent("a1").left_reason, "stuck:dead")
        self.assertEqual(self.agent("a2").left_reason, "stuck:tool")

    def test_all_dead_and_stale_seen_is_orphaned(self):
        self.hook("start", agent_id="a2", session=SID)
        self._age("a1")
        self._age("a2")
        os.utime(self.markers / "J.json", (0, 0))
        self.assertEqual([c.reason for c in self.sweep()], ["orphaned", "orphaned"])
        self.assertEqual(self.agent("a2").left_reason, "stuck:orphaned")

    def _archive_on(self):
        """[transcripts] on, a Claude projects dir, the orchestrator's transcript (SID)."""
        self.config.write_text(self.config.read_text() + '\n[transcripts]\nenabled = true\n')
        self.cfg = swarm.load_config(self.config)
        claude = self.tmp / "claude"
        p = mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(claude)})
        p.start()
        self.addCleanup(p.stop)
        self.main = claude / "projects" / "-proj" / f"{SID}.jsonl"
        self.main.parent.mkdir(parents=True)
        self.main.write_text(tline("orchestrating", "user"))

    def _agent_file(self, key="a1"):
        f = self.main.with_suffix("") / "subagents" / f"agent-{key}.jsonl"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(tline("[swarm job: J]\nwork", "user") + tline("working on it, then silence"))
        return f

    def _final_rows(self, key="a1"):
        with self.board() as b:
            return [(t.agent_key, t.final) for t in b.transcripts(job="J", agent_key=key)]

    def test_final_transcript_stored_after_close(self):
        self._archive_on()
        self._agent_file()
        self._age()
        self.assertEqual([c.reason for c in self.sweep()], ["dead"])
        self.assertEqual(self._final_rows(), [("a1", True)])

    def test_capture_failure_keeps_the_close_and_a_later_sweep_stores_it(self):
        self._archive_on()
        self._age()
        with mock.patch("swarm.supervisor.lost.capture_final", side_effect=RuntimeError("boom")):
            self.assertEqual([c.reason for c in self.sweep()], ["dead"])
        self.assertEqual(self.agent("a1").left_reason, "stuck:dead")
        self.assertIn("not captured: RuntimeError", st.log_path().read_text())
        self.assertEqual(self._final_rows(), [])
        self._agent_file()
        with self.board() as b:
            swarm.sweep_jobs(b, self.cfg)
        self.assertEqual(self._final_rows(), [("a1", True)])

    def test_transcript_not_found_yet_is_retried_until_found(self):
        self._archive_on()
        self._age()
        self.sweep()                                   # no transcript file: nothing stored
        with self.board() as b:
            swarm.sweep_jobs(b, self.cfg)              # still none: noted as not found
        self.assertEqual(self._final_rows(), [])
        self._agent_file()
        with self.board() as b:
            swarm.sweep_jobs(b, self.cfg)
        self.assertEqual(self._final_rows(), [("a1", True)])

    @posix_only("needs POSIX file modes (Windows has ACLs)")
    def test_retries_rotate_so_missing_transcripts_cannot_starve_the_rest(self):
        # FINALIZE_PER_SWEEP (20) patched to 2, with 3 missing: the same shape as >20 missing
        self._archive_on()
        for key in ("a2", "a3"):
            self.hook("start", agent_id=key, session=SID)
        for key in ("a1", "a2", "a3"):
            self._age(key)
        self.assertEqual(len(self.sweep()), 3)                 # closed; no files: nothing stored
        with mock.patch.object(transcripts, "FINALIZE_PER_SWEEP", 2):
            with self.board() as b:
                self.assertEqual(lost.finalize_stuck_owned(b, self.cfg), 0)   # 2 tried, still missing
            tried = {k.split("\t")[1] for k in json.loads(lost.retry_state_path().read_text())}
            self.assertEqual(len(tried), 2)
            (untried,) = {"a1", "a2", "a3"} - tried
            self._agent_file(untried)
            with self.board() as b:
                self.assertEqual(lost.finalize_stuck_owned(b, self.cfg), 1)   # its turn now
        self.assertEqual(self._final_rows(untried), [(untried, True)])
        self.assertEqual(oct(lost.retry_state_path().stat().st_mode & 0o777), "0o600")
        with self.board() as b:
            lost.finalize_stuck_owned(b, self.cfg)
        left = {k.split("\t")[1] for k in json.loads(lost.retry_state_path().read_text())}
        self.assertEqual(left, tried)                          # the stored one is forgotten

    @posix_only("needs POSIX file modes (Windows has ACLs)")
    def test_lost_rollouts_file_is_private(self):
        f = self.tmp / "state" / "lost.txt"
        transcripts._write_lost(f, {"J\tk1"})
        self.assertEqual((f.read_text(), oct(f.stat().st_mode & 0o777)), ("J\tk1\n", "0o600"))

    def test_stuck_closed_codex_agent_is_pending_for_its_owner(self):
        self.h.update_agent("a1", harness="codex")
        self._age()
        self.sweep()
        with self.board() as b:
            pending = b.pending_final_transcripts(transcripts._host(), getpass.getuser(), "codex",
                                                  b.now() - dt.timedelta(days=1))
        self.assertIn(("J", "a1"), pending)

    def test_spent_deadline_stops_the_scan(self):
        self._age()
        with self.board() as b:
            self.assertEqual(stuck.close_stuck_owned(b, self.cfg, deadline=time.monotonic() - 1), [])
        self.assertIsNone(self.agent("a1").ended_at)
        self.assertEqual([c.reason for c in self.sweep()], ["dead"])   # the next sweep does it

    def test_two_stuck_agents_one_wait_flag(self):
        self.hook("start", agent_id="a2", session=SID)
        self._age("a1")
        self._age("a2")
        self.assertEqual(len(self.sweep()), 2)
        with self.board() as b:
            self.assertEqual(b.job_status("J").waiting_on, stuck.WAITING_PREFIX + self.name)

    def test_job_already_waiting_keeps_its_wait(self):
        with self.board() as b:
            b.set_waiting("J", "ci run")
        self._age()
        self.assertEqual(len(self.sweep()), 0)
        with self.board() as b:
            self.assertEqual(b.job_status("J").waiting_on, "ci run")

    def test_find_stuck_owned_is_read_only(self):
        self._age()
        with self.board() as b:
            found = stuck.find_stuck_owned(b, self.cfg)
        self.assertEqual([(js.job, a.agent_key, why) for js, a, why in found], [("J", "a1", "dead")])
        self.assertIsNone(self.agent("a1").ended_at)

    def test_sweep_jobs_runs_it(self):
        self._age()
        with self.board() as b:
            swarm.sweep_jobs(b, self.cfg)
        self.assertEqual(self.agent("a1").left_reason, "stuck:dead")



class LostPathTests(unittest.TestCase):
    """lost.transcript_path: the Claude session is looked up by UUID only, by
    exact name (hosts.claude.find_session_transcript), from the local enrolment record's session
    first; never a glob over the projects dir."""

    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="swarm-lost-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.home, ignore_errors=True))
        os.chmod(self.home, 0o700)
        p = mock.patch.dict(os.environ, {**home_env(self.home), "CLAUDE_CONFIG_DIR": str(self.home / ".claude"),
                                         "CODEX_HOME": str(self.home / ".codex")})
        p.start()
        self.addCleanup(p.stop)
        self.proj = self.home / ".claude/projects/-work"
        self.proj.mkdir(parents=True)
        self.cfg = {"board": {"backend": "memory"}, "memory": {"store": "lost-tests"}}

    def enrol(self, key, session_id):
        from swarm import enrolment
        from swarm.board.autoinit import store_key
        enrolment.write(store_key(self.cfg), job="J", agent_key=key, harness="claude",
                        session_id=session_id, cwd=str(self.home))

    def _a(self, key, harness="claude", resume_of=None):
        t = dt.datetime(2026, 9, 27, tzinfo=UTC)
        return AgentStatus("J", "N", None, "left", None, 0, 0, t, t, None, t, "h", key, harness,
                           None, getpass.getuser(), "stuck:dead", resume_of)

    def _js(self, sid=SID):
        t = dt.datetime(2026, 9, 27, tzinfo=UTC)
        return JobStatus("J", "active", None, None, None, None, sid, t, None, None, 1, 0, 0, 0, 0, 0, 0, None)

    def test_claude_subagent_under_orchestrator_session(self):
        (self.proj / f"{SID}.jsonl").write_text("{}\n")
        sub = self.proj / f"{SID}/subagents/agent-a1.jsonl"
        sub.parent.mkdir(parents=True)
        sub.write_text("{}\n")
        self.assertEqual(lost.transcript_path(self._js(), self._a("a1")), sub)

    def test_claude_subagent_of_attached_session_found_by_its_record(self):
        (self.proj / f"{OTHER_SID}.jsonl").write_text("{}\n")
        sub = self.proj / f"{OTHER_SID}/subagents/agent-a2.jsonl"
        sub.parent.mkdir(parents=True)
        sub.write_text("{}\n")
        self.assertIsNone(lost.transcript_path(self._js(), self._a("a2")))       # no glob any more
        self.enrol("a2", OTHER_SID)
        self.assertEqual(lost.transcript_path(self._js(), self._a("a2"), self.cfg), sub)

    def test_a_board_session_id_that_is_no_uuid_finds_nothing(self):
        for sid in ("sess-1", "*", "../-work/x", "0b9f6c1e*"):
            with self.subTest(sid=sid):
                sub = self.proj / f"{sid}/subagents/agent-a1.jsonl" if "/" not in sid and "*" not in sid else None
                if sub:
                    (self.proj / f"{sid}.jsonl").write_text("{}\n")
                    sub.parent.mkdir(parents=True, exist_ok=True)
                    sub.write_text("{}\n")
                with mock.patch("glob.glob", side_effect=AssertionError("globbed")), \
                        mock.patch.object(Path, "glob", side_effect=AssertionError("globbed")):
                    self.assertIsNone(lost.transcript_path(self._js(sid), self._a("a1")))

    def test_claude_replacement_is_a_root_session(self):
        (self.proj / "3f0c1e9a-0000-4000-8000-000000000001.jsonl").write_text("{}\n")
        a = self._a("3f0c1e9a-0000-4000-8000-000000000001", resume_of="a1")
        self.assertEqual(lost.transcript_path(self._js(), a).name, "3f0c1e9a-0000-4000-8000-000000000001.jsonl")

    def test_codex_rollout_by_thread_id(self):
        tid = "00000000-0000-4000-8000-000000000003"
        r = self.home / f".codex/sessions/2026/09/27/rollout-2026-09-27T00-00-00-{tid}.jsonl"
        r.parent.mkdir(parents=True)
        r.write_text("{}\n")
        self.assertEqual(lost.transcript_path(self._js(), self._a(tid, "codex")), r)

    def test_key_with_slash_finds_nothing(self):
        self.assertIsNone(lost.transcript_path(self._js(), self._a("../x")))

    def test_key_with_glob_characters_finds_nothing(self):
        sub = self.proj / f"{OTHER_SID}/subagents/agent-a2.jsonl"
        sub.parent.mkdir(parents=True)
        sub.write_text("{}\n")
        for key in ("a*", "a?", "[a]2", ""):
            self.assertIsNone(lost.transcript_path(self._js(), self._a(key)), key)
