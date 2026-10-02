"""The PostToolUse hook pins memory writes to the agent's transcript, on $SWARM_TEST_BACKEND."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from unittest import mock

import provenance_fixtures as PF
from test_hooks_cli import Env  # noqa: E402  (sets sys.path)
import codex_fixtures as CF  # noqa: E402

from swarm import hindsight  # noqa: E402

SECRET_KEY = "sk-ant-api03-" + "Z" * 40
SID = "0b6f6a1e-3c2d-4f5e-9a7b-1c2d3e4f5a6b"   # a Claude session is a UUID (own_transcript)


def claude_lines(call_id="toolu_mem", pad_mb=0):
    out = [json.dumps({"type": "user", "message": {"content": "[swarm job: J]"}}) + "\n"]
    for i in range(30):
        out.append(json.dumps({"type": "assistant", "timestamp": "2026-09-28T11:00:00Z",
                               "message": {"model": "m", "content": [{"type": "text", "text": f"step {i}"}]}}) + "\n")
    if pad_mb:
        pad = json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "p" * 1000}]}}) + "\n"
        out.append(pad * (pad_mb * 1024))
    out.append(json.dumps({"type": "assistant", "timestamp": "2026-09-28T11:59:00Z", "message": {"content": [
        {"type": "tool_use", "id": call_id, "name": "Bash", "input": {"command": "retain"}}]}}) + "\n")
    return "".join(out)


class ClaudeAgents:
    """Mixin: job J active, Claude subagents a1 and a2 enrolled, each with a transcript under the
    temp HOME's projects dir. Call setup_agents() from setUp after any config change."""

    def setup_agents(self):
        rc, _, err = self.cli("activate", "--job", "J")
        self.assertEqual(rc, 0, err)
        self.proj = Path(os.environ["HOME"]) / ".claude" / "projects" / "-work"
        self.main = self.proj / f"{SID}.jsonl"
        (self.proj / SID / "subagents").mkdir(parents=True)
        self.main.write_text("")
        for key in ("a1", "a2"):
            self.transcript(key).write_text(claude_lines())
            self.hook("start", agent_id=key, session=SID, transcript_path=str(self.main), prompt="[swarm job: J]")
            self.hook("turn", agent_id=key, session=SID, transcript_path=str(self.main), tool_name="Bash")

    def transcript(self, key):
        return self.proj / SID / "subagents" / f"agent-{key}.jsonl"

    def post(self, command, stdout, agent_id="a1", call_id="toolu_mem", **extra):
        p = PF.claude_post(command, stdout, agent_id=agent_id, session_id=SID,
                           transcript_path=str(self.main), tool_use_id=call_id)
        p.update(extra)
        return self.hook("done", agent_id=p.pop("agent_id"), session=p.pop("session_id"), **p)

    def refs(self, **kw):
        with self.board() as b:
            return b.memory_refs(**kw)

    def excerpt(self, doc):
        with self.board() as b:
            return (b.memory_ref_excerpt(doc) or b"").decode()

    def errors(self) -> str:
        return self.error_log.read_text() if self.error_log.exists() else ""

    def name_of(self, key):
        return self.agent(key).name


