"""Transcript capture end to end: the hooks (SubagentStop, the Start/Stop snapshot sweep, never
the per-tool hooks), deactivate, auto-close and purge, on the backend under test."""
from __future__ import annotations

import datetime as dt
import json
import os
import time
from pathlib import Path
from unittest import mock

from test_hooks_cli import Env  # noqa: F401  (sets sys.path)

from swarm import cli as swarm  # noqa: E402
from swarm import enrolment  # noqa: E402
from swarm import hooks as swarm_hooks  # noqa: E402
from swarm import transcripts  # noqa: E402
from swarm.board.autoinit import store_key  # noqa: E402

UTC = dt.timezone.utc
SESSION = "5e5510a0-0000-4000-8000-000000000001"   # Claude session ids are UUIDs
OTHER_SESSION = "5e5510a0-0000-4000-8000-000000000002"


def now_iso(offset: float = 0) -> str:
    return (dt.datetime.now(UTC) + dt.timedelta(seconds=offset)).isoformat().replace("+00:00", "Z")


def line(text: str, offset: float = 0, kind: str = "assistant") -> str:
    return json.dumps({"type": kind, "timestamp": now_iso(offset),
                       "message": {"role": kind, "content": [{"type": "text", "text": text}]}}) + "\n"


class CaptureEnv(Env):
    enabled = True

    def setUp(self):
        super().setUp()
        with self.config.open("a") as fh:
            fh.write(f"[transcripts]\nenabled = {'true' if self.enabled else 'false'}\n")
        self.cfg = swarm.load_config(self.config)
        self.claude = self.tmp / "claude"
        self.main = self.claude / "projects" / "-proj" / f"{SESSION}.jsonl"
        self.main.parent.mkdir(parents=True)
        patcher = mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.claude)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.stamp = transcripts.snapshot_stamp(self.cfg)

    def fresh_stamp(self):
        self.stamp.parent.mkdir(parents=True, exist_ok=True)
        self.stamp.touch()

    def old_stamp(self):
        self.fresh_stamp()
        old = time.time() - 3600
        os.utime(self.stamp, (old, old))

    def activate(self, job: str = "J"):
        self.main.write_text(line("before the job", -3600, "user"))
        rc, _, err = self.cli("activate", "--job", job, "--session", SESSION)
        self.assertEqual(rc, 0, err)
        self.enrol_job(job)
        with self.main.open("a") as fh:
            fh.write(line("orchestrating the job, api_key=abcd1234efgh5678"))

    # The local enrolment records the unsandboxed hook writes: the job at the activate
    # call's own hook, each agent at its SubagentStart. Capture trusts only these.
    def enrol_job(self, job: str = "J", ago: float = 0, session: str = SESSION):
        enrolment.write_job(store_key(self.cfg), job=job, harness="claude", session_id=session,
                            cwd=str(self.tmp), now=time.time() - ago)

    def enrol(self, agent_id: str, job: str = "J"):
        enrolment.write(store_key(self.cfg), job=job, agent_key=agent_id, harness="claude",
                        session_id=SESSION, cwd=str(self.tmp))

    def agent_file(self, agent_id: str, text: str = "working on it") -> Path:
        p = self.main.with_suffix("") / "subagents" / f"agent-{agent_id}.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(line(f"[swarm job: J]\n{text}", kind="user") + line("done, token=Zq8Zq8Zq8Zq8"))
        return p

    def start(self, agent_id: str):
        self.hook("start", agent_id=agent_id, session=SESSION, transcript_path=str(self.main))
        self.enrol(agent_id)

    def rows(self, **kw):
        with self.board() as b:
            return b.transcripts(**kw)

    def body(self, job: str, key: str) -> str:
        with self.board() as b:
            data = b.transcript_body(job, key)
        return data.decode() if data is not None else None


