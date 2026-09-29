"""Transcripts of a restarted agent: both runs, keyed, readable by the replacement."""
from __future__ import annotations

from test_transcript_cli import TranscriptEnv, jsonl


class BothRunsTests(TranscriptEnv):
    def setUp(self):
        super().setUp()
        self.cli("activate", "--job", "J")
        with self.board() as b:
            self.name = b.allocate_name("orig", "J")
            b.close_agent("orig", "stuck:dead")
            b.claim_resume("rep-1", "orig", "J")
        self.seed("J", "orig", self.name, jsonl(("user", "the task"), ("assistant", "step one done")))
        self.seed("J", "rep-1", self.name, jsonl(("user", "resume brief"), ("assistant", "step two done")))

    def test_show_by_agent_prints_both_runs_in_order(self):
        rc, out, err = self.cli("transcript", "show", "--job", "J", "--agent", self.name)
        self.assertEqual(rc, 0, err)
        first, second = out.index("run 1 of 2: key orig"), out.index("run 2 of 2: key rep-1")
        self.assertLess(first, second)
        self.assertIn("replaces orig", out)
        self.assertLess(out.index("step one done"), out.index("step two done"))

    def test_show_by_key_is_one_run(self):
        rc, out, _ = self.cli("transcript", "show", "--job", "J", "--key", "orig", "--tail", "1")
        self.assertEqual(rc, 0)
        self.assertIn("step one done", out)
        self.assertNotIn("step two done", out)
        self.assertNotIn("===", out)

    def test_grep_applies_per_run(self):
        rc, out, _ = self.cli("transcript", "show", "--job", "J", "--agent", self.name, "--grep", "two")
        self.assertIn("step two done", out)
        self.assertNotIn("step one done", out)

    def test_list_has_replaces_column(self):
        rc, out, _ = self.cli("transcript", "list", "--job", "J")
        header, *rows = out.splitlines()
        self.assertIn("REPLACES", header)
        rep = next(r for r in rows if r.rstrip().endswith("rep-1") or " rep-1 " in r)
        self.assertIn("orig", rep)

    def test_across_jobs_still_asks_for_job_and_shows_keys(self):
        self.seed("K", "other", self.name, jsonl(("user", "x")))
        rc, _, err = self.cli("transcript", "show", "--agent", self.name)
        self.assertEqual(rc, 1)
        self.assertIn("pick one with --job or --key", err)
        self.assertIn("rep-1", err)


class RunOrderTests(TranscriptEnv):
    """Runs follow the resume_of chain, not capture time: the original's transcript may be
    re-captured (final) after its replacement's first capture."""

    def test_chain_order_beats_capture_time(self):
        self.cli("activate", "--job", "J")
        with self.board() as b:
            name = b.allocate_name("orig", "J")
            b.close_agent("orig", "stuck:dead")
            b.claim_resume("rep-1", "orig", "J")
            b.close_agent("rep-1", "stuck:silent")
            b.claim_resume("rep-2", "rep-1", "J")
        self.seed("J", "rep-2", name, jsonl(("user", "brief 2"), ("assistant", "third run")), captured_days_ago=3)
        self.seed("J", "rep-1", name, jsonl(("user", "brief 1"), ("assistant", "second run")), captured_days_ago=2)
        self.seed("J", "orig", name, jsonl(("user", "the task"), ("assistant", "first run")))
        rc, out, err = self.cli("transcript", "show", "--job", "J", "--agent", name)
        self.assertEqual(rc, 0, err)
        self.assertLess(out.index("run 1 of 3: key orig"), out.index("run 2 of 3: key rep-1"))
        self.assertLess(out.index("run 2 of 3: key rep-1"), out.index("run 3 of 3: key rep-2"))
        self.assertIn("key rep-2, captured", out)
        self.assertIn("replaces rep-1", out)
        self.assertLess(out.index("first run"), out.index("second run"))
