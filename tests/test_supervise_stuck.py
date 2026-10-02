"""Stuck detection (pure) and the board-outage window."""
from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from support import base_config, home_env, posix_only  # noqa: F401

from swarm.board import AgentStatus, JobStatus
from swarm.supervisor import outage, stuck
from swarm.supervisor.settings import settings

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 9, 27, 12, 0, tzinfo=UTC)
BOARD = {"dead_minutes": 30, "idle_minutes": 5, "tool_timeout_minutes": 60}


def agent(minutes_quiet: float, *, tool=None, status="started", post_minutes=None, ended=False):
    last = NOW - dt.timedelta(minutes=minutes_quiet)
    return AgentStatus(job="J", name="Homer Simpson", role=None, status=status, current_tool=tool,
                       tool_calls=3, messages=1, joined_at=NOW - dt.timedelta(hours=2),
                       last_contact_at=last,
                       last_post_at=None if post_minutes is None else NOW - dt.timedelta(minutes=post_minutes),
                       ended_at=NOW if ended else None, host="h", agent_key="k1", harness="claude")


def job(waiting=None):
    return JobStatus(job="J", status="active", description=None, task=None, outcome=None, created_by=None,
                     session_id="s", created_at=NOW - dt.timedelta(hours=3), activated_at=None,
                     finished_at=None, agents=1, started=0, running=0, idle=0, completed=0,
                     dead_or_left=0, messages=1, last_activity_at=None, waiting_on=waiting)


def sup_config(**supervise) -> dict:
    cfg = base_config()
    cfg["supervise"] = supervise
    return cfg


class StuckReasonTests(unittest.TestCase):
    # silent_minutes 45 (the spec's figure) so the silent rule fires before the tool timeout
    sup = settings(sup_config(silent_minutes=45))

    def reason(self, a, js=None, out=None):
        return stuck.stuck_reason(a, js or job(), NOW, BOARD, self.sup, out)

    def test_dead(self):
        self.assertEqual(self.reason(agent(31, status="dead")), "dead")
        self.assertIsNone(self.reason(agent(29, status="idle")))

    def test_tool_past_timeout(self):
        self.assertEqual(self.reason(agent(61, tool="Bash", status="dead")), "tool")

    def test_silent_long_tool_call(self):
        self.assertEqual(self.reason(agent(46, tool="Bash", status="running")), "silent")
        self.assertIsNone(self.reason(agent(44, tool="Bash", status="running")))

    def test_recent_post_is_not_silent(self):
        self.assertIsNone(self.reason(agent(46, tool="Bash", status="running", post_minutes=10)))

    def test_waiting_job_is_never_silent(self):
        self.assertIsNone(self.reason(agent(50, tool="Bash", status="running"), job(waiting="ci run")))

    def test_supervisor_wait_does_not_hide_silence(self):
        w = stuck.WAITING_PREFIX + "Bart Simpson"
        self.assertEqual(self.reason(agent(50, tool="Bash", status="running"), job(waiting=w)), "silent")

    def test_default_silent_minutes_is_90(self):
        sup = settings(base_config())
        a = agent(50, tool="Bash", status="running")
        self.assertIsNone(stuck.stuck_reason(a, job(), NOW, BOARD, sup))
        a = agent(91, tool="Bash", status="running")
        self.assertEqual(stuck.stuck_reason(a, job(), NOW, BOARD, sup), "silent")

    def test_departed_is_not_stuck(self):
        self.assertIsNone(self.reason(agent(300, status="left", ended=True)))

    def test_outage_open_nobody_is_stuck(self):
        out = outage.Outage(NOW - dt.timedelta(minutes=40), None)
        self.assertIsNone(self.reason(agent(35, status="dead"), out=out))

    def test_outage_grace_then_dead(self):
        out = outage.Outage(NOW - dt.timedelta(minutes=90), NOW - dt.timedelta(minutes=20))
        self.assertIsNone(self.reason(agent(85, status="dead"), out=out))    # 20 min since recovery
        out = outage.Outage(NOW - dt.timedelta(minutes=90), NOW - dt.timedelta(minutes=31))
        self.assertEqual(self.reason(agent(85, status="dead"), out=out), "dead")

    def test_outage_long_ago_is_ignored(self):
        out = outage.Outage(NOW - dt.timedelta(hours=10), NOW - dt.timedelta(hours=9))
        self.assertEqual(self.reason(agent(31, status="dead"), out=out), "dead")