class HookCaptureTests(CaptureEnv):
    def test_stop_captures_the_final_redacted_transcript(self):
        self.activate()
        self.fresh_stamp()
        self.start("a1")
        self.agent_file("a1")
        self.hook("stop", agent_id="a1", session=SESSION, transcript_path=str(self.main))
        [row] = self.rows(agent_key="a1")
        name = self.agent("a1").name
        self.assertEqual((row.job, row.agent_name, row.role, row.final, row.session_id),
                         ("J", name, "subagent", True, SESSION))
        self.assertEqual(row.harness, "claude")
        self.assertGreaterEqual(row.redactions, 1)
        body = self.body("J", "a1")
        self.assertIn("working on it", body)
        self.assertNotIn("Zq8Zq8Zq8Zq8", body)
        self.assertFalse(self.error_log.exists() and "transcript" in self.error_log.read_text())

    def test_stop_prefers_agent_transcript_path(self):
        self.activate()
        self.fresh_stamp()
        self.start("a1")
        # under the transcript root but not where the main transcript says (see test_hardening)
        other = self.claude / "projects" / "-other" / SESSION / "subagents" / "agent-a1.jsonl"
        other.parent.mkdir(parents=True)
        other.write_text(line("from the payload path"))
        self.hook("stop", agent_id="a1", session=SESSION, transcript_path=str(self.main),
                  agent_transcript_path=str(other))
        self.assertIn("from the payload path", self.body("J", "a1"))

    def test_stop_of_a_non_member_captures_nothing(self):
        self.activate()
        self.fresh_stamp()
        self.agent_file("stranger")
        self.hook("stop", agent_id="stranger", session=SESSION, transcript_path=str(self.main))
        self.assertEqual(self.rows(), [])

    def test_per_tool_hooks_never_capture(self):
        self.activate()
        self.fresh_stamp()
        self.start("a1")
        self.agent_file("a1")
        self.old_stamp()   # a snapshot round would be due
        boom = mock.Mock(side_effect=AssertionError("captured from a per-tool hook"))
        with mock.patch.multiple(transcripts, capture_subagent=boom, capture_orchestrator=boom,
                                 capture_job=boom, run_snapshots=boom, snapshot_due=boom,
                                 capture_closed=boom, rotate=boom):
            for _ in range(3):
                self.hook("turn", agent_id="a1", session=SESSION, tool_name="Bash",
                          transcript_path=str(self.main))
                self.hook("done", agent_id="a1", session=SESSION, tool_name="Bash",
                          transcript_path=str(self.main))
        self.assertEqual(boom.call_count, 0)
        self.assertEqual(self.rows(), [])

    def test_snapshot_round_from_the_start_sweep_when_due(self):
        self.activate()
        self.fresh_stamp()
        self.start("a1")
        self.agent_file("a1", "still running")
        self.old_stamp()
        before = self.stamp.stat().st_mtime
        self.start("a2")   # its sweep runs the due round
        rows = {r.agent_key: r for r in self.rows()}
        self.assertEqual(set(rows), {"a1", "orchestrator"})
        self.assertFalse(rows["a1"].final)
        self.assertFalse(rows["orchestrator"].final)
        orch = self.body("J", "orchestrator")
        self.assertIn("orchestrating the job", orch)
        self.assertNotIn("before the job", orch)
        self.assertNotIn("abcd1234efgh5678", orch)
        self.assertGreater(self.stamp.stat().st_mtime, before)
        # not due again right away: a new line is not picked up by the next start
        with self.main.open("a") as fh:
            fh.write(line("later"))
        self.start("a3")
        self.assertNotIn("later", self.body("J", "orchestrator"))

    def test_hook_out_of_time_is_logged_and_the_agent_still_stops(self):
        self.activate()
        self.fresh_stamp()
        self.start("a1")
        self.agent_file("a1")
        with mock.patch.object(swarm_hooks, "TRANSCRIPT_BUDGET_SECONDS", -1):
            self.hook("stop", agent_id="a1", session=SESSION, transcript_path=str(self.main))
        self.assertEqual(self.agent("a1").status, "completed")
        self.assertEqual(self.rows(), [])
        self.assertIn("OutOfTime", self.error_log.read_text())


