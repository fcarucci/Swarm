from __future__ import annotations

import contextlib
import os
import unittest
from unittest import mock

from support import home_env  # noqa: E402
from test_hooks_cli import Env  # noqa: E402
import codex_fixtures as F  # noqa: E402

from swarm import transcript_view as tv, transcripts as T  # noqa: E402


class CodexViewTests(unittest.TestCase):
    def test_codex_rollout_renders_turn_kinds(self):
        items = tv.turns(F.rollout("root").read_text())
        kinds = {t.kind for t in items}
        self.assertTrue({"user", "assistant", "tool call", "tool result"} <= kinds, kinds)
        calls = [t.label for t in items if t.kind == "tool call"]
        self.assertIn("spawn_agent", calls)
        self.assertIn("apply_patch", calls)
        self.assertFalse(any("<environment_context>" in t.text for t in items))

    def test_codex_tool_output_items_render_as_text_and_image(self):
        import json
        lines = [
            {"type": "session_meta", "payload": {"id": "x", "source": "exec"}},
            {"type": "response_item", "payload": {"type": "function_call", "name": "view_image", "call_id": "c1",
                                                  "arguments": "{}"}},
            {"type": "response_item", "payload": {"type": "function_call_output", "call_id": "c1", "output": [
                {"type": "input_text", "text": "looked at pixel.png"},
                {"type": "input_image", "image_url": "data:application/octet-stream;base64,AAAA", "detail": "auto"}]}},
            {"type": "response_item", "payload": {"type": "function_call_output", "call_id": "c1",
                                                  "output": {"content": [{"type": "output_text", "text": "wrapped"}]}}},
        ]
        items = tv.turns("\n".join(json.dumps(l) for l in lines))
        results = [t.full for t in items if t.kind == "tool result"]
        self.assertEqual(results, ["looked at pixel.png\n[image]", "wrapped"])
        self.assertTrue(all("[input_" not in t.full for t in items))

    def test_fixture_view_image_result_shows_image_label(self):
        items = tv.turns(F.rollout("root").read_text())
        viewed = [t for t in items if t.kind == "tool result" and t.label == "view_image"]
        self.assertTrue(viewed, "the fixture has a view_image result")
        self.assertIn("[image]", viewed[0].full)
        self.assertNotIn("[input_image]", viewed[0].full)

    def test_fixture_view_image_and_apply_patch_items_match_their_provenance(self):
        """Pins the exact wire shapes recorded in make_fixtures.py's PROVENANCE section (checked
        against the Codex 0.157.1 source at rust-v0.157.1), so a future merge/rebase conflict
        resolution can't silently regress the fixture back to a guessed shape."""
        import json
        lines = [json.loads(l) for l in F.rollout("root").read_text().splitlines()]
        view_call = next(e for e in lines if e["payload"].get("call_id") == "call_synthetic_021"
                         and e["payload"]["type"] == "function_call")
        view_out = next(e for e in lines if e["payload"].get("call_id") == "call_synthetic_021"
                        and e["payload"]["type"] == "function_call_output")
        patch_call = next(e for e in lines if e["payload"].get("call_id") == "call_synthetic_022"
                          and e["payload"]["type"] == "custom_tool_call")
        patch_out = next(e for e in lines if e["payload"].get("call_id") == "call_synthetic_022"
                         and e["payload"]["type"] == "custom_tool_call_output")
        # view_image.rs:239-254 to_response_item: ContentItems([InputImage{..}]) only -- no
        # sibling input_text -- with detail defaulting to DEFAULT_IMAGE_DETAIL = "high"
        # (protocol/src/models.rs:928-935).
        self.assertEqual(view_call["payload"]["name"], "view_image")
        output = view_out["payload"]["output"]
        self.assertEqual(len(output), 1, output)
        self.assertEqual(output[0]["type"], "input_image")
        self.assertEqual(output[0]["detail"], "high")
        self.assertTrue(output[0]["image_url"].startswith("data:application/octet-stream;base64,"))
        # apply_patch.rs:376 ToolPayload::Custom{input}: a custom_tool_call (not function_call)
        # with a raw-string `input` (not JSON `arguments`); its output collapses to a bare string
        # (core/src/tools/context.rs:326-333, 578-600; protocol/src/models.rs:2249-2260).
        self.assertEqual(patch_call["payload"]["name"], "apply_patch")
        self.assertIsInstance(patch_call["payload"]["input"], str)
        self.assertNotIn("arguments", patch_call["payload"])
        self.assertIsInstance(patch_out["payload"]["output"], str)

    def test_claude_rendering_unchanged(self):
        line = '{"type": "user", "message": {"content": "hi"}, "timestamp": "2026-09-26T10:00:00Z"}'
        self.assertEqual([(t.kind, t.text) for t in tv.turns(line)], [("user", "hi")])


