"""Person authority and caller identity (spec 3.4 [G6], [F17], 4.0): the PreToolUse hook's single-use grant
(a receipts.Receipt of kind "grant", drivegate.write_grants) names the caller, never an environment variable;
the requester's MAIN session holds person authority; a person outside any agent host confirms on /dev/tty;
everything else is refused. Each case runs under a temporary HOME inside the suite sandbox. Probes P2, P3a, P3b
and P4 of the Task 6b security review are regression tests here."""
from __future__ import annotations

import argparse
import os
import shlex
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from support import SANDBOX, MemoryHarness, home_env, posix_only  # noqa: F401

from swarm import cli, drivegate, enrolment, hosts, person, receipts  # noqa: E402
from swarm.board.autoinit import store_key  # noqa: E402
from swarm.supervisor import ledger  # noqa: E402

JOB = "J"
HALT = ["halt", "--job", JOB]
NO_HOST = lambda: None   # noqa: E731  (the process-tree walk finds no agent host)
UNDER_CLAUDE = lambda: "claude"   # noqa: E731


class PersonCase(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp(dir=SANDBOX, prefix="person-home-"))
        p = mock.patch.dict(os.environ, home_env(self.home))
        p.start()
        self.addCleanup(p.stop)
        person.forget()
        self.addCleanup(person.forget)
        self.h = MemoryHarness()
        self.cfg = self.h.cfg
        self.bk = store_key(self.cfg)
        led = ledger.create(self.bk, JOB, budgets={}, activated_at="2026-10-11T00:00:00+00:00")
        led.requester_session = "REQ"
        led.save()
        enrolment.write_job_owner(self.bk, job=JOB, harness="claude", session_id="REQ", cwd=str(self.home))

    def led(self):
        return ledger.load(self.bk, JOB)

    def enrol_seat(self, session="SEAT", key="k-eng"):
        enrolment.write(self.bk, job=JOB, agent_key=key, harness="claude", session_id=session, cwd=str(self.home))

    def hook(self, session, argv=HALT, agent_id=None, command=None, ctx=None, now=None):
        """What the PreToolUse hook does for this session's shell call `swarm <argv>`."""
        payload = {"session_id": session, "tool_name": "Bash",
                   "tool_input": {"command": command or shlex.join(["swarm", *argv])}}
        if agent_id:
            payload["agent_id"] = agent_id
        return drivegate.write_grants(self.cfg, payload, hosts.get("claude"), ctx, now=now)

    @staticmethod
    def ns(argv=HALT):
        return cli._parser().parse_args(list(argv))

    def caller(self, argv=HALT, led=False):
        person.forget()
        return person.caller(self.bk, JOB, self.led() if led is False else led, self.ns(argv))

    def require(self, env=None, confirm=lambda a, j: False, ancestor=NO_HOST, argv=HALT):
        return person.require("halt", JOB, board_key=self.bk, env={} if env is None else env, confirm=confirm,
                              who=self.caller(argv), ancestor=ancestor)