class CliCaptureTests(CaptureEnv):
    def test_deactivate_captures_orchestrator_and_agents_final(self):
        self.activate()
        self.fresh_stamp()
        self.start("a1")
        self.agent_file("a1", "unfinished work")
        rc, out, err = self.cli("deactivate", "--job", "J", "--status", "completed", "--force")
        self.assertEqual(rc, 0, err)
        rows = {r.agent_key: r for r in self.rows(job="J")}
        self.assertEqual(set(rows), {"a1", "orchestrator"})
        self.assertTrue(all(r.final for r in rows.values()))
        self.assertEqual({r.harness for r in rows.values()}, {"claude"})
        self.assertIn("orchestrating the job", self.body("J", "orchestrator"))

    def test_auto_close_captures_the_orchestrator_slice(self):
        self.activate()
        self.fresh_stamp()
        self.start("a1")
        self.agent_file("a1")
        self.hook("stop", agent_id="a1", session=SESSION, transcript_path=str(self.main))
        self.h.backdate_job("J", created_at=4000, activated_at=4000)
        self.enrol_job(ago=4000)   # the local record's activation, which sets the window
        self.h.backdate_agent("a1", joined_at=3900, last_seen=3800, left_at=3800)
        # the slice starts at the (backdated) activation
        self.main.write_text(line("old", -5000) + line("the whole run", -3950))
        _, out, _ = self.cli("status")
        self.assertIn("J: auto-closed", out)
        [orch] = self.rows(role="orchestrator")
        self.assertTrue(orch.final)
        body = self.body("J", "orchestrator")
        self.assertIn("the whole run", body)
        self.assertNotIn('"old"', body)

    def test_purge_rotates_old_transcripts(self):
        with self.board() as b:
            b.save_transcript(transcripts.make_row("gone", "k", "n", "subagent", "{}\n",
                                                   captured_at=dt.datetime.now(UTC) - dt.timedelta(days=90)))
            b.save_transcript(transcripts.make_row("kept", "k", "n", "subagent", "{}\n"))
        rc, _, _ = self.cli("purge")
        self.assertEqual(rc, 0)
        self.assertEqual([r.job for r in self.rows()], ["kept"])


