"""Person authority and caller identity (spec 3.4 [G6], [F17], 4.0): the hook's single-use grant names the
caller (never an environment variable), the requester's MAIN session holds person authority, a person outside
any agent host confirms on /dev/tty, and everything else is refused. Each case runs under a temporary HOME
inside the suite sandbox. Probes P2, P3a, P3b and P4 of the Task 6b security review are regression tests here."""
from __future__ import annotations

import os
import shlex
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from support import SANDBOX, home_env, posix_only  # noqa: F401

from swarm import enrolment, person  # noqa: E402

BOARD = "memory-contract"
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
        enrolment.write_job_owner(BOARD, job=JOB, harness="claude", session_id="REQ", cwd=str(self.home))

    def enrol_seat(self, session="SEAT", key="k-eng"):
        enrolment.write(BOARD, job=JOB, agent_key=key, harness="claude", session_id=session, cwd=str(self.home))

    def hook(self, session, argv=HALT, agent_id=None, command=None):
        """What the PreToolUse hook does for a shell call `swarm <argv>` of this session."""
        payload = {"session_id": session}
        if agent_id:
            payload["agent_id"] = agent_id
        return person.write_grants(BOARD, payload, command or shlex.join(["swarm", *argv]))

    def require(self, env=None, confirm=lambda a, j: False, ancestor=NO_HOST, argv=HALT):
        person.forget()
        return person.require("halt", JOB, board_key=BOARD, env={} if env is None else env, confirm=confirm,
                              argv=argv, ancestor=ancestor)


class RequesterByGrant(PersonCase):
    def test_requester_main_session_allowed_without_tty(self):
        def no_tty(a, j):
            raise AssertionError("the requester session must not be asked on a tty")
        self.hook("REQ")
        self.assertEqual(self.require({"CLAUDE_CODE_SESSION_ID": "REQ"}, no_tty, UNDER_CLAUDE), "requester")

    def test_the_environment_alone_gives_no_authority(self):
        """P1: a seat exports the requester's session id; without the requester's grant it is refused."""
        with self.assertRaises(person.PersonRefused):
            self.require({"CLAUDE_CODE_SESSION_ID": "REQ"}, lambda a, j: True, UNDER_CLAUDE)

    def test_a_grant_of_another_session_is_refused_whatever_the_environment_says(self):
        self.enrol_seat("SEAT")
        self.hook("SEAT")
        with self.assertRaises(person.PersonRefused) as cm:
            self.require({"CLAUDE_CODE_SESSION_ID": "REQ"}, lambda a, j: True, UNDER_CLAUDE)
        self.assertIn("not the requester", str(cm.exception))

    def test_a_subagent_of_the_requester_is_not_the_requester(self):
        """M2: a subagent's payload carries the parent's session id and its own agent_id."""
        self.hook("REQ", agent_id="sub-1")
        with self.assertRaises(person.PersonRefused) as cm:
            self.require({"CLAUDE_CODE_SESSION_ID": "REQ"}, lambda a, j: True, UNDER_CLAUDE)
        self.assertIn("subagent", str(cm.exception))

    def test_P4_requester_still_allowed_after_one_of_its_subagents_enrolled(self):
        """P4 (fixed): the subagent's enrolment record carries the requester's session id; the requester's own
        main-session grant still holds."""
        self.enrol_seat("REQ", key="k-sub-of-requester")
        self.hook("REQ")
        self.assertEqual(self.require({"CLAUDE_CODE_SESSION_ID": "REQ"}, ancestor=UNDER_CLAUDE), "requester")

    def test_owner_without_session_gives_no_requester_authority(self):
        enrolment.write_job_owner(BOARD, job="K", harness="codex", session_id=None, cwd=str(self.home))
        person.write_grants(BOARD, {"session_id": "X"}, "swarm halt --job K")
        with self.assertRaises(person.PersonRefused):
            person.require("halt", "K", board_key=BOARD, env={}, confirm=lambda a, j: True,
                           argv=["halt", "--job", "K"], ancestor=NO_HOST)

    def test_a_grant_is_used_once(self):
        self.hook("REQ")
        self.assertEqual(self.require(ancestor=UNDER_CLAUDE), "requester")
        with self.assertRaises(person.PersonRefused):
            self.require(ancestor=UNDER_CLAUDE)

    def test_a_grant_is_for_its_exact_argv(self):
        self.hook("REQ", argv=["halt", "--job", "OTHER"])
        with self.assertRaises(person.PersonRefused):
            self.require(ancestor=UNDER_CLAUDE)

    def test_an_expired_grant_is_refused(self):
        person.write_grants(BOARD, {"session_id": "REQ"}, "swarm halt --job J",
                            now=time.time() - person.GRANT_SECONDS - 1)
        with self.assertRaises(person.PersonRefused):
            self.require(ancestor=UNDER_CLAUDE)

    def test_a_grant_of_another_board_is_refused(self):
        person.write_grants(BOARD + "-other", {"session_id": "REQ"}, "swarm halt --job J")
        with self.assertRaises(person.PersonRefused):
            self.require(ancestor=UNDER_CLAUDE)

    def test_a_grant_that_cannot_be_used_up_is_refused(self):
        """A Codex seat's sandbox can read the enrolled directory but not unlink in it: no grant."""
        self.hook("REQ")
        with mock.patch.object(person.compat, "unlink", side_effect=PermissionError("read-only")):
            with self.assertRaises(person.PersonRefused):
                self.require(ancestor=UNDER_CLAUDE)