class ProvenanceHookTests(ClaudeAgents, Env):
    def setUp(self):
        super().setUp()
        PF.add_note_writer(self)
        self.setup_agents()

    def test_a_multi_id_write_without_hindsight_logs_nothing(self):
        self.post(PF.NOTE_MULTI_CMD, PF.NOTE_MULTI_OUT)
        self.assertEqual(len(self.refs()), 2)
        self.assertEqual(self.errors(), "")

    def test_retain_py_write_is_recorded_with_excerpt(self):
        self.post(PF.NOTE_MULTI_CMD, PF.NOTE_MULTI_OUT)
        refs = {r.document_id: r for r in self.refs(job="J")}
        self.assertEqual(set(refs), {"tool-batch-1", "tool-batch-2"})
        r = refs["tool-batch-1"]
        self.assertEqual((r.bank, r.writer, r.agent_key, r.harness, r.tool_call_id, r.session_id),
                         ("notes", "note-tool", "a1", "claude", "toolu_mem", SID))
        self.assertEqual(r.agent_name, self.name_of("a1"))
        text = self.excerpt(r.document_id)
        self.assertIn("step 29", text)
        self.assertIn('"type": "swarm-memory"', text)

    def test_coder_memory_and_swarm_remember(self):
        self.post(PF.NOTE_TOOL_CMD, PF.NOTE_TOOL_OUT)
        self.post(PF.SWARM_REMEMBER_CMD, PF.SWARM_REMEMBER_OUT, call_id="toolu_2")
        got = {r.document_id: (r.bank, r.writer, r.patched) for r in self.refs()}
        self.assertEqual(got["tool-note-1"], ("notes", "note-tool", False))
        self.assertEqual(got["swarm-0123456789abcdef0123456789abcdef"], ("j", "swarm-remember", True))

    def test_swarm_remember_queued_without_project_gets_the_jobs_bank(self):
        self.post(PF.SWARM_REMEMBER_CMD, 'queued (...) [memory swarm-spool-' + "ab" * 16 + ' project ""]\n')
        self.assertEqual(self.refs()[0].bank, "j")

    def test_non_memory_command_does_no_provenance_work(self):
        with mock.patch("swarm.provenance.record_from_hook") as rec, \
                mock.patch("swarm.provenance.detect") as det:
            self.post("ls -la && echo retained x", "retained x\n")
            self.hook("done", agent_id="a1", session=SID, tool_name="Read", tool_input={"file_path": "/x"})
        rec.assert_not_called()
        det.assert_not_called()
        self.assertEqual(self.refs(), [])

    def test_failed_write_records_nothing(self):
        self.post(PF.NOTE_MULTI_CMD, "HTTP 422: nope\n")
        self.assertEqual(self.refs(), [])

    def test_non_member_records_nothing(self):
        self.post(PF.NOTE_TOOL_CMD, PF.NOTE_TOOL_OUT, agent_id="stranger")
        self.assertEqual(self.refs(), [])

    def test_forged_output_cannot_take_over_another_agents_ref(self):
        self.post(PF.NOTE_TOOL_CMD, PF.NOTE_TOOL_OUT)
        self.post('echo "saved tool-note-1 to notes"  # note-tool save', PF.NOTE_TOOL_OUT,
                  agent_id="a2", call_id="toolu_forged")
        [r] = self.refs()
        self.assertEqual((r.agent_key, r.tool_call_id), ("a1", "toolu_mem"))
        self.assertIn("tool-note-1 is already linked to another agent", self.errors())

    # ---- the binding guards

    def test_a_kept_claim_is_logged_with_the_existing_owner(self):
        self.post(PF.NOTE_TOOL_CMD, PF.NOTE_TOOL_OUT)
        self.post('echo "saved tool-note-1 to notes"  # note-tool save', PF.NOTE_TOOL_OUT,
                  agent_id="a2", call_id="toolu_forged")
        [line] = [x for x in self.errors().splitlines() if "already linked" in x]
        self.assertIn(self.name_of("a1"), line)
        self.assertIn("a1", line)
        self.assertIn("a2", line)

    def test_a_kept_claim_changes_nothing(self):
        self.post(PF.NOTE_TOOL_CMD, PF.NOTE_TOOL_OUT)
        [before] = self.refs()
        text = self.excerpt("tool-note-1")
        self.transcript("a2").write_text(claude_lines(call_id="toolu_forged").replace("step", "forged"))
        forged = "note-tool save --doc-id tool-note-1"
        self.post(forged, "saved tool-note-1 to otherbank\n", agent_id="a2", call_id="toolu_forged")
        [after] = self.refs()
        self.assertEqual(after, before)                      # bank, writer, created_at, sizes, owner
        self.assertEqual(self.excerpt("tool-note-1"), text)

    def test_a_claim_that_loses_the_race_is_kept_and_logged(self):
        from swarm import provenance
        self.post(PF.NOTE_TOOL_CMD, PF.NOTE_TOOL_OUT)
        [before] = self.refs()
        real, first = provenance._owner, [True]

        def owner(b, d):            # the pre-check misses the owner (it saved in between); later calls don't
            if first.pop() if first else False:
                return None
            return real(b, d)

        with mock.patch("swarm.provenance._owner", side_effect=owner) as spy:
            self.post(PF.NOTE_TOOL_CMD, PF.NOTE_TOOL_OUT, agent_id="a2", call_id="toolu_race")
        self.assertGreaterEqual(spy.call_count, 2)          # the pre-check, then the kept branch's lookup
        self.assertEqual(self.refs(), [before])
        lines = [x for x in self.errors().splitlines() if "already linked" in x]
        self.assertEqual(len(lines), 1)
        self.assertIn(self.name_of("a1"), lines[0])
        self.assertIn("a2", lines[0])

    def test_document_id_alone_is_the_key_a_forged_bank_makes_no_second_row(self):
        self.post(PF.NOTE_TOOL_CMD, PF.NOTE_TOOL_OUT)
        forged = "note-tool save --doc-id tool-note-1"
        self.post(forged, "saved tool-note-1 to otherbank\n", agent_id="a1", call_id="toolu_2")
        self.post(forged, "saved tool-note-1 to thirdbank\n", agent_id="a2", call_id="toolu_3")
        [r] = self.refs()
        self.assertEqual((r.document_id, r.agent_key), ("tool-note-1", "a1"))

    def test_the_same_agent_saving_again_updates_its_ref(self):
        self.post(PF.NOTE_TOOL_CMD, PF.NOTE_TOOL_OUT)
        self.post(PF.NOTE_TOOL_CMD, PF.NOTE_TOOL_OUT, call_id="toolu_again")
        [r] = self.refs()
        self.assertEqual((r.agent_key, r.tool_call_id), ("a1", "toolu_again"))
        self.assertNotIn("already linked", self.errors())

    def test_agent_key_comes_from_the_payload_never_from_the_output(self):
        out = f'saved tool-note-1 to notes\nagent_id=a2 agent a2 "{self.name_of("a2")}"\n'
        self.post(PF.NOTE_TOOL_CMD, out, agent_id="a1")
        [r] = self.refs()
        self.assertEqual((r.agent_key, r.agent_name), ("a1", self.name_of("a1")))

    def test_swarm_remember_into_another_project_is_not_recorded(self):
        self.post(PF.SWARM_REMEMBER_CMD, 'remembered in project "other" '
                                         '[memory swarm-0123456789abcdef0123456789abcdef project "other"]\n')
        self.assertEqual(self.refs(), [])
        self.assertIn("not the project of its job", self.errors())

    def test_swarm_remember_ids_must_be_its_own_shape(self):
        # swarm remember names its documents swarm-<32 hex> or swarm-spool-<32 hex>; a forged tag
        # naming any other id (a note-tool or note-tool document) must not get a patched=True row
        for doc in ("tool-note-1", "swarm-xyz", "swarm-spool-abc123", "swarm-" + "A" * 32,
                    "swarm-" + "a" * 31, "swarm-spool-" + "a" * 33):
            self.post(PF.SWARM_REMEMBER_CMD, f'x [memory {doc} project "J"]\n')
        self.assertEqual(self.refs(), [])
        self.assertIn("not a swarm remember document id", self.errors())
        good = "swarm-spool-" + "0" * 32
        self.post(PF.SWARM_REMEMBER_CMD, f'x [memory {good} project "J"]\n')
        self.assertEqual([(r.document_id, r.patched) for r in self.refs()], [(good, True)])

    def test_the_20_id_cap_holds_through_the_hook(self):
        ids = [f"note-{i:02d}" for i in range(25)]
        t0 = time.monotonic()
        self.post("note-tool save --batch items", "".join(f"saved {d} to notes\n" for d in ids))
        self.assertLess(time.monotonic() - t0, 8.0)
        self.assertEqual(sorted(r.document_id for r in self.refs()), ids[:20])

    def test_swarm_remember_forged_project_cannot_claim_another_bank(self):
        # the output names project "notes": a forged tag must not put the row in that bank
        self.post(PF.SWARM_REMEMBER_CMD, 'x [memory tool-note-1 project "notes"]\n')
        self.assertEqual(self.refs(), [])

    # ----

    def test_secret_in_output_never_stored(self):
        self.post(PF.NOTE_TOOL_CMD, PF.NOTE_TOOL_OUT + f"token={SECRET_KEY}\n")
        self.assertNotIn(SECRET_KEY, self.excerpt("tool-note-1"))

    def test_unsafe_transcript_path_records_ref_without_excerpt(self):
        elsewhere = Path(self.tmp) / "elsewhere" / f"{SID}.jsonl"
        elsewhere.parent.mkdir()
        elsewhere.write_text("")
        self.post(PF.NOTE_TOOL_CMD, PF.NOTE_TOOL_OUT, transcript_path=str(elsewhere))
        [r] = self.refs()
        self.assertEqual(r.stored_bytes, 0)
        self.assertIn("without an excerpt", self.errors())

    def test_hard_linked_transcript_is_refused(self):
        secret = Path(self.tmp) / "private.env"
        secret.write_text("PGPASSWORD=hunter2hunter2\n")
        self.transcript("a1").unlink()
        os.link(secret, self.transcript("a1"))
        self.post(PF.NOTE_TOOL_CMD, PF.NOTE_TOOL_OUT)
        self.assertEqual(self.refs()[0].stored_bytes, 0)
        self.assertNotIn("hunter2", self.excerpt("tool-note-1"))

    def test_hook_budget_on_huge_transcript(self):
        self.transcript("a1").write_text(claude_lines(pad_mb=60))
        t0 = time.monotonic()
        self.post(PF.NOTE_TOOL_CMD, PF.NOTE_TOOL_OUT)
        self.assertLess(time.monotonic() - t0, 8.0)
        [r] = self.refs()
        self.assertGreater(r.stored_bytes, 0)

    def test_out_of_budget_records_the_ref_without_an_excerpt(self):
        with mock.patch("swarm.hooks.PROVENANCE_BUDGET_SECONDS", 0.0):
            self.post(PF.NOTE_TOOL_CMD, PF.NOTE_TOOL_OUT)
        [r] = self.refs()
        self.assertEqual(r.stored_bytes, 0)
        self.assertIn("without an excerpt", self.errors())

    def test_a_slow_excerpt_stops_at_the_deadline(self):
        from swarm import transcripts
        real = transcripts.redact

        def slow(text, deadline=None):
            time.sleep(0.2)
            transcripts._check(deadline)
            return real(text, deadline)

        with mock.patch("swarm.hooks.PROVENANCE_BUDGET_SECONDS", 0.3), \
                mock.patch("swarm.transcripts.redact", side_effect=slow):
            t0 = time.monotonic()
            self.post(PF.NOTE_TOOL_CMD, PF.NOTE_TOOL_OUT)
            took = time.monotonic() - t0
        [r] = self.refs()
        self.assertEqual(r.stored_bytes, 0)
        self.assertLess(took, 2.0)

    def test_failure_inside_never_fails_the_agent(self):
        with mock.patch("swarm.provenance.record_from_hook", side_effect=RuntimeError("boom")):
            self.post(PF.NOTE_TOOL_CMD, PF.NOTE_TOOL_OUT)   # Env.hook asserts exit code 0
        self.assertIn("done memory", self.errors())
        self.assertIn("boom", self.errors())

    def test_a_board_refusal_is_logged_not_raised(self):
        with mock.patch("swarm.board.base.check_excerpt", side_effect=ValueError("bad excerpt")):
            self.post(PF.NOTE_TOOL_CMD, PF.NOTE_TOOL_OUT)
        self.assertIn("bad excerpt", self.errors())

    def test_tool_finished_still_runs(self):
        self.assertIsNotNone(self.agent("a1").current_tool)      # setup's turn left Bash in flight
        self.post(PF.NOTE_TOOL_CMD, PF.NOTE_TOOL_OUT)
        self.assertIsNone(self.agent("a1").current_tool)

    def test_provenance_off(self):
        self.config.write_text(self.config.read_text().replace("[provenance]\n", "[provenance]\nenabled = false\n", 1))
        from swarm import cli as swarm
        self.cfg = swarm.load_config(self.config)
        self.post(PF.NOTE_TOOL_CMD, PF.NOTE_TOOL_OUT)
        self.assertEqual(self.refs(), [])


