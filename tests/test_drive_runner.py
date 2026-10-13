"""Runner helpers for drive seats: usage capture, failure classes, and the tri-state containment check."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from support import ROOT, posix_only  # noqa: F401  (puts lib on sys.path)

from swarm import clock
from swarm.supervisor import runner

SLICE = "app.slice"
UNIT = "swarm-r1-ab.scope"


def cres(**kw):
    return json.dumps({"type": "result", **kw})


class ParseUsageTests(unittest.TestCase):
    def test_claude_json(self):
        u = runner.parse_usage("claude", '{"type":"result","total_cost_usd":0.42,"usage":{"input_tokens":10,"output_tokens":5}}')
        self.assertEqual((u.cost_usd, u.tokens_in, u.tokens_out), (0.42, 10, 5))

    def test_claude_last_result_line_wins_and_partial_first_line_is_skipped(self):
        tail = ('ncated-line-of-an-earlier-' + cres(total_cost_usd=9.0, usage={"input_tokens": 1, "output_tokens": 1})
                + "\n" + cres(total_cost_usd=0.5, usage={"input_tokens": 7, "output_tokens": 3}) + "\n")
        u = runner.parse_usage("claude", tail)
        self.assertEqual((u.cost_usd, u.tokens_in, u.tokens_out), (0.5, 7, 3))

    def test_negative_cost_is_unreadable(self):
        self.assertIsNone(runner.parse_usage("claude", cres(total_cost_usd=-1.0, usage={})))

    def test_claude_without_numeric_cost_is_unreadable(self):
        self.assertIsNone(runner.parse_usage("claude", cres(usage={"input_tokens": 1, "output_tokens": 1})))
        self.assertIsNone(runner.parse_usage("claude", cres(total_cost_usd="0.4", usage={})))

    def test_missing_usage_charges_reservation(self):
        self.assertIsNone(runner.parse_usage("claude", "killed"))
        self.assertIsNone(runner.parse_usage("claude", ""))
        self.assertIsNone(runner.parse_usage("claude", '{"type":"result","total_cost_usd":'))   # malformed

    def test_codex_sums_turn_usage_and_has_no_cost(self):
        tail = "\n".join([
            json.dumps({"type": "thread.started", "thread_id": "t1"}),
            json.dumps({"type": "turn.completed", "usage": {"input_tokens": 100, "output_tokens": 20}}),
            json.dumps({"type": "turn.completed", "usage": {"input_tokens": 50, "output_tokens": 5}}),
        ])
        u = runner.parse_usage("codex", tail)
        self.assertEqual((u.cost_usd, u.tokens_in, u.tokens_out), (None, 150, 25))

    def test_codex_without_usage_is_none(self):
        self.assertIsNone(runner.parse_usage("codex", json.dumps({"type": "thread.started"})))


class ClassifyFailureTests(unittest.TestCase):
    def test_overload_is_outage(self):
        tail = cres(subtype="success", is_error=True, api_error_status=529, result="API Error: 529 overloaded_error")
        self.assertEqual(runner.classify_failure("claude", tail), "outage")

    def test_pure_status_5xx_is_outage_with_neutral_text(self):
        for status in (500, 503, 529, 599):
            tail = cres(subtype="success", is_error=True, api_error_status=status, result="upstream request failed")
            self.assertEqual(runner.classify_failure("claude", tail), "outage", status)

    def test_status_400_with_neutral_text_is_not_outage(self):
        self.assertIsNone(runner.classify_failure("claude", cres(is_error=True, api_error_status=400,
                                                                 result="bad request")))

    def test_auth_and_rate_limit_are_outages(self):
        self.assertEqual(runner.classify_failure("claude", cres(is_error=True, api_error_status=401,
                                                                result="authentication_error")), "outage")
        self.assertEqual(runner.classify_failure("claude", cres(is_error=True, api_error_status=429)), "outage")

    def test_codex_error_event_of_a_provider_class_is_outage(self):
        tail = json.dumps({"type": "error", "message": "stream error: server overloaded, retry later"})
        self.assertEqual(runner.classify_failure("codex", tail), "outage")

    def test_codex_pure_status_error_is_outage(self):
        self.assertEqual(runner.classify_failure("codex", json.dumps({"type": "error", "status": 503,
                                                                      "message": "request failed"})), "outage")

    def test_missing_binary_is_error_not_outage(self):
        self.assertEqual(runner.classify_failure("claude", "could not start the session: FileNotFoundError\n"), "error")
        self.assertEqual(runner.classify_failure("codex", "could not start the session: PermissionError\n"), "error")

    def test_ordinary_failures_are_none(self):
        self.assertIsNone(runner.classify_failure("claude", cres(is_error=False, result="done")))
        self.assertIsNone(runner.classify_failure("claude", cres(is_error=False, result="overloaded, retried")))
        self.assertIsNone(runner.classify_failure("claude", cres(is_error=True, subtype="error_max_turns")))
        self.assertIsNone(runner.classify_failure("codex", json.dumps({"type": "turn.completed"})))
        self.assertIsNone(runner.classify_failure("claude", ""))


class FakeClock:
    def __init__(self, m=100.0):
        self.m = m

    def now(self):
        raise AssertionError("not used")

    def monotonic(self):
        return self.m


def _cp(stdout: str, rc: int = 0):
    return subprocess.CompletedProcess(["systemctl"], rc, stdout=stdout, stderr="")


class _Tree:
    """A fake cgroup v2 tree under a temp dir; runner.CGROUP_ROOT points at it. `events` is the
    scope's cgroup.events text (None: no file)."""

    def __init__(self, uid=None, slices=(SLICE,), scope=None, events=None, v2=True):
        self.root = Path(tempfile.mkdtemp(prefix="swarm-cg-", dir=os.environ.get("SWARM_TEST_SANDBOX")))
        self.uid = os.getuid() if uid is None else uid
        if v2:
            (self.root / "cgroup.controllers").write_text("cpu memory\n")
        self.parent = self.root / "user.slice" / f"user-{self.uid}.slice" / f"user@{self.uid}.service"
        self.parent.mkdir(parents=True)
        for s in slices:
            (self.parent / s).mkdir()
        if scope is not None:
            d = self.parent / scope[0] / scope[1]
            d.mkdir(parents=True)
            if events is not None:
                (d / "cgroup.events").write_text(events)

    def __enter__(self):
        self._patch = mock.patch.object(runner, "CGROUP_ROOT", str(self.root))
        self._patch.start()
        return self

    def __exit__(self, *exc):
        self._patch.stop()
        shutil.rmtree(self.root, ignore_errors=True)


