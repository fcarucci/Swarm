"""0.3.0: drive mode is refused on Windows (activation and seat launch); plain jobs are unaffected."""
from __future__ import annotations

import sys
import unittest
from unittest import mock

import support  # noqa: F401  (puts lib/ on sys.path)

from swarm import compat


def real():
    return getattr(compat, "REAL_DRIVE_UNSUPPORTED", compat.drive_unsupported)


class DrivePlatformTest(unittest.TestCase):
    def test_windows_refuses_and_other_platforms_do_not(self):
        with mock.patch.object(compat, "IS_WINDOWS", True):
            self.assertEqual(real()(), compat.WINDOWS_DRIVE_REFUSAL)
        with mock.patch.object(compat, "IS_WINDOWS", False):
            self.assertIsNone(real()())

    def test_activation_and_seat_launch_refuse_on_windows(self):
        from swarm import drive_activate
        from swarm.supervisor import runner
        with mock.patch.object(compat, "drive_unsupported", lambda: compat.WINDOWS_DRIVE_REFUSAL):
            self.assertIn("arrive in 0.3.1", drive_activate.driver_problem({}))
            argv, why = runner._seat_argv({}, {"argv": ["claude", "-p"], "launch_id": "L1", "job": "J",
                                               "name": "s", "cwd": "/w"})
            self.assertEqual((argv, why), (["claude", "-p"], compat.WINDOWS_DRIVE_REFUSAL))
            # a plain (non-drive) replacement is untouched
            self.assertEqual(runner._seat_argv({}, {"argv": ["claude", "-p"], "job": "J", "name": "s", "cwd": "/w"}),
                             (["claude", "-p"], None))


if __name__ == "__main__":
    unittest.main()
