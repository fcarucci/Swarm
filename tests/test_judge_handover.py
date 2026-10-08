"""Judge seat handover: a fresh judge of a fix round takes the seat from a judge that has completed
or died, under its own name; a live judge keeps it; `--as` another agent is still refused."""
from __future__ import annotations

from test_hooks_cli import Env  # noqa: E402  (sets sys.path)


class JudgeHandoverTests(Env):
    def setUp(self):
        super().setUp()
        self.assertEqual(self.cli("activate", "--job", "J", "--goal", "ship it")[0], 0)

    def go_dead(self, key):
        """The judge's session ended without a stop: no contact for longer than dead_minutes, its
        row still active, so it still holds the seat."""
        self.h.backdate_agent(key, joined_at=3 * 3600, last_seen=2 * 3600)

    def join_judge(self, key):
        rc, out, err = self.cli("join", "--job", "J", "--key", key, "--judge")
        return rc, out.strip(), err

    def verdict(self, name, kind="met", *extra):
        return self.cli("verdict", "--job", "J", "--as", name, *extra, kind, "because")

    def round_one_not_met(self):
        rc, first, err = self.join_judge("judge-1")
        self.assertEqual(rc, 0, err)
        rc, _, err = self.cli("verdict", "--job", "J", "--as", first, "--reason", "missing",
                              "--next", "fix it", "not_met")
        self.assertEqual(rc, 0, err)
        return first

    def test_fresh_judge_records_after_a_not_met_round(self):
        first = self.round_one_not_met()
        self.go_dead("judge-1")
        rc, second, err = self.join_judge("judge-2")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.agent("judge-2").name, second)   # (a finished agent's name is free again)
        rc, _, err = self.verdict(second)
        self.assertEqual(rc, 0, err)
        with self.board() as b:
            s = b.job_status("J")
            self.assertEqual((s.verdict, s.verdict_by, s.judge), ("met", second, second))

    def test_impersonation_is_still_refused(self):
        self.round_one_not_met()
        self.go_dead("judge-1")
        self.join_judge("judge-2")
        self.cli("join", "--job", "J", "--key", "worker-1")
        worker = self.agent("worker-1").name
        rc, _, err = self.verdict(worker)
        self.assertEqual(rc, 1)
        self.assertIn("not the judge", err)

    def test_a_live_judge_cannot_be_displaced(self):
        first = self.round_one_not_met()
        self.h.backdate_agent("judge-1", joined_at=3600, last_seen=10 * 60)   # idle, not dead: still live
        rc, _, err = self.join_judge("judge-2")
        self.assertEqual(rc, 1)
        self.assertIn("already the judge", err)
        with self.board() as b:
            self.assertEqual(b.job_status("J").judge, first)