class CodexProvenanceTests(Env):
    def setUp(self):
        super().setUp()
        PF.add_note_writer(self)
        self.codex_home = CF.staged(self.tmp)
        p = mock.patch.dict(os.environ, {"CODEX_HOME": str(self.codex_home)}); p.start(); self.addCleanup(p.stop)
        self.sid = CF.payloads("SubagentStart")[0]["session_id"]
        rc, _, err = self.cli("activate", "--job", "fixture", "--session", self.sid)
        self.assertEqual(rc, 0, err)
        start = CF.localize(CF.payloads("SubagentStart")[0], self.codex_home)
        self.hook("start", agent_id=start.pop("agent_id"), session=start.pop("session_id"), host="codex", **start)

    def test_codex_bash_write_is_recorded_with_codex_harness(self):
        p = CF.localize(PF.codex_post(PF.NOTE_TOOL_CMD, PF.NOTE_TOOL_OUT), self.codex_home)
        call = p["tool_use_id"]
        self.hook("done", agent_id=p.pop("agent_id"), session=p.pop("session_id"), host="codex", **p)
        with self.board() as b:
            [r] = b.memory_refs(job="fixture")
            text = b.memory_ref_excerpt(r.document_id).decode()
        self.assertEqual((r.harness, r.tool_call_id, r.bank), ("codex", call, "notes"))
        self.assertIn('"type": "swarm-memory"', text)
        self.assertIn("response_item", text)


