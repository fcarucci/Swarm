"""Swarm installs no system timer: bootstrap (what `swarm upgrade` runs) never enables one and removes a
legacy swarm-supervise timer and service; a missing [supervise] enabled is off; lib/ holds no timer writer."""
from __future__ import annotations

import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import support  # noqa: F401  (puts lib/ on sys.path)
from swarm import bootstrap, paths
from swarm.supervisor import settings as st, systemd

LIB = Path(__file__).resolve().parent.parent / "lib"


class Run:
    def __init__(self):
        self.calls = []

    def __call__(self, argv, **kw):
        self.calls.append(list(argv))
        return mock.Mock(returncode=0, stdout="", stderr="")


@unittest.skipIf(sys.platform == "win32", "no systemd on Windows")
class LegacyTimerRemoval(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = {"XDG_CONFIG_HOME": self.tmp.name}
        env.pop("SWARM_NO_SYSTEMD", None)
        p = mock.patch.dict(os.environ, env)
        p.start()
        self.addCleanup(p.stop)
        os.environ.pop("SWARM_NO_SYSTEMD", None)
        self.units = Path(self.tmp.name) / "systemd" / "user"
        w = mock.patch.object(systemd.shutil, "which", return_value="/usr/bin/systemctl")
        w.start()
        self.addCleanup(w.stop)

    def plant(self):
        (self.units / "timers.target.wants").mkdir(parents=True)
        (self.units / systemd.LEGACY_TIMER).write_text("[Timer]\n")
        (self.units / systemd.LEGACY_SERVICE).write_text("[Service]\n")
        (self.units / "timers.target.wants" / systemd.LEGACY_TIMER).symlink_to(self.units / systemd.LEGACY_TIMER)

    def test_upgrade_bootstrap_step_removes_legacy_timer_and_never_enables(self):
        self.plant()
        run = Run()
        step = bootstrap.legacy_timer_step(run)
        self.assertEqual(step.status, "changed")
        self.assertIn("removed the legacy swarm-supervise.timer", step.detail)
        self.assertEqual(systemd.legacy_files(), [])
        self.assertIn(["systemctl", "--user", "disable", "--now", systemd.LEGACY_TIMER], run.calls)
        self.assertIn(["systemctl", "--user", "daemon-reload"], run.calls)
        self.assertFalse([c for c in run.calls if "enable" in c])
        self.assertFalse(list(self.units.rglob("*.timer")))

    def test_nothing_to_remove_is_silent_and_runs_nothing(self):
        run = Run()
        self.assertIsNone(bootstrap.legacy_timer_step(run))
        self.assertEqual(run.calls, [])

    def test_doctor_fails_on_a_legacy_timer(self):
        self.plant()
        checks = bootstrap.supervisor_checks({"supervise": {}}, None, run=Run())
        self.assertTrue(any(c.ok is False and c.detail == systemd.LEGACY_FOUND for c in checks))
        self.assertFalse(any("supervise timer" in c.name for c in checks))


class MissingEnabledIsOff(unittest.TestCase):
    def test_missing_enabled_is_off_even_for_an_old_config(self):
        self.assertFalse(st.settings({"supervise": {}, "_supervise_unset": True})["enabled"])
        self.assertFalse(st.settings({})["enabled"])

    def test_drive_seats_ignore_supervise_enabled(self):
        cfg = {"supervise": {"enabled": False}}
        with mock.patch.object(st, "switched_off", return_value=None):
            self.assertIsNotNone(st.off_reason(cfg))
            self.assertIsNone(st.off_reason(cfg, drive=True))


class NoTimerCodeInLib(unittest.TestCase):
    """A repo-wide guard: no code under lib/ writes a timer, plist or scheduled task, or enables one."""
    FORBIDDEN = [
        re.compile(r"\[Timer\]"), re.compile(r"OnUnitActiveSec|OnBootSec|OnCalendar"),
        re.compile(r"timers\.target\b(?!\.wants)"), re.compile(r"WantedBy="),
        re.compile(r"systemctl[\"', ]+(--user[\"', ]+)?enable"),
        re.compile(r"[\"']enable[\"'],\s*[\"']--now[\"']"),
        re.compile(r"launchctl|LaunchAgents|\.plist\b", re.I),
        re.compile(r"schtasks|Register-ScheduledTask|New-ScheduledTask", re.I),
        re.compile(r"crontab\s+-(?!l)"),
    ]

    def test_no_timer_install_code(self):
        hits = []
        for path in LIB.rglob("*.py"):
            if path.name == "shellguard.py":   # it refuses agents' crontab/at commands; it installs nothing
                continue
            for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if any(rx.search(line) for rx in self.FORBIDDEN):
                    hits.append(f"{path.relative_to(LIB)}:{n}: {line.strip()}")
        self.assertEqual(hits, [], "timer install code in lib/:\n" + "\n".join(hits))


if __name__ == "__main__":
    unittest.main()
