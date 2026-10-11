"""Person authority (spec 4.3 [G6], [F17]): the requester session, a /dev/tty confirmation, and a refusal
for enrolled sessions. Each case runs under a temporary HOME inside the suite sandbox."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from support import SANDBOX, home_env  # noqa: F401

from swarm import enrolment, person  # noqa: E402

BOARD = "memory-contract"
JOB = "J"


class PersonCase(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp(dir=SANDBOX, prefix="person-home-"))
        p = mock.patch.dict(os.environ, home_env(self.home))
        p.start()
        self.addCleanup(p.stop)
        enrolment.write_job_owner(BOARD, job=JOB, harness="claude", session_id="REQ", cwd=str(self.home))

    def enrol_seat(self, session="SEAT"):
        enrolment.write(BOARD, job=JOB, agent_key="k-eng", harness="claude", session_id=session,
                        cwd=str(self.home))

    def require(self, env, confirm=lambda a, j: False):
        return person.require("halt", JOB, board_key=BOARD, env=env, confirm=confirm)


class Authority(PersonCase):
    def test_requester_session_allowed_without_tty(self):
        def no_tty(a, j):
            raise AssertionError("the requester session must not be asked on a tty")
        self.assertEqual(self.require({"CLAUDE_CODE_SESSION_ID": "REQ"}, no_tty), "requester")

    def test_owner_without_session_gives_no_requester_authority(self):
        # an owner record activated from a shell with no session id must not match an unknown caller
        enrolment.write_job_owner(BOARD, job="K", harness="codex", session_id=None, cwd=str(self.home))
        with self.assertRaises(person.PersonRefused):
            person.require("halt", "K", board_key=BOARD, env={}, confirm=lambda a, j: False)

    def test_requester_can_unhalt_without_tty(self):
        self.assertEqual(person.require("unhalt", JOB, board_key=BOARD,
                                        env={"CLAUDE_CODE_SESSION_ID": "REQ"},
                                        confirm=lambda a, j: False), "requester")

    def test_other_session_needs_tty_confirmation_and_refused_without_it(self):
        with self.assertRaises(person.PersonRefused):
            self.require({"CLAUDE_CODE_SESSION_ID": "OTHER"}, confirm=lambda a, j: False)

    def test_other_session_allowed_on_tty_yes(self):
        self.assertEqual(self.require({"CLAUDE_CODE_SESSION_ID": "OTHER"}, confirm=lambda a, j: True), "tty")

    def test_no_session_needs_tty(self):
        self.assertEqual(self.require({}, confirm=lambda a, j: True), "tty")
        with self.assertRaises(person.PersonRefused):
            self.require({}, confirm=lambda a, j: False)

    def test_enrolled_seat_refused_person_path(self):
        self.enrol_seat("SEAT")
        with self.assertRaises(person.PersonRefused) as cm:
            self.require({"CLAUDE_CODE_SESSION_ID": "SEAT"}, confirm=lambda a, j: True)
        self.assertIn("enrolled", str(cm.exception))

    def test_enrolled_seat_refused_even_as_requester(self):
        # an enrolled session is never the requester, whatever the owner record says
        enrolment.write_job_owner(BOARD, job=JOB, harness="claude", session_id="SEAT", cwd=str(self.home))
        self.enrol_seat("SEAT")
        with self.assertRaises(person.PersonRefused):
            self.require({"CLAUDE_CODE_SESSION_ID": "SEAT"}, confirm=lambda a, j: True)

    def test_enrolment_on_another_board_does_not_refuse(self):
        enrolment.write(BOARD + "-other", job=JOB, agent_key="k-x", harness="claude", session_id="SEAT",
                        cwd=str(self.home))
        self.assertEqual(self.require({"CLAUDE_CODE_SESSION_ID": "SEAT"}, confirm=lambda a, j: True), "tty")

    def test_tty_unavailable_is_a_refusal(self):
        with mock.patch.object(person, "_open_tty", side_effect=OSError("no tty")):
            with self.assertRaises(person.PersonRefused):
                person.require("halt", JOB, board_key=BOARD, env={})

    def test_tty_answer_must_be_yes(self):
        for answer, ok in (("y\n", True), ("yes\n", True), ("\n", False), ("n\n", False), ("yesno\n", False)):
            fake = mock.MagicMock()
            fake.readline.return_value = answer
            with mock.patch.object(person, "_open_tty", return_value=fake):
                got = person.require("halt", JOB, board_key=BOARD, env={}) if ok else None
                if ok:
                    self.assertEqual(got, "tty")
                else:
                    with self.assertRaises(person.PersonRefused):
                        person.require("halt", JOB, board_key=BOARD, env={})
