"""Crash barriers: SIGKILL self only when SWARM_CRASH_AT names this barrier."""
import os
import signal
import subprocess
import sys
import unittest

from support import ROOT


def run(name, crash_at, prelude=""):
    env = {k: v for k, v in os.environ.items() if k != "SWARM_CRASH_AT"}
    env["PYTHONPATH"] = str(ROOT / "lib")
    if crash_at is not None:
        env["SWARM_CRASH_AT"] = crash_at
    code = f"{prelude}from swarm.supervisor import barrier; barrier.hit({name!r})"
    return subprocess.run([sys.executable, "-B", "-c", code], env=env, timeout=30).returncode


class BarrierTests(unittest.TestCase):
    def test_matching_name_kills_with_sigkill(self):
        if not hasattr(signal, "SIGKILL"):
            self.skipTest("platform has no SIGKILL; see test_missing_sigkill_falls_back_to_sigterm")
        self.assertEqual(run("x", "x"), -signal.SIGKILL)

    def test_missing_sigkill_falls_back_to_sigterm(self):
        # Windows has no SIGKILL: the barrier must still terminate the process with SIGTERM's code.
        # Simulated on any platform by removing the attribute before the barrier module is imported.
        prelude = "import signal; signal.__dict__.pop('SIGKILL', None); "
        self.assertEqual(run("x", "x", prelude), int(signal.SIGTERM) if os.name == "nt" else -signal.SIGTERM)

    def test_other_name_exits_zero(self):
        self.assertEqual(run("x", "y"), 0)

    def test_unset_exits_zero(self):
        self.assertEqual(run("x", None), 0)

    def test_name_is_not_matched_as_substring_of_env_value(self):
        # env names a barrier that merely contains this one: must not fire.
        self.assertEqual(run("x", "xy"), 0)
        self.assertEqual(run("x", "ax"), 0)

    def test_env_is_not_matched_as_substring_of_name(self):
        self.assertEqual(run("after_xy", "after_x"), 0)


if __name__ == "__main__":
    unittest.main()
