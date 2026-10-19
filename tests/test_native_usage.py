"""Native accounting uses final own-session evidence without repricing old ledger rows."""
import contextlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import support  # noqa: F401
from swarm.supervisor import runner


def transcript(*, key="thread-12345678", complete=True, usage=None):
    usage = usage or {"input_tokens": 3000000, "cached_input_tokens": 2800000,
                      "output_tokens": 40000, "total_tokens": 3040000}
    events = [
        {"type": "session_meta", "payload": {"id": key}},
        {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "turn-1"}},
        {"type": "event_msg", "payload": {"type": "token_count", "info": {
            "total_token_usage": usage}}},
    ]
    if complete:
        events.append({"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "turn-1",
                      "error": {"codex_error_info": "session_budget_exceeded"}}})
    return "\n".join(json.dumps(x) for x in events)


class NativeUsage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.run = {"board": "test", "restart_id": 1, "harness": "codex",
                    "agent_key": "thread-12345678", "codex_usd_per_mtok": 10}
        self.write("r1.tail.txt", '{"type":"turn.failed","error":{"message":"budget exhausted"}}')

        @contextlib.contextmanager
        def directory(*args, **kwargs):
            fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                yield fd
            finally:
                os.close(fd)
        patcher = mock.patch.object(runner, "_runs_fd", directory)
        patcher.start()
        self.addCleanup(patcher.stop)

    def write(self, name, text):
        (self.root / name).write_text(text)
        (self.root / name).chmod(0o600)

    def test_failed_turn_uses_final_raw_counts_and_pinned_rate(self):
        self.write("r1.transcript.jsonl", transcript())
        runner._read_drive_usage({"drive": {"codex_usd_per_mtok": 99}}, self.run)
        self.assertEqual(self.run["cost_usd"], 30.4)
        self.assertEqual(self.run["tokens_in"], 3000000)
        self.assertEqual(self.run["tokens_out"], 40000)
        self.assertEqual(self.run["cached_input_tokens"], 2800000)
        self.assertEqual(self.run["usage_source"], "final_transcript")
        self.assertEqual(self.run["cost_basis"], "flat_raw_token_proxy")
        self.write("r1.transcript.jsonl", transcript(usage={"input_tokens": 1, "cached_input_tokens": 0,
                                                        "output_tokens": 1, "total_tokens": 2}))
        runner._read_drive_usage({}, self.run)
        self.assertEqual(self.run["cost_usd"], 30.4)

    def test_unrelated_incomplete_or_invalid_evidence_keeps_reservation_estimate(self):
        bad = {"input_tokens": 10, "cached_input_tokens": 11, "output_tokens": 1, "total_tokens": 11}
        for text in (transcript(key="other-12345678"), transcript(complete=False),
                     transcript() + "\n{broken",
                     transcript().replace('"id": "thread-12345678"',
                                          '"id": "thread-12345678", "forked_from_id": "older-thread"'),
                     transcript(usage=bad), transcript(usage={**bad, "cached_input_tokens": True}),
                     transcript() + "\n" + json.dumps({"type": "event_msg", "payload": {
                         "type": "task_started", "turn_id": "turn-2"}})):
            with self.subTest(text=text):
                self.write("r1.transcript.jsonl", text)
                run = dict(self.run)
                runner._read_drive_usage({}, run)
                self.assertIsNone(run["cost_usd"])
                self.assertEqual(run.get("usage_source"), "reservation_estimate")

    def test_transcript_symlink_is_not_read(self):
        self.write("outside", transcript())
        (self.root / "r1.transcript.jsonl").symlink_to(self.root / "outside")
        runner._read_drive_usage({}, self.run)
        self.assertIsNone(self.run["cost_usd"])

    def test_finish_notice_persists_proxy_policy_and_usage_provenance(self):
        self.write("r1.transcript.jsonl", transcript())
        self.run.update(name="writer", job="J")
        runner._read_drive_usage({}, self.run)
        board = mock.Mock()
        board.restarts.return_value = []
        board.finish_restart.return_value = True
        with mock.patch.object(runner, "write_run"), mock.patch.object(runner, "_save_progress"):
            runner._finish_row_and_post(board, self.run, "failed", "supervisor")
        message = board.post.call_args.args[2]
        self.assertIn("proxy USD 30.4", message)
        self.assertIn("10/M raw", message)
        self.assertIn("cache=2800000", message)
        self.assertIn("final_transcript", message)

    def test_completed_stdout_preserves_cached_telemetry(self):
        self.write("r1.tail.txt", json.dumps({"type": "turn.completed", "usage": {
            "input_tokens": 100, "cached_input_tokens": 80, "output_tokens": 20}}))
        runner._read_drive_usage({}, self.run)
        self.assertEqual(self.run["cached_input_tokens"], 80)
        self.assertEqual(self.run["cost_usd"], 0.0012)
        self.assertEqual(self.run["usage_source"], "harness_result")

    def test_resumed_run_cannot_charge_an_unbaselined_cumulative_transcript(self):
        self.run["argv"] = ["codex", "exec", "resume", "thread-12345678", "-"]
        self.write("r1.transcript.jsonl", transcript())
        runner._read_drive_usage({}, self.run)
        self.assertIsNone(self.run["cost_usd"])