class OutageFileTests(unittest.TestCase):
    def setUp(self):
        home = tempfile.mkdtemp(prefix="swarm-out-")
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        p = mock.patch.dict(os.environ, {**home_env(home)})
        p.start()
        self.addCleanup(p.stop)

    @posix_only("needs POSIX file modes (Windows has ACLs)")
    def test_unreachable_then_reachable(self):
        self.assertIsNone(outage.current(NOW))
        o1 = outage.note_unreachable(NOW)
        o2 = outage.note_unreachable(NOW + dt.timedelta(minutes=2))
        self.assertEqual((o1.started, o2.started, o2.recovered), (NOW, NOW, None))
        back = outage.note_reachable(NOW + dt.timedelta(minutes=10))
        self.assertEqual(back.recovered, NOW + dt.timedelta(minutes=10))
        self.assertEqual(outage.current(NOW + dt.timedelta(hours=1)), back)
        self.assertIsNone(outage.current(NOW + dt.timedelta(hours=30)))
        self.assertEqual(oct(outage.path().stat().st_mode & 0o777), "0o600")

    def test_reachable_without_outage_writes_nothing(self):
        self.assertIsNone(outage.note_reachable(NOW))
        self.assertFalse(outage.path().exists())

    def test_new_outage_after_recovery(self):
        outage.note_unreachable(NOW)
        outage.note_reachable(NOW + dt.timedelta(minutes=5))
        o = outage.note_unreachable(NOW + dt.timedelta(hours=2))
        self.assertEqual((o.started, o.recovered), (NOW + dt.timedelta(hours=2), None))


class OrchestratorGoneTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="swarm-orc-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.cfg = base_config()
        self.cfg["hook"]["marker_dir"] = str(self.tmp)

    def _marker(self, name, job, seen_age=None, marker_age=3600):
        p = self.tmp / name
        p.write_text(json.dumps({"job": job, "session_id": "s"}))
        os.utime(p, (time.time() - marker_age,) * 2)
        if seen_age is not None:
            s = p.with_suffix(".seen")
            s.touch()
            os.utime(s, (time.time() - seen_age,) * 2)
        return p

    def test_no_marker_here_cannot_tell(self):
        self.assertIsNone(stuck.orchestrator_gone(self.cfg, "J", 30))

    def test_recent_seen_is_alive(self):
        self._marker("J.json", "J", seen_age=60)
        self.assertFalse(stuck.orchestrator_gone(self.cfg, "J", 30))

    def test_stale_or_missing_seen_is_gone(self):
        self._marker("J.json", "J", seen_age=3000)
        self.assertTrue(stuck.orchestrator_gone(self.cfg, "J", 30))
        self._marker("J--other.json", "J", seen_age=None)
        self.assertTrue(stuck.orchestrator_gone(self.cfg, "J", 30))

    def test_fresh_marker_without_seen_is_not_gone(self):
        self._marker("J.json", "J", seen_age=None, marker_age=60)
        self.assertFalse(stuck.orchestrator_gone(self.cfg, "J", 30))

    def test_resume_markers_do_not_count(self):
        self._marker("J--resume-r1.json", "J", seen_age=None)
        self.assertIsNone(stuck.orchestrator_gone(self.cfg, "J", 30))

    def test_marker_with_resume_section_does_not_count(self):
        p = self.tmp / "J--x.json"
        p.write_text(json.dumps({"job": "J", "session_id": None, "resume": {"agent_key": None}}))
        self.assertIsNone(stuck.orchestrator_gone(self.cfg, "J", 30))

    def test_other_jobs_markers_do_not_count(self):
        self._marker("K.json", "K", seen_age=60)
        self.assertIsNone(stuck.orchestrator_gone(self.cfg, "J", 30))