from test_hindsight import HindsightEnv  # noqa: E402


class PatchTests(ClaudeAgents, HindsightEnv):
    def setUp(self):
        super().setUp()
        PF.add_note_writer(self)
        self.enable()
        self.setup_agents()
        hindsight.Client(self.cfg).retain("notes", "a fact", [], {"source": "notes-claude"},
                                          document_id="tool-note-1")
        self.fake.requests.clear()

    def test_patched_only_when_the_cached_capability_says_so(self):
        self.post(PF.NOTE_TOOL_CMD, PF.NOTE_TOOL_OUT)
        self.assertEqual(self.fake.calls("PATCH"), [])
        self.assertEqual(self.fake.calls("GET", "/openapi.json"), [])    # the hook never probes
        self.assertFalse(self.refs()[0].patched)
        self.fake.metadata_patch = True
        hindsight.refresh_caps(self.cfg)
        from swarm import provenance
        real, seen = provenance._patch, []

        def spy(*a, **kw):          # the row as saved just before the patch
            seen.append(self.refs()[0].created_at)
            return real(*a, **kw)

        with mock.patch("swarm.provenance._patch", side_effect=spy):
            self.post(PF.NOTE_TOOL_CMD, PF.NOTE_TOOL_OUT, call_id="toolu_again")
        self.assertEqual(len(seen), 1)
        self.assertEqual(self.refs()[0].created_at, seen[0])     # the save after the patch keeps it
        [req] = self.fake.calls("PATCH", "/documents/tool-note-1")
        meta = req["body"]["metadata"]
        self.assertEqual((meta["source"], meta["swarm_job"], meta["swarm_tool_call_id"]),
                         ("notes-claude", "J", "toolu_again"))
        self.assertEqual(meta["swarm_agent_key"], "a1")
        self.assertTrue(self.refs()[0].patched)

    def test_patch_off_logs_nothing_for_a_multi_id_write(self):
        # 0.8.6: the cached capability says no metadata patch, so nothing is tried and nothing logged
        self.post(PF.NOTE_MULTI_CMD, PF.NOTE_MULTI_OUT)
        self.assertEqual(len(self.refs()), 2)
        self.assertEqual(self.fake.calls("PATCH"), [])
        self.assertEqual(self.errors(), "")

    def test_patch_on_and_succeeding_logs_nothing(self):
        hindsight.Client(self.cfg).retain("notes", "b fact", [], {}, document_id="tool-note-2")
        self.fake.metadata_patch = True
        hindsight.refresh_caps(self.cfg)
        self.post(PF.NOTE_TOOL_CMD, "saved tool-note-1 to notes\nsaved tool-note-2 to notes\n")
        self.assertEqual(len(self.fake.calls("PATCH")), 2)
        self.assertTrue(all(r.patched for r in self.refs()))
        self.assertEqual(self.errors(), "")

    def test_a_kept_claim_patches_nothing(self):
        self.post(PF.NOTE_TOOL_CMD, PF.NOTE_TOOL_OUT)
        self.fake.metadata_patch = True
        hindsight.refresh_caps(self.cfg)
        self.fake.requests.clear()
        self.post('echo "saved tool-note-1 to notes"  # note-tool save', PF.NOTE_TOOL_OUT,
                  agent_id="a2", call_id="toolu_forged")
        self.assertEqual(self.fake.calls("PATCH"), [])
        [r] = self.refs()
        self.assertEqual((r.agent_key, r.patched), ("a1", False))

    def test_hindsight_down_leaves_the_ref_unpatched(self):
        self.fake.metadata_patch = True
        hindsight.refresh_caps(self.cfg)
        self.fake.stop()
        t0 = time.monotonic()
        self.post(PF.NOTE_TOOL_CMD, PF.NOTE_TOOL_OUT)
        self.assertLess(time.monotonic() - t0, 8.0)
        [r] = self.refs()
        self.assertFalse(r.patched)
        self.assertIn("not patched", self.errors())