@posix_only("cgroup v2 fallback is Linux-only")
class PresenceTests(unittest.TestCase):
    def test_systemctl_error_falls_back_to_cgroupfs(self):
        with mock.patch.object(runner, "_systemctl", return_value=None), \
             mock.patch("os.path.isdir", return_value=False), mock.patch("os.access", return_value=True):
            self.assertEqual(runner.unit_presence(UNIT, slice_name=SLICE), "absent")

    def test_no_recorded_slice_is_unknown_never_a_guess(self):
        # the same layout that answers "absent" with a recorded slice: without one, no cgroup answer
        with mock.patch.object(runner, "_systemctl", return_value=None), \
             mock.patch("os.path.isdir", return_value=False), mock.patch("os.access", return_value=True):
            self.assertEqual(runner.unit_presence(UNIT), "unknown")
            self.assertEqual(runner.unit_presence(UNIT, slice_name=None), "unknown")
            self.assertEqual(runner.unit_presence(UNIT, slice_name=None, deadline=1e9), "unknown")

    def test_unreadable_is_unknown(self):
        with mock.patch.object(runner, "_systemctl", return_value=None), \
             mock.patch("os.access", return_value=False):
            self.assertEqual(runner.unit_presence(UNIT, slice_name=SLICE), "unknown")

    def test_systemctl_answers_not_loaded_is_absent_and_loaded_is_present(self):
        with mock.patch.object(runner, "_systemctl", return_value=_cp("LoadState=not-found\nActiveState=inactive\n")):
            self.assertEqual(runner.unit_presence(UNIT), "absent")
        with mock.patch.object(runner, "_systemctl", return_value=_cp("LoadState=loaded\nActiveState=active\n")):
            self.assertEqual(runner.unit_presence(UNIT), "present")

    def test_cgroup_v1_is_unknown(self):
        with _Tree(v2=False, scope=(SLICE, UNIT), events="populated 0\nfrozen 0\n"):
            with mock.patch.object(runner, "_systemctl", return_value=None):
                self.assertEqual(runner.unit_presence(UNIT, slice_name=SLICE), "unknown")

    def test_wrong_slice_is_unknown(self):
        # the run recorded session.slice, but the user manager's tree only has app.slice
        with _Tree(scope=(SLICE, UNIT), events="populated 0\nfrozen 0\n"):
            with mock.patch.object(runner, "_systemctl", return_value=None):
                self.assertEqual(runner.unit_presence(UNIT, slice_name="session.slice"), "unknown")
                self.assertEqual(runner.unit_presence(UNIT, slice_name=SLICE), "absent")

    def test_slice_name_that_is_a_path_is_refused(self):
        # a populated scope sits where "../x" resolves to: an unvalidated join would read it
        with _Tree(scope=(SLICE, UNIT), events="populated 0\nfrozen 0\n") as t:
            escaped = t.parent / "x" / UNIT
            escaped.mkdir(parents=True)
            (escaped / "cgroup.events").write_text("populated 0\nfrozen 0\n")
            with mock.patch.object(runner, "_systemctl", return_value=None):
                for bad in ("../x", "x/" + SLICE, "/abs.slice", "app.slice/..", "", "app.scope"):
                    self.assertEqual(runner.unit_presence(UNIT, slice_name=bad), "unknown", bad)

    def test_real_events_file_with_extra_keys_reads_populated(self):
        # the kernel's file is two lines (populated, frozen); newer kernels add keys: only `populated` counts
        for text, want in (("populated 1\nfrozen 0\n", "present"),
                           ("populated 0\nfrozen 1\n", "absent"),
                           ("frozen 0\npopulated 1\nnew_key 7\n", "present"),
                           ("populated 0\nfrozen 0\nnew_key 7\n", "absent")):
            with _Tree(scope=(SLICE, UNIT), events=text):
                with mock.patch.object(runner, "_systemctl", return_value=None):
                    self.assertEqual(runner.unit_presence(UNIT, slice_name=SLICE), want, text)

    def test_a_missing_or_malformed_populated_key_is_unknown(self):
        # the populated key must appear once with 0 or 1; a missing file or any other text is unknown
        for events in ("populated 1\npopulated 0\n", "populated 2\nfrozen 0\n", "frozen 0\n", "populated 1 2\n",
                       None, "populated maybe\n", "frozen 1\n", ""):
            with _Tree(scope=(SLICE, UNIT), events=events):
                with mock.patch.object(runner, "_systemctl", return_value=None):
                    self.assertEqual(runner.unit_presence(UNIT, slice_name=SLICE), "unknown", events)

    def test_unreadable_user_manager_tree_is_unknown(self):
        # the slice tree is all there, but user@<uid>.service itself is unreadable: no positive answer
        real_access = os.access

        def access(path, mode):
            return not str(path).endswith("user@%d.service" % os.getuid()) and real_access(path, mode)

        with _Tree(scope=(SLICE, UNIT), events="populated 0\nfrozen 0\n"):
            with mock.patch.object(runner, "_systemctl", return_value=None), \
                 mock.patch.object(runner.os, "access", side_effect=access):
                self.assertEqual(runner.unit_presence(UNIT, slice_name=SLICE), "unknown")

    def test_slow_systemctl_capped_by_remaining_time(self):
        seen = {}

        def fake_run(argv, **kw):
            seen["timeout"] = kw.get("timeout")
            raise subprocess.TimeoutExpired(argv, kw.get("timeout"))

        with clock.use(FakeClock(100.0)), mock.patch.object(runner.subprocess, "run", side_effect=fake_run):
            with _Tree(scope=(SLICE, UNIT), events="populated 0\nfrozen 0\n"):
                self.assertEqual(runner.unit_presence(UNIT, slice_name=SLICE, deadline=103.0), "absent")
        self.assertEqual(seen["timeout"], 3.0)
        with clock.use(FakeClock(100.0)), mock.patch.object(runner.subprocess, "run", side_effect=fake_run):
            with _Tree(scope=(SLICE, UNIT), events="populated 0\nfrozen 0\n"):
                runner.unit_presence(UNIT, slice_name=SLICE)
        self.assertEqual(seen["timeout"], runner.SYSTEMCTL_TIMEOUT)

    def test_systemctl_that_uses_up_the_deadline_gets_no_cgroup_read(self):
        clk = FakeClock(100.0)

        def slow_fail(argv, **kw):
            clk.m = 105.0   # the wait ran past the caller's deadline (103)
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="")

        with clock.use(clk), mock.patch.object(runner.subprocess, "run", side_effect=slow_fail), \
             mock.patch.object(runner, "_cgroup_presence", side_effect=AssertionError("read past deadline")):
            self.assertEqual(runner.unit_presence(UNIT, slice_name=SLICE, deadline=103.0), "unknown")

    def test_no_time_left_is_unknown_without_asking(self):
        with clock.use(FakeClock(100.0)), mock.patch.object(runner, "_systemctl") as sc:
            self.assertEqual(runner.unit_presence(UNIT, slice_name=SLICE, deadline=100.0), "unknown")
        sc.assert_not_called()


class UnitSliceTests(unittest.TestCase):
    def test_slice_is_read_from_systemctl_show(self):
        with mock.patch.object(runner, "_systemctl", return_value=_cp("Slice=app.slice\n")):
            self.assertEqual(runner._unit_slice(UNIT), "app.slice")

    def test_a_slice_that_is_not_a_slice_name_is_none(self):
        for out in ("Slice=../../etc\n", "Slice=\n", "garbage\n"):
            with mock.patch.object(runner, "_systemctl", return_value=_cp(out)):
                self.assertIsNone(runner._unit_slice(UNIT))
        with mock.patch.object(runner, "_systemctl", return_value=None):
            self.assertIsNone(runner._unit_slice(UNIT))


if __name__ == "__main__":
    unittest.main()
