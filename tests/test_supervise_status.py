"""What status and watch show about the supervisor."""
from __future__ import annotations

import os
from unittest import mock

from test_hooks_cli import Env

from swarm import cli as swarm


class StatusTests(Env):
    def setUp(self):
        super().setUp()
        self.config.write_text(self.config.read_text() + "\n[supervise]\nenabled = true\n")
        self.cfg = swarm.load_config(self.config)
        self.cli("activate", "--job", "J")
        with self.board() as b:
            self.name = b.allocate_name("orig", "J")
            b.close_agent("orig", "stuck:silent")
            r = b.record_restart("J", "orig", "orig", "stuck:silent", "claude", 60.0)
            b.claim_resume("rep", "orig", "J")
            b.set_restart_agent(r.id, "rep")

    def test_job_detail_shows_reason_restart_and_caps(self):
        rc, out, _ = self.cli("status", "--job", "J", "--all-agents", "--no-color")
        self.assertIn("stuck:silent", out)
        self.assertIn("started (restarted ×1)", out)
        self.assertRegex(out, r"supervise  on: restarts 1/6, minutes \d+/180")

    def test_no_supervise_job_line(self):
        with self.board() as b:
            b.set_job_supervise("J", False)
        _, out, _ = self.cli("status", "--job", "J", "--no-color")
        self.assertIn("supervise  off for this job (--no-supervise)", out)

    def test_overview_footer(self):
        _, out, _ = self.cli("status", "--no-color")
        self.assertRegex(out, r"supervisor: on, today \d+/480 restart minutes on this host")

    def test_disabled_shows_nothing_without_restarts(self):
        self.config.write_text(self.config.read_text().replace("enabled = true", "enabled = false"))
        self.cli("activate", "--job", "K")
        _, out, _ = self.cli("status", "--job", "K", "--no-color")
        self.assertNotIn("supervise", out)
        _, out, _ = self.cli("status", "--no-color")
        self.assertNotIn("supervisor:", out)

    def test_watch_frame_has_the_cells(self):
        with self.board() as b:
            text = swarm.agents_table(b, "J", False, b.now())
        self.assertIn("(restarted ×1)", text)

    def test_watch_job_head_shows_the_real_supervise_state(self):
        """Through the real watch/job-detail path (_watch_frame), not agents_table directly:
        the job head must say supervise is on, the same as `status --job J` does."""
        view = {"offset": 0, "wrap": False, "max_offset": 0, "all_agents": False,
                "anchor": None, "scroll": 0, "mark": None, "page": 1, "recent_minutes": None}
        sup = swarm._sup_or_none(self.cfg)
        with self.board() as b, mock.patch("shutil.get_terminal_size",
                                           return_value=os.terminal_size((200, 30))):
            lines = swarm._watch_frame(b, "J", 2.0, False, True, view, sup=sup)
        text = "\n".join(lines)
        self.assertIn("supervise  on: restarts 1/6", text)
        self.assertNotIn("enabled = false", text)

    def test_status_survives_a_non_finite_setting(self):
        self.config.write_text(self.config.read_text() + 'backoff_minutes = ["inf"]\n')
        rc, out, err = self.cli("status", "--job", "J", "--no-color")
        self.assertEqual(rc, 0, err)
        rc, out, err = self.cli("status", "--no-color")
        self.assertEqual(rc, 0, err)
