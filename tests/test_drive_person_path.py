"""drive_activate._person against the REAL swarm.person (the FakePerson stub in test_drive_activate
accepted calls without `who`, so a `swarm drive models` TypeError reached production)."""
import inspect
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from swarm import drive_activate, person  # noqa: E402


class PersonPathTest(unittest.TestCase):
    def setUp(self):
        person.forget()
        self.cfg = {}
        self.bk = mock.patch.object(drive_activate, "board_key", return_value="bk").start()
        self.addCleanup(mock.patch.stopall)

    def test_requester_session_is_allowed(self):
        """The job's requester session (the hook attested it) may run `swarm drive models`."""
        who = person.Caller(seat=None, action_id="requester:sess-1")
        with mock.patch.object(person, "caller", return_value=who):
            self.assertIsNone(drive_activate._person("drive models", self.cfg, "J", args=object()))

    def test_seat_is_refused_with_a_message(self):
        """A seat is refused with text, never an exception escaping the CLI."""
        who = person.Caller(seat="eng-1", action_id="seat:eng-1")
        with mock.patch.object(person, "caller", return_value=who):
            refusal = drive_activate._person("drive models", self.cfg, "J", args=object())
        self.assertIsNotNone(refusal)
        self.assertIn("drive models", refusal)

    def test_call_matches_the_real_require_signature(self):
        """Every keyword person.require needs is passed."""
        seen = {}
        real = inspect.signature(person.require)

        def spy(*a, **kw):
            real.bind(*a, **kw)   # TypeError here is the production bug
            seen.update(kw)
            return "requester"
        with mock.patch.object(person, "require", side_effect=spy), \
                mock.patch.object(person, "caller", return_value=None):
            self.assertIsNone(drive_activate._person("drive models", self.cfg, "J", args=object()))
        self.assertIn("who", seen)


if __name__ == "__main__":
    unittest.main()