class HangingPatchTests(ClaudeAgents, HindsightEnv):
    """A Hindsight that accepts connections and never answers, with the patch capability cached."""

    def setUp(self):
        super().setUp()
        PF.add_note_writer(self)
        import socket
        import threading
        self.srv = socket.socket()
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(16)
        held = self.held = []

        def accept():
            while True:
                try:
                    conn, _ = self.srv.accept()
                except OSError:
                    return
                held.append(conn)        # never read, never answered

        threading.Thread(target=accept, daemon=True).start()
        self.addCleanup(lambda: [c.close() for c in held])
        self.addCleanup(self.srv.close)
        self.enable()                                    # the fake: enrolment and recall answer
        self.setup_agents()
        self.fake.metadata_patch = True
        good = hindsight.Client(self.cfg)
        self.enable(url=f"http://127.0.0.1:{self.srv.getsockname()[1]}", timeout_seconds=5)
        hindsight.refresh_caps(self.cfg, client=good)    # the hanging server's cached capability
        self.assertTrue(hindsight.metadata_patch_supported(self.cfg))
        held.clear()

    def test_one_line_and_within_budget_when_hindsight_hangs(self):
        t0 = time.monotonic()
        self.post(PF.NOTE_MULTI_CMD, PF.NOTE_MULTI_OUT)
        self.assertLess(time.monotonic() - t0, 2.0)          # one PATCH_SECONDS wait, not one per document
        self.assertEqual(len(self.held), 1)                  # the second document was not tried
        refs = self.refs()
        self.assertEqual(len(refs), 2)
        self.assertFalse(any(r.patched for r in refs))
        lines = [x for x in self.errors().splitlines() if "not patched" in x]
        self.assertEqual(len(lines), 1, self.errors())
        self.assertIn("tool-batch-1", lines[0])
        self.assertIn("tool-batch-2", lines[0])