class CodexCaptureTests(Env):
    def setUp(self):
        super().setUp()
        self.codex_home = F.staged(self.tmp)
        p = mock.patch.dict(os.environ, {"CODEX_HOME": str(self.codex_home)}); p.start(); self.addCleanup(p.stop)
        # in the parsed dict (direct T.* calls) and on disk (self.cli() re-parses self.config)
        with open(self.config, "a") as fh:
            fh.write("[transcripts]\nenabled = true\n")
        from swarm.cli import load_config
        self.cfg = load_config(self.config)

    # The local enrolment records the unsandboxed hook writes; capture trusts only them.
    def enrol_job(self, sid: str, job: str = "fixture"):
        from swarm import enrolment
        from swarm.board.autoinit import store_key
        enrolment.write_job(store_key(self.cfg), job=job, harness="codex", session_id=sid,
                            cwd=str(self.tmp), now=self.fixture_start().timestamp())

    def enrol(self, key: str, job: str = "fixture", sid: str | None = None):
        from swarm import enrolment
        from swarm.board.autoinit import store_key
        enrolment.write(store_key(self.cfg), job=job, agent_key=key, harness="codex",
                        session_id=sid, cwd=str(self.tmp))

    def fixture_start(self):
        import datetime as dt, json
        ts = json.loads(F.rollout("root").read_text().splitlines()[0])["timestamp"]
        return dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))

    def test_session_transcript_finds_codex_root(self):
        path, harness = T.session_transcript(F.ids()["root"]["session"])
        self.assertEqual(harness, "codex")
        self.assertTrue(path.exists())

    def test_capture_job_with_codex_orchestrator_and_agent(self):
        sid = F.payloads("SubagentStart")[0]["session_id"]
        child = F.ids()["child"]["thread"]
        self.cli("activate", "--job", "fixture", "--session", sid)
        self.enrol_job(sid)
        with self.board() as b:
            b.allocate_name(child, "fixture")
            b.set_agent_runtime(child, "codex", "m")
        self.enrol(child)
        self.h.update_job("fixture", activated_at=self.fixture_start(), created_at=self.fixture_start())
        with self.board() as b:
            n = T.capture_job(b, self.cfg, "fixture", True)
            rows = {r.agent_key: r for r in b.transcripts(job="fixture")}
        self.assertGreaterEqual(n, 2)
        self.assertEqual(rows["orchestrator"].harness, "codex")
        self.assertEqual(rows[child].harness, "codex")

    def test_capture_job_captures_owned_agent_when_root_rollout_is_missing(self):
        """The root/orchestrator transcript not being found (not yet written, or this session
        genuinely isn't on this machine) must not block capturing an agent whose own rollout is
        here: agent_transcript (Codex: found by the agent's own key) does not need `main`."""
        child = F.ids()["child"]["thread"]
        self.cli("activate", "--job", "fixture", "--session", "no-such-session-00000000")
        self.enrol_job("no-such-session-00000000")
        with self.board() as b:
            b.allocate_name(child, "fixture")
            b.set_agent_runtime(child, "codex", "m")
        self.enrol(child)
        self.h.update_job("fixture", activated_at=self.fixture_start(), created_at=self.fixture_start())
        with self.board() as b:
            n = T.capture_job(b, self.cfg, "fixture", True)
            rows = {r.agent_key: r for r in b.transcripts(job="fixture")}
        self.assertEqual(n, 1)
        self.assertNotIn("orchestrator", rows)          # no root rollout: nothing to slice
        self.assertEqual(rows[child].harness, "codex")  # the agent's own rollout was still captured

    def test_run_snapshots_captures_owned_agent_when_root_rollout_is_missing(self):
        child = F.ids()["child"]["thread"]
        self.cli("activate", "--job", "fixture", "--session", "no-such-session-00000000")
        self.enrol_job("no-such-session-00000000")
        with self.board() as b:
            b.allocate_name(child, "fixture")
            b.set_agent_runtime(child, "codex", "m")
        self.enrol(child)
        self.h.update_job("fixture", activated_at=self.fixture_start(), created_at=self.fixture_start())
        with self.board() as b:
            n = T.run_snapshots(b, self.cfg)
            rows = {r.agent_key: r for r in b.transcripts(job="fixture")}
        self.assertEqual(n, 1)
        self.assertNotIn("orchestrator", rows)
        self.assertEqual(rows[child].harness, "codex")

    def test_codex_view_image_is_stored_and_restored(self):
        from make_fixtures import SYNTH_B64
        text = F.rollout("root").read_text()
        self.assertIn(SYNTH_B64, text, "the fixture must carry a view_image image (make_fixtures.py keeps one)")
        sid = F.payloads("SubagentStart")[0]["session_id"]
        self.cli("activate", "--job", "fixture", "--session", sid)
        self.enrol_job(sid)
        self.h.update_job("fixture", activated_at=self.fixture_start(), created_at=self.fixture_start())
        with self.board() as b:
            T.capture_job(b, self.cfg, "fixture", True)
            imgs = b.transcript_images(job="fixture", agent_key="orchestrator")
            self.assertEqual([i.mime for i in imgs], ["image/png"])
            body = b.transcript_body("fixture", "orchestrator").decode()   # transcript_body is already decompressed
            self.assertNotIn(SYNTH_B64, body)                                  # stored once, out of the text
            restored = T.restore_images(body, lambda sha: getattr(b.transcript_image(sha), "data", None))
        self.assertIn(SYNTH_B64, restored)

    def test_octet_stream_data_url_image_round_trips_byte_exact(self):
        import base64, json
        from make_fixtures import SYNTH_B64
        text = json.dumps({"output": [{"type": "input_image",
                                       "image_url": "data:application/octet-stream;base64," + SYNTH_B64}]}) + "\n"
        stripped, imgs = T.extract_images(text)
        self.assertEqual([i.mime for i in imgs], ["image/png"])            # sniffed, not the URL's type
        self.assertEqual(T.restore_images(stripped, {imgs[0].sha256: imgs[0].data}.get), text)
        junk = json.dumps({"u": "data:application/octet-stream;base64," + base64.b64encode(b"\0" * 600).decode()})
        self.assertEqual(T.extract_images(junk), (junk, []))                 # not an image: stays inline

    def test_corrupt_zst_is_logged_and_nothing_stored(self):
        child = F.ids()["child"]["thread"]
        bad = self.tmp / "bad.jsonl.zst"; bad.write_bytes(b"garbage")
        with self.board() as b:
            b.ensure_job("fixture")
            self.assertFalse(T.capture_subagent(b, self.cfg, "fixture", child, bad, True, harness="codex"))
            self.assertEqual(b.transcripts(job="fixture", agent_key=child), [])

    def _owned_codex_agent_with_stop_capture(self):
        """The codex user's agent: enrolled, one turn ended, its SubagentStop capture stored (non-final)."""
        sid = F.payloads("SubagentStart")[0]["session_id"]
        child = F.ids()["child"]["thread"]
        self.cli("activate", "--job", "fixture", "--session", sid)
        self.enrol_job(sid)
        self.h.update_job("fixture", activated_at=self.fixture_start(), created_at=self.fixture_start())
        with self.board() as b:
            b.allocate_name(child, "fixture")
            b.set_agent_runtime(child, "codex", "m")
        self.enrol(child)
        with self.board() as b:
            b.agent_turn_ended(child)
        self.h.update_agent(child, os_user="codex")
        # a real rollout-*-<thread>.jsonl under CODEX_HOME/sessions (F.staged in setUp put it
        # there): transcript_ok (lane-harden) requires exactly that shape and location, so a
        # scratch copy elsewhere would be refused as "not a transcript of this agent".
        from swarm.hosts.codex import find_rollout
        self.child_path = find_rollout(child)
        self.assertIsNotNone(self.child_path, "the child rollout must be staged under CODEX_HOME")
        with mock.patch("getpass.getuser", return_value="codex"), self.board() as b:
            T.capture_subagent(b, self.cfg, "fixture", child, self.child_path, False, harness="codex")
        return child

    def _row(self, child):
        with self.board() as b:
            return b.transcripts(job="fixture", agent_key=child)[0]

    def _as_claude_user(self, stack: contextlib.ExitStack) -> None:
        """Become the other OS user: its own empty CODEX_HOME, and any transcript write fails."""
        from swarm.board import backend_class
        board_cls = backend_class(self.cfg)
        empty = self.tmp / "claude-codex-home"; empty.mkdir(exist_ok=True)
        forbid = AssertionError("a non-owner touched a transcript row")
        other_home = self.tmp / "claude-home"; other_home.mkdir(exist_ok=True)
        for p_ in (mock.patch.dict(os.environ, {"CODEX_HOME": str(empty), **home_env(other_home)}),
                   mock.patch("getpass.getuser", return_value="claude"),
                   mock.patch.object(T, "capture_subagent", side_effect=forbid),
                   mock.patch.object(board_cls, "refresh_transcript", side_effect=forbid),
                   mock.patch.object(board_cls, "save_transcript", side_effect=forbid)):
            stack.enter_context(p_)

    def test_non_owner_sweep_completes_agent_but_never_touches_its_row(self):
        from swarm import cli as swarm
        child = self._owned_codex_agent_with_stop_capture()
        before = self._row(child)
        self.h.backdate_agent(child, turn_ended_at=600)
        with contextlib.ExitStack() as stack:
            self._as_claude_user(stack)
            with self.board() as b:
                swarm.sweep_jobs(b, self.cfg)
        self.assertEqual(self.agent(child, job="fixture").status, "completed")
        after = self._row(child)
        self.assertEqual((after.final, after.captured_at), (False, before.captured_at))

    def test_owner_sweep_finalizes_agent_another_user_completed(self):
        from swarm import cli as swarm
        child = self._owned_codex_agent_with_stop_capture()
        self.h.backdate_agent(child, turn_ended_at=600)
        with mock.patch("getpass.getuser", return_value="claude"), self.board() as b:
            b.finish_quiet_agents(60)                                 # completed by the claude user's sweep
        with mock.patch("getpass.getuser", return_value="codex"), self.board() as b:
            swarm.sweep_jobs(b, self.cfg)                             # the owner's next sweep
        self.assertTrue(self._row(child).final)

    def test_job_closed_during_quiet_window_is_finalized_by_owner(self):
        from swarm import cli as swarm
        child = self._owned_codex_agent_with_stop_capture()
        with contextlib.ExitStack() as stack:
            self._as_claude_user(stack)
            rc, _, err = self.cli("deactivate", "--job", "fixture", "--outcome", "done", "--force")
        self.assertEqual(rc, 0, err)
        self.assertFalse(self._row(child).final)                      # the closer didn't touch it
        with mock.patch("getpass.getuser", return_value="codex"), self.board() as b:
            swarm.sweep_jobs(b, self.cfg)
        self.assertTrue(self._row(child).final)

    def test_stop_hook_on_unchanged_untouched_file_refreshes_captured_at(self):
        child = self._owned_codex_agent_with_stop_capture()
        original = self._row(child)
        self.h.backdate_transcript("fixture", child, 60)
        first = self._row(child)                                      # file bytes and mtime unchanged
        self.assertLess(first.captured_at, original.captured_at)
        sid = F.payloads("SubagentStart")[0]["session_id"]
        with mock.patch("getpass.getuser", return_value="codex"):
            self.hook("stop", agent_id=child, session=sid, host="codex", turn_id="t9", model="m",
                      agent_transcript_path=str(self.child_path))
        second = self._row(child)
        self.assertEqual(second.sha256, first.sha256)
        self.assertGreater(second.captured_at, first.captured_at)

    def test_owner_recovers_after_failed_stop_capture(self):
        from swarm import cli as swarm
        sid = F.payloads("SubagentStart")[0]["session_id"]
        child = F.ids()["child"]["thread"]
        self.cli("activate", "--job", "fixture", "--session", sid)
        self.enrol_job(sid)
        with self.board() as b:
            b.allocate_name(child, "fixture")
            b.set_agent_runtime(child, "codex", "m")
        self.enrol(child)
        with self.board() as b:
            b.agent_turn_ended(child)                                 # its stop capture failed: no row at all
        self.h.update_agent(child, os_user="codex")
        with contextlib.ExitStack() as stack:
            self._as_claude_user(stack)
            rc, _, err = self.cli("deactivate", "--job", "fixture", "--outcome", "done", "--force")
        self.assertEqual(rc, 0, err)
        with self.board() as b:
            self.assertEqual(b.transcripts(job="fixture", agent_key=child), [])
        with mock.patch("getpass.getuser", return_value="codex"), self.board() as b:
            swarm.sweep_jobs(b, self.cfg)                             # the owner's next sweep
        self.assertTrue(self._row(child).final)

    def test_finalize_skips_an_unenrolled_codex_agent_the_board_says_is_ours(self):
        """Host and os_user on the row are this machine and user, and its rollout is here,
        but this host user never enrolled it: finalize_owned leaves it alone."""
        import getpass
        child = F.ids()["child"]["thread"]
        with self.board() as b:
            b.ensure_job("fixture")
            b.allocate_name(child, "fixture")
            b.set_agent_runtime(child, "codex", "m")
            b.agent_stopped(child)
        self.h.update_agent(child, host=T._host(), os_user=getpass.getuser())
        with self.board() as b:
            self.assertEqual(T.finalize_owned(b, self.cfg), 0)
            self.assertEqual(b.transcripts(job="fixture", agent_key=child), [])
        self.enrol(child, job="other-job")                  # a record for another job is not enough
        with self.board() as b:
            self.assertEqual(T.finalize_owned(b, self.cfg), 0)
        self.enrol(child)
        with self.board() as b:
            self.assertEqual(T.finalize_owned(b, self.cfg), 1)

    def test_list_shows_host_column(self):
        sid = F.payloads("SubagentStart")[0]["session_id"]
        self.cli("activate", "--job", "fixture", "--session", sid)
        self.enrol_job(sid)
        self.h.update_job("fixture", activated_at=self.fixture_start(), created_at=self.fixture_start())
        with self.board() as b:
            T.capture_job(b, self.cfg, "fixture", True)
        rc, out, _ = self.cli("transcript", "list", "--job", "fixture")
        self.assertEqual(rc, 0)
        self.assertIn("HOST", out)
        self.assertIn("codex", out)


if __name__ == "__main__":
    unittest.main()
