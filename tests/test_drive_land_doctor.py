"""Task 11b: doctor lines for landing (swarm.land_doctor)."""
from __future__ import annotations

import os
import socket
import unittest
from unittest import mock

import support  # noqa: F401
from swarm import land_doctor
from swarm.land_doctor import Line, check_destination, checks
from swarm.supervisor import mirror
from swarm.supervisor.mirror import Mirror, MirrorError
from test_drive_mirror import MirrorCase, git, spy

EXAMPLE_LINE = "example.test ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl"


class FakeMirror:
    def __init__(self, url, cred, error=None):
        self.url, self.cred, self.error = url, cred, error

    def ls_remote(self, timeout):
        self.timeout = timeout
        if self.error:
            raise self.error
        return {"refs/heads/main": "1" * 40}


class Doctor(MirrorCase):
    def by_name(self, lines):
        return {l.name: l for l in lines}

    def test_reachable_destination_reports_ok_with_branch_count(self):
        remote, _ = self.remote_with_main()
        lines = check_destination("a", remote, {"kind": "none"})
        got = self.by_name(lines)
        self.assertTrue(got["land a ls-remote"].ok is True)
        self.assertIn("1 branch", got["land a ls-remote"].detail)

    def test_unreachable_destination_fails_without_echoing_the_url(self):
        url = os.path.join(self.root, "missing-remote.git")
        line = self.by_name(check_destination("a", url, {"kind": "none"}))["land a ls-remote"]
        self.assertIs(line.ok, False)
        self.assertIn("git ls-remote failed", line.detail)
        self.assertNotIn(url, line.detail)
        self.assertTrue(line.fix)

    def test_ls_remote_runs_with_the_pinned_environment(self):
        remote, _ = self.remote_with_main()
        calls = []
        with spy(calls):
            check_destination("a", remote, self.agent_cred())
        cmd, kw = [c for c in calls if "ls-remote" in c[0]][0]
        self.assertEqual(kw["env"]["SSH_AUTH_SOCK"], support.abs_("/run/agent.sock"))
        self.assertIn("core.hooksPath=/dev/null", cmd)

    def test_the_timeout_is_passed_to_ls_remote(self):
        made = []

        def factory(url, cred):
            made.append(FakeMirror(url, cred))
            return made[-1]
        check_destination("a", "/x.git", {"kind": "none"}, mirror_factory=factory, timeout=7)
        self.assertEqual(made[0].timeout, 7)
        check_destination("a", "/x.git", {"kind": "none"}, mirror_factory=factory)
        self.assertEqual(made[1].timeout, land_doctor.LS_REMOTE_SECONDS)

    @support.posix_only("unix sockets")
    def test_agent_socket_missing_or_present(self):
        missing = os.path.join(self.root, "no.sock")
        line = self.by_name(check_destination("a", "/x.git", {"kind": "ssh-agent", "socket": missing},
                                              mirror_factory=FakeMirror))["land a agent"]
        self.assertIs(line.ok, False)
        self.assertIn("missing", line.detail)
        self.assertIn("reboot", line.fix)
        sock = os.path.join(self.root, "a.sock")
        s = socket.socket(socket.AF_UNIX)
        s.bind(sock)
        self.addCleanup(s.close)
        line = self.by_name(check_destination("a", "/x.git", {"kind": "ssh-agent", "socket": sock},
                                              mirror_factory=FakeMirror))["land a agent"]
        self.assertIs(line.ok, True)
        plain = os.path.join(self.root, "plain-file")
        open(plain, "w").close()
        line = self.by_name(check_destination("a", "/x.git", {"kind": "ssh-agent", "socket": plain},
                                              mirror_factory=FakeMirror))["land a agent"]
        self.assertIs(line.ok, False)

    def test_ssh_key_missing_or_readable(self):
        key = os.path.join(self.root, "id")
        line = self.by_name(check_destination("a", "/x.git", {"kind": "ssh-key", "path": key},
                                              mirror_factory=FakeMirror))["land a key"]
        self.assertIs(line.ok, False)
        with open(key, "w") as fh:
            fh.write("not a real key\n")
        line = self.by_name(check_destination("a", "/x.git", {"kind": "ssh-key", "path": key},
                                              mirror_factory=FakeMirror))["land a key"]
        self.assertIs(line.ok, True)

    def test_ssh_host_key_must_be_in_the_private_known_hosts(self):
        url = "ssh://git@example.test/o/r.git"
        line = self.by_name(check_destination("a", url, self.agent_cred(), mirror_factory=FakeMirror))["land a hosts"]
        self.assertIs(line.ok, False)
        self.assertIn("not in the private known_hosts", line.detail)
        self.assertIn(f"swarm land hosts add {url}", line.fix)
        mirror.add_known_host(mirror.known_hosts_file(), "example.test", 22, [EXAMPLE_LINE], confirmed=True)
        line = self.by_name(check_destination("a", url, self.agent_cred(), mirror_factory=FakeMirror))["land a hosts"]
        self.assertIs(line.ok, True)

    def test_non_ssh_destinations_have_no_host_or_agent_lines(self):
        names = self.by_name(check_destination("a", "https://example.test/o/r.git", {"kind": "none"},
                                               mirror_factory=FakeMirror))
        self.assertEqual(set(names), {"land a ls-remote"})

    def test_checks_dedupes_destinations_and_ends_with_the_audit_line(self):
        owner = {"remotes": {"origin": "/x.git", "mirror": "/x.git"}, "credentials": {}}
        made = []

        def factory(url, cred):
            made.append(url)
            return FakeMirror(url, cred)
        out = checks({}, [owner, owner], mirror_factory=factory)
        self.assertEqual(len(made), 1)
        self.assertEqual(out[-1].name, "land audit")
        self.assertIs(out[-1].ok, True)
        self.assertIn("DRIVER-AUDIT", out[-1].detail)

    def test_checks_reports_an_owner_without_destinations(self):
        out = checks({}, [{"remotes": {}, "credentials": {}}], mirror_factory=FakeMirror)
        self.assertEqual(out[0].name, "land destinations")
        self.assertIs(out[0].ok, False)
        self.assertEqual(checks({}, None, mirror_factory=FakeMirror)[-1].name, "land audit")

    def test_a_constructor_error_is_a_failed_line(self):
        def factory(url, cred):
            raise MirrorError("credential helper /h/gh is missing or not executable")
        line = self.by_name(check_destination("a", "/x.git", {"kind": "none"}, mirror_factory=factory))["land a ls-remote"]
        self.assertIs(line.ok, False)
        self.assertIn("not executable", line.detail)

    def test_audit_help_explains_causes_and_resolution(self):
        text = land_doctor.AUDIT_HELP
        for word in ("DRIVER-AUDIT", "ruling", "patch-id", "revert", "swarm land", "merge", "judge"):
            self.assertIn(word, text)


if __name__ == "__main__":
    unittest.main()