class StalePatchTests(ClaudeAgents, HindsightEnv):
    def setUp(self):
        super().setUp()
        PF.add_note_writer(self)
        self.enable()
        self.setup_agents()
        hindsight.Client(self.cfg).retain("notes", "a fact", [], {"source": "notes-claude"},
                                          document_id="tool-note-1")
        self.fake.metadata_patch = True
        hindsight.refresh_caps(self.cfg)
        self.fake.requests.clear()

    def test_a_document_last_written_long_before_the_hook_is_not_patched(self):
        self.fake.set_document_time("notes", "tool-note-1", "2026-01-01T00:00:00+00:00")
        self.post(PF.NOTE_TOOL_CMD, PF.NOTE_TOOL_OUT)
        self.assertEqual(self.fake.calls("PATCH"), [])
        [r] = self.refs()
        self.assertFalse(r.patched)
        self.assertIn("last written", self.errors())

    def test_a_fresh_document_is_patched(self):
        self.post(PF.NOTE_TOOL_CMD, PF.NOTE_TOOL_OUT)
        self.assertEqual(len(self.fake.calls("PATCH")), 1)
        self.assertTrue(self.refs()[0].patched)

    def test_the_client_refuses_a_stale_document(self):
        import datetime as dt
        self.fake.set_document_time("notes", "tool-note-1", "2026-01-01T00:00:00+00:00")
        c = hindsight.Client(self.cfg)
        with self.assertRaises(hindsight.HindsightError):
            c.patch_document_metadata("notes", "tool-note-1", {"swarm_job": "J"},
                                      written_after=dt.datetime.now(dt.timezone.utc))
        self.assertEqual(self.fake.calls("PATCH"), [])
        c.patch_document_metadata("notes", "tool-note-1", {"swarm_job": "J"})   # no gate: as before
        self.assertEqual(len(self.fake.calls("PATCH")), 1)