class LocalAuthorityTests(CaptureEnv):
    """What capture reads is decided by the local enrolment records, never by board
    rows (host, os_user, session_id, activated_at are all writable by agents and other users)."""

    def forged_agent(self, key: str = "forged"):
        """A row claiming this machine and user, with a transcript here, never enrolled here."""
        import getpass
        with self.board() as b:
            b.allocate_name(key, "J")
        self.h.update_agent(key, host=transcripts._host(), os_user=getpass.getuser())
        self.agent_file(key, "someone else's session")

    def snapshot_round(self):
        self.old_stamp()
        with self.board() as b:
            return transcripts.run_snapshots(b, self.cfg)

    def test_owns_needs_a_local_enrolment_record_for_the_rows_job(self):
        import getpass
        from types import SimpleNamespace as NS
        row = NS(job="J", agent_key="a1", host=transcripts._host(), os_user=getpass.getuser())
        self.assertFalse(transcripts.owns(row, self.cfg))           # board says ours; no record
        self.enrol("a1", job="K")
        self.assertFalse(transcripts.owns(row, self.cfg))           # a record for another job
        self.enrol("a1")
        self.assertTrue(transcripts.owns(row, self.cfg))
        self.assertTrue(transcripts.owns(NS(job="J", agent_key="a1", host="elsewhere", os_user=None), self.cfg))
        self.assertFalse(transcripts.owns(row))                     # no board: nobody's

    def test_forged_os_user_row_is_not_captured(self):
        self.activate()
        self.fresh_stamp()
        self.start("a1")
        self.agent_file("a1")
        self.forged_agent()
        self.snapshot_round()
        self.assertEqual({r.agent_key for r in self.rows()}, {"a1", "orchestrator"})
        rc, _, err = self.cli("deactivate", "--job", "J", "--status", "completed", "--force")
        self.assertEqual(rc, 0, err)
        self.assertEqual({r.agent_key for r in self.rows()}, {"a1", "orchestrator"})

    def test_snapshot_skips_jobs_not_activated_here(self):
        self.activate()
        self.fresh_stamp()
        self.start("a1")
        self.agent_file("a1")
        enrolment.remove_job(store_key(self.cfg), "J")   # the board job carries our real session id
        self.snapshot_round()
        self.assertEqual({r.agent_key for r in self.rows()}, {"a1"})   # the enrolled agent only
        rc, _, err = self.cli("deactivate", "--job", "J", "--status", "completed", "--force")
        self.assertEqual(rc, 0, err)
        self.assertNotIn("orchestrator", {r.agent_key for r in self.rows()})

    def test_snapshot_window_from_local_record(self):
        self.activate()
        self.fresh_stamp()
        self.h.backdate_job("J", created_at=7200, activated_at=7200)   # a forged, earlier start
        self.snapshot_round()
        body = self.body("J", "orchestrator")
        self.assertIn("orchestrating the job", body)
        self.assertNotIn("before the job", body)

    def test_orchestrator_session_from_local_record(self):
        other = self.main.with_name(f"{OTHER_SESSION}.jsonl")
        other.write_text(line("another session of mine: private"))
        self.activate()
        self.fresh_stamp()
        self.h.update_job("J", session_id=OTHER_SESSION)   # the board row points elsewhere
        self.snapshot_round()
        body = self.body("J", "orchestrator")
        self.assertIn("orchestrating the job", body)
        self.assertNotIn("private", body)

    def test_a_board_session_id_glob_finds_nothing(self):
        self.activate()
        self.fresh_stamp()
        self.enrol_job(session="*")                          # never a pattern, even from a record
        self.snapshot_round()
        self.assertEqual(self.rows(), [])


class SweeperAndConfigTests(CaptureEnv):
    def test_defaults_agree_and_example_config_documents_them(self):
        import tomllib
        from support import ROOT
        self.assertEqual(swarm.DEFAULTS["transcripts"], transcripts.DEFAULTS)
        example = tomllib.loads((ROOT / "config.example.toml").read_text())["transcripts"]
        self.assertEqual(example, transcripts.DEFAULTS)

    def test_watch_sweeper_runs_a_due_snapshot_round(self):
        self.activate()
        self.fresh_stamp()
        self.start("a1")
        self.agent_file("a1")
        self.old_stamp()
        with self.board() as b:
            swarm.Sweeper(self.cfg)(b)
        self.assertEqual({r.agent_key for r in self.rows()}, {"a1", "orchestrator"})


class DisabledCaptureTests(CaptureEnv):
    enabled = False

    def test_disabled_touches_no_transcript_storage(self):
        from swarm.board import Board
        boom = mock.Mock(side_effect=AssertionError("transcript storage touched"))
        self.activate()
        self.agent_file("a1")
        with self.board() as b:
            backend = type(b)
        with mock.patch.multiple(Board, save_transcript=boom, transcripts=boom, transcript_body=boom,
                                 rotate_transcripts=boom, transcript_totals=boom), \
                mock.patch.multiple(backend, save_transcript=boom, transcripts=boom,
                                    transcript_body=boom, _delete_transcripts=boom):
            self.start("a1")
            self.hook("turn", agent_id="a1", session=SESSION, tool_name="Bash")
            self.hook("stop", agent_id="a1", session=SESSION, transcript_path=str(self.main))
            self.cli("purge")
            self.cli("deactivate", "--job", "J", "--status", "completed", "--force")
        self.assertEqual(boom.call_count, 0)
        self.assertFalse(self.stamp.exists())


if __name__ == "__main__":
    import unittest
    unittest.main()