class RequesterByGrant(PersonCase):
    def test_requester_main_session_allowed_without_tty(self):
        def no_tty(a, j):
            raise AssertionError("the requester session must not be asked on a tty")
        self.assertEqual(self.hook("REQ"), 1)
        self.assertEqual(self.require({"CLAUDE_CODE_SESSION_ID": "REQ"}, no_tty, UNDER_CLAUDE), "requester")

    def test_the_grant_is_a_task5_receipt(self):
        self.hook("REQ")
        led = self.led()
        rid = drivegate.grant_id(self.ns())
        r = receipts.find(self.bk, JOB, "grant", rid, rev=1, incarnation=led.incarnation,
                          generation=led.generation, now=time.time())
        self.assertEqual((r.seat, r.action_id, r.args_sha), (None, "requester:halt", rid))

    def test_P1_the_environment_alone_gives_no_authority(self):
        with self.assertRaises(person.PersonRefused):
            self.require({"CLAUDE_CODE_SESSION_ID": "REQ"}, lambda a, j: True, UNDER_CLAUDE)

    def test_another_session_gets_no_grant(self):
        self.enrol_seat("SEAT")
        self.assertEqual(self.hook("SEAT"), 0)
        with self.assertRaises(person.PersonRefused):
            self.require({"CLAUDE_CODE_SESSION_ID": "REQ"}, lambda a, j: True, UNDER_CLAUDE)

    def test_a_seat_grant_is_not_the_requester(self):
        ctx = drivegate.Ctx(kind="seat", job=JOB, led=self.led(), seat="eng",
                            intent=ledger.Intent("L", "A", 1, "eng", 0.0, 0.0, [], "claimed"))
        self.assertEqual(self.hook("SEAT", ctx=ctx), 1)
        with self.assertRaises(person.PersonRefused) as cm:
            self.require({"CLAUDE_CODE_SESSION_ID": "REQ"}, lambda a, j: True, UNDER_CLAUDE)
        self.assertIn("not the requester", str(cm.exception))

    def test_M2_a_subagent_of_the_requester_gets_no_grant(self):
        self.assertEqual(self.hook("REQ", agent_id="sub-1"), 0)
        with self.assertRaises(person.PersonRefused):
            self.require({"CLAUDE_CODE_SESSION_ID": "REQ"}, lambda a, j: True, UNDER_CLAUDE)

    def test_P4_requester_still_allowed_after_one_of_its_subagents_enrolled(self):
        self.enrol_seat("REQ", key="k-sub-of-requester")
        self.hook("REQ")
        self.assertEqual(self.require({"CLAUDE_CODE_SESSION_ID": "REQ"}, ancestor=UNDER_CLAUDE), "requester")

    def test_a_grant_is_used_once(self):
        self.hook("REQ")
        self.assertEqual(self.require(ancestor=UNDER_CLAUDE), "requester")
        with self.assertRaises(person.PersonRefused):
            self.require(ancestor=UNDER_CLAUDE)

    def test_a_grant_is_for_its_exact_arguments(self):
        self.hook("REQ", argv=["halt", "--job", JOB, "--reason", "x"])
        with self.assertRaises(person.PersonRefused):
            self.require(ancestor=UNDER_CLAUDE)

    def test_an_expired_grant_is_refused(self):
        self.hook("REQ", now=time.time() - drivegate.GRANT_SECONDS - 1)
        with self.assertRaises(person.PersonRefused):
            self.require(ancestor=UNDER_CLAUDE)

    def test_a_grant_of_another_incarnation_is_refused(self):
        self.hook("REQ")
        led = self.led()
        led.incarnation = "f" * 32
        person.forget()
        self.assertIsNone(person.caller(self.bk, JOB, led, self.ns()))

    def test_a_grant_that_cannot_be_used_up_is_refused(self):
        """A Codex seat's sandbox can read the enrolled directory but not unlink in it: no grant."""
        self.hook("REQ")
        with mock.patch("swarm.compat.unlink", side_effect=PermissionError("read-only")):
            with self.assertRaises(person.PersonRefused):
                self.require(ancestor=UNDER_CLAUDE)

    def test_drive_repair_of_an_unreadable_ledger_is_granted_by_the_owner_record(self):
        hits = list(self.home.rglob(ledger._name(self.bk, JOB)))
        hits[0].write_bytes(b"bad")
        argv = ["drive", "repair", "--job", JOB]
        self.assertEqual(self.hook("OTHER", argv=argv), 0)
        self.assertEqual(self.hook("REQ", argv=argv), 1)
        self.assertTrue(self.caller(argv, led=None).requester)


class Grants(PersonCase):
    def test_only_control_commands_and_direct_calls_get_a_grant(self):
        for cmd in ("swarm read --job J", "echo swarm halt --job J", "swarm halt --job $J"):
            self.assertEqual(self.hook("REQ", command=cmd), 0, cmd)
        self.assertEqual(self.hook("REQ", command="cd /x && swarm --config c.toml halt --job J 2>&1 | tail -2"), 1)
        self.assertIsNotNone(self.caller())

    def test_two_calls_in_one_command_get_two_grants(self):
        self.assertEqual(self.hook("REQ", command="swarm halt --job J; swarm unhalt --job J"), 2)