from test_supervise_hooks import SupervisedEnv  # noqa: E402
from swarm.supervisor import markers  # noqa: E402


class ReplacementProvenanceTests(SupervisedEnv):
    """A supervisor replacement is a root session: no agent_id in its payload; the resume marker
    makes it the member, and its transcript is the session's own."""
    RID = "11111111-2222-4333-8444-555555555555"

    def setUp(self):
        super().setUp()
        PF.add_note_writer(self)
        self.enable_supervisor()
        self.cli("activate", "--job", "J", "--session", SID)
        self.hook("start", agent_id="orig", session=SID)
        self.name = self.agent("orig").name
        with self.board() as b:
            b.close_agent("orig", "stuck:dead")
            rid = b.record_restart("J", "orig", "orig", "stuck:dead", "claude", 60).id
        markers.write_resume_marker(self.cfg, "J", rid, resume_of="orig", name=self.name,
                                    harness="claude", session_id=self.RID)
        proj = Path(os.environ["HOME"]) / ".claude" / "projects" / "-work"
        proj.mkdir(parents=True)
        self.root = proj / f"{self.RID}.jsonl"
        self.root.write_text(claude_lines())
        self.hook("turn", agent_id=None, session=self.RID, transcript_path=str(self.root),
                  tool_name="Bash", tool_input={"command": "ls"})

    def test_a_replacements_write_is_pinned_to_its_own_key_and_transcript(self):
        p = PF.claude_post(PF.NOTE_TOOL_CMD, PF.NOTE_TOOL_OUT, session_id=self.RID,
                           transcript_path=str(self.root), tool_use_id="toolu_mem")
        p.pop("agent_id")
        self.hook("done", agent_id=None, session=p.pop("session_id"), **p)
        with self.board() as b:
            [r] = b.memory_refs()
            text = b.memory_ref_excerpt(r.document_id).decode()
        self.assertEqual((r.agent_key, r.agent_name, r.session_id, r.job), (self.RID, self.name, self.RID, "J"))
        self.assertIn("step 29", text)
        self.assertIn('"type": "swarm-memory"', text)