class Grants(PersonCase):
    def test_swarm_calls_reads_direct_invocations(self):
        cmd = ("cd ~/w && FOO=1 swarm halt --job 'J x' 2>&1 | tail -3\n"
               "~/.local/bin/swarm seat ack --job J --upto 5; echo swarm halt --job J")
        self.assertEqual(person.swarm_calls(cmd), [["halt", "--job", "J x", "2"], ["halt", "--job", "J x"],
                                                    ["seat", "ack", "--job", "J", "--upto", "5"]])

    def test_substitutions_and_nested_shells_get_no_grant(self):
        for cmd in ("swarm halt --job $JOB", "swarm halt --job `cat j`", "bash -c 'swarm halt --job J'",
                    "env -u CLAUDE_CODE_SESSION_ID swarm halt --job J", "swarm halt --job 'unterminated"):
            self.assertEqual(self.hook("REQ", command=cmd), 0, cmd)

    def test_only_control_commands_get_a_grant(self):
        self.assertEqual(self.hook("REQ", command="swarm read --job J"), 0)
        self.assertEqual(self.hook("REQ", command="swarm --config c.toml seat ack --job J --upto 1"), 1)

    def test_a_grant_file_is_private(self):
        self.hook("REQ")
        names = [p for p in (self.home / ".local/share/swarm/host/enrolled").iterdir() if p.name.startswith("grant-")]
        self.assertEqual(len(names), 1)
        if os.name == "posix":
            self.assertEqual(names[0].stat().st_mode & 0o777, 0o600)

    def test_expired_grants_are_pruned_by_the_next_write(self):
        person.write_grants(BOARD, {"session_id": "REQ"}, "swarm halt --job J", now=1.0)
        self.hook("REQ", argv=["unhalt", "--job", JOB])
        names = [p.name for p in (self.home / ".local/share/swarm/host/enrolled").iterdir()
                 if p.name.startswith("grant-")]
        self.assertEqual(names, [person.grant_name(BOARD, ["unhalt", "--job", JOB])])


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
                person.require("halt", JOB, board_key=BOARD, env={}, argv=HALT, ancestor=NO_HOST)

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
                return person.require("halt", JOB, board_key=BOARD, env={}, argv=HALT, ancestor=ancestor) == "tty"
            except person.PersonRefused:
                return False
        self.assertEqual(self._pty_answer(lambda: gate(UNDER_CLAUDE)), 1)
        self.assertEqual(self._pty_answer(lambda: gate(NO_HOST)), 0)


if __name__ == "__main__":
    unittest.main()
