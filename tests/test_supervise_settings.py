"""[supervise] settings: defaults from the spec, validation, the off switch, the log."""
from __future__ import annotations

import os
import stat
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from support import base_config, home_env, posix_only  # noqa: F401  (sets sys.path)

from swarm.supervisor import settings as st


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="swarm-sup-"))
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        p = mock.patch.dict(os.environ, {**home_env(self.home)})
        p.start()
        self.addCleanup(p.stop)

    def test_spec_defaults(self):
        s = st.settings(base_config())
        self.assertEqual((s["enabled"], s["silent_minutes"], s["max_restarts_per_agent"],
                          s["max_restarts_per_job"], s["backoff_minutes"], s["max_concurrent_replacements"],
                          s["max_turns"], s["max_minutes"], s["max_restart_minutes"],
                          s["daily_restart_minutes"], s["brief_turns"], s["timer_minutes"]),
                         (False, 90, 3, 6, [2, 10, 30], 2, 60, 60, 180, 480, 40, 5))   # opt-in (B3)

    def test_user_values_override_and_are_typed(self):
        cfg = base_config()
        cfg["supervise"] = {"enabled": True, "silent_minutes": "20", "backoff_minutes": [1, 5]}
        s = st.settings(cfg)
        self.assertEqual((s["enabled"], s["silent_minutes"], s["backoff_minutes"]), (True, 20, [1, 5]))

    def test_pass_env_is_a_list_of_variable_names(self):
        self.assertEqual(st.settings(base_config())["pass_env"], [])
        cfg = base_config()
        cfg["supervise"] = {"pass_env": ["ANTHROPIC_API_KEY"]}
        self.assertEqual(st.settings(cfg)["pass_env"], ["ANTHROPIC_API_KEY"])
        for bad in ("ANTHROPIC_API_KEY", ["A=B"], ["1X"], [""], [3], ["A B"]):
            with self.subTest(bad=bad):
                cfg["supervise"] = {"pass_env": bad}
                with self.assertRaises(st.SettingsError):
                    st.settings(cfg)

    def test_bypass_permission_mode_is_refused(self):
        cfg = base_config()
        cfg["supervise"] = {"claude_permission_mode": "bypassPermissions"}
        with self.assertRaises(st.SettingsError):
            st.settings(cfg)

    def test_bad_numbers_are_refused(self):
        for bad in ({"max_minutes": -1}, {"backoff_minutes": []}, {"silent_minutes": "x"},
                    {"codex_token_limit": -5}, {"max_budget_usd": -1}, {"enabled": "false"}):
            cfg = base_config()
            cfg["supervise"] = bad
            with self.assertRaises(st.SettingsError, msg=bad):
                st.settings(cfg)

    def test_non_finite_numbers_are_refused(self):
        for bad in ({"backoff_minutes": ["inf"]}, {"backoff_minutes": ["nan"]},
                    {"backoff_minutes": [float("inf")]}, {"backoff_minutes": [float("nan")]},
                    {"max_budget_usd": "inf"}, {"max_budget_usd": float("inf")},
                    {"max_budget_usd": float("nan")}, {"silent_minutes": float("inf")},
                    {"max_turns": float("nan")}):
            cfg = base_config()
            cfg["supervise"] = bad
            with self.assertRaises(st.SettingsError, msg=bad):
                st.settings(cfg)

    def test_brief_cap_below_the_minimum_is_refused(self):
        cfg = base_config()
        for cap in (0, 100, st.MIN_BRIEF_CHARS - 1):
            cfg["supervise"] = {"brief_max_chars": cap}
            with self.assertRaises(st.SettingsError, msg=cap) as cm:
                st.settings(cfg)
            self.assertIn(f"at least {st.MIN_BRIEF_CHARS}", str(cm.exception))
        cfg["supervise"] = {"brief_max_chars": st.MIN_BRIEF_CHARS}
        self.assertEqual(st.settings(cfg)["brief_max_chars"], st.MIN_BRIEF_CHARS)

    def test_enabled_needs_flag_and_no_off_file(self):
        cfg = base_config()
        self.assertFalse(st.enabled(cfg))   # opt-in: off until enabled
        cfg["supervise"] = {"enabled": True}
        self.assertTrue(st.enabled(cfg))
        st.off_file().parent.mkdir(parents=True, exist_ok=True)
        st.off_file().write_text("")
        self.assertFalse(st.enabled(cfg))

    def test_a_supervise_value_that_is_not_a_table_is_refused(self):
        for bad in (2, ["enabled"], "on", True):
            cfg = base_config()
            cfg["supervise"] = bad
            with self.assertRaises(st.SettingsError, msg=bad):
                st.settings(cfg)
            self.assertFalse(st.enabled(cfg), msg=bad)

    def test_log_never_writes_through_a_symlink_and_never_raises(self):
        target = self.home / "elsewhere"
        target.write_text("")
        st.log_path().parent.mkdir(parents=True, exist_ok=True)
        st.log_path().symlink_to(target)
        st.log("x")
        self.assertEqual(target.read_text(), "")

    def test_enabled_is_false_on_invalid_settings(self):
        cfg = base_config()
        cfg["supervise"] = {"enabled": True, "max_turns": "lots"}
        self.assertFalse(st.enabled(cfg))

    @posix_only("needs POSIX file modes (Windows has ACLs)")
    def test_log_appends_private_lines(self):
        st.log("closed Homer Simpson on J: stuck:dead")
        st.log("second")
        lines = st.log_path().read_text().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertRegex(lines[0], r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d closed Homer Simpson on J: stuck:dead$")
        self.assertEqual(stat.S_IMODE(st.log_path().stat().st_mode), 0o600)

    @posix_only("needs POSIX file modes (Windows has ACLs)")
    def test_state_roundtrip_private(self):
        self.assertEqual(st.load_state(), {})
        st.save_state({"last_run_at": "x"})
        self.assertEqual(st.load_state(), {"last_run_at": "x"})
        self.assertEqual(stat.S_IMODE(st.state_path().stat().st_mode), 0o600)
