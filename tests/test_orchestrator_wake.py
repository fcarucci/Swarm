"""The orchestrator wake (spec requirement 2, slice b): one `swarm await --job J` per orchestrating session per open
job it owns, re-armed by the orchestrator's own hook after every exit, shown by `swarm doctor` as armed or NOT ARMED.

lib/swarm/supervisor/orchestrator_wake.py (the lock, the owned jobs, the hook's lines), hooks._orchestrator_wake and
bootstrap._orchestrator_wake_checks. A stub await stands in for `swarm await` (slice a): it only takes the lock.

Run: cd tests && python3 -B -m unittest test_orchestrator_wake
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from support import base_config  # noqa: F401  (puts lib/ on sys.path)

from swarm import bootstrap, hooks
from swarm.supervisor import orchestrator_wake as ow
from test_hooks_cli import Env

SESSION = "11111111-2222-3333-4444-555555555555"
OTHER = "66666666-7777-8888-9999-000000000000"


class OrchestratorWakeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="swarm-ow-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.cfg = base_config()
        self.cfg["hook"]["marker_dir"] = str(self.tmp / "markers")
        # no drive ledger unless a test says so
        patcher = mock.patch.object(ow.session_driver, "unfinished", return_value=[])
        self.unfinished = patcher.start()
        self.addCleanup(patcher.stop)
        hooks._OUTPUT.clear()
        self.addCleanup(hooks._OUTPUT.clear)

    def activate(self, job, session=SESSION, **extra):
        d = self.tmp / "markers"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{job}.json").write_text(json.dumps({"job": job, "session_id": session, **extra}))

    def test_no_active_job_is_a_no_op(self):
        with mock.patch.object(ow.session_driver, "unfinished") as unfinished:
            self.assertEqual(ow.owned_pairs(self.cfg), [])
            self.assertEqual(ow.pending_lines(self.cfg, SESSION), [])
            hooks._orchestrator_wake("turn", self.cfg, SESSION)
            unfinished.assert_not_called()
        self.assertEqual(hooks._OUTPUT, {})
        self.assertEqual(bootstrap._orchestrator_wake_checks(self.cfg), [])

    def test_an_unarmed_owned_job_is_named_on_the_hook(self):
        self.activate("J1")
        self.assertEqual(ow.owned_pairs(self.cfg), [(SESSION, "J1")])
        hooks._orchestrator_wake("turn", self.cfg, SESSION)
        text = hooks._OUTPUT["hookSpecificOutput"]["additionalContext"]
        self.assertIn("orchestrator wake NOT ARMED for J1", text)
        self.assertIn("swarm await --job J1", text)

    def test_only_the_owning_session_is_told(self):
        self.activate("J1", session=SESSION)
        self.assertEqual(ow.pending_lines(self.cfg, OTHER), [])
        hooks._orchestrator_wake("turn", self.cfg, OTHER)
        self.assertEqual(hooks._OUTPUT, {})

    def test_a_running_await_arms_the_job_and_silences_the_hook(self):
        self.activate("J1")
        fd = ow.hold(SESSION, "J1")
        self.assertIsNotNone(fd)
        self.addCleanup(os.close, fd)
        self.assertTrue(ow.armed(SESSION, "J1"))
        self.assertEqual(ow.pending_lines(self.cfg, SESSION), [])
        hooks._orchestrator_wake("turn", self.cfg, SESSION)
        self.assertEqual(hooks._OUTPUT, {})

    def test_re_arm_after_the_await_exits(self):
        self.activate("J1")
        fd = ow.hold(SESSION, "J1")
        self.assertEqual(ow.pending_lines(self.cfg, SESSION), [])
        os.close(fd)                       # the await exited: its lock is released
        self.assertFalse(ow.armed(SESSION, "J1"))
        self.assertEqual(len(ow.pending_lines(self.cfg, SESSION)), 1)

    def test_no_second_instance_while_one_runs(self):
        self.activate("J1")
        fd = ow.hold(SESSION, "J1")
        self.addCleanup(os.close, fd)
        self.assertIsNone(ow.hold(SESSION, "J1"))     # a second await exits at once
        self.assertIsNotNone(ow.hold(SESSION, "J2"))  # another job of the same session is its own wake
        self.assertIsNotNone(ow.hold(OTHER, "J1"))    # and another session's await of the same job too

    def test_drive_jobs_are_left_to_their_session_driver(self):
        self.activate("D1")
        self.activate("J1")
        self.unfinished.return_value = [SimpleNamespace(job="D1")]
        self.assertEqual(ow.owned_pairs(self.cfg), [(SESSION, "J1")])
        self.assertEqual([c.detail for c in bootstrap._orchestrator_wake_checks(self.cfg)],
                         ["orchestrator wake: NOT ARMED for J1: the orchestrating session has no "
                          "`swarm await --job J1` running"])

    def test_unreadable_drive_ledgers_arm_nothing(self):
        self.activate("J1")
        self.unfinished.return_value = None
        self.assertEqual(ow.owned_pairs(self.cfg), [])
        hooks._orchestrator_wake("turn", self.cfg, SESSION)
        self.assertEqual(hooks._OUTPUT, {})

    def test_resume_markers_are_not_owned(self):
        self.activate("J1", resume={"agent_key": None})
        self.assertEqual(ow.owned_pairs(self.cfg), [])

    def test_doctor_reports_armed_and_not_armed_per_job(self):
        self.activate("J1")
        self.activate("J2", session=OTHER)
        fd = ow.hold(OTHER, "J2")
        self.addCleanup(os.close, fd)
        by_detail = {c.detail: c.ok for c in bootstrap._orchestrator_wake_checks(self.cfg)}
        self.assertEqual(by_detail, {
            "orchestrator wake: armed for J2": True,
            "orchestrator wake: NOT ARMED for J1: the orchestrating session has no "
            "`swarm await --job J1` running": None,
        })

    def test_the_lock_name_is_per_session_and_job(self):
        self.assertNotEqual(ow.lock_name(SESSION, "J1"), ow.lock_name(SESSION, "J2"))
        self.assertNotEqual(ow.lock_name(SESSION, "J1"), ow.lock_name(OTHER, "J1"))
        self.assertTrue(ow.lock_name(SESSION, "J1").startswith("orchestrator-"))


class WakeHookIntegrationTests(Env):
    def test_enabled_hook_rearms_after_the_background_await_exits(self):
        self.cli("activate", "--job", "J", "--session", SESSION)
        with mock.patch.dict(os.environ, {"SWARM_SESSION_DRIVER": "1"}):
            for _ in range(2):
                text = self.context(self.hook("turn", agent_id=None, session=SESSION))
                self.assertIn("orchestrator wake NOT ARMED for J", text)
            fd = ow.hold(SESSION, "J")
            self.assertIsNotNone(fd)
            try:
                self.assertIsNone(self.hook("turn", agent_id=None, session=SESSION))
            finally:
                os.close(fd)
            text = self.context(self.hook("turn", agent_id=None, session=SESSION))
            self.assertIn("orchestrator wake NOT ARMED for J", text)

    def test_disabled_session_automation_preserves_silent_tool_hooks(self):
        self.cli("activate", "--job", "J", "--session", SESSION)
        with mock.patch.dict(os.environ, {"SWARM_SESSION_DRIVER": "0"}):
            self.assertIsNone(self.hook("turn", agent_id=None, session=SESSION))