class TtyPath(PersonCase):
    def test_no_grant_outside_any_host_needs_tty(self):
        self.assertEqual(self.require({}, confirm=lambda a, j: True), "tty")
        with self.assertRaises(person.PersonRefused):
            self.require({}, confirm=lambda a, j: False)

    def test_P2_unset_session_variables_under_an_agent_host_refused(self):
        """P2 (fixed): `env -u CLAUDE_CODE_SESSION_ID swarm halt` still runs under the claude process."""
        self.enrol_seat("ENG-SESSION")
        with self.assertRaises(person.PersonRefused) as cm:
            self.require({"HOME": str(self.home)}, confirm=lambda a, j: True, ancestor=UNDER_CLAUDE)
        self.assertIn("agent host", str(cm.exception))

    def test_a_host_variable_refuses_the_tty_path(self):
        for var in ("CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID", "CLAUDECODE"):
            with self.assertRaises(person.PersonRefused):
                self.require({var: "x"}, confirm=lambda a, j: True)

    def test_the_real_ancestor_walk_sees_a_codex_or_claude_parent(self):
        fake = {10: (9, ["bash"]), 9: (8, ["codex", "exec"]), 8: (1, ["init"])}
        with mock.patch.object(person.hosts, "_proc_parent", side_effect=lambda pid: fake.get(pid)), \
                mock.patch.object(person.os, "getppid", return_value=10):
            self.assertEqual(person._ancestor_host(), "codex")

    def test_enrolled_seat_refused_person_path(self):
        self.enrol_seat("SEAT")
        with self.assertRaises(person.PersonRefused) as cm:
            self.require({"CLAUDE_CODE_SESSION_ID": "SEAT"}, confirm=lambda a, j: True)
        self.assertIn("enrolled", str(cm.exception))

    def test_tty_unavailable_is_a_refusal(self):
        with mock.patch.object(person, "_open_tty", side_effect=OSError("no tty")):
            with self.assertRaises(person.PersonRefused):
                person.require("halt", JOB, board_key=self.bk, env={}, who=None, ancestor=NO_HOST)

    def test_tty_answer_must_be_yes(self):
        for answer, ok in ((b"y\n", True), (b"yes\n", True), (b"\n", False), (b"n\n", False), (b"yesno\n", False)):
            r, w = os.pipe()
            os.write(w, answer)
            os.close(w)
            with mock.patch.object(person, "_open_tty", return_value=r), \
                    mock.patch.object(person.os, "write", return_value=0):
                self.assertEqual(person._tty_confirm("halt", JOB), ok, answer)

    def _pty_answer(self, run) -> int:
        import pty
        pid, fd = pty.fork()
        if pid == 0:   # the child: a new session whose controlling terminal is the pty
            try:
                os._exit(0 if run() else 1)
            except BaseException:
                os._exit(2)
        buf = b""
        try:
            while b"confirm" not in buf:
                chunk = os.read(fd, 1024)
                if not chunk:
                    break
                buf += chunk
            os.write(fd, b"y\n")
            while os.read(fd, 1024):
                pass
        except OSError:
            pass
        return os.waitstatus_to_exitcode(os.waitpid(pid, 0)[1])

    @posix_only("a pty")
    def test_P3a_tty_confirm_works_on_a_real_terminal(self):
        """P3a (fixed): the prompt opens /dev/tty unbuffered; a `y` on a real terminal confirms."""
        self.assertEqual(self._pty_answer(lambda: person._tty_confirm("halt", JOB)), 0)

    @posix_only("a pty")
    def test_P3b_an_agent_owned_pty_does_not_pass_the_gate(self):
        """P3b (fixed as far as the spec allows): an agent that answers its own prompt through a pty it owns
        still runs under its agent host, so the gate refuses before asking. Outside a host, the tty answers."""
        def gate(ancestor):
            person.forget()
            try:
                return person.require("halt", JOB, board_key=self.bk, env={}, who=None, ancestor=ancestor) == "tty"
            except person.PersonRefused:
                return False
        self.assertEqual(self._pty_answer(lambda: gate(UNDER_CLAUDE)), 1)
        self.assertEqual(self._pty_answer(lambda: gate(NO_HOST)), 0)


if __name__ == "__main__":
    unittest.main()
